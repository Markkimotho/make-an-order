"""Owner settings: M-Pesa credentials (write-only), compliance, token rotation."""
import pytest
from cryptography.fernet import Fernet

from auth.tenancy import unscoped
from models import Tenant, db
from tests import daraja_fakes

FORM = {"daraja_shortcode_type": "paybill", "daraja_shortcode": "600123",
        "daraja_env": "sandbox", "daraja_consumer_key": "CK-SECRET-1",
        "daraja_consumer_secret": "CS-SECRET-2", "daraja_passkey": "PK-SECRET-3",
        "at_sender_id": "ACMECHEM", "kra_pin": "a123456789b",
        "ppb_premises_license": "PPB/R/2026/123", "ppb_premises_license_expiry": "2027-06-30",
        "superintendent_reg_no": "PH-4411", "manual_mpesa_enabled": "on"}
SECRETS = ("CK-SECRET-1", "CS-SECRET-2", "PK-SECRET-3")


@pytest.fixture
def owner(app, login_as, monkeypatch):
    monkeypatch.setitem(app.config, "TENANT_SECRETS_KEY", Fernet.generate_key().decode())
    return login_as("superuser")


def tenant(app):
    with unscoped():
        t = db.session.get(Tenant, app.acme_id)
        return {k: getattr(t, k) for k in (
            "daraja_shortcode", "daraja_shortcode_type", "daraja_consumer_key",
            "daraja_consumer_secret", "daraja_passkey", "kra_pin", "at_sender_id",
            "manual_mpesa_enabled", "callback_token", "ppb_premises_license_expiry")}


def test_save_and_secrets_never_shown(app, owner):
    r = owner.post("/settings/", data=FORM)
    assert r.status_code == 302
    t = tenant(app)
    assert (t["daraja_consumer_key"], t["daraja_passkey"], t["kra_pin"]) == (
        "CK-SECRET-1", "PK-SECRET-3", "A123456789B")
    page = owner.get("/settings/").get_data(as_text=True)
    for secret in SECRETS:
        assert secret not in page
    assert page.count(">set<") >= 3
    assert t["callback_token"] in page          # the URLs to give Safaricom are shown
    with unscoped():
        raw = db.session.execute(db.text(
            "SELECT daraja_passkey_encrypted FROM tenants WHERE id = :i"), {"i": app.acme_id}).scalar()
    assert raw and "PK-SECRET-3" not in raw    # encrypted at rest


def test_blank_secret_keeps_stored_value(app, owner):
    owner.post("/settings/", data=FORM)
    owner.post("/settings/", data={**FORM, "daraja_consumer_key": "", "daraja_passkey": ""})
    t = tenant(app)
    assert (t["daraja_consumer_key"], t["daraja_passkey"]) == ("CK-SECRET-1", "PK-SECRET-3")


@pytest.mark.parametrize("field,value,message", [
    ("daraja_shortcode", "60O123", "digits only"),
    ("kra_pin", "12345", "KRA PIN"),
    ("daraja_shortcode_type", "till", "till number"),
    ("ppb_premises_license_expiry", "next year", "date"),
])
def test_validation(app, owner, field, value, message):
    r = owner.post("/settings/", data={**FORM, field: value})
    assert r.status_code == 400 and message in r.get_data(as_text=True)
    assert tenant(app)["daraja_shortcode"] is None


def test_missing_secrets_key_is_explained(app, login_as, monkeypatch):
    monkeypatch.delenv("TENANT_SECRETS_KEY", raising=False)
    monkeypatch.setitem(app.config, "TENANT_SECRETS_KEY", None)
    r = login_as("superuser").post("/settings/", data=FORM)
    assert r.status_code == 500 and "TENANT_SECRETS_KEY" in r.get_data(as_text=True)
    assert tenant(app)["daraja_consumer_key"] is None


def test_rotate_token_reregisters_and_keeps_old_url_a_day(app, owner, client, monkeypatch):
    from datetime import datetime, timedelta
    fake = daraja_fakes.install(monkeypatch)
    fake.default("/registerurl", 200, {"ResponseCode": "0"})
    owner.post("/settings/", data=FORM)
    old = tenant(app)["callback_token"]
    assert owner.post("/settings/rotate-token").status_code == 302
    new = tenant(app)["callback_token"]
    assert new != old
    assert fake.posts("/registerurl")[-1]["ConfirmationURL"].endswith(f"/acme/{new}")
    body = daraja_fakes.c2b("SJK0000001", 100, "X")
    assert client.post(f"/api/payments/daraja/c2b/acme/{new}", json=body).status_code == 200
    # Results for pushes already in flight (old URL) still land for 24 hours...
    assert client.post(f"/api/payments/daraja/c2b/acme/{old}",
                       json=daraja_fakes.c2b("SJK0000002", 100, "X")).status_code == 200
    with unscoped():
        db.session.get(Tenant, app.acme_id).callback_token_rotated_at = (
            datetime.utcnow() - timedelta(hours=25))
        db.session.commit()
    # ...and not after.
    assert client.post(f"/api/payments/daraja/c2b/acme/{old}",
                       json=daraja_fakes.c2b("SJK0000003", 100, "X")).status_code == 404


def test_rotation_refused_by_safaricom_changes_nothing(app, owner, monkeypatch):
    """Review finding: the new token was committed before Safaricom accepted
    the new URLs, so Paybill confirmations would hit a dead URL."""
    fake = daraja_fakes.install(monkeypatch)
    owner.post("/settings/", data=FORM)
    old = tenant(app)["callback_token"]
    fake.answer("/registerurl", 400, {"errorMessage": "Invalid ValidationURL"})
    r = owner.post("/settings/rotate-token", follow_redirects=True)
    assert "not changed" in r.get_data(as_text=True)
    assert tenant(app)["callback_token"] == old


def test_register_c2b_button(app, owner, monkeypatch):
    fake = daraja_fakes.install(monkeypatch)
    fake.answer("/registerurl", 200, {"ResponseCode": "0"})
    owner.post("/settings/", data=FORM)
    r = owner.post("/settings/register-c2b", follow_redirects=True)
    assert "registered" in r.get_data(as_text=True)
    assert fake.posts("/registerurl")[0]["ResponseType"] == "Completed"


def test_only_the_owner(login_as):
    for role in ("admin", "cashier"):
        c = login_as(role)
        assert c.get("/settings/").status_code == 302
        assert c.post("/settings/", data=FORM).status_code == 302


def test_cli_import_env_and_register(app, monkeypatch):
    from config import Config
    monkeypatch.setitem(app.config, "TENANT_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TENANT_SECRETS_KEY", app.config["TENANT_SECRETS_KEY"])
    for name, value in (("MPESA_BUSINESS_SHORT_CODE", "174379"), ("MPESA_CONSUMER_KEY", "k"),
                        ("MPESA_CONSUMER_SECRET", "s"), ("MPESA_PASSKEY", "p"),
                        ("MPESA_ENV", "sandbox")):
        monkeypatch.setattr(Config, name, value)
    runner = app.test_cli_runner()
    result = runner.invoke(args=["tenants", "import-env-credentials", "acme"])
    assert result.exit_code == 0, result.output
    assert tenant(app)["daraja_shortcode"] == "174379"
    fake = daraja_fakes.install(monkeypatch)
    fake.answer("/registerurl", 200, {"ResponseCode": "0"})
    result = runner.invoke(args=["tenants", "register-c2b", "acme"])
    assert result.exit_code == 0, result.output
    assert tenant(app)["callback_token"] not in result.output     # token not echoed
    assert runner.invoke(args=["tenants", "register-c2b", "nope"]).exit_code != 0
