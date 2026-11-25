"""Pharmacy: controlled-drug register, controlled items, refill fields,
pharmacists' PPB numbers.

Nothing is marked controlled by this migration (no existing column says
so). Marking an item controlled writes "opening" register entries for the
stock on hand (services/controlled.open_register), so the running balance
starts where the stock is.

Revision ID: 0008_pharmacy
Revises: 0007_remote_orders
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_pharmacy"
down_revision = "0007_remote_orders"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("inventory_items") as batch:
        batch.add_column(sa.Column("is_controlled", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.add_column(sa.Column("drug_schedule", sa.String(length=16), nullable=True))
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("ppb_reg_no", sa.String(length=64), nullable=True))
    with op.batch_alter_table("prescriptions") as batch:
        batch.add_column(sa.Column("prescriber_reg_no", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("is_chronic", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.add_column(sa.Column("duration_days", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("last_dispensed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("refill_due_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("refills_remaining", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("refill_status", sa.String(length=16), nullable=False,
                                   server_default="active"))
        batch.add_column(sa.Column("refill_token", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("callback_requested_at", sa.DateTime(), nullable=True))
        batch.create_unique_constraint("uq_prescriptions_tenant_refill_token",
                                       ["tenant_id", "refill_token"])
        batch.create_index("ix_prescriptions_refill_due_date", ["refill_due_date"])

    op.create_table(
        "controlled_drug_ledger",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("inventory_item_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("entry_type", sa.String(length=16), nullable=False),
        sa.Column("entry_date", sa.DateTime(), nullable=False),
        sa.Column("drug_name", sa.String(length=255), nullable=False),
        sa.Column("drug_schedule", sa.String(length=16), nullable=True),
        sa.Column("batch_id", sa.Integer(), nullable=True),
        sa.Column("batch_number", sa.String(length=64), nullable=True),
        sa.Column("quantity_in", sa.Integer(), nullable=False),
        sa.Column("quantity_out", sa.Integer(), nullable=False),
        sa.Column("opening_balance", sa.Integer(), nullable=False),
        sa.Column("closing_balance", sa.Integer(), nullable=False),
        sa.Column("prescription_id", sa.Integer(), nullable=True),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("prescriber_name", sa.String(length=255), nullable=True),
        sa.Column("prescriber_reg_no", sa.String(length=64), nullable=True),
        sa.Column("patient_name", sa.String(length=255), nullable=True),
        sa.Column("patient_national_id", sa.String(length=32), nullable=True),
        sa.Column("patient_address", sa.String(length=255), nullable=True),
        sa.Column("pharmacist_id", sa.Integer(), nullable=True),
        sa.Column("pharmacist_name", sa.String(length=255), nullable=True),
        sa.Column("pharmacist_reg_no", sa.String(length=64), nullable=True),
        sa.Column("supplier_name", sa.String(length=255), nullable=True),
        sa.Column("reference_doc_no", sa.String(length=64), nullable=True),
        sa.Column("witness_name", sa.String(length=255), nullable=True),
        sa.Column("witness_reg_no", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column("reverses_entry_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_controlled_drug_ledger_tenant_id_tenants",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_items.id"],
                                name="fk_controlled_drug_ledger_inventory_item_id_inventory_items",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"],
                                name="fk_controlled_drug_ledger_batch_id_batches",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["prescription_id"], ["prescriptions.id"],
                                name="fk_controlled_drug_ledger_prescription_id_prescriptions",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"],
                                name="fk_controlled_drug_ledger_transaction_id_transactions",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pharmacist_id"], ["users.id"],
                                name="fk_controlled_drug_ledger_pharmacist_id_users",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reverses_entry_id"], ["controlled_drug_ledger.id"],
                                # MySQL names stop at 64 characters.
                                name="fk_controlled_drug_ledger_reverses_entry_id",
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_controlled_drug_ledger"),
        sa.UniqueConstraint("tenant_id", "inventory_item_id", "seq",
                            name="uq_controlled_drug_ledger_tenant_item_seq"),
    )
    op.create_index("ix_controlled_drug_ledger_tenant_id", "controlled_drug_ledger", ["tenant_id"])
    op.create_index("ix_controlled_drug_ledger_inventory_item_id", "controlled_drug_ledger",
                    ["inventory_item_id"])


def downgrade():
    op.drop_table("controlled_drug_ledger")
    with op.batch_alter_table("prescriptions") as batch:
        batch.drop_index("ix_prescriptions_refill_due_date")
        batch.drop_constraint("uq_prescriptions_tenant_refill_token", type_="unique")
        for column in ("callback_requested_at", "refill_token", "refill_status",
                       "refills_remaining", "refill_due_date", "last_dispensed_at",
                       "duration_days", "is_chronic", "prescriber_reg_no"):
            batch.drop_column(column)
    with op.batch_alter_table("users") as batch:
        batch.drop_column("ppb_reg_no")
    with op.batch_alter_table("inventory_items") as batch:
        batch.drop_column("drug_schedule")
        batch.drop_column("is_controlled")
