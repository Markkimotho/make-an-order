"""Migrations 0002/0003 on a legacy database with real-looking data.

Each test builds the pre-Alembic schema (tests/legacy_schema.py), inserts
rows the way the old app stored them, stamps 0001_baseline and upgrades.
"""
import pytest
import sqlalchemy as sa
from alembic import command
from flask import current_app
from flask_migrate import downgrade, stamp, upgrade

from models import db
from tests.conftest import make_app
from tests.legacy_schema import create_legacy_schema, legacy_metadata
from tests.test_migrations import diff_against_models, table_names


@pytest.fixture
def legacy(database_url):
    """App on a legacy-shaped empty database stamped at 0001_baseline."""
    app = make_app(database_url)
    with app.app_context():
        create_legacy_schema(db.engine)
        stamp(revision="0001_baseline")
        yield app
        db.session.remove()
        db.engine.dispose()


def insert(table, *rows):
    t = legacy_metadata().tables[table]
    with db.engine.begin() as conn:
        conn.execute(t.insert(), list(rows))


def fetch(sql):
    with db.engine.connect() as conn:
        return conn.exec_driver_sql(sql).all()


def alembic(action, revision):
    """Run Alembic directly so a failing revision raises its own exception
    (Flask-Migrate's wrappers log it and exit instead)."""
    config = current_app.extensions["migrate"].migrate.get_config()
    getattr(command, action)(config, revision)


def version():
    return fetch("SELECT version_num FROM alembic_version")[0][0]


def seed_shop(config=None, phones=("0712345678",)):
    if config:
        insert("system_config", *({"key": k, "value": v} for k, v in config.items()))
    insert("users", {"email": "owner@shop.co", "role": "superuser", "status": "approved",
                     "phone_number": "0700000001"})
    insert("customers", *({"name": f"C{i}", "phone_number": p, "code": f"C{i}",
                           "loyalty_points": 0, "credit_limit": 0, "credit_balance": 0,
                           "customer_type": "retail"} for i, p in enumerate(phones)))
    insert("inventory_items", {"name": "Panadol", "price": 50, "stock": 10, "sku": "PAN",
                               "reorder_level": 5, "is_drug": True,
                               "requires_prescription": False})


def test_empty_database_gets_no_tenant(legacy):
    upgrade()
    assert fetch("SELECT COUNT(*) FROM tenants")[0][0] == 0


def test_existing_data_moves_into_default_tenant(legacy):
    seed_shop({"business_type": "pharmacy", "business_name": "Kilimani Chemist"})
    upgrade()
    tenants = fetch("SELECT id, name, slug, vertical, subscription_status FROM tenants")
    assert len(tenants) == 1
    tid, name, slug, vertical, status = tenants[0]
    assert (name, slug, vertical, status) == ("Kilimani Chemist", "kilimani-chemist",
                                              "pharmacy", "active")
    for table in ("users", "customers", "inventory_items"):
        assert fetch(f"SELECT DISTINCT tenant_id FROM {table}") == [(tid,)]
    assert "system_config" not in table_names()


def test_no_system_config_falls_back(legacy):
    seed_shop()
    upgrade()
    assert fetch("SELECT name, slug, vertical FROM tenants") == [
        ("Default Business", "default-business", "distributor")]


def test_non_pharmacy_business_type_becomes_distributor(legacy):
    seed_shop({"business_type": "hardware", "business_name": "Mjengo Hardware"})
    upgrade()
    assert fetch("SELECT vertical FROM tenants") == [("distributor",)]


def test_phones_are_normalised(legacy):
    seed_shop(phones=("0712345678", "+254 722 000111", "0110123456"))  # columns are VARCHAR(15)
    upgrade()
    assert sorted(r[0] for r in fetch("SELECT phone_number FROM customers")) == [
        "254110123456", "254712345678", "254722000111"]
    assert fetch("SELECT phone_number FROM users") == [("254700000001",)]


def test_bad_and_colliding_phones_stop_the_upgrade_with_every_problem(legacy):
    seed_shop(phones=("+25756098389", "0712345678", "254712345678", "0722000111"))
    with pytest.raises(Exception) as excinfo:
        alembic("upgrade", "head")
    message = str(excinfo.value)
    assert "+25756098389" in message
    assert "'0712345678'" in message and "'254712345678'" in message
    assert "nothing was changed" in message
    # 0003 changed nothing: still at 0002, phones untouched, tenant_id nullable.
    assert version() == "0002_tenants_expand"
    assert sorted(r[0] for r in fetch("SELECT phone_number FROM customers")) == [
        "+25756098389", "0712345678", "0722000111", "254712345678"]


def test_upgrade_after_fixing_phones_succeeds(legacy):
    seed_shop(phones=("+25756098389",))
    with pytest.raises(Exception):
        alembic("upgrade", "head")
    with db.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE customers SET phone_number = '0712345678'")
    upgrade()
    assert version() == "0003_tenants_contract"
    assert diff_against_models(legacy) == []


def test_upgraded_legacy_database_matches_models(legacy):
    seed_shop({"business_type": "pharmacy", "business_name": "Kilimani Chemist"},
              phones=("0712345678", "0722000111"))
    upgrade()
    assert diff_against_models(legacy) == []


def test_uniques_are_per_tenant_after_upgrade(legacy):
    seed_shop()
    upgrade()
    tid = fetch("SELECT id FROM tenants")[0][0]
    with db.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO tenants (name, slug, vertical, daraja_env, callback_token, "
            "subscription_status, created_at) VALUES "
            "('Other', 'other', 'distributor', 'sandbox', 'tok', 'trial', CURRENT_TIMESTAMP)")
    other = fetch("SELECT id FROM tenants WHERE slug = 'other'")[0][0]
    customers = sa.table("customers", *(sa.column(c) for c in (
        "tenant_id", "name", "phone_number", "code", "loyalty_points", "credit_limit",
        "credit_balance", "customer_type")))
    row = {"name": "X", "phone_number": "254712345678", "code": "C0", "loyalty_points": 0,
           "credit_limit": 0, "credit_balance": 0, "customer_type": "retail"}
    with db.engine.begin() as conn:
        conn.execute(customers.insert(), {**row, "tenant_id": other})   # same phone, other tenant
    with pytest.raises(sa.exc.IntegrityError):
        with db.engine.begin() as conn:
            conn.execute(customers.insert(), {**row, "tenant_id": tid, "code": "NEW"})


def test_downgrade_with_one_tenant_restores_legacy_shape(legacy):
    seed_shop({"business_type": "pharmacy", "business_name": "Kilimani Chemist"})
    upgrade()
    downgrade(revision="0001_baseline")
    assert version() == "0001_baseline"
    assert "tenants" not in table_names()
    config = dict(fetch("SELECT `key`, value FROM system_config")
                  if db.engine.dialect.name == "mysql"
                  else fetch('SELECT "key", value FROM system_config'))
    assert config == {"business_name": "Kilimani Chemist", "business_type": "pharmacy"}
    columns = {c["name"] for c in db.inspect(db.engine).get_columns("customers")}
    assert "tenant_id" not in columns


def test_downgrade_with_two_tenants_refuses(legacy):
    seed_shop()
    upgrade()
    with db.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO tenants (name, slug, vertical, daraja_env, callback_token, "
            "subscription_status, created_at) VALUES "
            "('Other', 'other', 'distributor', 'sandbox', 'tok', 'trial', CURRENT_TIMESTAMP)")
    with pytest.raises(Exception) as excinfo:
        alembic("downgrade", "0002_tenants_expand")
    assert "2 tenants" in str(excinfo.value)
    assert version() == "0003_tenants_contract"
