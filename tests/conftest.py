"""Shared fixtures.

Every test gets a fresh app on its own file-backed SQLite database, so the
suite needs no MySQL, Redis, credentials or network.
"""
import pytest
from flask_migrate import downgrade, upgrade

import app as app_module
from models import db


@pytest.fixture
def app(tmp_path):
    """Fresh app whose schema is built by the real migrations, not create_all."""
    flask_app = app_module.create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'app.db'}",
    })
    with flask_app.app_context():
        upgrade()
        yield flask_app
        db.session.remove()
        downgrade(revision="base")


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
