"""Distributor: price tiers, tier prices, alternate payer phones, returnable
containers; duka account codes, order links, KYC and route details.

Existing customers get an account code from their customer code (upper
case, letters and digits; a numeric suffix if two codes collapse to the
same value) and a random order-link token.

Revision ID: 0009_distributor
Revises: 0008_pharmacy
Create Date: 2026-11-25
"""
import re
import secrets

import sqlalchemy as sa
from alembic import op

revision = "0009_distributor"
down_revision = "0008_pharmacy"
branch_labels = None
depends_on = None


def _tenant_fk(table):
    return sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                   name=f"fk_{table}_tenant_id_tenants", ondelete="CASCADE")


def upgrade():
    op.create_table(
        "price_tiers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        _tenant_fk("price_tiers"),
        sa.PrimaryKeyConstraint("id", name="pk_price_tiers"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_price_tiers_tenant_name"),
    )
    op.create_index("ix_price_tiers_tenant_id", "price_tiers", ["tenant_id"])

    op.create_table(
        "tier_prices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("price_tier_id", sa.Integer(), nullable=False),
        sa.Column("inventory_item_id", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        _tenant_fk("tier_prices"),
        sa.ForeignKeyConstraint(["price_tier_id"], ["price_tiers.id"],
                                name="fk_tier_prices_price_tier_id_price_tiers", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_items.id"],
                                name="fk_tier_prices_inventory_item_id_inventory_items",
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_tier_prices"),
        sa.UniqueConstraint("tenant_id", "price_tier_id", "inventory_item_id",
                            name="uq_tier_prices_tenant_tier_item"),
    )
    for col in ("tenant_id", "price_tier_id", "inventory_item_id"):
        op.create_index(f"ix_tier_prices_{col}", "tier_prices", [col])

    op.create_table(
        "customer_phones",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("phone", sa.String(length=15), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=True),
        _tenant_fk("customer_phones"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"],
                                name="fk_customer_phones_customer_id_customers", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_customer_phones"),
        sa.UniqueConstraint("tenant_id", "phone", name="uq_customer_phones_tenant_phone"),
    )
    for col in ("tenant_id", "customer_id"):
        op.create_index(f"ix_customer_phones_{col}", "customer_phones", [col])

    op.create_table(
        "container_movements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("inventory_item_id", sa.Integer(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _tenant_fk("container_movements"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"],
                                name="fk_container_movements_customer_id_customers",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_items.id"],
                                name="fk_container_movements_inventory_item_id_inventory_items",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"],
                                name="fk_container_movements_order_id_orders", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                name="fk_container_movements_created_by_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_container_movements"),
    )
    for col in ("tenant_id", "customer_id", "inventory_item_id"):
        op.create_index(f"ix_container_movements_{col}", "container_movements", [col])

    with op.batch_alter_table("inventory_items") as batch:
        batch.add_column(sa.Column("is_returnable_container", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column("account_ref", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("public_token", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("national_id", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("physical_address", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("kra_pin", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("route_zone", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("gps_lat", sa.Numeric(precision=9, scale=6), nullable=True))
        batch.add_column(sa.Column("gps_lng", sa.Numeric(precision=9, scale=6), nullable=True))
        batch.add_column(sa.Column("price_tier_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_customers_price_tier_id_price_tiers", "price_tiers",
                                 ["price_tier_id"], ["id"], ondelete="SET NULL")

    bind = op.get_bind()
    customers = sa.table("customers", sa.column("id", sa.Integer),
                         sa.column("tenant_id", sa.Integer), sa.column("code", sa.String),
                         sa.column("account_ref", sa.String), sa.column("public_token", sa.String))
    used = {}
    for cid, tenant_id, code in bind.execute(
            sa.select(customers.c.id, customers.c.tenant_id, customers.c.code)
            .order_by(customers.c.id)).all():
        base = re.sub(r"[^A-Z0-9]", "", (code or "").upper())[:16] or f"C{cid}"
        ref, n = base, 1
        taken = used.setdefault(tenant_id, set())
        while ref in taken:
            n += 1
            ref = f"{base}{n}"[:20]
        taken.add(ref)
        bind.execute(customers.update().where(customers.c.id == cid)
                     .values(account_ref=ref, public_token=secrets.token_urlsafe(16)[:22]))

    with op.batch_alter_table("customers") as batch:
        batch.create_unique_constraint("uq_customers_tenant_account_ref",
                                       ["tenant_id", "account_ref"])
        batch.create_unique_constraint("uq_customers_tenant_public_token",
                                       ["tenant_id", "public_token"])


def downgrade():
    with op.batch_alter_table("customers") as batch:
        batch.drop_constraint("uq_customers_tenant_public_token", type_="unique")
        batch.drop_constraint("uq_customers_tenant_account_ref", type_="unique")
        batch.drop_constraint("fk_customers_price_tier_id_price_tiers", type_="foreignkey")
        for column in ("price_tier_id", "gps_lng", "gps_lat", "route_zone", "kra_pin",
                       "physical_address", "national_id", "public_token", "account_ref"):
            batch.drop_column(column)
    with op.batch_alter_table("inventory_items") as batch:
        batch.drop_column("is_returnable_container")
    for table in ("container_movements", "customer_phones", "tier_prices", "price_tiers"):
        op.drop_table(table)
