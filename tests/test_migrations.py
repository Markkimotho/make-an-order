"""Migrations: round trip, parity with the models, named constraints,
and upgrading a database built by the pre-Alembic app."""
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from flask_migrate import downgrade, stamp, upgrade
from sqlalchemy import create_engine

from models import db
from migrations.env_helpers import compare_server_default
from tests.conftest import make_app, on_mysql

BASELINE_TABLES = {
    "users", "categories", "suppliers", "inventory_items", "customers", "orders",
    "stock_movements", "order_items", "shifts", "cash_movements", "transactions",
    "transaction_items", "purchase_orders", "purchase_order_items", "prescriptions",
    "prescription_items", "loyalty_transactions", "audit_logs", "system_config",
}
BILLING_TABLES = {"invoices", "payments", "payment_allocations", "payment_intents",
                  "invoice_adjustments"}
STOCK_TABLES = {"batches", "item_variants", "line_batches"}
SMS_TABLES = {"sms_messages"}
PHARMACY_TABLES = {"controlled_drug_ledger"}
DISTRIBUTOR_TABLES = {"price_tiers", "tier_prices", "customer_phones", "container_movements"}
INSTITUTION_TABLES = {"members", "campaigns", "campaign_items"}
HEAD_TABLES = ((BASELINE_TABLES - {"system_config"}) | {"tenants"} | BILLING_TABLES | STOCK_TABLES | SMS_TABLES
               | PHARMACY_TABLES | DISTRIBUTOR_TABLES | INSTITUTION_TABLES)


@pytest.fixture
def mapp(database_url):
    """App on an empty database (no migrations run)."""
    flask_app = make_app(database_url)
    yield flask_app
    with flask_app.app_context():
        db.engine.dispose()


def diff_against_models(app):
    with app.app_context(), db.engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={
            "render_as_batch": True,
            "compare_type": True,
            "compare_server_default": compare_server_default,
        })
        return compare_metadata(ctx, db.metadata)


def table_names():
    return set(db.inspect(db.engine).get_table_names())


def test_round_trip(mapp):
    with mapp.app_context():
        upgrade()
        assert table_names() == HEAD_TABLES | {"alembic_version"}
        downgrade(revision="0001_baseline")
        assert table_names() == BASELINE_TABLES | {"alembic_version"}
        downgrade(revision="base")
        assert table_names() == {"alembic_version"}
        upgrade()
        assert table_names() == HEAD_TABLES | {"alembic_version"}


def test_head_matches_models(mapp):
    with mapp.app_context():
        upgrade()
    assert diff_against_models(mapp) == []


def test_every_constraint_named(mapp):
    with mapp.app_context():
        upgrade()
        insp = db.inspect(db.engine)
        for table in HEAD_TABLES:
            for fk in insp.get_foreign_keys(table):
                assert fk["name"], f"unnamed FK on {table}: {fk}"
            for uq in insp.get_unique_constraints(table):
                assert uq["name"], f"unnamed unique on {table}: {uq}"
            # MySQL ignores primary key names (always PRIMARY), so only
            # SQLite can show whether the PK name was emitted.
            if not on_mysql():
                assert insp.get_pk_constraint(table)["name"], f"unnamed PK on {table}"


def test_legacy_database_upgrades_to_models_with_no_drift(mapp):
    """A database built by the pre-Alembic create_all(), with unnamed
    constraints (SQLite) or MySQL's column-named ones. Stamped at the
    baseline and upgraded, it must end exactly like the models: 0003 finds
    the legacy uniques by column and replaces them."""
    from tests.legacy_schema import create_legacy_schema

    with mapp.app_context():
        create_legacy_schema(db.engine)
        stamp(revision="0001_baseline")
        upgrade()
        assert table_names() == HEAD_TABLES | {"alembic_version"}
    assert diff_against_models(mapp) == []


def test_upgrade_is_idempotent_at_head(mapp):
    with mapp.app_context():
        upgrade()
        upgrade()
        stamp(revision="head")
    assert diff_against_models(mapp) == []


@pytest.mark.skipif(on_mysql(), reason="SQLite PRAGMA only")
def test_disable_sqlite_foreign_keys(tmp_path):
    from migrations.env_helpers import disable_sqlite_foreign_keys
    engine = create_engine(f"sqlite:///{tmp_path / 'fk.db'}")
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        disable_sqlite_foreign_keys(conn)
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0


def test_disable_foreign_keys_ignores_other_dialects():
    from migrations.env_helpers import disable_sqlite_foreign_keys

    class FakeConn:
        class dialect:
            name = "mysql"

        def exec_driver_sql(self, sql):
            raise AssertionError("must not run PRAGMA on MySQL")

    disable_sqlite_foreign_keys(FakeConn())


@pytest.mark.parametrize("inspected,rendered,expected", [
    ("CURRENT_TIMESTAMP", "now()", False),            # MySQL reflection vs model
    ("(CURRENT_TIMESTAMP)", "(CURRENT_TIMESTAMP)", False),  # SQLite
    ("current_timestamp()", "now()", False),
    ("'0'", "false", False),                          # MySQL boolean default
    ("0", "0", False),                                # SQLite boolean default
    ("'1'", "false", None),                           # a real change
    ("'0.000'", "'0'", False),                        # MySQL DECIMAL default
    ("'30'", "'14'", None),                           # a real change
    ("'0'", "now()", None),                           # a real change: let Alembic decide
    (None, "now()", None),
])
def test_compare_server_default(inspected, rendered, expected):
    assert compare_server_default(None, None, None, inspected, None, rendered) is expected


def test_names_fit_mysql_identifier_limit():
    """MySQL refuses names over 64 characters; SQLite does not, so only the
    MySQL lane would notice. Check models and migration files here."""
    import re
    from pathlib import Path
    limit = 64
    too_long = []
    for table in db.metadata.tables.values():
        for thing in [*table.constraints, *table.indexes]:
            name = str(thing.name) if thing.name else ""
            if len(name) > limit:
                too_long.append(f"model {table.name}: {name}")
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    for path in sorted(versions.glob("*.py")):
        for name in re.findall(r'"((?:fk|uq|ix|pk|ck)_[A-Za-z0-9_]+)"', path.read_text()):
            if len(name) > limit:
                too_long.append(f"{path.name}: {name}")
    assert too_long == []
