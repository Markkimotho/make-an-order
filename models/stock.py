"""Stock in batches (with expiry) or variants (sizes), and which batch each
sale line drew from. Only services/stock.py writes these quantities.
"""
from datetime import datetime

from models.base import TenantMixin, db


class Batch(TenantMixin, db.Model):
    __tablename__ = "batches"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "inventory_item_id", "batch_number",
                            name="uq_batches_tenant_item_batch_number"),
    )

    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey("inventory_items.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    batch_number      = db.Column(db.String(64), nullable=False)
    expiry_date       = db.Column(db.Date, nullable=True)
    quantity_on_hand  = db.Column(db.Integer, nullable=False, default=0)
    unit_cost         = db.Column(db.Numeric(12, 2), nullable=True)
    received_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Returned goods whose batch and expiry are unknown: never sold until an
    # admin checks them and releases the batch (PUT /inventory/batches/<id>).
    quarantined       = db.Column(db.Boolean, nullable=False, default=False,
                                  server_default=db.false())

    def to_dict(self):
        return {"id": self.id, "inventory_item_id": self.inventory_item_id,
                "batch_number": self.batch_number,
                "expiry_date": self.expiry_date.isoformat() if self.expiry_date else None,
                "quantity_on_hand": self.quantity_on_hand,
                "unit_cost": float(self.unit_cost) if self.unit_cost is not None else None,
                "quarantined": bool(self.quarantined),
                "received_at": self.received_at.isoformat() if self.received_at else None}


class ItemVariant(TenantMixin, db.Model):
    """A size or colour of an item. An item has variants or batches, never both."""
    __tablename__ = "item_variants"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "inventory_item_id", "label",
                            name="uq_item_variants_tenant_item_label"),
    )

    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey("inventory_items.id", ondelete="CASCADE"),
                                  nullable=False, index=True)
    label             = db.Column(db.String(64), nullable=False)
    price_override    = db.Column(db.Numeric(12, 2), nullable=True)
    stock             = db.Column(db.Integer, nullable=False, default=0)

    def to_dict(self):
        return {"id": self.id, "inventory_item_id": self.inventory_item_id, "label": self.label,
                "price_override": float(self.price_override) if self.price_override is not None else None,
                "stock": self.stock}


class LineBatch(TenantMixin, db.Model):
    """How many units of one sale/order line came out of one batch, so a
    void or return puts them back in exactly that batch."""
    __tablename__ = "line_batches"

    id                  = db.Column(db.Integer, primary_key=True, autoincrement=True)
    transaction_item_id = db.Column(db.Integer, db.ForeignKey("transaction_items.id", ondelete="CASCADE"),
                                    nullable=True, index=True)
    order_item_id       = db.Column(db.Integer, db.ForeignKey("order_items.id", ondelete="CASCADE"),
                                    nullable=True, index=True)
    batch_id            = db.Column(db.Integer, db.ForeignKey("batches.id", ondelete="CASCADE"),
                                    nullable=False, index=True)
    quantity            = db.Column(db.Integer, nullable=False)
    returned_quantity   = db.Column(db.Integer, nullable=False, default=0)
