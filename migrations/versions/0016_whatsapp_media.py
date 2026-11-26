"""WhatsApp photos and files: Meta's media id on the message, so staff can
open what a customer sent (fetched from Meta when viewed, not stored here).

Revision ID: 0016_whatsapp_media
Revises: 0015_whatsapp_bot
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_whatsapp_media"
down_revision = "0015_whatsapp_bot"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("whatsapp_messages") as batch:
        batch.add_column(sa.Column("media_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("media_type", sa.String(100), nullable=True))
        batch.add_column(sa.Column("media_name", sa.String(255), nullable=True))


def downgrade():
    with op.batch_alter_table("whatsapp_messages") as batch:
        batch.drop_column("media_name")
        batch.drop_column("media_type")
        batch.drop_column("media_id")
