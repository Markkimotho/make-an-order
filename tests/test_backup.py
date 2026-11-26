"""Backups: create, verify, restore, rotate, the Settings > Backup page,
the CLI, MySQL command lines, and the deploy package.

The real dump and restore run on SQLite. MySQL command construction is
checked with a fake mysqldump on PATH; the real MySQL round trip runs only
with TEST_MYSQL_URL set and the MySQL client tools installed.
"""
import gzip
import json
import os
import re
import shutil
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auth.tenancy import unscoped
from models import AuditLog, Tenant, User, db
from services import backup
from tests.conftest import create_tenant, on_mysql

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 26, 9, 30, 0, tzinfo=timezone.utc)

sqlite_only = pytest.mark.skipif(on_mysql(), reason="SQLite dump/restore path")


@pytest.fixture
def bdir(app, tmp_path):
    path = tmp_path / "backups"
    app.config["BACKUP_DIR"] = str(path)
    return path


def tenant_count():
    with unscoped():
        return Tenant.query.count()


def touch_backup(dest, when, label=None, kind="sqlite", body=b"x"):
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / backup.backup_name(kind, when, label)
    path.write_bytes(body)
    backup.manifest_path(path).write_text("{}")
    return path


# --- create ------------------------------------------------------------------

@sqlite_only
def test_create_writes_gzip_and_manifest_with_sha_and_counts(app, bdir):
    manifest = backup.create_backup(bdir, now=NOW)
    path = Path(manifest["path"])
    assert path.name == "mao-20261126-093000.sqlite.gz"
    assert path.read_bytes()[:2] == b"\x1f\x8b"                       # gzip magic
    on_disk = json.loads(backup.manifest_path(path).read_text())
    assert on_disk["sha256"] == backup._sha256(path)
    assert on_disk["size"] == path.stat().st_size
    assert on_disk["created_at"] == "2026-11-26T09:30:00+00:00"
    assert on_disk["database_kind"] == "sqlite"
    assert on_disk["row_counts"]["tenants"] == 1
    assert on_disk["total_rows"] == sum(on_disk["row_counts"].values())
    assert on_disk["migration_head"] == backup.code_migration_head()
    assert on_disk["database_revision"] == on_disk["migration_head"]
    assert not list(bdir.glob("*.part"))


@sqlite_only
def test_create_refuses_while_another_backup_holds_the_lock(app, bdir):
    bdir.mkdir()
    (bdir / ".backup.lock").write_text("123")
    with pytest.raises(backup.BackupBusy):
        backup.create_backup(bdir, now=NOW)
    assert backup.list_backups(bdir) == []


@sqlite_only
def test_a_stale_lock_is_ignored(app, bdir):
    bdir.mkdir()
    lock = bdir / ".backup.lock"
    lock.write_text("123")
    old = (datetime.now() - timedelta(hours=3)).timestamp()
    os.utime(lock, (old, old))
    backup.create_backup(bdir, now=NOW)
    assert not lock.exists()


def test_in_memory_sqlite_cannot_be_backed_up(tmp_path):
    with pytest.raises(backup.BackupError):
        backup.create_backup(tmp_path, database_url="sqlite://")


@sqlite_only
def test_offsite_failure_is_reported_not_fatal(app, bdir, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which",
                        lambda name: None if name == "rclone" else shutil.which(name))
    manifest = backup.create_backup(bdir, now=NOW, offsite="remote:bucket")
    assert Path(manifest["path"]).exists()
    assert manifest["offsite"]["ok"] is False
    assert "rclone" in manifest["offsite"]["message"]


@sqlite_only
def test_offsite_copy_runs_rclone_for_file_and_manifest(app, bdir, tmp_path, monkeypatch):
    log = tmp_path / "rclone.log"
    fake = write_script(tmp_path / "bin", "rclone",
                        f"import sys\nopen({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n")
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    manifest = backup.create_backup(bdir, now=NOW, offsite="usb:mao")
    assert manifest["offsite"]["ok"] is True
    lines = log.read_text().splitlines()
    assert lines == [f"copy {manifest['path']} usb:mao", f"copy {manifest['path']}.json usb:mao"]


# --- verify ------------------------------------------------------------------

@sqlite_only
def test_verify_passes_for_a_good_backup(app, bdir):
    manifest = backup.create_backup(bdir, now=NOW)
    result = backup.verify_backup(manifest["path"])
    assert result["ok"] and not result["skipped"]
    assert result["row_counts"] == manifest["row_counts"]


@sqlite_only
def test_verify_detects_a_corrupted_file(app, bdir):
    path = Path(backup.create_backup(bdir, now=NOW)["path"])
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.verify_backup(path)


@sqlite_only
def test_verify_detects_damage_even_without_a_manifest(app, bdir):
    path = Path(backup.create_backup(bdir, now=NOW)["path"])
    backup.manifest_path(path).unlink()
    path.write_bytes(path.read_bytes()[:200])                 # truncated gzip
    with pytest.raises(backup.BackupError, match="damaged"):
        backup.verify_backup(path)


@sqlite_only
def test_verify_reports_row_count_mismatch(app, bdir):
    path = Path(backup.create_backup(bdir, now=NOW)["path"])
    manifest = json.loads(backup.manifest_path(path).read_text())
    manifest["row_counts"]["tenants"] = 7
    backup.manifest_path(path).write_text(json.dumps(manifest))
    result = backup.verify_backup(path)
    assert result["ok"] is False
    assert "tenants: manifest 7, restored 1" in " ".join(result["messages"])


# --- restore -----------------------------------------------------------------

@sqlite_only
def test_restore_refuses_without_the_database_name(app, bdir):
    path = backup.create_backup(bdir, now=NOW)["path"]
    for wrong in ("", "yes", "DB.SQLITE", None):
        with pytest.raises(backup.BackupError, match="type the database name"):
            backup.restore_backup(path, confirm=wrong)
    assert len(backup.list_backups(bdir)) == 1                 # no pre-restore copy taken


@sqlite_only
def test_restore_takes_a_pre_restore_backup_then_restores(app, bdir):
    path = backup.create_backup(bdir, now=NOW)["path"]
    create_tenant("Later Shop", "later")
    assert tenant_count() == 2
    db.session.remove()
    result = backup.restore_backup(path, confirm=backup.database_name(),
                                   now=NOW + timedelta(hours=1))
    assert result["pre_restore"] == "mao-20261126-103000-prerestore.sqlite.gz"
    pre = backup.read_manifest(bdir / result["pre_restore"])
    assert pre["row_counts"]["tenants"] == 2                 # the data as it was
    db.session.remove()
    assert tenant_count() == 1                               # the backup's data


@sqlite_only
def test_restore_refuses_a_damaged_file_before_touching_anything(app, bdir):
    path = Path(backup.create_backup(bdir, now=NOW)["path"])
    path.write_bytes(path.read_bytes()[:-10] + b"0123456789")
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.restore_backup(path, confirm=backup.database_name())
    assert [b["label"] for b in backup.list_backups(bdir)] == [None]


@sqlite_only
def test_restore_refuses_a_backup_of_the_other_kind(app, bdir):
    path = touch_backup(bdir, NOW, kind="mysql")
    with pytest.raises(backup.BackupError, match="mysql backup"):
        backup.restore_backup(path, confirm=backup.database_name())


# --- rotate ------------------------------------------------------------------

def test_rotate_keeps_newest_per_day_for_14_days_and_per_week_for_8_weeks(tmp_path):
    end = datetime(2026, 11, 26, tzinfo=timezone.utc)
    for day in range(70):
        date = end - timedelta(days=day)
        touch_backup(tmp_path, date.replace(hour=1))           # 04:00 Nairobi
        touch_backup(tmp_path, date.replace(hour=13))          # 16:00 Nairobi
    old_safety = touch_backup(tmp_path, end - timedelta(days=200), label="prerestore")
    stranger = tmp_path / "notes.txt"
    stranger.write_text("keep me")

    deleted = backup.rotate(tmp_path)

    kept = {b["name"] for b in backup.list_backups(tmp_path)}
    daily = {backup.backup_name("sqlite", (end - timedelta(days=d)).replace(hour=13))
             for d in range(14)}                               # Nov 13..26, newest of each day
    sundays = {backup.backup_name("sqlite", datetime(2026, m, d, 13, tzinfo=timezone.utc))
               for m, d in ((11, 8), (11, 1), (10, 25), (10, 18), (10, 11))}
    assert kept == daily | sundays | {old_safety.name}
    assert len(deleted) == 140 - 19
    assert stranger.exists()
    leftovers = {p.name for p in tmp_path.glob("*.json")}
    assert leftovers == {n + ".json" for n in kept}            # sidecars go with their file


def test_rotate_never_deletes_the_newest_or_everything(tmp_path):
    only = touch_backup(tmp_path, NOW - timedelta(days=400))
    assert backup.rotate(tmp_path, keep_daily=0, keep_weekly=0) == []
    assert only.exists()


def test_list_ignores_files_that_are_not_backups(tmp_path):
    touch_backup(tmp_path, NOW)
    for name in ("mao-20261126-093000.sqlite.gz.part", "mao-2026.sql.gz", "x.sql.gz",
                 "mao-20261126-093000.zip", "mao-20261399-093000.sql.gz"):
        (tmp_path / name).write_text("x")
    assert [b["name"] for b in backup.list_backups(tmp_path)] == ["mao-20261126-093000.sqlite.gz"]


# --- status ------------------------------------------------------------------

def test_status_is_stale_after_26_hours(tmp_path):
    assert backup.backup_status(tmp_path, now=NOW)["stale"] is True       # none yet
    touch_backup(tmp_path, NOW - timedelta(hours=25))
    fresh = backup.backup_status(tmp_path, now=NOW)
    assert fresh["stale"] is False and fresh["age_hours"] == 25.0
    assert backup.backup_status(tmp_path, now=NOW + timedelta(hours=1, minutes=1))["stale"] is True


def test_too_soon_allows_one_manual_backup_per_5_minutes(tmp_path):
    assert backup.too_soon(tmp_path, now=NOW) == 0
    touch_backup(tmp_path, NOW - timedelta(minutes=2))
    assert backup.too_soon(tmp_path, now=NOW) == 180
    assert backup.too_soon(tmp_path, now=NOW + timedelta(minutes=3)) == 0


# --- page and routes ---------------------------------------------------------

def test_page_renders_for_the_owner(app, bdir, login_as):
    touch_backup(bdir, datetime.now(timezone.utc) - timedelta(hours=30))
    resp = login_as("superuser").get("/settings-ui/backup")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Back up now" in html and "backupStale" in html
    assert "mao-" in html


def test_page_redirects_an_admin(app, bdir, login_as):
    resp = login_as("admin").get("/settings-ui/backup")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/dashboard")


def test_settings_page_links_to_backup(app, bdir, login_as):
    html = login_as("superuser").get("/settings/").get_data(as_text=True)
    assert "/settings-ui/backup" in html


@sqlite_only
def test_back_up_now_creates_one_then_rate_limits(app, bdir, login_as):
    client = login_as("superuser")
    resp = client.post("/settings/backup/run")
    assert resp.status_code == 303
    assert len(backup.list_backups(bdir)) == 1
    resp = client.post("/settings/backup/run", follow_redirects=True)
    assert "less than 5 minutes ago" in resp.get_data(as_text=True)
    assert len(backup.list_backups(bdir)) == 1
    with unscoped():
        assert AuditLog.query.filter_by(action="backup.create").count() == 1


def test_back_up_now_is_owner_only(app, bdir, login_as):
    resp = login_as("admin").post("/settings/backup/run")
    assert resp.status_code == 302
    assert backup.list_backups(bdir) == []


def test_status_json(app, bdir, login_as):
    touch_backup(bdir, datetime.now(timezone.utc) - timedelta(hours=27))
    data = login_as("superuser").get("/settings/backup/status").get_json()
    assert data["stale"] is True and data["has_backup"] is True
    assert login_as("cashier").get("/settings/backup/status").status_code == 403


@pytest.mark.parametrize("name", ["..%2F..%2Fapp.py", "..%2Fdb.sqlite", "app.py",
                                  "mao-20261126-093000.sqlite.gz.json", ".backup.lock",
                                  "mao-20261126-093000.sqlite.gz%00", "%2Fetc%2Fpasswd"])
def test_download_refuses_traversal_and_other_names(app, bdir, login_as, name):
    app.config["SINGLE_BUSINESS_INSTALL"] = True
    touch_backup(bdir, NOW)
    resp = login_as("superuser").get(f"/settings/backup/{name}")
    assert resp.status_code == 404


def test_download_needs_platform_admin_on_a_shared_server(app, bdir, login_as):
    app.config["SINGLE_BUSINESS_INSTALL"] = False
    path = touch_backup(bdir, NOW, body=b"dump-bytes")
    client = login_as("superuser")
    assert client.get(f"/settings/backup/{path.name}").status_code == 403
    assert "Download</a>" not in client.get("/settings-ui/backup").get_data(as_text=True)

    with unscoped():
        user = User.query.filter_by(email=f"superuser-{app.acme_id}@example.com").first()
        user.is_platform_admin = True
        db.session.commit()
    resp = client.get(f"/settings/backup/{path.name}")
    assert resp.status_code == 200 and resp.data == b"dump-bytes"
    assert "attachment" in resp.headers["Content-Disposition"]


def test_download_allowed_for_owner_on_a_single_business_install(app, bdir, login_as):
    app.config["SINGLE_BUSINESS_INSTALL"] = True
    path = touch_backup(bdir, NOW, body=b"dump-bytes")
    client = login_as("superuser")
    resp = client.get(f"/settings/backup/{path.name}")
    assert resp.status_code == 200 and resp.data == b"dump-bytes"
    assert client.get("/settings/backup/mao-20261126-000000.sqlite.gz").status_code == 404
    assert login_as("admin").get(f"/settings/backup/{path.name}").status_code == 302
    with unscoped():
        assert AuditLog.query.filter_by(action="backup.download").count() == 1


def test_a_second_business_on_a_single_install_cannot_download(app, bdir, login_as,
                                                               other_tenant):
    """A dump holds every business: once a second one exists, the shop flag
    no longer unlocks downloads (only a platform admin can)."""
    app.config["SINGLE_BUSINESS_INSTALL"] = True
    path = touch_backup(bdir, NOW, body=b"dump-bytes")
    assert login_as("superuser").get(f"/settings/backup/{path.name}").status_code == 403
    intruder = login_as("superuser", tenant_id=other_tenant)
    assert intruder.get(f"/settings/backup/{path.name}").status_code == 403


def test_signup_is_closed_on_a_single_business_install(app, client):
    app.config["SINGLE_BUSINESS_INSTALL"] = True
    r = client.post("/auth/register", data={
        "business_name": "Intruder Ltd", "vertical": "pharmacy", "name": "X",
        "email": "x@intruder.test", "password": "longenough1", "phone_number": "0712345999"})
    assert r.status_code == 400 and b"already has its business" in r.data
    from models import Tenant
    with unscoped():
        assert Tenant.query.count() == 1


# --- CLI ---------------------------------------------------------------------

@sqlite_only
def test_cli_create_list_verify_rotate(app, bdir):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["backup", "create"])
    assert result.exit_code == 0, result.output
    assert "Created" in result.output
    name = backup.list_backups(bdir)[0]["name"]

    result = runner.invoke(args=["backup", "create", "--if-older-than", "20"])
    assert result.exit_code == 0 and "Skipped" in result.output

    result = runner.invoke(args=["backup", "list"])
    assert result.exit_code == 0 and name in result.output

    result = runner.invoke(args=["backup", "list", "--json"])
    assert json.loads(result.output)["name"] == name

    result = runner.invoke(args=["backup", "verify", name])      # name inside BACKUP_DIR
    assert result.exit_code == 0 and "OK" in result.output, result.output

    result = runner.invoke(args=["backup", "rotate"])
    assert result.exit_code == 0 and "Deleted 0" in result.output


@sqlite_only
def test_cli_verify_fails_on_a_damaged_file(app, bdir):
    path = Path(backup.create_backup(bdir, now=NOW)["path"])
    path.write_bytes(b"garbage")
    result = app.test_cli_runner().invoke(args=["backup", "verify", str(path)])
    assert result.exit_code != 0 and "checksum" in result.output


@sqlite_only
def test_cli_restore_confirmations(app, bdir):
    runner = app.test_cli_runner()
    path = backup.create_backup(bdir, now=NOW)["path"]
    create_tenant("Later Shop", "later")
    db.session.remove()

    result = runner.invoke(args=["backup", "restore", path], input="n\n")
    assert result.exit_code != 0 and tenant_count() == 2           # said no

    result = runner.invoke(args=["backup", "restore", path, "--yes", "--confirm-name", "nope"])
    assert result.exit_code != 0 and "type the database name" in result.output
    db.session.remove()
    assert tenant_count() == 2

    result = runner.invoke(args=["backup", "restore", path], input="y\ndb.sqlite\n")
    assert result.exit_code == 0, result.output
    assert "prerestore" in result.output
    db.session.remove()
    assert tenant_count() == 1


# --- MySQL command lines -------------------------------------------------------

MYSQL_URL = "mysql+pymysql://shop:S3cr3t-pw!@db.local:3307/mao"


def test_mysqldump_argv_never_contains_the_password():
    url = backup.target_for(MYSQL_URL).url
    argv = backup.mysqldump_argv(url, binary="mysqldump", gtid=True)
    assert "S3cr3t-pw!" not in " ".join(argv)
    assert not any(a.startswith(("-p", "--password")) for a in argv)
    for flag in ("--single-transaction", "--routines", "--triggers", "--set-gtid-purged=OFF",
                 "--host=db.local", "--port=3307", "--user=shop"):
        assert flag in argv
    assert argv[-1] == "mao"
    assert "--set-gtid-purged=OFF" not in backup.mysqldump_argv(url, gtid=False)
    assert backup.mysql_env(url, base={"MYSQL_PWD": "old"})["MYSQL_PWD"] == "S3cr3t-pw!"
    client = backup.mysql_argv(url, "scratch")
    assert "S3cr3t-pw!" not in " ".join(client) and client[-1] == "scratch"


def test_mysql_socket_urls_use_the_socket():
    url = backup.target_for("mysql+pymysql://u:p@/mao?unix_socket=/run/mysqld.sock").url
    argv = backup.mysql_argv(url)
    assert "--socket=/run/mysqld.sock" in argv and not any(a.startswith("--host") for a in argv)


def write_script(bin_dir, name, body):
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return bin_dir


FAKE_DUMP = r'''
import json, os, sys
open(os.environ["FAKE_LOG"], "w").write(json.dumps({"argv": sys.argv, "pwd": os.environ.get("MYSQL_PWD")}))
if "--help" in sys.argv:
    print("  --set-gtid-purged=name  Add 'SET @@GLOBAL.GTID_PURGED'"); sys.exit(0)
if os.environ.get("FAKE_FAIL"):
    sys.stderr.write("Access denied for user"); sys.exit(2)
print("-- MySQL dump 10.13")
print("CREATE TABLE `customers` (")
print("  `id` int NOT NULL")
print(") ENGINE=InnoDB;")
print("INSERT INTO `customers` VALUES (1,'Ann (shop)','it\\'s ok'),(2,'B),(C',NULL),(3,'x\\\\',0x28);")
print("INSERT INTO `customers` VALUES (4,'D',NULL);")
print("CREATE TABLE `empty_table` (")
print(") ENGINE=InnoDB;")
print("INSERT INTO `alembic_version` VALUES ('0012_pharmacy_owner');")
print("-- Dump completed on 2026-11-26  9:30:00")
'''


@pytest.fixture
def fake_mysqldump(tmp_path, monkeypatch):
    bin_dir = write_script(tmp_path / "bin", "mysqldump", FAKE_DUMP)
    log = tmp_path / "dump.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("MYSQL_PWD", raising=False)
    backup._dump_supports_gtid.cache_clear()
    yield log
    backup._dump_supports_gtid.cache_clear()


def test_mysql_create_passes_password_by_env_and_counts_rows(app, tmp_path, fake_mysqldump):
    manifest = backup.create_backup(tmp_path / "b", now=NOW, database_url=MYSQL_URL, offsite="")
    call = json.loads(fake_mysqldump.read_text())
    assert call["pwd"] == "S3cr3t-pw!"
    assert "S3cr3t-pw!" not in " ".join(call["argv"])
    assert "--set-gtid-purged=OFF" in call["argv"]
    assert manifest["name"] == "mao-20261126-093000.sql.gz"
    assert manifest["row_counts"] == {"customers": 4, "empty_table": 0, "alembic_version": 1}
    assert manifest["database_revision"] == "0012_pharmacy_owner"
    with gzip.open(manifest["path"]) as gz:
        assert b"Dump completed" in gz.read()
    result = backup.verify_backup(manifest["path"], verify_mysql_url="")
    assert result["ok"] and result["skipped"]
    assert "BACKUP_VERIFY_MYSQL_URL" in result["messages"][-1]


def test_mysql_dump_failure_leaves_no_file(app, tmp_path, fake_mysqldump, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "1")
    with pytest.raises(backup.BackupError, match="Access denied"):
        backup.create_backup(tmp_path / "b", now=NOW, database_url=MYSQL_URL, offsite="")
    assert list((tmp_path / "b").iterdir()) == []


def test_count_dump_rows_ignores_parentheses_inside_strings():
    lines = [b"INSERT INTO `t` VALUES (1,'(('),(2,'it\\'s (x)'),(3,')),((');\n",
             b"INSERT INTO `we``ird` VALUES (1);\n"]
    counts, revision, completed = backup.count_dump_rows(lines)
    assert counts == {"t": 3, "we`ird": 1} and revision is None and completed is False


@pytest.mark.skipif(not (on_mysql() and shutil.which("mysqldump") and shutil.which("mysql")),
                    reason="needs TEST_MYSQL_URL and the MySQL client tools")
def test_mysql_round_trip(app, tmp_path):
    from tests.conftest import MYSQL_SERVER_URL
    dest = tmp_path / "b"
    manifest = backup.create_backup(dest, now=NOW, offsite="")
    assert manifest["row_counts"]["tenants"] == 1
    result = backup.verify_backup(manifest["path"], verify_mysql_url=MYSQL_SERVER_URL)
    assert result["ok"] and not result["skipped"], result
    create_tenant("Later Shop", "later")
    db.session.remove()
    backup.restore_backup(manifest["path"], confirm=backup.database_name(),
                          now=NOW + timedelta(hours=1))
    db.session.remove()
    assert tenant_count() == 1


# --- deploy package ------------------------------------------------------------

DEPLOY = ROOT / "deploy"


def test_compose_file_defines_the_services_and_profiles():
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((DEPLOY / "compose.yml").read_text())
    services = compose["services"]
    assert set(services) >= {"db", "redis", "app", "backup", "cloudflared", "caddy"}
    assert services["cloudflared"]["profiles"] == ["tunnel"]
    assert services["caddy"]["profiles"] == ["vps"]
    for name in ("db", "redis", "app", "backup"):
        assert "profiles" not in services[name], name           # always on
    assert services["app"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert "healthcheck" in services["db"]


def test_every_compose_variable_is_documented_in_env_example():
    compose = (DEPLOY / "compose.yml").read_text()
    used = set(re.findall(r"\$\{([A-Z0-9_]+)", compose))
    documented = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", (DEPLOY / ".env.example").read_text(),
                                re.MULTILINE))
    assert used and used <= documented, sorted(used - documented)


def test_env_example_has_no_real_secrets():
    text = (DEPLOY / ".env.example").read_text()
    for key in ("MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "APP_SECRET_KEY",
                "CLOUDFLARE_TUNNEL_TOKEN", "TENANT_SECRETS_KEY"):
        value = re.search(rf"^{key}=(.*)$", text, re.MULTILINE).group(1).strip()
        assert value == "" or value.startswith("change-me"), key


def test_dockerfile_runs_as_a_non_root_user_and_scripts_are_executable():
    dockerfile = (DEPLOY / "Dockerfile").read_text()
    assert re.search(r"^USER\s+(?!root)\w+", dockerfile, re.MULTILINE)
    assert "requirements.txt" in dockerfile
    for script in ("entrypoint.sh", "scripts/lan-address.sh", "scripts/restore.sh"):
        path = DEPLOY / script
        assert path.read_text().startswith("#!"), script
        assert os.access(path, os.X_OK), script


def test_relative_sqlite_path_is_the_instance_folder_like_flask_sqlalchemy(app):
    """Flask-SQLAlchemy opens sqlite:///shop.sqlite in the instance folder;
    backup and restore must use that same file."""
    from pathlib import Path
    target = backup.target_for("sqlite:///shop.sqlite")
    assert target.sqlite_path == Path(app.instance_path) / "shop.sqlite"
    assert backup.target_for("sqlite:////abs/shop.sqlite").sqlite_path == Path("/abs/shop.sqlite")
