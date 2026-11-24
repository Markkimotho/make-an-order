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


def test_create_all_database_stamps_clean(tmp_path):
    """An existing database built by the old startup create_all() is stamped."""
    app = _app(tmp_path)
    with app.app_context():
        db.create_all()
        stamp(revision="0001_baseline")
        upgrade()  # already at head: must be a no-op
        assert _tables() == BASELINE_TABLES | {"alembic_version"}
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
