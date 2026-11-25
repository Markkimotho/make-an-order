"""Remote orders: numbers, channel, fulfilment, stock holds, pickup;
and batches.quarantined (returned goods on hold).

Existing orders are numbered ORD<id> (ids are unique across businesses, so
the numbers are unique within each) and marked fulfilled the way their
status says.

Revision ID: 0007_remote_orders
Revises: 0006_sms_outbox
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_remote_orders"
down_revision = "0006_sms_outbox"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("batches") as batch:
        batch.add_column(sa.Column("quarantined", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("order_number", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("channel", sa.String(length=16), nullable=False,
                                   server_default="staff"))
        batch.add_column(sa.Column("fulfillment_status", sa.String(length=16), nullable=False,
                                   server_default="pending"))
        batch.add_column(sa.Column("delivery_address", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("reserved_until", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("prescription_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("pickup_code_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("picked_up_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("picked_up_by", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_orders_prescription_id_prescriptions", "prescriptions",
                                 ["prescription_id"], ["id"], ondelete="SET NULL")
        batch.create_foreign_key("fk_orders_picked_up_by_users", "users",
                                 ["picked_up_by"], ["id"], ondelete="SET NULL")
        batch.create_unique_constraint("uq_orders_tenant_order_number",
                                       ["tenant_id", "order_number"])
        batch.create_index("ix_orders_reserved_until", ["reserved_until"])
    with op.batch_alter_table("order_items") as batch:
        batch.add_column(sa.Column("variant_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_order_items_variant_id_item_variants", "item_variants",
                                 ["variant_id"], ["id"], ondelete="SET NULL")

    orders = sa.table("orders", sa.column("id", sa.Integer), sa.column("status", sa.String),
                      sa.column("order_number", sa.String),
                      sa.column("fulfillment_status", sa.String))
    bind = op.get_bind()
    for order_id, status in bind.execute(sa.select(orders.c.id, orders.c.status)).all():
        fulfilment = {"completed": "collected", "cancelled": "cancelled",
                      "refunded": "cancelled"}.get(status, "pending")
        bind.execute(orders.update().where(orders.c.id == order_id).values(
            order_number=f"ORD{order_id:06d}", fulfillment_status=fulfilment))


def downgrade():
    with op.batch_alter_table("order_items") as batch:
        batch.drop_constraint("fk_order_items_variant_id_item_variants", type_="foreignkey")
        batch.drop_column("variant_id")
    with op.batch_alter_table("orders") as batch:
        batch.drop_index("ix_orders_reserved_until")
        batch.drop_constraint("uq_orders_tenant_order_number", type_="unique")
        batch.drop_constraint("fk_orders_picked_up_by_users", type_="foreignkey")
        batch.drop_constraint("fk_orders_prescription_id_prescriptions", type_="foreignkey")
        for column in ("picked_up_by", "picked_up_at", "pickup_code_hash", "prescription_id",
                       "reserved_until", "delivery_address", "fulfillment_status", "channel",
                       "order_number"):
            batch.drop_column(column)
    with op.batch_alter_table("batches") as batch:
        batch.drop_column("quarantined")
