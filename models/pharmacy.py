"""The controlled-drug register: one row per movement of a controlled
drug, per batch, append-only, with a running balance per drug.

Rows are written only by services/controlled.py, from inside
services/stock.py, so a controlled drug's stock cannot change without its
register entry. Corrections are new rows (``reverses_entry_id``); updating
or deleting a row raises.
"""
from datetime import datetime

from sqlalchemy import event

from models.base import TenantMixin, db

ENTRY_TYPES = ("opening", "receipt", "dispense", "return", "adjustment", "destruction")


class RegisterIsAppendOnly(RuntimeError):
    """A controlled-drug register row was changed or deleted."""


class ControlledDrugEntry(TenantMixin, db.Model):
    __tablename__ = "controlled_drug_ledger"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "inventory_item_id", "seq",
                            name="uq_controlled_drug_ledger_tenant_item_seq"),
    )

    id                 = db.Column(db.Integer, primary_key=True, autoincrement=True)
    inventory_item_id  = db.Column(db.Integer, db.ForeignKey("inventory_items.id", ondelete="CASCADE"),
                                   nullable=False, index=True)
    seq                = db.Column(db.Integer, nullable=False)
    entry_type         = db.Column(db.String(16), nullable=False)
    entry_date         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    drug_name          = db.Column(db.String(255), nullable=False)
    drug_schedule      = db.Column(db.String(16), nullable=True)
    batch_id           = db.Column(db.Integer, db.ForeignKey("batches.id", ondelete="SET NULL"),
                                   nullable=True)
    batch_number       = db.Column(db.String(64), nullable=True)
    quantity_in        = db.Column(db.Integer, nullable=False, default=0)
    quantity_out       = db.Column(db.Integer, nullable=False, default=0)
    opening_balance    = db.Column(db.Integer, nullable=False)
    closing_balance    = db.Column(db.Integer, nullable=False)
    # Dispensing: who prescribed, who received, who dispensed (snapshots).
    prescription_id    = db.Column(db.Integer, db.ForeignKey("prescriptions.id", ondelete="SET NULL"),
                                   nullable=True)
    transaction_id     = db.Column(db.Integer, db.ForeignKey("transactions.id", ondelete="SET NULL"),
                                   nullable=True)
    prescriber_name    = db.Column(db.String(255), nullable=True)
    prescriber_reg_no  = db.Column(db.String(64), nullable=True)
    patient_name       = db.Column(db.String(255), nullable=True)
    patient_national_id = db.Column(db.String(32), nullable=True)
    patient_address    = db.Column(db.String(255), nullable=True)
    pharmacist_id      = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                                   nullable=True)
    pharmacist_name    = db.Column(db.String(255), nullable=True)
    pharmacist_reg_no  = db.Column(db.String(64), nullable=True)
    # Receipts: from whom, on which document. Adjustments: who witnessed.
    supplier_name      = db.Column(db.String(255), nullable=True)
    reference_doc_no   = db.Column(db.String(64), nullable=True)
    witness_name       = db.Column(db.String(255), nullable=True)
    witness_reg_no     = db.Column(db.String(64), nullable=True)
    notes              = db.Column(db.String(255), nullable=True)
    reverses_entry_id  = db.Column(db.Integer, db.ForeignKey(
        "controlled_drug_ledger.id", ondelete="SET NULL",
        name="fk_controlled_drug_ledger_reverses_entry_id"), nullable=True)   # 64-char limit

    def to_dict(self):
        out = {c.name: getattr(self, c.name) for c in self.__table__.columns
               if c.name != "tenant_id"}
        out["entry_date"] = self.entry_date.isoformat() if self.entry_date else None
        return out


@event.listens_for(ControlledDrugEntry, "before_update")
def _no_update(mapper, connection, target):
    raise RegisterIsAppendOnly("controlled-drug register rows cannot be changed; "
                               "write a correcting entry")


@event.listens_for(ControlledDrugEntry, "before_delete")
def _no_delete(mapper, connection, target):
    raise RegisterIsAppendOnly("controlled-drug register rows cannot be deleted")
