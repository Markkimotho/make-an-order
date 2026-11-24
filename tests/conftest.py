"""Shared fixtures.

Every test gets a fresh app on its own file-backed SQLite database, so the
suite needs no MySQL, Redis, credentials or network.
"""
import pytest

import app as app_module
from models import db


@pytest.fixture
def app(tmp_path):
    flask_app = app_module.create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'app.db'}",
    })
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


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
