"""Shared fixtures.

Every test gets a fresh app on its own database, built by the real
migrations, so the suite needs no MySQL, Redis, credentials or network.

Database backend:
  default              a SQLite file under the test's tmp_path
  TEST_MYSQL_URL=...    a fresh MySQL database per test on that server
                        (server URL without a database name, for example
                        mysql+pymysql://root:root@127.0.0.1:3306). CI runs
                        both lanes; locally use scripts/local_mysql.sh.
"""
import os
import uuid

import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import create_engine

import app as app_module
from models import db

MYSQL_SERVER_URL = os.environ.get("TEST_MYSQL_URL", "").rstrip("/")


def on_mysql():
    return bool(MYSQL_SERVER_URL)


@pytest.fixture
def database_url(tmp_path):
    """URL of an empty database that exists only for this test."""
    if not MYSQL_SERVER_URL:
        yield f"sqlite:///{tmp_path / 'db.sqlite'}"
        return
    name = f"t_{uuid.uuid4().hex[:8]}"
    server = create_engine(MYSQL_SERVER_URL, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
    try:
        yield f"{MYSQL_SERVER_URL}/{name}?charset=utf8mb4"
    finally:
        with server.connect() as conn:
            conn.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
        server.dispose()


def make_app(database_url, **extra):
    return app_module.create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "SQLALCHEMY_DATABASE_URI": database_url,
        **extra,
    })


@pytest.fixture
def app(database_url):
    """Fresh app whose schema is built by the real migrations, not create_all."""
    flask_app = make_app(database_url)
    with flask_app.app_context():
        upgrade()
        yield flask_app
        db.session.remove()
        downgrade(revision="base")
        db.engine.dispose()


class FakeSMS:
    """Stands in for the Africa's Talking SMS client. Records every send."""

    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, message, recipients, sender_id=None):
        if self.fail:
            raise RuntimeError("simulated Africa's Talking outage")
        self.sent.append({"message": message, "to": list(recipients), "sender_id": sender_id})
        return {"SMSMessageData": {"Recipients": [{"status": "Success"}]}}


@pytest.fixture(autouse=True)
def sms_outbox(monkeypatch):
    """Every test gets a fake SMS client, so no test can text a real phone,
    whatever credentials CI or a local .env provides."""
    import services.sms_service as sms_service
    fake = FakeSMS()
    monkeypatch.setattr(sms_service, "_client", lambda: fake)
    return fake


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def login_as(client):
    """Return a function that logs ``client`` in with a role and returns it."""
    def _login(role="admin", email="test@example.com", user_id=1):
        with client.session_transaction() as sess:
            sess["profile"] = {"email": email, "name": "Test User"}
            sess["role"] = role
            sess["user_id"] = user_id
        return client
    return _login
