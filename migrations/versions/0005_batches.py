"""Stock in batches: batches, item_variants, line_batches.

Every item with stock gets one batch holding all of it, numbered from the
item's batch_number (or LEGACY) with the item's expiry date. An empty item
with a batch number or expiry keeps them in an empty batch, so returns of
earlier sales can find the batch they came from. Then
inventory_items.batch_number and expiry_date are dropped: an item has as
many batches as deliveries, each with its own expiry, and sales take the
soonest-expiring stock first.

Downgrade puts the soonest-expiring stocked batch's number and expiry back
on each item, or an empty batch's for an empty item (that is all the old
columns could hold).

Revision ID: 0005_batches
Revises: 0004_billing
Create Date: 2026-11-24
"""
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0005_batches"
down_revision = "0004_billing"
branch_labels = None
depends_on = None


def _tenant_fk(table):
    return sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                   name=f"fk_{table}_tenant_id_tenants", ondelete="CASCADE")


def upgrade():
    op.create_table(
        "batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("inventory_item_id", sa.Integer(), nullable=False),
        sa.Column("batch_number", sa.String(length=64), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("quantity_on_hand", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("quarantined", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_items.id"],
                                name="fk_batches_inventory_item_id_inventory_items",
                                ondelete="CASCADE"),
        _tenant_fk("batches"),
        sa.PrimaryKeyConstraint("id", name="pk_batches"),
        sa.UniqueConstraint("tenant_id", "inventory_item_id", "batch_number",
                            name="uq_batches_tenant_item_batch_number"),
    )
    op.create_index("ix_batches_tenant_id", "batches", ["tenant_id"])
    op.create_index("ix_batches_inventory_item_id", "batches", ["inventory_item_id"])

    op.create_table(
        "item_variants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("inventory_item_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("price_override", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_items.id"],
                                name="fk_item_variants_inventory_item_id_inventory_items",
                                ondelete="CASCADE"),
        _tenant_fk("item_variants"),
        sa.PrimaryKeyConstraint("id", name="pk_item_variants"),
        sa.UniqueConstraint("tenant_id", "inventory_item_id", "label",
                            name="uq_item_variants_tenant_item_label"),
    )
    op.create_index("ix_item_variants_tenant_id", "item_variants", ["tenant_id"])
    op.create_index("ix_item_variants_inventory_item_id", "item_variants", ["inventory_item_id"])

    op.create_table(
        "line_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("transaction_item_id", sa.Integer(), nullable=True),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("returned_quantity", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["transaction_item_id"], ["transaction_items.id"],
                                name="fk_line_batches_transaction_item_id_transaction_items",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"],
                                name="fk_line_batches_order_item_id_order_items",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"],
                                name="fk_line_batches_batch_id_batches", ondelete="CASCADE"),
        _tenant_fk("line_batches"),
        sa.PrimaryKeyConstraint("id", name="pk_line_batches"),
    )
    for col in ("tenant_id", "transaction_item_id", "order_item_id", "batch_id"):
        op.create_index(f"ix_line_batches_{col}", "line_batches", [col])

    with op.batch_alter_table("inventory_items") as batch:
        batch.add_column(sa.Column("has_variants", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))

    bind = op.get_bind()
    items = sa.table("inventory_items", sa.column("id", sa.Integer),
                     sa.column("tenant_id", sa.Integer), sa.column("stock", sa.Integer),
                     sa.column("batch_number", sa.String), sa.column("expiry_date", sa.Date),
                     sa.column("cost_price", sa.Numeric(12, 2)))
    batches = sa.table("batches", sa.column("tenant_id", sa.Integer),
                       sa.column("inventory_item_id", sa.Integer),
                       sa.column("batch_number", sa.String), sa.column("expiry_date", sa.Date),
                       sa.column("quantity_on_hand", sa.Integer),
                       sa.column("unit_cost", sa.Numeric(12, 2)),
                       sa.column("received_at", sa.DateTime))
    now = datetime.utcnow()
    for item_id, tenant_id, stock, number, expiry, cost in bind.execute(
            sa.select(items.c.id, items.c.tenant_id, items.c.stock, items.c.batch_number,
                      items.c.expiry_date, items.c.cost_price)).all():
        number = (number or "").strip()
        if (stock or 0) <= 0 and not number and expiry is None:
            continue
        bind.execute(batches.insert().values(
            tenant_id=tenant_id, inventory_item_id=item_id,
            batch_number=number or "LEGACY", expiry_date=expiry,
            quantity_on_hand=max(stock or 0, 0), unit_cost=cost, received_at=now))

    with op.batch_alter_table("inventory_items") as batch:
        batch.drop_column("batch_number")
        batch.drop_column("expiry_date")


def downgrade():
    with op.batch_alter_table("inventory_items") as batch:
        batch.add_column(sa.Column("expiry_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("batch_number", sa.String(length=64), nullable=True))
    bind = op.get_bind()
    batches = sa.table("batches", sa.column("id", sa.Integer),
                       sa.column("inventory_item_id", sa.Integer),
                       sa.column("batch_number", sa.String), sa.column("expiry_date", sa.Date),
                       sa.column("quantity_on_hand", sa.Integer))
    rows = bind.execute(
        sa.select(batches.c.inventory_item_id, batches.c.batch_number, batches.c.expiry_date)
        .order_by(batches.c.inventory_item_id,
                  sa.case((batches.c.quantity_on_hand > 0, 0), else_=1),
                  batches.c.expiry_date.is_(None), batches.c.expiry_date, batches.c.id)).all()
    items = sa.table("inventory_items", sa.column("id", sa.Integer),
                     sa.column("batch_number", sa.String), sa.column("expiry_date", sa.Date))
    seen = set()
    for item_id, number, expiry in rows:
        if item_id in seen:
            continue
        seen.add(item_id)
        bind.execute(items.update().where(items.c.id == item_id)
                     .values(batch_number=number, expiry_date=expiry))
    with op.batch_alter_table("inventory_items") as batch:
        batch.drop_column("has_variants")
    for table in ("line_batches", "item_variants", "batches"):
        op.drop_table(table)
