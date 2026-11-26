"""WhatsApp chatbot: the business's Cloud API connection, chats and messages.

Revision ID: 0015_whatsapp_bot
Revises: 0014_business_types_shop
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_whatsapp_bot"
down_revision = "0014_business_types_shop"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(sa.Column("wa_phone_number_id", sa.String(40), nullable=True))
        batch.add_column(sa.Column("wa_business_account_id", sa.String(40), nullable=True))
        batch.add_column(sa.Column("wa_access_token_encrypted", sa.String(1024), nullable=True))
        batch.add_column(sa.Column("wa_app_secret_encrypted", sa.String(512), nullable=True))
        batch.add_column(sa.Column("wa_verify_token", sa.String(64), nullable=True))
        batch.add_column(sa.Column("wa_display_phone", sa.String(32), nullable=True))
        batch.add_column(sa.Column("wa_bot_enabled", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.create_unique_constraint("uq_tenants_wa_phone_number_id", ["wa_phone_number_id"])

    op.create_table(
        "whatsapp_chats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("wa_id", sa.String(20), nullable=False),
        sa.Column("profile_name", sa.String(120), nullable=True),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("needs_reply", sa.Boolean(), nullable=False),
        sa.Column("handoff_at", sa.DateTime(), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_whatsapp_chats_tenant_id_tenants", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_chats"),
        sa.UniqueConstraint("tenant_id", "wa_id", name="uq_whatsapp_chats_tenant_wa_id"),
    )
    op.create_index("ix_whatsapp_chats_tenant_id", "whatsapp_chats", ["tenant_id"])

    op.create_table(
        "whatsapp_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(3), nullable=False),
        sa.Column("wa_message_id", sa.String(128), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column("sent_by", sa.Integer(), nullable=True),
        sa.Column("dedupe_key", sa.String(80), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                name="fk_whatsapp_messages_tenant_id_tenants",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chat_id"], ["whatsapp_chats.id"],
                                name="fk_whatsapp_messages_chat_id_whatsapp_chats",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sent_by"], ["users.id"],
                                name="fk_whatsapp_messages_sent_by_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_messages"),
        sa.UniqueConstraint("tenant_id", "wa_message_id",
                            name="uq_whatsapp_messages_tenant_wa_message_id"),
        sa.UniqueConstraint("tenant_id", "dedupe_key",
                            name="uq_whatsapp_messages_tenant_dedupe_key"),
    )
    op.create_index("ix_whatsapp_messages_tenant_id", "whatsapp_messages", ["tenant_id"])
    op.create_index("ix_whatsapp_messages_chat_id", "whatsapp_messages", ["chat_id"])


def downgrade():
    # Drop the tables whole: MySQL will not drop an index its foreign key uses.
    op.drop_table("whatsapp_messages")
    op.drop_table("whatsapp_chats")
    with op.batch_alter_table("tenants") as batch:
        batch.drop_constraint("uq_tenants_wa_phone_number_id", type_="unique")
        batch.drop_column("wa_bot_enabled")
        batch.drop_column("wa_display_phone")
        batch.drop_column("wa_verify_token")
        batch.drop_column("wa_app_secret_encrypted")
        batch.drop_column("wa_access_token_encrypted")
        batch.drop_column("wa_business_account_id")
        batch.drop_column("wa_phone_number_id")
