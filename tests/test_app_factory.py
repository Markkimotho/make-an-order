"""create_app() must build a fully wired app with no network I/O."""
import subprocess
import sys
import textwrap
from pathlib import Path

from flask import request

from app import create_app
from models import NAMING_CONVENTION, db

REPO = Path(__file__).resolve().parents[1]


def _app(tmp_path, **extra):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return create_app({
        "TESTING": True,
        "SECRET_KEY": "t",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 't.db'}",
        **extra,
    })


def _ip_probe(app):
    app.add_url_rule("/_ip", "_ip", lambda: request.remote_addr or "")
    return app.test_client()


def test_import_does_no_network_io():
    code = textwrap.dedent("""
        import socket, MySQLdb, redis
        def boom(*a, **k):
            raise AssertionError("network I/O at import")
        MySQLdb.connect = boom
        redis.Redis.from_url = boom
        socket.create_connection = boom
        socket.socket.connect = boom
        import app
        assert app.app is not None
    """)
    # MYSQL_HOST is a non-routable address: any dial would hang or fail.
    env = {"PATH": "/usr/bin:/bin", "MYSQL_HOST": "10.255.255.1",
           "MYSQL_USER": "u", "MYSQL_DB": "d"}
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=REPO,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_create_app_twice(tmp_path):
    a = _app(tmp_path / "a")
    b = _app(tmp_path / "b")
    assert a is not b
    assert len(a.logger.handlers) == len(b.logger.handlers)


def test_module_app_has_all_blueprints():
    from app import app
    rules = {r.rule for r in app.url_map.iter_rules()}
    for path in ("/pos/transaction/new", "/payments/mpesa/callback",
                 "/auth/login", "/dashboard", "/onboarding", "/"):
        assert path in rules, path


def test_endpoint_names_unchanged(tmp_path):
    app = _app(tmp_path)
    endpoints = set(app.view_functions)
    for name in ("index", "dashboard", "login_page", "register_page", "onboarding",
                 "pos_page", "shift_page", "pharmacy_page", "purchase_orders_page",
                 "inventory_page", "customers_page", "orders_page", "users_page",
                 "reports_page", "reports_dashboard_page", "logout", "auth.login"):
        assert name in endpoints, name


def test_override_wins_over_config(tmp_path):
    app = _app(tmp_path, SECRET_KEY="override")
    assert app.secret_key == "override"
    assert app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///")


def test_proxy_fix_trusts_one_hop(tmp_path):
    client = _ip_probe(_app(tmp_path))
    r = client.get("/_ip", headers={"X-Forwarded-For": "6.6.6.6, 196.201.214.200"})
    assert r.get_data(as_text=True) == "196.201.214.200"


def test_proxy_fix_hops_configurable(tmp_path):
    client = _ip_probe(_app(tmp_path, PROXY_FIX_X_FOR=2))
    r = client.get("/_ip", headers={"X-Forwarded-For": "6.6.6.6, 196.201.214.200"})
    assert r.get_data(as_text=True) == "6.6.6.6"


def test_naming_convention_on_metadata():
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}
    for key, pattern in NAMING_CONVENTION.items():
        assert db.metadata.naming_convention[key] == pattern


def test_no_tables_created_at_startup(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        assert db.inspect(db.engine).get_table_names() == []


def test_create_db_command_registered(tmp_path):
    app = _app(tmp_path)
    assert "create-db" in app.cli.commands
