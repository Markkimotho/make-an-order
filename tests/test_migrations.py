"""Alembic baseline: round trip, parity with models, named constraints."""
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from flask_migrate import downgrade, stamp, upgrade
from sqlalchemy import create_engine

import app as app_module
from models import db

BASELINE_TABLES = {
    "users", "categories", "suppliers", "inventory_items", "customers", "orders",
    "stock_movements", "order_items", "shifts", "cash_movements", "transactions",
    "transaction_items", "purchase_orders", "purchase_order_items", "prescriptions",
    "prescription_items", "loyalty_transactions", "audit_logs", "system_config",
}


def _app(tmp_path):
    return app_module.create_app({
        "TESTING": True,
        "SECRET_KEY": "t",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'm.db'}",
    })


def _diff(app):
    with app.app_context(), db.engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"render_as_batch": True,
                                                     "compare_server_default": True})
        return compare_metadata(ctx, db.metadata)


def _tables():
    return set(db.inspect(db.engine).get_table_names())


def test_round_trip(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        upgrade()
        assert _tables() == BASELINE_TABLES | {"alembic_version"}
        downgrade(revision="base")
        assert _tables() == {"alembic_version"}
        upgrade()
        assert _tables() == BASELINE_TABLES | {"alembic_version"}


def test_baseline_matches_models(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        upgrade()
    assert _diff(app) == []


def test_every_constraint_named(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        upgrade()
        insp = db.inspect(db.engine)
        for table in BASELINE_TABLES:
            for fk in insp.get_foreign_keys(table):
                assert fk["name"], f"unnamed FK on {table}: {fk}"
            for uq in insp.get_unique_constraints(table):
                assert uq["name"], f"unnamed unique on {table}: {uq}"
            assert insp.get_pk_constraint(table)["name"], f"unnamed PK on {table}"


def test_legacy_database_stamps_with_only_unique_name_drift(tmp_path):
    """A database built by the pre-Alembic create_all() (unnamed constraints).

    Stamping must work and leave no table, column or type drift. The one
    known difference is the nine single-column unique constraints, unnamed
    there and convention-named in the models; 0003 normalises them.
    """
    from tests.legacy_schema import LEGACY_UNIQUE_COLUMNS, create_legacy_schema

    app = _app(tmp_path)
    with app.app_context():
        create_legacy_schema(db.engine)
        stamp(revision="0001_baseline")
        upgrade()  # already at head: must be a no-op
        assert _tables() == BASELINE_TABLES | {"alembic_version"}

    diff = _diff(app)
    unique_drift = set()
    for op in diff:
        assert isinstance(op, tuple) and op[0] == "add_constraint", f"unexpected drift: {op}"
        constraint = op[1]
        assert len(constraint.columns) == 1, f"unexpected drift: {op}"
        column = next(iter(constraint.columns))
        unique_drift.add((constraint.table.name, column.name))
    assert unique_drift == LEGACY_UNIQUE_COLUMNS


def test_fresh_database_has_no_drift_after_stamp(tmp_path):
    """Stamping a database already built by the baseline is a no-op."""
    app = _app(tmp_path)
    with app.app_context():
        upgrade()
        stamp(revision="0001_baseline")
        upgrade()
    assert _diff(app) == []


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
