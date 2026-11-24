"""SMS outbox: one row per message sent, with delivery status.

Revision ID: 0006_sms_outbox
Revises: 0005_batches
Create Date: 2026-11-24
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_sms_outbox"
down_revision = "0005_batches"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sms_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("to_phone", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider_message_id", sa.String(length=80), nullable=True),
        sa.Column("delivery_status", sa.String(length=32), nullable=True),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_sms_messages_tenant_id_tenants", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_sms_messages"),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_sms_messages_tenant_dedupe_key"),
        sa.UniqueConstraint("provider_message_id", name="uq_sms_messages_provider_message_id"),
    )
    op.create_index("ix_sms_messages_tenant_id", "sms_messages", ["tenant_id"])


def downgrade():
    op.drop_table("sms_messages")
