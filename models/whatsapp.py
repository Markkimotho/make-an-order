"""WhatsApp chatbot: one conversation per customer number, and every message
in and out.

``WhatsAppChat.state`` is the bot's memory for that customer (JSON: the
step, the cart, the name and address given so far). The cart only holds item
ids and quantities; prices and stock are read again at checkout.

``WhatsAppMessage.wa_message_id`` is Meta's message id. Meta retries a
webhook it thinks failed, so an inbound id seen before is ignored.

``WhatsAppMessage.dedupe_key`` (unique per business) makes a notice
(``paid:<order id>``, ``order_ready:<order id>``) go out once however many
callbacks or sweeps try; ``attempts`` counts sends, so a failed notice is
retried a few times and then left alone.
"""
from datetime import datetime

from models.base import TenantMixin, db

CHAT_MODES = ("bot", "person")        # person: a staff member is answering
MESSAGE_DIRECTIONS = ("in", "out")
MESSAGE_STATUSES = ("received", "queued", "sending", "sent", "delivered", "read", "failed")


class WhatsAppChat(TenantMixin, db.Model):
    __tablename__ = "whatsapp_chats"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "wa_id", name="uq_whatsapp_chats_tenant_wa_id"),
    )

    id               = db.Column(db.Integer, primary_key=True, autoincrement=True)
    wa_id            = db.Column(db.String(20), nullable=False)       # 2547XXXXXXXX
    profile_name     = db.Column(db.String(120), nullable=True)
    state            = db.Column(db.JSON, nullable=False, default=dict)
    mode             = db.Column(db.String(10), nullable=False, default="bot")
    needs_reply      = db.Column(db.Boolean, nullable=False, default=False)
    handoff_at       = db.Column(db.DateTime, nullable=True)
    last_inbound_at  = db.Column(db.DateTime, nullable=True)
    last_outbound_at = db.Column(db.DateTime, nullable=True)
    created_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "wa_id": self.wa_id, "profile_name": self.profile_name,
                "mode": self.mode, "needs_reply": bool(self.needs_reply),
                "handoff_at": _iso(self.handoff_at),
                "last_inbound_at": _iso(self.last_inbound_at),
                "last_outbound_at": _iso(self.last_outbound_at)}


class WhatsAppMessage(TenantMixin, db.Model):
    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "wa_message_id",
                            name="uq_whatsapp_messages_tenant_wa_message_id"),
        db.UniqueConstraint("tenant_id", "dedupe_key",
                            name="uq_whatsapp_messages_tenant_dedupe_key"),
    )

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    chat_id       = db.Column(db.Integer, db.ForeignKey("whatsapp_chats.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    direction     = db.Column(db.String(3), nullable=False)
    wa_message_id = db.Column(db.String(128), nullable=True)
    kind          = db.Column(db.String(20), nullable=False)    # text, buttons, list, ...
    body          = db.Column(db.Text, nullable=False)          # what a person would read
    status        = db.Column(db.String(12), nullable=False)
    error         = db.Column(db.String(255), nullable=True)
    sent_by       = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                              nullable=True)                    # staff replies only
    # A photo, document, voice note or video the customer sent (migration
    # 0016): Meta's media id, fetched from Meta when staff open it.
    media_id      = db.Column(db.String(128), nullable=True)
    media_type    = db.Column(db.String(100), nullable=True)
    media_name    = db.Column(db.String(255), nullable=True)
    dedupe_key    = db.Column(db.String(80), nullable=True)
    attempts      = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "direction": self.direction, "kind": self.kind,
                "body": self.body, "status": self.status, "error": self.error,
                "staff": self.sent_by is not None, "created_at": _iso(self.created_at),
                "media": ({"type": self.media_type or "", "name": self.media_name or ""}
                          if self.media_id else None)}


def _iso(value):
    return value.isoformat() if value else None
