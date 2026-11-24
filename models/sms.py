"""Every text message the app sends, one row per recipient."""
from datetime import datetime

from models.base import TenantMixin, db

SMS_STATUSES = ("queued", "sent", "failed", "skipped")


class SmsMessage(TenantMixin, db.Model):
    """The outbox. ``status`` is our side (did Africa's Talking accept it);
    ``delivery_status`` is the network's, from delivery reports.

    ``dedupe_key`` (unique per business) makes a send idempotent: the daily
    reminder job can rerun without texting anyone twice.
    """
    __tablename__ = "sms_messages"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "dedupe_key", name="uq_sms_messages_tenant_dedupe_key"),
        db.UniqueConstraint("provider_message_id", name="uq_sms_messages_provider_message_id"),
    )

    id                  = db.Column(db.Integer, primary_key=True, autoincrement=True)
    to_phone            = db.Column(db.String(32), nullable=False)
    body                = db.Column(db.Text, nullable=False)
    kind                = db.Column(db.String(40), nullable=False)
    dedupe_key          = db.Column(db.String(160), nullable=True)
    status              = db.Column(db.String(16), nullable=False, default="queued")
    provider_message_id = db.Column(db.String(80), nullable=True)
    delivery_status     = db.Column(db.String(32), nullable=True)
    error               = db.Column(db.String(255), nullable=True)
    created_at          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    sent_at             = db.Column(db.DateTime, nullable=True)
    delivered_at        = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {"id": self.id, "to_phone": self.to_phone, "kind": self.kind,
                "status": self.status, "delivery_status": self.delivery_status,
                "error": self.error,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "sent_at": self.sent_at.isoformat() if self.sent_at else None,
                "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None}
