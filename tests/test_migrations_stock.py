"""Migration 0005: item stock moves into batches; downgrade puts it back."""
from datetime import date, datetime

import pytest
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


def seed_items(*rows):
    """rows: (name, stock, batch_number, expiry_date, cost_price)."""
    t = legacy_metadata().tables["inventory_items"]
    with db.engine.begin() as conn:
        conn.execute(t.insert(), [
            {"name": name, "price": 100, "cost_price": cost, "stock": stock,
             "reorder_level": 5, "batch_number": number, "expiry_date": expiry,
             "is_drug": False, "requires_prescription": False}
            for name, stock, number, expiry, cost in rows])


def test_stocked_items_get_one_batch_each(legacy):
    seed_items(("Amoxil", 40, "AMX-7", date(2027, 3, 1), 12),
               ("Panadol", 15, None, None, None),
               ("Empty", 0, "OLD-1", date(2026, 1, 1), 5),
               ("Blank", 0, None, None, None))
    upgrade(revision="0005_batches")
    rows = fetch("SELECT i.name, b.batch_number, b.expiry_date, b.quantity_on_hand, b.unit_cost, "
                 "b.tenant_id = i.tenant_id FROM batches b "
                 "JOIN inventory_items i ON i.id = b.inventory_item_id ORDER BY i.name")
    assert [(r[0], r[1], str(r[2]) if r[2] else None, r[3],
             float(r[4]) if r[4] is not None else None, bool(r[5])) for r in rows] == [
        ("Amoxil", "AMX-7", "2027-03-01", 40, 12.0, True),
        # Empty but with batch details: kept, so returns of old sales find it.
        ("Empty", "OLD-1", "2026-01-01", 0, 5.0, True),
        ("Panadol", "LEGACY", None, 15, None, True),
    ]
    columns = {c["name"] for c in db.inspect(db.engine).get_columns("inventory_items")}
    assert {"batch_number", "expiry_date"}.isdisjoint(columns) and "has_variants" in columns
    # Stock totals are untouched: item stock equals the sum of its batches.
    assert fetch("SELECT name, stock FROM inventory_items ORDER BY name") == [
        ("Amoxil", 40), ("Blank", 0), ("Empty", 0), ("Panadol", 15)]


def test_head_has_no_drift_after_data_migration(legacy):
    seed_items(("Amoxil", 40, "AMX-7", date(2027, 3, 1), 12))
    upgrade()
    assert diff_against_models(legacy) == []


def test_downgrade_restores_soonest_stocked_batch(legacy):
    seed_items(("Amoxil", 40, "AMX-7", date(2027, 3, 1), 12),
               ("Panadol", 15, None, None, None))
    upgrade(revision="0005_batches")
    item_id, tenant_id = fetch("SELECT id, tenant_id FROM inventory_items WHERE name='Amoxil'")[0]
    t = db.Table("batches", db.MetaData(), autoload_with=db.engine)
    with db.engine.begin() as conn:
        conn.execute(t.insert(), [
            {"tenant_id": tenant_id, "inventory_item_id": item_id, "batch_number": "AMX-1",
             "expiry_date": date(2026, 12, 1), "quantity_on_hand": 5,
             "received_at": datetime(2026, 11, 1)},
            {"tenant_id": tenant_id, "inventory_item_id": item_id, "batch_number": "AMX-0",
             "expiry_date": date(2026, 6, 1), "quantity_on_hand": 0,     # empty: ignored
             "received_at": datetime(2026, 11, 1)}])
    downgrade(revision="0004_billing")
    rows = fetch("SELECT name, batch_number, expiry_date FROM inventory_items ORDER BY name")
    assert [(n, b, str(e) if e else None) for n, b, e in rows] == [
        ("Amoxil", "AMX-1", "2026-12-01"), ("Panadol", "LEGACY", None)]
    tables = set(db.inspect(db.engine).get_table_names())
    assert {"batches", "item_variants", "line_batches"}.isdisjoint(tables)


def test_0007_keeps_quarantine_flags_from_an_earlier_0005(legacy):
    """Databases built while 0005 still created batches.quarantined keep
    the column and its values through 0007."""
    import sqlalchemy as sa
    from alembic import op  # noqa: F401
    seed_items(("Amoxil", 4, "AMX-7", date(2027, 3, 1), 12))
    upgrade(revision="0006_sms_outbox")
    with db.engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE batches ADD COLUMN quarantined BOOLEAN NOT NULL DEFAULT 0")
        conn.exec_driver_sql("UPDATE batches SET quarantined = 1")
    upgrade()
    assert fetch("SELECT quarantined FROM batches") in ([(1,)], [(True,)])
    assert sa.inspect(db.engine).get_columns("batches")


def test_0011_removes_a_cascade_left_by_the_first_0008(legacy):
    """Databases that ran 0008 before it was changed kept ON DELETE CASCADE
    on the register's item key; 0011 replaces it."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    fk = "fk_controlled_drug_ledger_inventory_item_id_inventory_items"
    upgrade(revision="0010_institution")
    with db.engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        ops = Operations(MigrationContext.configure(conn, opts={"render_as_batch": True}))
        with ops.batch_alter_table("controlled_drug_ledger") as batch:
            batch.drop_constraint(fk, type_="foreignkey")
            batch.create_foreign_key(fk, "inventory_items", ["inventory_item_id"], ["id"],
                                     ondelete="CASCADE")

    def ondelete():
        keys = db.inspect(db.engine).get_foreign_keys("controlled_drug_ledger")
        return next(k for k in keys if k["name"] == fk)["options"].get("ondelete")

    assert (ondelete() or "").upper() == "CASCADE"
    upgrade()
    assert ondelete() in (None, "NO ACTION", "RESTRICT")
    assert diff_against_models(legacy) == []
