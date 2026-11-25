"""Migration 0012 columns: bank references, tenant till/pricing rules,
expenses, recall fields."""
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from auth.tenancy import unscoped
from models import Expense, Payment, Tenant, db


def test_bank_reference_is_normalised_and_unique_per_business(app, other_tenant):
    db.session.add(Payment(method="bank", amount=Decimal("100"), bank_name="Equity",
                           bank_reference=" ft24 330 x9 ", verified=False))
    db.session.add_all([Payment(method="cash", amount=Decimal("1")) for _ in range(2)])
    db.session.commit()                         # many cash rows, no reference: no clash
    assert Payment.query.filter_by(method="bank").one().bank_reference == "FT24330X9"
    db.session.add(Payment(method="bank", amount=Decimal("100"), bank_reference="FT24330x9"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_same_bank_reference_in_two_businesses(app, other_tenant):
    from auth.tenancy import tenant_scope
    for tid in (app.acme_id, other_tenant):
        with tenant_scope(tid):
            db.session.add(Payment(method="bank", amount=Decimal("5"), bank_reference="REF1"))
            db.session.commit()


def test_tenant_defaults(app):
    with unscoped():
        t = db.session.get(Tenant, app.acme_id)
        assert (t.max_discount_pct, t.default_markup_pct, t.price_rounding,
                t.cash_variance_alert, t.session_idle_minutes) == (10, 30, 5, 500, 30)
        assert "max_discount_pct" in t.to_dict()


def test_expense_round_trip(app):
    db.session.add(Expense(category="rent", amount=Decimal("25000"), method="bank"))
    db.session.commit()
    assert Expense.query.one().to_dict()["amount"] == 25000.0
