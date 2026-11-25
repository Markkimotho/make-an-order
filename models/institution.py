"""Institution (school, estate, chama): members and the campaigns they pay
for (uniform, trip, levy), item by item, in full or in instalments."""
from datetime import datetime

from sqlalchemy.orm import validates

from models.base import TenantMixin, db
from services.phone import normalize_ke_phone

CAMPAIGN_STATUSES = ("draft", "open", "closed")


class Member(TenantMixin, db.Model):
    """A student or a house: who an order is for. Separate from customers
    because one guardian phone can pay for several children."""
    __tablename__ = "members"
    __table_args__ = (db.UniqueConstraint("tenant_id", "member_ref", name="uq_members_tenant_member_ref"),)

    id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    member_ref      = db.Column(db.String(32), nullable=False)       # admission / house number
    name            = db.Column(db.String(255), nullable=False)
    group_label     = db.Column(db.String(64), nullable=True)        # Form 1 East / Court B
    guardian_phone  = db.Column(db.String(15), nullable=True)
    secondary_phone = db.Column(db.String(15), nullable=True)
    created_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @validates("member_ref")
    def _normalize_ref(self, key, value):
        # Matched against Paybill account references, which are normalised
        # the same way (upper case, letters and digits).
        import re
        value = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
        if not value:
            raise ValueError("member_ref is required")
        return value

    @validates("guardian_phone", "secondary_phone")
    def _normalize_phone(self, key, value):
        return normalize_ke_phone(value) if value else None

    def to_dict(self):
        return {"id": self.id, "member_ref": self.member_ref, "name": self.name,
                "group_label": self.group_label, "guardian_phone": self.guardian_phone,
                "secondary_phone": self.secondary_phone}


class Campaign(TenantMixin, db.Model):
    __tablename__ = "campaigns"
    __table_args__ = (db.UniqueConstraint("tenant_id", "slug", name="uq_campaigns_tenant_slug"),)

    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name              = db.Column(db.String(255), nullable=False)
    slug              = db.Column(db.String(64), nullable=False)
    description       = db.Column(db.Text, nullable=True)
    id_label          = db.Column(db.String(32), nullable=False, default="Admission No",
                                  server_default="Admission No")
    opens_at          = db.Column(db.DateTime, nullable=True)
    closes_at         = db.Column(db.DateTime, nullable=True)
    status            = db.Column(db.String(10), nullable=False, default="draft",
                                  server_default="draft")
    allow_installments = db.Column(db.Boolean, nullable=False, default=False,
                                   server_default=db.false())
    min_installment   = db.Column(db.Numeric(12, 2), nullable=True)
    created_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    items = db.relationship("CampaignItem", backref="campaign", lazy=True,
                            cascade="all, delete-orphan", order_by="CampaignItem.id")

    def is_open(self, now=None):
        now = now or datetime.utcnow()
        return (self.status == "open" and (self.opens_at is None or self.opens_at <= now)
                and (self.closes_at is None or now < self.closes_at))

    def to_dict(self):
        return {"id": self.id, "name": self.name, "slug": self.slug,
                "description": self.description, "id_label": self.id_label,
                "opens_at": self.opens_at.isoformat() if self.opens_at else None,
                "closes_at": self.closes_at.isoformat() if self.closes_at else None,
                "status": self.status, "allow_installments": bool(self.allow_installments),
                "min_installment": float(self.min_installment) if self.min_installment is not None else None,
                "items": [i.to_dict() for i in self.items]}


class CampaignItem(TenantMixin, db.Model):
    __tablename__ = "campaign_items"
    __table_args__ = (db.UniqueConstraint("tenant_id", "campaign_id", "inventory_item_id",
                                          name="uq_campaign_items_tenant_campaign_item"),)

    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    campaign_id       = db.Column(db.Integer, db.ForeignKey("campaigns.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey("inventory_items.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    price             = db.Column(db.Numeric(12, 2), nullable=False)

    def to_dict(self):
        return {"id": self.id, "campaign_id": self.campaign_id,
                "inventory_item_id": self.inventory_item_id, "price": float(self.price)}
