"""What a pharmacy owner asked for: till discount cap, bank payments,
salesperson on a sale, batch recalls, markup pricing, expenses, login
lockout.

Revision ID: 0012_pharmacy_owner
Revises: 0011_register_fk_no_cascade
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_pharmacy_owner"
down_revision = "0011_register_fk_no_cascade"
branch_labels = None
depends_on = None

TENANT_COLUMNS = (
    ("max_discount_pct", sa.Numeric(5, 2), "10"),
    ("default_markup_pct", sa.Numeric(6, 2), "30"),
    ("price_rounding", sa.Integer(), "5"),
    ("cash_variance_alert", sa.Numeric(12, 2), "500"),
    ("session_idle_minutes", sa.Integer(), "30"),
)


def upgrade():
    with op.batch_alter_table("tenants") as batch:
        for name, type_, default in TENANT_COLUMNS:
            batch.add_column(sa.Column(name, type_, nullable=False, server_default=default))

    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("failed_logins", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("locked_until", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("last_login_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("inventory_items") as batch:
        batch.add_column(sa.Column("reorder_quantity", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("markup_pct", sa.Numeric(6, 2), nullable=True))
        batch.add_column(sa.Column("price_mode", sa.String(10), nullable=False,
                                   server_default="manual"))

    with op.batch_alter_table("transactions") as batch:
        batch.add_column(sa.Column("served_by_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_transactions_served_by_id_users", "users",
                                 ["served_by_id"], ["id"], ondelete="SET NULL")

    with op.batch_alter_table("batches") as batch:
        batch.add_column(sa.Column("recalled_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("recall_reason", sa.String(255), nullable=True))
        batch.add_column(sa.Column("recall_ref", sa.String(64), nullable=True))

    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("bank_name", sa.String(64), nullable=True))
        batch.add_column(sa.Column("bank_reference", sa.String(64), nullable=True))
        batch.add_column(sa.Column("verified_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("verified_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key("fk_payments_verified_by_users", "users",
                                 ["verified_by"], ["id"], ondelete="SET NULL")
        batch.create_unique_constraint("uq_payments_tenant_bank_reference",
                                       ["tenant_id", "bank_reference"])

    op.create_table(
        "expenses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("spent_on", sa.Date(), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("shift_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_expenses_tenant_id_tenants", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shift_id"], ["shifts.id"],
                                name="fk_expenses_shift_id_shifts", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                name="fk_expenses_created_by_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_expenses"),
    )
    op.create_index("ix_expenses_tenant_id", "expenses", ["tenant_id"])
    op.create_index("ix_expenses_spent_on", "expenses", ["spent_on"])


def downgrade():
    # Indexes go with the table (MySQL refuses to drop one a foreign key uses).
    op.drop_table("expenses")

    with op.batch_alter_table("payments") as batch:
        batch.drop_constraint("uq_payments_tenant_bank_reference", type_="unique")
        batch.drop_constraint("fk_payments_verified_by_users", type_="foreignkey")
        for col in ("verified_at", "verified_by", "bank_reference", "bank_name"):
            batch.drop_column(col)

    with op.batch_alter_table("batches") as batch:
        for col in ("recall_ref", "recall_reason", "recalled_at"):
            batch.drop_column(col)

    with op.batch_alter_table("transactions") as batch:
        batch.drop_constraint("fk_transactions_served_by_id_users", type_="foreignkey")
        batch.drop_column("served_by_id")

    with op.batch_alter_table("inventory_items") as batch:
        for col in ("price_mode", "markup_pct", "reorder_quantity"):
            batch.drop_column(col)

    with op.batch_alter_table("users") as batch:
        for col in ("last_login_at", "locked_until", "failed_logins"):
            batch.drop_column(col)

    with op.batch_alter_table("tenants") as batch:
        for name, _, _ in reversed(TENANT_COLUMNS):
            batch.drop_column(name)
