"""Distributor: price tiers, alternate payer phones, returnable containers."""
from datetime import datetime

from sqlalchemy.orm import validates

from models.base import TenantMixin, db
from services.phone import normalize_ke_phone


class PriceTier(TenantMixin, db.Model):
    """A named price list (wholesale, key account...). Items without a tier
    price sell at the item's own price."""
    __tablename__ = "price_tiers"
    __table_args__ = (db.UniqueConstraint("tenant_id", "name", name="uq_price_tiers_tenant_name"),)

    id   = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(64), nullable=False)

    def to_dict(self):
        return {"id": self.id, "name": self.name}


class TierPrice(TenantMixin, db.Model):
    __tablename__ = "tier_prices"
    __table_args__ = (db.UniqueConstraint("tenant_id", "price_tier_id", "inventory_item_id",
                                          name="uq_tier_prices_tenant_tier_item"),)

    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    price_tier_id     = db.Column(db.Integer, db.ForeignKey("price_tiers.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey("inventory_items.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    price             = db.Column(db.Numeric(12, 2), nullable=False)

    def to_dict(self):
        return {"id": self.id, "price_tier_id": self.price_tier_id,
                "inventory_item_id": self.inventory_item_id, "price": float(self.price)}


class CustomerPhone(TenantMixin, db.Model):
    """Another number that pays for a duka (the owner's wife, the shop
    assistant). Unique per business, like customers' own numbers."""
    __tablename__ = "customer_phones"
    __table_args__ = (db.UniqueConstraint("tenant_id", "phone", name="uq_customer_phones_tenant_phone"),)

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    phone       = db.Column(db.String(15), nullable=False)
    label       = db.Column(db.String(64), nullable=True)

    @validates("phone")
    def _normalize_phone(self, key, value):
        return normalize_ke_phone(value)

    def to_dict(self):
        return {"id": self.id, "customer_id": self.customer_id, "phone": self.phone,
                "label": self.label}


class ContainerMovement(TenantMixin, db.Model):
    """Returnable containers (gas cylinders, crates): +issued, -returned.
    A duka's balance is the sum."""
    __tablename__ = "container_movements"

    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    customer_id       = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey("inventory_items.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    delta             = db.Column(db.Integer, nullable=False)
    order_id          = db.Column(db.Integer, db.ForeignKey("orders.id", ondelete="SET NULL"),
                                  nullable=True)
    note              = db.Column(db.String(255), nullable=True)
    created_by        = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                                  nullable=True)
    created_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "customer_id": self.customer_id,
                "inventory_item_id": self.inventory_item_id, "delta": self.delta,
                "order_id": self.order_id, "note": self.note,
                "created_at": self.created_at.isoformat() if self.created_at else None}
