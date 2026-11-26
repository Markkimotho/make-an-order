"""Tenant model, TenantMixin coverage and phone validation on models."""
from datetime import datetime, timedelta

import pytest

from auth.tenancy import unscoped
from models import Customer, Tenant, TenantMixin, User, db
from services.phone import InvalidPhone


def test_every_model_but_tenant_is_tenant_owned():
    """Adding a model without TenantMixin must be a deliberate choice."""
    allowlist = {Tenant}
    for mapper in db.Model.registry.mappers:
        cls = mapper.class_
        if cls in allowlist:
            continue
        assert issubclass(cls, TenantMixin), f"{cls.__name__} has no tenant_id"


def test_tenant_defaults(app):
    with unscoped():
        t = Tenant(name="Kilimani Chemist", slug="kilimani", vertical="pharmacy")
        db.session.add(t)
        db.session.commit()
        assert t.subscription_status == "trial"
        assert t.daraja_env == "sandbox"
        assert len(t.callback_token) >= 24
        # MySQL DATETIME rounds to the nearest second, hence the 1s slack.
        assert (timedelta(days=13) < t.trial_ends_at - datetime.utcnow()
                <= timedelta(days=14, seconds=1))
        assert t.is_active_subscription()


def test_slug_is_unique(app):
    with unscoped():
        db.session.add(Tenant(name="Dup", slug="acme", vertical="pharmacy"))
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()


def test_vertical_is_validated(app):
    with pytest.raises(ValueError):
        Tenant(name="X", slug="x", vertical="spaceship")


@pytest.mark.parametrize("status,ends,expected", [
    ("trial", timedelta(days=3), True),
    ("trial", timedelta(days=-1), False),
    ("active", timedelta(days=-100), True),
    ("past_due", timedelta(days=10), False),
    ("cancelled", timedelta(days=10), False),
])
def test_is_active_subscription(status, ends, expected):
    now = datetime(2026, 11, 24, 12, 0)
    t = Tenant(name="X", slug="x", vertical="pharmacy", subscription_status=status,
               trial_ends_at=now + ends)
    assert t.is_active_subscription(now=now) is expected


def test_to_dict_never_contains_secrets(app, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setitem(app.config, "TENANT_SECRETS_KEY", Fernet.generate_key().decode())
    with unscoped():
        t = Tenant(name="S", slug="s", vertical="distributor", daraja_passkey="pk-123",
                   daraja_consumer_key="ck-456", daraja_consumer_secret="cs-789",
                   etims_token="tok-000")
        db.session.add(t)
        db.session.commit()
        text = repr(t.to_dict())
    for secret in ("pk-123", "ck-456", "cs-789", "tok-000", "callback_token"):
        assert secret not in text


def test_customer_phone_is_normalised(app):
    c = Customer(name="Wanjiru", phone_number="0712 345 678", code="W1")
    assert c.phone_number == "254712345678"


def test_customer_bad_phone_raises(app):
    with pytest.raises(InvalidPhone):
        Customer(name="X", phone_number="+25756098389", code="X1")


def test_user_phone_optional_but_normalised(app):
    assert User(email="a@x.co", phone_number="").phone_number is None
    assert User(email="b@x.co", phone_number="+254 110 123 456").phone_number == "254110123456"


def test_same_phone_and_code_allowed_in_two_tenants(app, other_tenant):
    from auth.tenancy import tenant_scope
    db.session.add(Customer(name="A", phone_number="0712345678", code="C1"))
    db.session.commit()
    with tenant_scope(other_tenant):
        db.session.add(Customer(name="B", phone_number="0712345678", code="C1"))
        db.session.commit()


def test_same_phone_twice_in_one_tenant_refused(app):
    db.session.add(Customer(name="A", phone_number="0712345678", code="C1"))
    db.session.commit()
    db.session.add(Customer(name="B", phone_number="+254712345678", code="C2"))
    with pytest.raises(Exception):
        db.session.commit()
    db.session.rollback()
