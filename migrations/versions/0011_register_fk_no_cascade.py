"""The controlled-drug register no longer cascade-deletes with its item.

0008 first created this foreign key with ON DELETE CASCADE; 0008 was later
changed, but a database that had already run it kept the cascade. This
revision replaces the key either way, so deleting an item can never take
its register with it.

Revision ID: 0011_register_fk_no_cascade
Revises: 0010_institution
Create Date: 2026-11-25
"""
from alembic import op

revision = "0011_register_fk_no_cascade"
down_revision = "0010_institution"
branch_labels = None
depends_on = None

FK = "fk_controlled_drug_ledger_inventory_item_id_inventory_items"


def upgrade():
    # Default recreate="auto": a plain ALTER on MySQL (whose FK names are
    # schema-wide, so a table copy would clash), a rebuild on SQLite.
    with op.batch_alter_table("controlled_drug_ledger") as batch:
        batch.drop_constraint(FK, type_="foreignkey")
        batch.create_foreign_key(FK, "inventory_items", ["inventory_item_id"], ["id"])


def downgrade():
    pass        # keep the safer key: no cascade is never wrong for the register
