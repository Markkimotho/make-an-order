"""Build a database the way the pre-Alembic app did (main before step 1).

The old startup ran ``db.create_all()`` with no naming convention, so every
unique and foreign-key constraint was created unnamed (MySQL then names
them itself: ``email``, ``users_ibfk_1``, ...). Migrations that touch those
constraints on an existing database must look names up with the inspector;
tests for them start from this schema.
"""
from sqlalchemy import MetaData

from models import db

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


def legacy_metadata():
    """Copy of the baseline tables with every constraint left unnamed."""
    legacy = MetaData()
    for table in db.metadata.sorted_tables:
        table.to_metadata(legacy)
    for table in legacy.tables.values():
        for constraint in table.constraints:
            constraint.name = None
    return legacy


def create_legacy_schema(engine):
    legacy_metadata().create_all(engine)
