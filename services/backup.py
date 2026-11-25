"""Database backups: create, list, rotate, verify, restore, offsite copy.

One backup is two files in ``BACKUP_DIR``:

    mao-20261126-031500.sql.gz       MySQL: gzip of mysqldump output
    mao-20261126-031500.sqlite.gz    SQLite: gzip of a backup-API copy
    mao-20261126-031500.<ext>.json   manifest (created_at, kind, size, sha256,
                                     migration head, row counts per table)

The timestamp in the name is UTC. A pre-restore safety copy carries a label
(``mao-...-prerestore.sql.gz``) and is never deleted by ``rotate``.

MySQL goes through the ``mysqldump`` / ``mysql`` clients with arguments
built from the parsed SQLAlchemy URL. The password is passed in the
``MYSQL_PWD`` environment variable of the child process, never on the
command line (``ps`` shows argv to every user on the machine). SQLite uses
Python's ``sqlite3`` backup API on the database file.

Row counts come from the backup itself, not from live tables, so they
match what a restore produces even while tills keep selling: SQLite counts
the copied file; MySQL counts the row tuples in the dump's INSERT lines.

Restoring replaces the whole database. Stop the app first (the web
workers and the backup loop): ``deploy/scripts/restore.sh`` does that.
"""
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy.engine import make_url

from services.clock import NAIROBI

logger = logging.getLogger(__name__)

STALE_AFTER_HOURS = 26          # dashboard warning threshold
MIN_INTERVAL_SECONDS = 5 * 60   # "Back up now" at most once per 5 minutes
LOCK_STALE_SECONDS = 2 * 60 * 60
CHUNK = 1024 * 1024

NAME_RE = re.compile(r"^mao-(?P<stamp>\d{8}-\d{6})(?:-(?P<label>[a-z]{1,20}))?"
                     r"\.(?P<kind>sql|sqlite)\.gz$")
STAMP_FORMAT = "%Y%m%d-%H%M%S"
KIND_EXT = {"mysql": "sql", "sqlite": "sqlite"}

# Row tuples in a mysqldump extended INSERT: every "(" outside a quoted
# string starts one row (values are numbers, NULL, quoted strings, or 0x..
# hex with --hex-blob, so no other parentheses appear outside strings).
_INSERT_RE = re.compile(rb"^INSERT INTO `((?:[^`]|``)+)` VALUES ")
_TUPLE_TOKEN_RE = re.compile(rb"'(?:[^'\\]|\\.)*'|\(", re.DOTALL)
_ALEMBIC_RE = re.compile(rb"^INSERT INTO `alembic_version` VALUES \('([^']+)'\)")
_DUMP_DONE = b"-- Dump completed"


class BackupError(Exception):
    """A backup, verify or restore could not be done. The message is for people."""


class BackupBusy(BackupError):
    """Another backup or restore is running, or one ran too recently."""


# ---------------------------------------------------------------------------
# Database target
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Target:
    kind: str            # 'mysql' | 'sqlite'
    name: str            # database name (MySQL) or file name (SQLite)
    url: object          # sqlalchemy URL

    @property
    def sqlite_path(self):
        return Path(self.url.database)


def target_for(database_url=None):
    """Describe the configured database (default: the current app's)."""
    if database_url is None:
        from flask import current_app
        database_url = current_app.config["SQLALCHEMY_DATABASE_URI"]
    url = make_url(database_url)
    backend = url.get_backend_name()
    if backend == "sqlite":
        if not url.database or url.database == ":memory:":
            raise BackupError("An in-memory SQLite database cannot be backed up.")
        return Target("sqlite", Path(url.database).name, url)
    if backend in ("mysql", "mariadb"):
        if not url.database:
            raise BackupError("The database URL names no database.")
        return Target("mysql", url.database, url)
    raise BackupError(f"Backups support MySQL and SQLite, not {backend}.")


def database_name(database_url=None):
    """The name a person types to confirm a restore."""
    return target_for(database_url).name


# ---------------------------------------------------------------------------
# MySQL client command lines (password only ever in the environment)
# ---------------------------------------------------------------------------
def _connection_args(url):
    args = []
    query = dict(url.query)
    socket = query.get("unix_socket")
    if socket:
        args.append(f"--socket={socket}")
    else:
        args.append(f"--host={url.host or 'localhost'}")
        if url.port:
            args.append(f"--port={int(url.port)}")
        args.append("--protocol=TCP")
    if url.username:
        args.append(f"--user={url.username}")
    args.append("--default-character-set=utf8mb4")
    return args


def mysql_env(url, base=None):
    """Child-process environment carrying the password as MYSQL_PWD."""
    env = dict(os.environ if base is None else base)
    env.pop("MYSQL_PWD", None)
    if url.password:
        env["MYSQL_PWD"] = str(url.password)
    return env


@lru_cache(maxsize=8)
def _dump_supports_gtid(binary):
    try:
        out = subprocess.run([binary, "--help"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return b"set-gtid-purged" in out.stdout


def mysqldump_argv(url, binary="mysqldump", gtid=None):
    """argv for a consistent, restorable dump of one database. No password."""
    if gtid is None:
        gtid = _dump_supports_gtid(binary)
    argv = [binary, *_connection_args(url), "--single-transaction", "--quick",
            "--routines", "--triggers", "--events", "--hex-blob", "--no-tablespaces"]
    if gtid:
        argv.append("--set-gtid-purged=OFF")
    argv.append(url.database)
    return argv


def mysql_argv(url, database=None, binary="mysql", extra=()):
    """argv for the mysql client, optionally on one database. No password."""
    argv = [binary, *_connection_args(url), *extra]
    if database:
        argv.append(database)
    return argv


def _which(binary):
    path = shutil.which(binary)
    if path is None:
        raise BackupError(f"{binary} is not installed on this server. Install the MySQL "
                          "client tools (the Docker image includes them).")
    return path


def _mysql_query(url, sql, database=None):
    """Run SQL through the mysql client; rows as lists of strings."""
    argv = mysql_argv(url, database, binary=_which("mysql"),
                      extra=("--batch", "--skip-column-names", "--raw"))
    proc = subprocess.run(argv, input=sql.encode(), capture_output=True,
                          env=mysql_env(url), timeout=600, check=False)
    if proc.returncode != 0:
        raise BackupError(f"mysql failed: {_tail(proc.stderr)}")
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    return [line.split("\t") for line in lines if line]


def _tail(stderr, limit=500):
    text = (stderr or b"").decode("utf-8", "replace").strip()
    return text[-limit:] or "no error output"


def _quote_ident(name):
    return "`" + name.replace("`", "``") + "`"


# ---------------------------------------------------------------------------
# Names, listing, status
# ---------------------------------------------------------------------------
def _utcnow():
    return datetime.now(timezone.utc)


def _as_utc(now):
    if now is None:
        return _utcnow()
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def backup_name(kind, now, label=None):
    stamp = _as_utc(now).strftime(STAMP_FORMAT)
    suffix = f"-{label}" if label else ""
    return f"mao-{stamp}{suffix}.{KIND_EXT[kind]}.gz"


def parse_name(name):
    """``{'created_at', 'label', 'kind'}`` for a backup file name, else None."""
    match = NAME_RE.match(name or "")
    if match is None:
        return None
    try:
        created = datetime.strptime(match["stamp"], STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return {"created_at": created, "label": match["label"],
            "kind": "mysql" if match["kind"] == "sql" else "sqlite"}


def is_backup_name(name):
    return parse_name(name) is not None


def manifest_path(path):
    return Path(str(path) + ".json")


def read_manifest(path):
    try:
        return json.loads(manifest_path(path).read_text())
    except (OSError, ValueError):
        return None


def list_backups(dest_dir):
    """Backups in ``dest_dir``, newest first."""
    dest = Path(dest_dir)
    if not dest.is_dir():
        return []
    found = []
    for entry in dest.iterdir():
        info = parse_name(entry.name)
        if info is None or not entry.is_file():
            continue
        found.append({"name": entry.name, "path": entry, "size": entry.stat().st_size,
                      **info, "manifest": read_manifest(entry)})
    found.sort(key=lambda b: (b["created_at"], b["name"]), reverse=True)
    return found


def latest_backup(dest_dir):
    backups = list_backups(dest_dir)
    return backups[0] if backups else None


def backup_status(dest_dir, now=None, stale_after_hours=STALE_AFTER_HOURS):
    """What the dashboard shows: last backup, its age, and whether it is stale."""
    now = _as_utc(now)
    latest = latest_backup(dest_dir)
    if latest is None:
        return {"has_backup": False, "stale": True, "last_backup_at": None,
                "age_hours": None, "size": None, "name": None,
                "stale_after_hours": stale_after_hours,
                "message": "No backup has been made yet."}
    age = (now - latest["created_at"]).total_seconds() / 3600
    stale = age > stale_after_hours
    return {"has_backup": True, "stale": stale,
            "last_backup_at": latest["created_at"].isoformat(),
            "age_hours": round(age, 1), "size": latest["size"], "name": latest["name"],
            "stale_after_hours": stale_after_hours,
            "message": (f"The last backup is {age:.0f} hours old." if stale
                        else "Backups are up to date.")}


# ---------------------------------------------------------------------------
# Lock: one backup or restore at a time, across processes
# ---------------------------------------------------------------------------
class _Lock:
    def __init__(self, dest_dir):
        self.path = Path(dest_dir) / ".backup.lock"
        self.fd = None

    def __enter__(self):
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > LOCK_STALE_SECONDS:
                    logger.warning("removing stale backup lock %s", self.path)
                    self.path.unlink(missing_ok=True)
                    continue
                raise BackupBusy("Another backup or restore is running. Try again shortly.")
        raise BackupBusy("Another backup or restore is running. Try again shortly.")

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Migration head
# ---------------------------------------------------------------------------
def code_migration_head():
    """The newest Alembic revision this code ships (via Flask-Migrate's config)."""
    from alembic.script import ScriptDirectory
    try:
        from flask import current_app
        config = current_app.extensions["migrate"].migrate.get_config()
    except (RuntimeError, KeyError, AttributeError):
        from alembic.config import Config as AlembicConfig
        directory = Path(__file__).resolve().parents[1] / "migrations"
        config = AlembicConfig(str(directory / "alembic.ini"))
        config.set_main_option("script_location", str(directory))
    heads = ScriptDirectory.from_config(config).get_heads()
    return heads[0] if len(heads) == 1 else ",".join(sorted(heads))


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_counts(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    counts = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
    return counts


def _sqlite_revision(conn, counts):
    if "alembic_version" not in counts:
        return None
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def _dump_sqlite(target, out_path):
    """Snapshot with the backup API, then gzip. Returns (counts, revision)."""
    source = target.sqlite_path
    if not source.exists():
        raise BackupError(f"SQLite database {source} does not exist.")
    with tempfile.TemporaryDirectory(dir=out_path.parent) as scratch:
        snapshot = Path(scratch) / "snapshot.sqlite"
        src = sqlite3.connect(source)
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)
            counts = _sqlite_counts(dst)
            revision = _sqlite_revision(dst, counts)
        finally:
            dst.close()
            src.close()
        with open(snapshot, "rb") as fh, gzip.open(out_path, "wb", compresslevel=6) as gz:
            shutil.copyfileobj(fh, gz, CHUNK)
    return counts, revision


def count_dump_rows(lines):
    """Row counts per table and the alembic revision from mysqldump lines."""
    counts, revision, completed = {}, None, False
    for line in lines:
        match = _INSERT_RE.match(line)
        if match:
            table = match.group(1).replace(b"``", b"`").decode("utf-8", "replace")
            body = line[match.end():]
            rows = sum(1 for tok in _TUPLE_TOKEN_RE.findall(body) if tok == b"(")
            counts[table] = counts.get(table, 0) + rows
            if table == "alembic_version":
                found = _ALEMBIC_RE.match(line)
                revision = found.group(1).decode() if found else revision
        elif line.startswith(b"CREATE TABLE `"):
            name = line[len(b"CREATE TABLE `"):].split(b"`", 1)[0].decode("utf-8", "replace")
            counts.setdefault(name, 0)
        elif line.startswith(_DUMP_DONE):
            completed = True
    return counts, revision, completed


def _dump_mysql(target, out_path):
    """mysqldump | gzip, counting rows on the way. Returns (counts, revision)."""
    argv = mysqldump_argv(target.url, binary=_which("mysqldump"))

    def tee(stream, gz):
        for line in stream:
            gz.write(line)
            yield line

    with tempfile.TemporaryFile() as err, gzip.open(out_path, "wb", compresslevel=6) as gz:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err,
                                env=mysql_env(target.url))
        try:
            counts, revision, completed = count_dump_rows(tee(proc.stdout, gz))
        finally:
            proc.stdout.close()
            code = proc.wait()
        err.seek(0)
        stderr = err.read()
    if code != 0:
        raise BackupError(f"mysqldump failed: {_tail(stderr)}")
    if not completed:
        raise BackupError("mysqldump output is incomplete (no 'Dump completed' line).")
    return counts, revision


def create_backup(dest_dir, *, now=None, label=None, database_url=None, offsite=None):
    """Dump the database into ``dest_dir`` and write its manifest.

    Returns the manifest dict (plus ``path`` and, when an rclone remote is
    configured, ``offsite``). Raises BackupError / BackupBusy.
    """
    target = target_for(database_url)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    now = _as_utc(now)
    name = backup_name(target.kind, now, label)
    final = dest / name
    if final.exists():
        raise BackupBusy(f"{name} already exists; wait a second and try again.")
    part = dest / (name + ".part")
    started = time.monotonic()
    with _Lock(dest):
        try:
            if target.kind == "sqlite":
                counts, revision = _dump_sqlite(target, part)
            else:
                counts, revision = _dump_mysql(target, part)
            os.replace(part, final)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    manifest = {
        "name": name,
        "created_at": now.isoformat(),
        "database_kind": target.kind,
        "database_name": target.name,
        "size": final.stat().st_size,
        "sha256": _sha256(final),
        "migration_head": code_migration_head(),
        "database_revision": revision,
        "row_counts": counts,
        "total_rows": sum(counts.values()),
        "label": label,
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    tmp_manifest = manifest_path(final).with_suffix(".json.part")
    tmp_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.replace(tmp_manifest, manifest_path(final))
    logger.info("backup created %s (%s bytes, %s rows, %.1fs)", name, manifest["size"],
                manifest["total_rows"], manifest["duration_seconds"])
    result = {**manifest, "path": str(final)}
    remote = offsite if offsite is not None else _config("BACKUP_RCLONE_REMOTE")
    if remote:
        result["offsite"] = copy_offsite(final, remote)
    return result


# ---------------------------------------------------------------------------
# Offsite copy (never fatal)
# ---------------------------------------------------------------------------
def _config(key, default=None):
    try:
        from flask import current_app
        return current_app.config.get(key, default)
    except RuntimeError:
        return os.environ.get(key, default)


def copy_offsite(path, remote):
    """``rclone copy`` the backup and its manifest to ``remote``. Never raises."""
    rclone = shutil.which("rclone")
    if rclone is None:
        message = "rclone is not installed; offsite copy skipped"
        logger.error(message)
        return {"ok": False, "remote": remote, "message": message}
    for item in (Path(path), manifest_path(path)):
        try:
            proc = subprocess.run([rclone, "copy", str(item), remote], capture_output=True,
                                  timeout=3600, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.error("offsite copy of %s failed: %s", item.name, e)
            return {"ok": False, "remote": remote, "message": str(e)}
        if proc.returncode != 0:
            message = _tail(proc.stderr)
            logger.error("offsite copy of %s failed: %s", item.name, message)
            return {"ok": False, "remote": remote, "message": message}
    logger.info("offsite copy of %s to %s done", Path(path).name, remote)
    return {"ok": True, "remote": remote, "message": "copied"}


# ---------------------------------------------------------------------------
# Rotate
# ---------------------------------------------------------------------------
def select_to_keep(backups, keep_daily=14, keep_weekly=8):
    """Names to keep: the newest per Nairobi day for the ``keep_daily`` most
    recent days that have backups, and the newest per ISO week for the
    ``keep_weekly`` most recent weeks. Labelled backups (pre-restore) are
    always kept. The newest backup is always kept."""
    regular = sorted((b for b in backups if not b["label"]),
                     key=lambda b: (b["created_at"], b["name"]), reverse=True)
    keep = {b["name"] for b in backups if b["label"]}
    if regular:
        keep.add(regular[0]["name"])
    days, weeks = {}, {}
    for b in regular:                        # newest first: first seen wins
        local = b["created_at"].astimezone(NAIROBI)
        days.setdefault(local.date(), b["name"])
        weeks.setdefault(local.isocalendar()[:2], b["name"])
    keep.update(list(days.values())[:keep_daily])
    keep.update(list(weeks.values())[:keep_weekly])
    return keep


def rotate(dest_dir, keep_daily=14, keep_weekly=8):
    """Delete backups outside the retention policy. Returns deleted names."""
    backups = list_backups(dest_dir)
    keep = select_to_keep(backups, keep_daily, keep_weekly)
    deleted = []
    for b in backups:
        if b["name"] in keep:
            continue
        b["path"].unlink(missing_ok=True)
        manifest_path(b["path"]).unlink(missing_ok=True)
        deleted.append(b["name"])
    if deleted:
        logger.info("rotated out %d backups: %s", len(deleted), ", ".join(deleted))
    return deleted


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
def _resolve(path, dest_dir=None):
    candidate = Path(path)
    if not candidate.exists() and dest_dir is not None and is_backup_name(candidate.name):
        candidate = Path(dest_dir) / candidate.name
    if not candidate.is_file():
        raise BackupError(f"No backup file at {path}.")
    if parse_name(candidate.name) is None:
        raise BackupError(f"{candidate.name} is not a backup file name (mao-YYYYmmdd-HHMMSS...).")
    return candidate


def _gunzip_to(path, out):
    try:
        with gzip.open(path, "rb") as gz, open(out, "wb") as fh:
            shutil.copyfileobj(gz, fh, CHUNK)
    except (OSError, EOFError, gzip.BadGzipFile) as e:
        raise BackupError(f"{Path(path).name} is damaged: {e}")


def _check_file(path, manifest, problems):
    if manifest is None:
        problems.append("no manifest next to the file; checksum and row counts not checked")
        return
    if manifest.get("sha256") != _sha256(path):
        raise BackupError(f"{Path(path).name} does not match its manifest checksum: "
                          "the file is damaged or was changed.")


def _compare_counts(expected, actual):
    mismatches = []
    for table in sorted(set(expected) | set(actual)):
        if expected.get(table) != actual.get(table):
            mismatches.append(f"{table}: manifest {expected.get(table)}, "
                              f"restored {actual.get(table)}")
    return mismatches


def verify_backup(path, *, dest_dir=None, verify_mysql_url=None):
    """Restore into a scratch database and compare row counts with the manifest.

    Returns ``{'ok', 'skipped', 'name', 'messages', 'row_counts'}``. A
    damaged file raises BackupError.
    """
    path = _resolve(path, dest_dir)
    info = parse_name(path.name)
    manifest = read_manifest(path)
    messages = []
    _check_file(path, manifest, messages)
    expected = (manifest or {}).get("row_counts")
    if info["kind"] == "sqlite":
        actual = _verify_sqlite(path)
    else:
        server = verify_mysql_url or _config("BACKUP_VERIFY_MYSQL_URL")
        if not server:
            _check_dump_complete(path)
            messages.append("MySQL restore test skipped: set BACKUP_VERIFY_MYSQL_URL to a "
                            "server where a scratch database may be created. Checksum and "
                            "dump completeness were checked.")
            return {"ok": True, "skipped": True, "name": path.name, "messages": messages,
                    "row_counts": None}
        actual = _verify_mysql(path, server)
    if expected is not None:
        mismatches = _compare_counts(expected, actual)
        if mismatches:
            return {"ok": False, "skipped": False, "name": path.name,
                    "messages": messages + ["row counts differ: " + "; ".join(mismatches)],
                    "row_counts": actual}
    messages.append(f"restored {sum(actual.values())} rows in {len(actual)} tables")
    return {"ok": True, "skipped": False, "name": path.name, "messages": messages,
            "row_counts": actual}


def _verify_sqlite(path):
    with tempfile.TemporaryDirectory() as scratch:
        db_file = Path(scratch) / "verify.sqlite"
        _gunzip_to(path, db_file)
        try:
            conn = sqlite3.connect(db_file)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise BackupError(f"{path.name} failed the integrity check: {result}")
                return _sqlite_counts(conn)
            finally:
                conn.close()
        except sqlite3.DatabaseError as e:
            raise BackupError(f"{path.name} is not a readable SQLite database: {e}")


def _iter_dump_lines(path):
    try:
        with gzip.open(path, "rb") as gz:
            yield from gz
    except (OSError, EOFError, gzip.BadGzipFile) as e:
        raise BackupError(f"{Path(path).name} is damaged: {e}")


def _check_dump_complete(path):
    _, _, completed = count_dump_rows(_iter_dump_lines(path))
    if not completed:
        raise BackupError(f"{path.name} is incomplete (no 'Dump completed' line).")


def scratch_schema_name(path):
    stamp = parse_name(Path(path).name)["created_at"].strftime("%Y%m%d_%H%M%S")
    return f"mao_verify_{stamp}_{os.getpid()}"


def _verify_mysql(path, server_url):
    url = make_url(server_url)
    schema = scratch_schema_name(path)
    _mysql_query(url, f"CREATE DATABASE {_quote_ident(schema)} CHARACTER SET utf8mb4")
    try:
        _load_dump(url, schema, path)
        tables = [r[0] for r in _mysql_query(
            url, "SELECT table_name FROM information_schema.tables "
                 f"WHERE table_schema = '{schema}' AND table_type = 'BASE TABLE'")]
        if not tables:
            return {}
        sql = " UNION ALL ".join(
            f"SELECT '{t}', COUNT(*) FROM {_quote_ident(t)}" for t in tables
            if "'" not in t and "\\" not in t)
        return {row[0]: int(row[1]) for row in _mysql_query(url, sql, schema)}
    finally:
        try:
            _mysql_query(url, f"DROP DATABASE IF EXISTS {_quote_ident(schema)}")
        except BackupError as e:
            logger.error("could not drop scratch schema %s: %s", schema, e)


def _load_dump(url, database, path, preamble=b""):
    """Stream a gzip dump into the mysql client on ``database``."""
    argv = mysql_argv(url, database, binary=_which("mysql"))
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=err, env=mysql_env(url))
        try:
            if preamble:
                proc.stdin.write(preamble)
            for line in _iter_dump_lines(path):
                proc.stdin.write(line)
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
            code = proc.wait()
        err.seek(0)
        stderr = err.read()
    if code != 0:
        raise BackupError(f"mysql restore failed: {_tail(stderr)}")


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
def restore_backup(path, *, confirm, dest_dir=None, database_url=None, now=None):
    """Replace the configured database with the backup at ``path``.

    Refuses unless ``confirm`` equals the database name. Checks the file's
    checksum, takes a pre-restore backup of the current database into
    ``dest_dir`` (default: the backup's own folder), then restores.
    The app must be stopped while this runs.
    Returns ``{'restored', 'pre_restore', 'database'}``.
    """
    target = target_for(database_url)
    if confirm != target.name:
        raise BackupError(f"Refusing to restore: type the database name ({target.name}) "
                          "to confirm.")
    path = _resolve(path, dest_dir)
    info = parse_name(path.name)
    if info["kind"] != target.kind:
        raise BackupError(f"{path.name} is a {info['kind']} backup but the configured "
                          f"database is {target.kind}.")
    _check_file(path, read_manifest(path), [])
    safety_dir = Path(dest_dir) if dest_dir is not None else path.parent
    pre = create_backup(safety_dir, now=now, label="prerestore", database_url=database_url,
                        offsite="")
    with _Lock(safety_dir):
        if target.kind == "sqlite":
            _restore_sqlite(path, target)
        else:
            _restore_mysql(path, target)
    logger.warning("database %s restored from %s (pre-restore copy %s)", target.name,
                   path.name, pre["name"])
    return {"restored": path.name, "pre_restore": pre["name"], "database": target.name}


def _restore_sqlite(path, target):
    with tempfile.TemporaryDirectory() as scratch:
        db_file = Path(scratch) / "restore.sqlite"
        _gunzip_to(path, db_file)
        src = sqlite3.connect(db_file)
        dst = sqlite3.connect(target.sqlite_path)
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError(f"{path.name} failed the integrity check.")
            src.backup(dst)                # replaces every page of the live file
        except sqlite3.DatabaseError as e:
            raise BackupError(f"SQLite restore failed: {e}")
        finally:
            src.close()
            dst.close()


def _restore_mysql(path, target):
    """Drop every table and view in the database, then load the dump.

    Dropping first means tables added by later migrations do not survive
    next to an older alembic_version (``flask db upgrade`` would then fail).
    """
    url = target.url
    rows = _mysql_query(url, "SHOW FULL TABLES", url.database)
    tables = [r[0] for r in rows if len(r) > 1 and r[1] == "BASE TABLE"]
    views = [r[0] for r in rows if len(r) > 1 and r[1] == "VIEW"]
    preamble = ["SET FOREIGN_KEY_CHECKS=0;"]
    if views:
        preamble.append("DROP VIEW IF EXISTS " + ", ".join(map(_quote_ident, views)) + ";")
    if tables:
        preamble.append("DROP TABLE IF EXISTS " + ", ".join(map(_quote_ident, tables)) + ";")
    preamble.append("SET FOREIGN_KEY_CHECKS=1;\n")
    _load_dump(url, url.database, path, preamble="\n".join(preamble).encode())


def default_dir():
    """``BACKUP_DIR`` from config (relative paths are from the working directory)."""
    return Path(_config("BACKUP_DIR") or "backups").resolve()


def too_soon(dest_dir, now=None, min_interval=MIN_INTERVAL_SECONDS):
    """Seconds to wait before another manual backup, or 0."""
    latest = latest_backup(dest_dir)
    if latest is None:
        return 0
    elapsed = (_as_utc(now) - latest["created_at"]).total_seconds()
    return max(0, int(min_interval - elapsed)) if elapsed >= 0 else 0


def human_size(size):
    if size is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def age_text(created_at, now=None):
    delta = _as_utc(now) - created_at
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{max(int(delta.total_seconds() // 60), 0)} min ago"
    if hours < 48:
        return f"{hours:.0f} h ago"
    return f"{hours / 24:.0f} days ago"

