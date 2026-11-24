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
import shutil
import uuid

import pytest
from flask_migrate import upgrade
from sqlalchemy import create_engine

import app as app_module
from auth.tenancy import bind_tenant, reset_tenant, tenant_scope, unscoped
from models import Tenant, User, db

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


def create_tenant(name, slug, vertical="pharmacy", **fields):
    """Create a tenant (outside any scope) and return its id."""
    with unscoped():
        tenant = Tenant(name=name, slug=slug, vertical=vertical, **fields)
        db.session.add(tenant)
        db.session.commit()
        return tenant.id


@pytest.fixture(scope="session")
def sqlite_template(tmp_path_factory):
    """A SQLite file migrated to head once per test session.

    Each test copies it instead of re-running every migration. The
    migrations themselves are exercised from empty in test_migrations*.py.
    """
    path = tmp_path_factory.mktemp("template") / "head.sqlite"
    template_app = make_app(f"sqlite:///{path}")
    with template_app.app_context():
        upgrade()
        db.engine.dispose()
    return path


@pytest.fixture
def app(database_url, request):
    """Fresh app on a migrated database, with tenant 'acme' bound.

    Tests run as tenant acme unless they bind another one. ``app.acme_id``
    is its id. SQLite starts from a copy of the session template; MySQL
    runs the migrations into the test's own database.
    """
    if not on_mysql():
        template = request.getfixturevalue("sqlite_template")
        shutil.copyfile(template, database_url.removeprefix("sqlite:///"))
    flask_app = make_app(database_url)
    with flask_app.app_context():
        if on_mysql():
            upgrade()
        flask_app.acme_id = create_tenant("Acme Pharmacy", "acme", "pharmacy")
        token = bind_tenant(flask_app.acme_id)
        try:
            yield flask_app
        finally:
            reset_tenant(token)
            db.session.remove()
            db.engine.dispose()


@pytest.fixture
def other_tenant(app):
    """A second business, 'globex'. Returns its id; acme stays bound."""
    return create_tenant("Globex Distributors", "globex", "distributor")


class FakeSMS:
    """Stands in for the Africa's Talking SMS client. Records every send.

    Checks numbers the way the real SDK does (``+`` and digits, else
    ValueError) and replies in Africa's Talking's shape, with a unique
    messageId per recipient. ``reply_code`` sets the per-recipient
    statusCode (101 Sent; 4xx/5xx are refusals such as 405
    InsufficientBalance).
    """

    def __init__(self):
        self.sent = []
        self.fail = False
        self.reply_code = 101
        self._ids = 0

    def send(self, message, recipients, sender_id=None):
        import re
        for phone in recipients:
            if not re.match(r"^\+\d{1,3}\d{3,}$", phone):
                raise ValueError("Invalid phone number: " + phone)
        if self.fail:
            raise RuntimeError("simulated Africa's Talking outage")
        self.sent.append({"message": message, "to": list(recipients), "sender_id": sender_id})
        out = []
        for phone in recipients:
            self._ids += 1
            ok = self.reply_code in (100, 101, 102)
            out.append({"statusCode": self.reply_code, "number": phone,
                        "status": "Success" if ok else "InsufficientBalance",
                        "cost": "KES 0.8000" if ok else "0",
                        "messageId": f"ATXid_{self._ids:06d}" if ok else "None"})
        return {"SMSMessageData": {"Message": f"Sent to {len(out)}/{len(out)}",
                                   "Recipients": out}}


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
def login_as(app, client):
    """Return ``login(role, email=None, tenant_id=None)``: creates a real,
    approved user with that role in the tenant (default acme), puts them in
    ``client``'s session and returns the client."""
    def _login(role="admin", email=None, tenant_id=None):
        tenant_id = tenant_id or app.acme_id
        email = email or f"{role}-{tenant_id}@example.com"
        with tenant_scope(tenant_id):
            user = User.query.filter_by(email=email).first()
            if user is None:
                user = User(email=email, name=f"Test {role}", role=role, status="approved")
                db.session.add(user)
                db.session.commit()
            user_id = user.id
        with client.session_transaction() as sess:
            sess.clear()
            sess["profile"] = {"email": email, "name": f"Test {role}"}
            sess["role"] = role
            sess["user_id"] = user_id
            sess["tenant_id"] = tenant_id
        return client
    return _login
