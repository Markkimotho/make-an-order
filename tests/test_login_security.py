"""Login security: lockout after failed passwords, the login rate limit,
audit rows for every sign-in event, the idle timeout and admin unlock."""
import json
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import auth.auth_routes as auth_routes
from auth.tenancy import tenant_scope, unscoped
from models import AuditLog, Tenant, User, db

PASSWORD = "pass-word-1"
WRONG = "wrong-guess-9"


def make_user(tenant_id, email="clerk@acme.co", role="cashier", **fields):
    with tenant_scope(tenant_id):
        u = User(email=email, name="Clerk Njeri", role=role,
                 status=fields.pop("status", "approved"), **fields)
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.commit()
        return u.id


def user(email="clerk@acme.co"):
    with unscoped():
        return User.query.filter_by(email=email).first()


def audit_rows(action=None):
    with unscoped():
        q = AuditLog.query
        if action:
            q = q.filter_by(action=action)
        return q.order_by(AuditLog.id).all()


def login(client, email="clerk@acme.co", password=PASSWORD):
    return client.post("/auth/login/local", data={"email": email, "password": password})


def signed_in(client):
    with client.session_transaction() as sess:
        return "user_id" in sess


# --- lockout -----------------------------------------------------------------

def test_four_failures_do_not_lock_the_fifth_does(client, app):
    make_user(app.acme_id)
    for n in range(1, 5):
        r = login(client, password=WRONG)
        assert "Invalid+email+or+password" in r.headers["Location"]
        u = user()
        assert (u.failed_logins, u.locked_until) == (n, None)
    r = login(client, password=WRONG)
    # The same answer as any failure: the lock is not announced to the
    # person guessing (that would tell them the email has an account).
    assert "Invalid+email+or+password" in r.headers["Location"]
    u = user()
    assert u.failed_logins == 5
    assert u.locked_until > datetime.utcnow() + timedelta(minutes=14)


def test_right_password_refused_while_locked(client, app):
    make_user(app.acme_id)
    for _ in range(5):
        login(client, password=WRONG)
    r = login(client)
    assert "Invalid+email+or+password" in r.headers["Location"]
    assert not signed_in(client)
    # Attempts while locked neither extend the lock nor raise the count.
    assert user().failed_logins == 5


def test_lock_message_counts_down():
    """Shown only where identity is proven (Google sign-in)."""
    now = datetime(2026, 11, 26, 12, 0)
    assert "locked for 1 more minute." in auth_routes.locked_message(
        now + timedelta(seconds=50), now)
    assert "locked for 15 more minutes." in auth_routes.locked_message(
        now + timedelta(minutes=15), now)


def test_locked_unknown_and_wrong_answers_are_identical(client, app):
    """A locked account cannot be told apart from a missing one."""
    make_user(app.acme_id)
    for _ in range(5):
        login(client, password=WRONG)
    locked = login(client, password=WRONG).headers["Location"]
    unknown = login(client, email="nobody@acme.co", password=WRONG).headers["Location"]
    assert locked == unknown


def test_lock_expires(client, app):
    make_user(app.acme_id, failed_logins=5,
              locked_until=datetime.utcnow() - timedelta(seconds=1))
    r = login(client)
    assert r.headers["Location"] == "/dashboard"
    u = user()
    assert (u.failed_logins, u.locked_until) == (0, None)
    assert u.last_login_at is not None


def test_expired_lock_counts_failures_from_zero(client, app):
    make_user(app.acme_id, failed_logins=5,
              locked_until=datetime.utcnow() - timedelta(seconds=1))
    r = login(client, password=WRONG)
    assert "Invalid" in r.headers["Location"]
    u = user()
    assert (u.failed_logins, u.locked_until) == (1, None)


def test_lock_expiry_by_clock(client, app, monkeypatch):
    make_user(app.acme_id)
    for _ in range(5):
        login(client, password=WRONG)
    later = datetime.utcnow() + timedelta(minutes=16)
    monkeypatch.setattr(auth_routes, "_utcnow", lambda: later)
    assert login(client).headers["Location"] == "/dashboard"


def test_success_resets_the_count(client, app):
    make_user(app.acme_id)
    for _ in range(3):
        login(client, password=WRONG)
    assert user().failed_logins == 3
    assert login(client).headers["Location"] == "/dashboard"
    assert user().failed_logins == 0
    client.get("/logout")
    for _ in range(4):                       # a fresh run of four is still not a lock
        login(client, password=WRONG)
    assert user().locked_until is None


def test_unknown_email_gets_the_same_message(client, app):
    make_user(app.acme_id)
    unknown = login(client, email="nobody@acme.co", password=WRONG)
    wrong = login(client, password=WRONG)
    assert unknown.headers["Location"] == wrong.headers["Location"]


def test_google_sign_in_is_refused_while_locked(client, app):
    make_user(app.acme_id, email="known@gmail.com", failed_logins=5,
              locked_until=datetime.utcnow() + timedelta(minutes=10))
    google = app.extensions["authlib.integrations.flask_client"].google
    google.authorize_access_token = MagicMock(return_value={"access_token": "t"})
    google.get = MagicMock(return_value=MagicMock(json=lambda: {"email": "known@gmail.com"}))
    r = client.get("/auth/authorize")
    assert "locked" in r.headers["Location"]
    assert not signed_in(client)


def test_failed_count_increments_in_sql_not_from_a_stale_read(client, app, monkeypatch):
    """Two failures racing must both count: the increment is
    failed_logins = failed_logins + 1 in SQL, so a request that read the
    row before another request's failure still adds to the newer value."""
    uid = make_user(app.acme_id)
    original = User.check_password

    def slow_check(self, password):
        # Another request's failure lands after this one loaded the row
        # (written on a separate connection, as another worker would).
        from sqlalchemy.orm import Session
        side = Session(bind=db.engine)
        try:
            row = side.get(User, uid)
            row.failed_logins = (row.failed_logins or 0) + 1
            side.commit()
        finally:
            side.close()
        return original(self, password)

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("mysql"):
        pytest.skip("MySQL serializes on the row lock; this simulates the SQLite case")
    monkeypatch.setattr(User, "check_password", slow_check)
    login(client, password=WRONG)
    assert user().failed_logins == 2


# --- audit -------------------------------------------------------------------

def test_every_sign_in_event_is_audited_without_the_password(client, app):
    uid = make_user(app.acme_id)
    secret_tries = [f"guess-{n}-secret" for n in range(5)]
    for pw in secret_tries:
        login(client, password=pw)
    login(client, email="ghost@nowhere.co", password="ghost-secret")
    failed = audit_rows("login.failed")
    assert len(failed) == 6
    mine = [r for r in failed if r.user_id == uid]
    assert len(mine) == 5 and all(r.tenant_id == app.acme_id for r in mine)
    assert all(r.new_value["email"] == "clerk@acme.co" for r in mine)
    assert all(r.ip_address == "127.0.0.1" for r in failed)
    ghost = [r for r in failed if r.user_id is None]
    assert ghost[0].tenant_id is None and ghost[0].new_value["email"] == "ghost@nowhere.co"
    locked = audit_rows("login.locked")
    assert [(r.user_id, r.tenant_id) for r in locked] == [(uid, app.acme_id)]

    with unscoped():
        u = db.session.get(User, uid)
        u.locked_until = None
        db.session.commit()
    login(client)
    ok = audit_rows("login")
    assert [(r.user_id, r.tenant_id, r.new_value["method"]) for r in ok] == \
        [(uid, app.acme_id, "password")]
    client.get("/logout")
    out = audit_rows("logout")
    assert [(r.user_id, r.tenant_id) for r in out] == [(uid, app.acme_id)]

    dump = json.dumps([r.to_dict() for r in audit_rows()])
    for pw in [*secret_tries, "ghost-secret", PASSWORD]:
        assert pw not in dump
    with unscoped():
        assert "pbkdf2" not in dump and "scrypt" not in dump


def test_auth_logout_route_is_audited_too(client, app):
    make_user(app.acme_id)
    login(client)
    client.get("/auth/logout")
    assert len(audit_rows("logout")) == 1


# --- rate limit ----------------------------------------------------------------

def test_password_login_is_rate_limited_per_ip(client, app):
    codes = [login(client, email=f"guess{n}@acme.co", password=WRONG).status_code
             for n in range(11)]
    assert codes == [302] * 10 + [429]
    # Another IP still gets through.
    r = client.post("/auth/login/local", data={"email": "x@acme.co", "password": WRONG},
                    environ_base={"REMOTE_ADDR": "10.0.0.9"})
    assert r.status_code == 302


# --- idle timeout ----------------------------------------------------------------

def _age_session(client, seconds):
    with client.session_transaction() as sess:
        sess["last_seen"] = int(time.time()) - seconds


def test_idle_session_is_signed_out(login_as, app):
    client = login_as("admin")
    assert client.get("/dashboard").status_code == 200
    _age_session(client, 31 * 60)
    r = client.get("/dashboard")
    assert r.status_code == 302
    assert "signed+out+after+30+minutes+of+inactivity" in r.headers["Location"]
    assert not signed_in(client)
    out = audit_rows("logout")
    assert len(out) == 1 and out[0].new_value == {"reason": "idle"}
    assert out[0].tenant_id == app.acme_id


def test_activity_inside_the_window_keeps_the_session(login_as):
    client = login_as("admin")
    _age_session(client, 29 * 60)
    assert client.get("/dashboard").status_code == 200
    with client.session_transaction() as sess:
        assert int(time.time()) - sess["last_seen"] < 5     # the request refreshed it
    _age_session(client, 29 * 60)
    assert client.get("/users/").status_code == 200       # API polling counts as activity


def test_idle_api_call_gets_401_json(login_as):
    client = login_as("admin")
    client.get("/dashboard")
    _age_session(client, 31 * 60)
    r = client.get("/users/")
    assert r.status_code == 401
    assert r.get_json()["error"] == "Session expired"
    assert not signed_in(client)


def test_idle_limit_is_the_tenants_setting(login_as, app):
    with unscoped():
        db.session.get(Tenant, app.acme_id).session_idle_minutes = 5
        db.session.commit()
    client = login_as("admin")
    client.get("/dashboard")
    _age_session(client, 6 * 60)
    r = client.get("/dashboard")
    assert "after+5+minutes" in r.headers["Location"]


def test_login_stamps_last_seen(client, app):
    make_user(app.acme_id)
    login(client)
    with client.session_transaction() as sess:
        assert abs(sess["last_seen"] - time.time()) < 5


# --- admin unlock --------------------------------------------------------------

def _locked(app):
    return make_user(app.acme_id, failed_logins=5,
                     locked_until=datetime.utcnow() + timedelta(minutes=10))


def test_admin_unlocks_an_account(login_as, app):
    uid = _locked(app)
    admin = login_as("admin")
    listed = admin.get("/users/").get_json()
    row = next(u for u in listed if u["id"] == uid)    # admins see locked users
    assert row["locked"] and row["failed_logins"] == 5 and "password_hash" not in row
    r = admin.post(f"/users/{uid}/unlock")
    assert r.status_code == 200 and r.get_json()["user"]["locked"] is False
    u = user()
    assert (u.failed_logins, u.locked_until) == (0, None)
    rows = audit_rows("user.unlock")
    assert len(rows) == 1 and rows[0].entity_id == uid and rows[0].tenant_id == app.acme_id
    assert rows[0].old_value["failed_logins"] == 5


@pytest.mark.parametrize("role", ["cashier", "accountant", "pharmacist", "stock_clerk", "user"])
def test_unlock_needs_an_admin(login_as, app, role):
    uid = _locked(app)
    assert login_as(role).post(f"/users/{uid}/unlock").status_code == 403
    assert user().locked_until is not None


def test_unlock_cannot_reach_another_business(login_as, app, other_tenant):
    uid = make_user(other_tenant, email="theirs@globex.co", failed_logins=5,
                    locked_until=datetime.utcnow() + timedelta(minutes=10))
    assert login_as("superuser").post(f"/users/{uid}/unlock").status_code == 404
    assert user("theirs@globex.co").locked_until is not None


def test_unlocked_user_can_sign_in(client, login_as, app):
    uid = _locked(app)
    login_as("admin").post(f"/users/{uid}/unlock")
    fresh = app.test_client()
    assert login(fresh).headers["Location"] == "/dashboard"


# --- roles on the users API ------------------------------------------------------

def test_accountant_is_a_role_choice_and_unknown_roles_are_refused(login_as):
    owner = login_as("superuser")
    roles = owner.get("/users/roles").get_json()
    assert {r["key"] for r in roles} >= {"accountant", "cashier", "admin"}
    assert all(r["description"] for r in roles)
    r = owner.post("/users/", json={"email": "books@acme.co", "role": "accountant"})
    assert r.status_code == 201 and r.get_json()["user"]["role"] == "accountant"
    assert owner.post("/users/", json={"email": "x@acme.co", "role": "boss"}).status_code == 400
    uid = r.get_json()["user"]["id"]
    assert owner.put(f"/users/{uid}", json={"role": "god"}).status_code == 400
    assert owner.put(f"/users/{uid}", json={"role": "cashier"}).status_code == 200


def test_users_list_shows_last_login(client, login_as, app):
    make_user(app.acme_id)
    login(client)
    rows = login_as("superuser").get("/users/").get_json()
    clerk = next(u for u in rows if u["email"] == "clerk@acme.co")
    assert clerk["last_login_at"] and clerk["locked"] is False
