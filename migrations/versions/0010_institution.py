"""Institution: members, campaigns and their items; orders, invoices and
payments can belong to a member.

Revision ID: 0010_institution
Revises: 0009_distributor
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_institution"
down_revision = "0009_distributor"
branch_labels = None
depends_on = None


def _tenant_fk(table):
    return sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                   name=f"fk_{table}_tenant_id_tenants", ondelete="CASCADE")


def upgrade():
    op.create_table(
        "members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("member_ref", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("group_label", sa.String(length=64), nullable=True),
        sa.Column("guardian_phone", sa.String(length=15), nullable=True),
        sa.Column("secondary_phone", sa.String(length=15), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _tenant_fk("members"),
        sa.PrimaryKeyConstraint("id", name="pk_members"),
        sa.UniqueConstraint("tenant_id", "member_ref", name="uq_members_tenant_member_ref"),
    )
    op.create_index("ix_members_tenant_id", "members", ["tenant_id"])

    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("id_label", sa.String(length=32), nullable=False, server_default="Admission No"),
        sa.Column("opens_at", sa.DateTime(), nullable=True),
        sa.Column("closes_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="draft"),
        sa.Column("allow_installments", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("min_installment", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _tenant_fk("campaigns"),
        sa.PrimaryKeyConstraint("id", name="pk_campaigns"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_campaigns_tenant_slug"),
    )
    op.create_index("ix_campaigns_tenant_id", "campaigns", ["tenant_id"])

    op.create_table(
        "campaign_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("inventory_item_id", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        _tenant_fk("campaign_items"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"],
                                name="fk_campaign_items_campaign_id_campaigns", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_items.id"],
                                name="fk_campaign_items_inventory_item_id_inventory_items",
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_items"),
        sa.UniqueConstraint("tenant_id", "campaign_id", "inventory_item_id",
                            name="uq_campaign_items_tenant_campaign_item"),
    )
    for col in ("tenant_id", "campaign_id", "inventory_item_id"):
        op.create_index(f"ix_campaign_items_{col}", "campaign_items", [col])

    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("campaign_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("member_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_orders_campaign_id_campaigns", "campaigns",
                                 ["campaign_id"], ["id"], ondelete="SET NULL")
        batch.create_foreign_key("fk_orders_member_id_members", "members",
                                 ["member_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_orders_campaign_id", ["campaign_id"])
        batch.create_index("ix_orders_member_id", ["member_id"])
    for table in ("invoices", "payments"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("member_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(f"fk_{table}_member_id_members", "members",
                                     ["member_id"], ["id"], ondelete="SET NULL")
            batch.create_index(f"ix_{table}_member_id", ["member_id"])


def downgrade():
    # Refuse before any schema change (MySQL DDL cannot be rolled back):
    # dropping these tables would destroy members, campaigns and who paid.
    bind = op.get_bind()
    for table in ("members", "campaigns"):
        if bind.execute(sa.select(sa.func.count()).select_from(sa.table(table))).scalar():
            raise RuntimeError(f"{table} has rows; export them before downgrading")
    for table in ("payments", "invoices"):
        # MySQL refuses to drop an index a foreign key still uses: FK first.
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_member_id_members", type_="foreignkey")
            batch.drop_index(f"ix_{table}_member_id")
            batch.drop_column("member_id")
    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint("fk_orders_member_id_members", type_="foreignkey")
        batch.drop_constraint("fk_orders_campaign_id_campaigns", type_="foreignkey")
        batch.drop_index("ix_orders_member_id")
        batch.drop_index("ix_orders_campaign_id")
        batch.drop_column("member_id")
        batch.drop_column("campaign_id")
    for table in ("campaign_items", "campaigns", "members"):
        op.drop_table(table)
