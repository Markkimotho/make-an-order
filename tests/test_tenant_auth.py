"""Business signup, login, per-request tenant binding and platform admins."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from auth.tenancy import tenant_scope, unscoped
from models import Customer, Tenant, User, db
from tests.conftest import create_tenant

SIGNUP = {
    "business_name": "Kilimani Chemist",
    "vertical": "pharmacy",
    "name": "Achieng Otieno",
    "email": "achieng@kilimani.co.ke",
    "phone_number": "0712 345 678",
    "password": "correct-horse",
}


def _tenant(slug):
    with unscoped():
        return Tenant.query.filter_by(slug=slug).first()


def _user(email):
    with unscoped():
        return User.query.filter_by(email=email).first()


# --- signup ----------------------------------------------------------------

def test_signup_creates_tenant_and_owner_and_logs_in(client):
    r = client.post("/auth/register", data=SIGNUP)
    assert r.status_code == 302 and r.headers["Location"] == "/dashboard"
    tenant = _tenant("kilimani-chemist")
    owner = _user("achieng@kilimani.co.ke")
    assert tenant.vertical == "pharmacy" and tenant.subscription_status == "trial"
    assert owner.tenant_id == tenant.id
    assert owner.role == "superuser" and owner.status == "approved"
    assert owner.phone_number == "254712345678"
    assert client.get("/dashboard").status_code == 200
    with client.session_transaction() as sess:
        assert sess["tenant_id"] == tenant.id


def test_signup_same_business_name_gets_next_slug(client, app):
    client.post("/auth/register", data=SIGNUP)
    client.get("/logout")
    client.post("/auth/register", data={**SIGNUP, "email": "second@kilimani.co.ke",
                                        "phone_number": "0722000111"})
    assert _tenant("kilimani-chemist-2") is not None


@pytest.mark.parametrize("field,value,message", [
    ("email", "", "All fields are required"),
    ("vertical", "supermarket", "Choose a type of business"),
    ("password", "short", "at least 8 characters"),
    ("phone_number", "+25756098389", "Kenyan mobile number"),
])
def test_signup_validation(client, field, value, message):
    r = client.post("/auth/register", data={**SIGNUP, field: value})
    assert r.status_code == 400
    assert message in r.get_data(as_text=True)
    assert _tenant("kilimani-chemist") is None


def test_signup_duplicate_email_across_businesses_refused(client, login_as, app):
    login_as("admin", email="taken@x.co")
    client.get("/logout")
    r = client.post("/auth/register", data={**SIGNUP, "email": "taken@x.co"})
    assert r.status_code == 400
    assert "already exists" in r.get_data(as_text=True)


# --- login -------------------------------------------------------------------

def _make_user(tenant_id, email, password="pass-word-1", role="cashier", **fields):
    with tenant_scope(tenant_id):
        u = User(email=email, name="U", role=role, status=fields.pop("status", "approved"),
                 **fields)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        return u.id


def test_local_login_binds_the_users_tenant(client, app, other_tenant):
    _make_user(other_tenant, "clerk@globex.co")
    r = client.post("/auth/login/local", data={"email": "clerk@globex.co",
                                               "password": "pass-word-1"})
    assert r.headers["Location"] == "/dashboard"
    with client.session_transaction() as sess:
        assert sess["tenant_id"] == other_tenant
        assert sess["role"] == "cashier"


def test_wrong_password(client, app):
    _make_user(app.acme_id, "clerk@acme.co")
    r = client.post("/auth/login/local", data={"email": "clerk@acme.co", "password": "nope"})
    assert "Invalid" in r.headers["Location"] or "Invalid" in r.get_data(as_text=True)
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_pending_user_cannot_log_in(client, app):
    _make_user(app.acme_id, "new@acme.co", status="pending")
    r = client.post("/auth/login/local", data={"email": "new@acme.co", "password": "pass-word-1"})
    assert "awaiting" in r.headers["Location"]


def test_login_message_is_shown(client):
    r = client.get("/login?message=Invalid+email+or+password.")
    assert "Invalid email or password." in r.get_data(as_text=True)


# --- every request re-checks the session ----------------------------------------

def test_user_deleted_mid_session_is_logged_out(login_as, app):
    client = login_as("admin")
    with client.session_transaction() as sess:
        user_id = sess["user_id"]
    with tenant_scope(app.acme_id):
        db.session.delete(db.session.get(User, user_id))
        db.session.commit()
    r = client.get("/customers/view_customers")
    assert r.status_code == 401
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_session_pointing_at_another_tenant_is_rejected(login_as, app, other_tenant):
    """A tampered or stale session claiming a tenant the user is not in."""
    client = login_as("admin")
    with client.session_transaction() as sess:
        sess["tenant_id"] = other_tenant
    assert client.get("/customers/view_customers").status_code == 401


def test_rejected_user_mid_session_is_logged_out(login_as, app):
    client = login_as("admin")
    with client.session_transaction() as sess:
        user_id = sess["user_id"]
    with tenant_scope(app.acme_id):
        db.session.get(User, user_id).status = "rejected"
        db.session.commit()
    assert client.get("/dashboard").status_code == 302


# --- subscription -------------------------------------------------------------

def _expire(tenant_id):
    with unscoped():
        t = db.session.get(Tenant, tenant_id)
        t.trial_ends_at = datetime.utcnow() - timedelta(days=1)
        db.session.commit()


def test_expired_trial_blocks_ui_and_api(login_as, app):
    client = login_as("admin")
    _expire(app.acme_id)
    ui = client.get("/dashboard")
    assert ui.status_code == 402 and "trial has ended" in ui.get_data(as_text=True)
    api = client.get("/customers/view_customers")
    assert api.status_code == 402 and api.get_json() == {"error": "Subscription inactive"}
    assert client.get("/logout").status_code == 302


# --- Google -------------------------------------------------------------------

def _fake_google(app, email):
    google = app.extensions["authlib.integrations.flask_client"].google
    google.authorize_access_token = MagicMock(return_value={"access_token": "t"})
    google.get = MagicMock(return_value=MagicMock(json=lambda: {"email": email, "name": "G"}))


def test_google_login_for_unknown_email_creates_nothing(client, app):
    _fake_google(app, "stranger@gmail.com")
    r = client.get("/auth/authorize")
    assert r.status_code == 302 and r.headers["Location"].startswith("/register")
    assert _user("stranger@gmail.com") is None


def test_google_login_for_known_email(client, app):
    _make_user(app.acme_id, "known@gmail.com")
    _fake_google(app, "known@gmail.com")
    r = client.get("/auth/authorize")
    assert r.headers["Location"] == "/dashboard"
    with client.session_transaction() as sess:
        assert sess["tenant_id"] == app.acme_id


# --- platform admins -----------------------------------------------------------

def test_platform_admin_flag_comes_from_config(client, app, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "PLATFORM_ADMIN_EMAILS", "ops@platform.co, other@x.co")
    _make_user(app.acme_id, "ops@platform.co", role="superuser")
    client.post("/auth/login/local", data={"email": "ops@platform.co", "password": "pass-word-1"})
    assert _user("ops@platform.co").is_platform_admin
    monkeypatch.setattr(Config, "PLATFORM_ADMIN_EMAILS", "")
    client.get("/logout")
    client.post("/auth/login/local", data={"email": "ops@platform.co", "password": "pass-word-1"})
    assert not _user("ops@platform.co").is_platform_admin


def test_create_platform_admin_command(app):
    from config import Config
    runner = app.test_cli_runner()
    result = runner.invoke(args=["create-platform-admin", "ops@platform.co",
                                 "--password", "pass-word-1"])
    assert result.exit_code == 0, result.output
    ops = _user("ops@platform.co")
    assert ops.is_platform_admin and ops.tenant_id is None and ops.status == "approved"
    assert "PLATFORM_ADMIN_EMAILS" in result.output
    again = runner.invoke(args=["create-platform-admin", "ops@platform.co",
                                "--password", "pass-word-1"])
    assert again.exit_code != 0
    assert Config.PLATFORM_ADMIN_EMAILS == ""


def test_platform_admin_without_tenant_picks_one(client, app, other_tenant):
    with unscoped(), tenant_scope(None):
        ops = User(email="ops@platform.co", role="superuser", status="approved",
                   is_platform_admin=True)
        ops.set_password("pass-word-1")
        db.session.add(ops)
        db.session.commit()
    from config import Config
    Config.PLATFORM_ADMIN_EMAILS, saved = "ops@platform.co", Config.PLATFORM_ADMIN_EMAILS
    try:
        client.post("/auth/login/local", data={"email": "ops@platform.co",
                                               "password": "pass-word-1"})
    finally:
        Config.PLATFORM_ADMIN_EMAILS = saved
    assert client.get("/dashboard").headers["Location"].endswith("/platform/tenants")
    page = client.get("/platform/tenants").get_data(as_text=True)
    assert "acme" in page and "globex" in page
    r = client.post("/platform/tenants/globex/act")
    assert r.headers["Location"] == "/dashboard"
    with client.session_transaction() as sess:
        assert sess["tenant_id"] == other_tenant
    assert client.get("/dashboard").status_code == 200


def test_tenant_owner_cannot_use_platform_pages(login_as):
    client = login_as("superuser")
    assert client.get("/platform/tenants").status_code == 403
    assert client.post("/platform/tenants/acme/act").status_code == 403


# --- staff management stays inside the tenant -----------------------------------

def test_owner_adds_staff_into_own_tenant(login_as, app):
    client = login_as("superuser")
    r = client.post("/users/", json={"email": "cashier@acme.co", "role": "cashier",
                                      "phone_number": "0711222333", "password": "pass-word-1"})
    assert r.status_code == 201
    assert _user("cashier@acme.co").tenant_id == app.acme_id


def test_staff_email_used_by_another_business_is_refused(login_as, app, other_tenant):
    _make_user(other_tenant, "shared@x.co")
    r = login_as("superuser").post("/users/", json={"email": "shared@x.co"})
    assert r.status_code == 400


def test_owner_cannot_change_own_account(login_as):
    client = login_as("superuser")
    with client.session_transaction() as sess:
        me = sess["user_id"]
    assert client.put(f"/users/{me}", json={"role": "user"}).status_code == 403
    assert client.delete(f"/users/{me}").status_code == 403


def test_invalid_phone_is_400_not_500(login_as):
    client = login_as("admin")
    r = client.post("/customers/register", json={"name": "X", "phone_number": "+25756098389",
                                                 "code": "X1"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "Invalid Kenyan phone number"}


def test_owner_settings_page_updates_tenant(login_as, app):
    client = login_as("superuser")
    r = client.post("/onboarding", data={"business_type": "institution",
                                         "business_name": "Acme Academy"})
    assert r.status_code == 302
    t = _tenant("acme")
    assert (t.name, t.vertical) == ("Acme Academy", "institution")


def test_non_owner_cannot_open_settings(login_as):
    assert login_as("admin").get("/onboarding").status_code == 302
