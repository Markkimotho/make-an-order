"""Sale lines keep the item's cost price at the time of sale, so profit on
past sales does not change when a cost price is edited.

Revision ID: 0013_line_unit_cost
Revises: 0012_pharmacy_owner
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_line_unit_cost"
down_revision = "0012_pharmacy_owner"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("transaction_items") as batch:
        batch.add_column(sa.Column("unit_cost", sa.Numeric(12, 2), nullable=True))


def downgrade():
    with op.batch_alter_table("transaction_items") as batch:
        batch.drop_column("unit_cost")
