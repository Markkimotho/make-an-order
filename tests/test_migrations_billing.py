"""Migration 0004: stored credit balances become opening-balance invoices."""
import pytest
from alembic import command
from flask import current_app
from flask_migrate import downgrade, stamp, upgrade

from models import db
from tests.conftest import make_app
from tests.legacy_schema import create_legacy_schema, legacy_metadata
from tests.test_migrations import diff_against_models


@pytest.fixture
def legacy(database_url):
    app = make_app(database_url)
    with app.app_context():
        create_legacy_schema(db.engine)
        stamp(revision="0001_baseline")
        yield app
        db.session.remove()
        db.engine.dispose()


def fetch(sql):
    with db.engine.connect() as conn:
        return conn.exec_driver_sql(sql).all()


def seed_customers(*balances):
    t = legacy_metadata().tables["customers"]
    with db.engine.begin() as conn:
        conn.execute(t.insert(), [
            {"name": f"C{i}", "phone_number": f"07220001{i:02d}", "code": f"C{i}",
             "loyalty_points": 0, "credit_limit": 50000, "credit_balance": balance,
             "customer_type": "wholesale"}
            for i, balance in enumerate(balances)])


def test_credit_balances_become_opening_invoices(legacy):
    seed_customers(2500, 0, 1200.50)
    upgrade()
    rows = fetch("SELECT invoice_number, kind, customer_id, total, amount_paid, status, "
                 "due_date IS NOT NULL FROM invoices ORDER BY invoice_number")
    assert [(r[0], r[1], r[2], float(r[3]), float(r[4]), r[5], bool(r[6])) for r in rows] == [
        ("INV000001", "opening_balance", 1, 2500.0, 0.0, "open", True),
        ("INV000002", "opening_balance", 3, 1200.5, 0.0, "open", True),
    ]
    columns = {c["name"] for c in db.inspect(db.engine).get_columns("customers")}
    assert "credit_balance" not in columns and "credit_term_days" in columns
    assert fetch("SELECT DISTINCT credit_term_days FROM customers") == [(30,)]
    assert diff_against_models(legacy) == []


def test_no_balances_no_invoices(legacy):
    seed_customers(0, 0)
    upgrade()
    assert fetch("SELECT COUNT(*) FROM invoices") == [(0,)]


def test_pay_tokens_are_distinct(legacy):
    seed_customers(10, 20, 30)
    upgrade()
    tokens = [r[0] for r in fetch("SELECT pay_token FROM invoices")]
    assert len(set(tokens)) == 3 and all(len(t) >= 20 for t in tokens)


def test_downgrade_restores_balances(legacy):
    seed_customers(2500, 0)
    upgrade()
    downgrade(revision="0003_tenants_contract")
    rows = fetch("SELECT code, credit_balance FROM customers ORDER BY code")
    assert [(c, float(b)) for c, b in rows] == [("C0", 2500.0), ("C1", 0.0)]
    assert "invoices" not in db.inspect(db.engine).get_table_names()


def test_downgrade_refuses_with_paid_sales(legacy):
    seed_customers(100)
    upgrade()
    tenant_id = fetch("SELECT id FROM tenants")[0][0]
    with db.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO invoices (tenant_id, invoice_number, kind, total, amount_paid, "
            "amount_adjusted, status, issued_at, pay_token, etims_status) VALUES "
            f"({tenant_id}, 'INV000009', 'sale', 50, 50, 0, 'paid', CURRENT_TIMESTAMP, 'tok9', "
            "'not_submitted')")
        conn.exec_driver_sql(
            "INSERT INTO payments (tenant_id, method, amount, unallocated_amount, status, "
            "match_status, verified, received_at) VALUES "
            f"({tenant_id}, 'cash', 50, 0, 'confirmed', 'matched', 1, CURRENT_TIMESTAMP)")
        conn.exec_driver_sql(
            "INSERT INTO payment_allocations (tenant_id, payment_id, invoice_id, amount, "
            f"created_at) SELECT {tenant_id}, p.id, i.id, 50, CURRENT_TIMESTAMP "
            "FROM payments p, invoices i WHERE i.invoice_number = 'INV000009'")
    config = current_app.extensions["migrate"].migrate.get_config()
    with pytest.raises(Exception) as excinfo:
        command.downgrade(config, "0003_tenants_contract")
    assert "cannot be folded back" in str(excinfo.value)


def test_downgrade_refuses_with_unapplied_credit(legacy):
    """#13: KES 150 paid on a KES 100 opening balance leaves KES 50 credit
    that credit_balance cannot hold."""
    seed_customers(100)
    upgrade()
    tenant_id = fetch("SELECT id FROM tenants")[0][0]
    with db.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO payments (tenant_id, method, amount, unallocated_amount, status, "
            "match_status, verified, received_at) VALUES "
            f"({tenant_id}, 'cash', 150, 50, 'confirmed', 'partial', 1, CURRENT_TIMESTAMP)")
    config = current_app.extensions["migrate"].migrate.get_config()
    with pytest.raises(Exception) as excinfo:
        command.downgrade(config, "0003_tenants_contract")
    assert "unapplied payment credit" in str(excinfo.value)
