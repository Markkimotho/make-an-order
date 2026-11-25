"""Tenants, expand phase: add tenants and a nullable tenant_id everywhere.

If the database already holds data, it all moves into one default tenant
named from system_config.business_name ("Default Business" if unset), with
vertical pharmacy when business_type was pharmacy, otherwise distributor.
An empty database gets no tenant: the first signup creates one.

0003 then makes tenant_id NOT NULL and swaps the unique constraints.

Revision ID: 0002_tenants_expand
Revises: 0001_baseline
Create Date: 2026-11-24
"""
from datetime import datetime

import sqlalchemy as sa
from alembic import op

from migrations.helpers import (BUSINESS_TYPE_TO_VERTICAL, TENANT_TABLES,
                                new_callback_token, slugify, tenants_table)

revision = "0002_tenants_expand"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("vertical", sa.String(length=20), nullable=False),
        sa.Column("kra_pin", sa.String(length=20), nullable=True),
        sa.Column("etims_token_encrypted", sa.String(length=1024), nullable=True),
        sa.Column("daraja_shortcode", sa.String(length=20), nullable=True),
        sa.Column("daraja_shortcode_type", sa.String(length=10), nullable=True),
        sa.Column("daraja_till_number", sa.String(length=20), nullable=True),
        sa.Column("daraja_consumer_key_encrypted", sa.String(length=512), nullable=True),
        sa.Column("daraja_consumer_secret_encrypted", sa.String(length=512), nullable=True),
        sa.Column("daraja_passkey_encrypted", sa.String(length=512), nullable=True),
        sa.Column("daraja_env", sa.String(length=12), nullable=False),
        sa.Column("callback_token", sa.String(length=64), nullable=False),
        sa.Column("at_sender_id", sa.String(length=20), nullable=True),
        sa.Column("subscription_status", sa.String(length=20), nullable=False),
        sa.Column("trial_ends_at", sa.DateTime(), nullable=True),
        sa.Column("ppb_premises_license", sa.String(length=64), nullable=True),
        sa.Column("ppb_premises_license_expiry", sa.Date(), nullable=True),
        sa.Column("superintendent_reg_no", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("vertical IN ('pharmacy', 'distributor', 'institution')",
                           name=op.f("ck_tenants_vertical")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenants_slug")),
    )

    for table in TENANT_TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("is_platform_admin", sa.Boolean(), nullable=False,
                                     server_default=sa.false()))

    _move_existing_data_into_default_tenant()


def _move_existing_data_into_default_tenant():
    bind = op.get_bind()
    has_rows = any(
        bind.execute(sa.select(sa.func.count()).select_from(sa.table(t))).scalar()
        for t in TENANT_TABLES
    )
    if not has_rows:
        return

    config = sa.table("system_config", sa.column("key", sa.String), sa.column("value", sa.Text))
    settings = dict(bind.execute(sa.select(config.c.key, config.c.value)).all())
    name = (settings.get("business_name") or "").strip() or "Default Business"
    vertical = BUSINESS_TYPE_TO_VERTICAL.get(settings.get("business_type"), "distributor")

    tenants = tenants_table()
    bind.execute(tenants.insert().values(
        name=name, slug=slugify(name), vertical=vertical, daraja_env="sandbox",
        callback_token=new_callback_token(), subscription_status="active",
        trial_ends_at=None, created_at=datetime.utcnow(),
    ))
    tenant_id = bind.execute(sa.select(tenants.c.id)).scalar_one()

    for table in TENANT_TABLES:
        t = sa.table(table, sa.column("tenant_id", sa.Integer))
        bind.execute(t.update().values(tenant_id=tenant_id))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("is_platform_admin")
    for table in reversed(TENANT_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("tenant_id")
    op.drop_table("tenants")
