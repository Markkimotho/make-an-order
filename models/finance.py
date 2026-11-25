"""Money that leaves the business other than through stock: rent, wages,
power. Net profit is gross profit less these (services/reports)."""
from datetime import date

from models.base import TenantMixin, db

EXPENSE_CATEGORIES = ("rent", "salaries", "utilities", "transport", "licences", "supplies",
                      "repairs", "marketing", "bank_charges", "other")
EXPENSE_METHODS = ("cash", "mpesa", "bank")


class Expense(TenantMixin, db.Model):
    __tablename__ = "expenses"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    spent_on   = db.Column(db.Date, nullable=False, default=date.today, index=True)
    category   = db.Column(db.String(20), nullable=False)
    amount     = db.Column(db.Numeric(12, 2), nullable=False)
    method     = db.Column(db.String(10), nullable=False, default="cash")
    # Cash paid out of the drawer is also a cash movement on that shift.
    shift_id   = db.Column(db.Integer, db.ForeignKey("shifts.id", ondelete="SET NULL"),
                           nullable=True)
    note       = db.Column(db.String(255), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                           nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

    def to_dict(self):
        return {"id": self.id, "spent_on": self.spent_on.isoformat(), "category": self.category,
                "amount": float(self.amount), "method": self.method, "shift_id": self.shift_id,
                "note": self.note, "created_by": self.created_by,
                "created_at": self.created_at.isoformat() if self.created_at else None}
