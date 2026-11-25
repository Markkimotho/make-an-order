"""Build a database the way the pre-Alembic app did (main before step 1).

The old startup ran ``db.create_all()`` with no naming convention, so every
unique and foreign-key constraint was created unnamed (MySQL then names
them itself: ``email``, ``users_ibfk_1``, ...). Migrations that touch those
constraints on an existing database must look names up with the inspector;
tests for them start from this schema.

The shape comes from revision 0001_baseline, not from the live models, so
it stays the pre-pivot schema while the models move on.
"""
import tempfile
from pathlib import Path

from flask_migrate import upgrade
from sqlalchemy import MetaData

import app as app_module
from models import db

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

# The single-column unique constraints that differ by name between a legacy
# database and the models. 0003_tenants_contract replaces all of them except
# users.email with per-tenant constraints, looking the old names up first.
LEGACY_UNIQUE_COLUMNS = {
    ("categories", "name"),
    ("customers", "code"),
    ("customers", "phone_number"),
    ("inventory_items", "barcode"),
    ("inventory_items", "sku"),
    ("purchase_orders", "po_number"),
    ("system_config", "key"),
    ("transactions", "transaction_ref"),
    ("users", "email"),
}

_cached = None


def legacy_metadata():
    """The 0001_baseline tables, reflected, with every constraint unnamed."""
    global _cached
    if _cached is not None:
        return _cached
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{tmp}/baseline.db"
        scratch = app_module.create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": url})
        with scratch.app_context():
            upgrade(directory=str(MIGRATIONS_DIR), revision="0001_baseline")
            reflected = MetaData()
            reflected.reflect(bind=db.engine)
            db.engine.dispose()
    reflected.remove(reflected.tables["alembic_version"])
    for table in reflected.tables.values():
        for constraint in list(table.constraints):
            constraint.name = None
        for index in list(table.indexes):
            index.name = None
    _cached = reflected
    return reflected


def create_legacy_schema(engine):
    legacy_metadata().create_all(engine)
