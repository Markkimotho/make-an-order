"""Alembic baseline: round trip, parity with models, named constraints."""
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


@pytest.fixture
def mapp(database_url):
    """App on an empty database (no migrations run)."""
    flask_app = make_app(database_url)
    yield flask_app
    with flask_app.app_context():
        db.engine.dispose()


def _diff(app):
    with app.app_context(), db.engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={
            "render_as_batch": True,
            "compare_type": True,
            "compare_server_default": compare_server_default,
        })
        return compare_metadata(ctx, db.metadata)


def _tables():
    return set(db.inspect(db.engine).get_table_names())


def test_round_trip(mapp):
    app = mapp
    with app.app_context():
        upgrade()
        assert _tables() == BASELINE_TABLES | {"alembic_version"}
        downgrade(revision="base")
        assert _tables() == {"alembic_version"}
        upgrade()
        assert _tables() == BASELINE_TABLES | {"alembic_version"}


def test_baseline_matches_models(mapp):
    app = mapp
    with app.app_context():
        upgrade()
    assert _diff(app) == []


def test_every_constraint_named(mapp):
    app = mapp
    with app.app_context():
        upgrade()
        insp = db.inspect(db.engine)
        for table in BASELINE_TABLES:
            for fk in insp.get_foreign_keys(table):
                assert fk["name"], f"unnamed FK on {table}: {fk}"
            for uq in insp.get_unique_constraints(table):
                assert uq["name"], f"unnamed unique on {table}: {uq}"
            # MySQL ignores primary key names (always PRIMARY), so only
            # SQLite can show whether the PK name was emitted.
            if not on_mysql():
                assert insp.get_pk_constraint(table)["name"], f"unnamed PK on {table}"


def test_legacy_database_stamps_with_only_unique_name_drift(mapp):
    """A database built by the pre-Alembic create_all() (unnamed constraints).

    Stamping must work and leave no table, column or type drift. The one
    known difference is the nine single-column unique constraints, unnamed
    there and convention-named in the models; 0003 normalises them.
    """
    from tests.legacy_schema import LEGACY_UNIQUE_COLUMNS, create_legacy_schema

    app = mapp
    with app.app_context():
        create_legacy_schema(db.engine)
        stamp(revision="0001_baseline")
        upgrade()  # already at head: must be a no-op
        assert _tables() == BASELINE_TABLES | {"alembic_version"}

    # SQLite reflects the legacy uniques unnamed, so they show as "add".
    # MySQL names them after the column, so they show as remove + add.
    added, removed = set(), set()
    for op in _diff(app):
        assert isinstance(op, tuple), f"unexpected drift: {op}"
        kind, obj = op[0], op[1]
        cols = [c.name for c in (obj.columns if hasattr(obj, "columns") else obj.expressions)]
        assert len(cols) == 1, f"unexpected drift: {op}"
        key = (obj.table.name, cols[0])
        if kind == "add_constraint":
            added.add(key)
        elif kind in ("remove_constraint", "remove_index"):
            removed.add(key)
        else:
            raise AssertionError(f"unexpected drift: {op}")
    assert added == LEGACY_UNIQUE_COLUMNS
    assert removed <= LEGACY_UNIQUE_COLUMNS
    if on_mysql():
        assert removed == LEGACY_UNIQUE_COLUMNS


def test_fresh_database_has_no_drift_after_stamp(mapp):
    """Stamping a database already built by the baseline is a no-op."""
    app = mapp
    with app.app_context():
        upgrade()
        stamp(revision="0001_baseline")
        upgrade()
    assert _diff(app) == []


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
    ("'0'", "now()", None),                           # a real change: let Alembic decide
    (None, "now()", None),
])
def test_compare_server_default(inspected, rendered, expected):
    assert compare_server_default(None, None, None, inspected, None, rendered) is expected
