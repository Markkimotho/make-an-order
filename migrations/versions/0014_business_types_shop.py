"""More business types, and the online shop settings.

The tenants.vertical check grows from three types to eleven; tenants get
the WhatsApp number customers order through and two shop switches.

Revision ID: 0014_business_types_shop
Revises: 0013_line_unit_cost
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_business_types_shop"
down_revision = "0013_line_unit_cost"
branch_labels = None
depends_on = None

OLD = ("pharmacy", "distributor", "institution")
NEW = OLD + ("shop", "supermarket", "hardware", "salon", "restaurant", "agrovet",
             "electronics", "cosmetics")


def _check(types):
    return "vertical IN (" + ", ".join(f"'{t}'" for t in types) + ")"


def upgrade():
    with op.batch_alter_table("tenants") as batch:
        batch.drop_constraint(op.f("ck_tenants_vertical"), type_="check")
        batch.create_check_constraint("vertical", _check(NEW))
        batch.add_column(sa.Column("whatsapp_number", sa.String(15), nullable=True))
        batch.add_column(sa.Column("shop_open", sa.Boolean(), nullable=False,
                                   server_default=sa.true()))
        batch.add_column(sa.Column("shop_delivery", sa.Boolean(), nullable=False,
                                   server_default=sa.true()))


def downgrade():
    # A business on one of the new types cannot go back: refuse rather than
    # guess which of the old three it should become.
    bind = op.get_bind()
    placeholders = ", ".join(f"'{t}'" for t in OLD)
    count = bind.exec_driver_sql(
        f"SELECT COUNT(*) FROM tenants WHERE vertical NOT IN ({placeholders})").scalar()
    if count:
        raise RuntimeError(f"{count} business(es) use a type added in 0014; "
                           "change them to pharmacy, distributor or institution first")
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("shop_delivery")
        batch.drop_column("shop_open")
        batch.drop_column("whatsapp_number")
        batch.drop_constraint(op.f("ck_tenants_vertical"), type_="check")
        batch.create_check_constraint("vertical", _check(OLD))
