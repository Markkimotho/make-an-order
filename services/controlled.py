"""Controlled-drug register entries (models/pharmacy.py).

Called only from services/stock.py, inside the same transaction as the
stock change and after the item row is locked, so entries for one drug are
written one at a time: ``seq`` counts up per drug without gaps and each
row's opening balance is the previous row's closing balance.

What an entry must carry depends on its type (PPB register):

* dispense: prescriber name and registration, patient name, National ID
  and address, and the dispensing pharmacist (with a PPB number on file).
* receipt: supplier and the delivery note or invoice number.
* return, adjustment, destruction: a witness (name; registration for
  adjustments and destruction).
* opening: nothing (first entry when an item becomes controlled).

Missing details raise :class:`RegisterIncomplete` (a StockError) before
anything is written, so a controlled sale without a complete register
entry cannot exist.
"""
from dataclasses import dataclass, field, fields
from datetime import datetime

from models import ControlledDrugEntry, User, db
from services.stock import StockError

REQUIRED = {
    "dispense": ("prescriber_name", "prescriber_reg_no", "patient_name",
                 "patient_national_id", "patient_address", "pharmacist_name",
                 "pharmacist_reg_no"),
    "receipt": ("supplier_name", "reference_doc_no"),
    "return": ("witness_name",),
    "adjustment": ("witness_name", "witness_reg_no"),
    "destruction": ("witness_name", "witness_reg_no"),
    "opening": (),
}
LABELS = {
    "prescriber_name": "prescriber's name", "prescriber_reg_no": "prescriber's registration no.",
    "patient_name": "patient's name", "patient_national_id": "patient's National ID",
    "patient_address": "patient's address", "pharmacist_name": "pharmacist",
    "pharmacist_reg_no": "pharmacist's PPB registration no.",
    "supplier_name": "supplier", "reference_doc_no": "delivery note / invoice no.",
    "witness_name": "witness's name", "witness_reg_no": "witness's registration no.",
}


@dataclass
class Register:
    """The details a controlled movement is recorded with."""
    prescriber_name: str | None = None
    prescriber_reg_no: str | None = None
    patient_name: str | None = None
    patient_national_id: str | None = None
    patient_address: str | None = None
    pharmacist_id: int | None = None
    pharmacist_name: str | None = None
    pharmacist_reg_no: str | None = None
    supplier_name: str | None = None
    reference_doc_no: str | None = None
    witness_name: str | None = None
    witness_reg_no: str | None = None
    prescription_id: int | None = None
    transaction_id: int | None = None
    notes: str | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_request(cls, data, **known):
        """Fill from a JSON body (``register`` object or top-level keys)."""
        data = data or {}
        source = data.get("register") if isinstance(data.get("register"), dict) else data
        values = {f.name: (str(source.get(f.name)).strip() or None)
                  for f in fields(cls) if f.name not in ("extra", "pharmacist_id",
                                                         "prescription_id", "transaction_id")
                  and source.get(f.name) not in (None, "")}
        values.update({k: v for k, v in known.items() if v is not None})
        return cls(**values)

    def with_pharmacist(self, user):
        """Snapshot the dispensing pharmacist (name and PPB number)."""
        if user is not None:
            self.pharmacist_id = user.id
            self.pharmacist_name = user.name or user.email
            self.pharmacist_reg_no = user.ppb_reg_no
        return self


class RegisterIncomplete(StockError):
    def __init__(self, item_name, entry_type, missing):
        self.missing = list(missing)
        super().__init__(f"{item_name} is a controlled drug; the register needs: "
                         + ", ".join(LABELS.get(m, m) for m in self.missing))


def check(item, entry_type, register):
    """Raise RegisterIncomplete if ``register`` lacks what ``entry_type`` needs."""
    if register is None:
        raise RegisterIncomplete(item.name, entry_type, REQUIRED[entry_type] or ("details",))
    missing = [name for name in REQUIRED[entry_type] if not getattr(register, name, None)]
    if missing:
        raise RegisterIncomplete(item.name, entry_type, missing)


def _last(item_id):
    return (ControlledDrugEntry.query.filter_by(inventory_item_id=item_id)
            .order_by(ControlledDrugEntry.seq.desc()).first())


def record(item, entry_type, movements, register, stock_before, reverses=None):
    """Write one entry per (batch, quantity_in, quantity_out). Returns them.

    ``stock_before`` is the item's total before this change; it opens the
    register for a drug with no entries yet. ``reverses`` maps batch id to
    the entry a return reverses.
    """
    check(item, entry_type, register)
    last = _last(item.id)
    seq = last.seq if last else 0
    balance = last.closing_balance if last else int(stock_before)
    rows = []
    now = datetime.utcnow()
    for batch, qty_in, qty_out in movements:
        if not qty_in and not qty_out and entry_type != "opening":
            continue
        seq += 1
        row = ControlledDrugEntry(
            inventory_item_id=item.id, seq=seq, entry_type=entry_type, entry_date=now,
            drug_name=item.name, drug_schedule=item.drug_schedule,
            batch_id=batch.id if batch is not None else None,
            batch_number=batch.batch_number if batch is not None else None,
            quantity_in=int(qty_in), quantity_out=int(qty_out),
            opening_balance=balance, closing_balance=balance + int(qty_in) - int(qty_out),
            reverses_entry_id=(reverses or {}).get(batch.id if batch is not None else None),
            **{f.name: getattr(register, f.name) for f in fields(Register)
               if f.name != "extra"} if register is not None else {})
        balance = row.closing_balance
        db.session.add(row)
        rows.append(row)
    db.session.flush()
    return rows


def open_register(item, user=None):
    """First entries for an item that has just become controlled: one
    "opening" row per stocked batch, so the running balance starts at the
    stock on hand. Does nothing if the drug already has entries."""
    if _last(item.id) is not None:
        return []
    from models import Batch
    batches = (Batch.query.filter(Batch.inventory_item_id == item.id, Batch.quantity_on_hand > 0)
               .order_by(Batch.id).all())
    register = Register(notes="register opened").with_pharmacist(user)
    if not batches:
        return record(item, "opening", [(None, 0, 0)], register, 0)
    last_seq, rows = 0, []
    balance = 0
    for batch in batches:
        last_seq += 1
        row = ControlledDrugEntry(
            inventory_item_id=item.id, seq=last_seq, entry_type="opening",
            entry_date=datetime.utcnow(), drug_name=item.name, drug_schedule=item.drug_schedule,
            batch_id=batch.id, batch_number=batch.batch_number,
            quantity_in=batch.quantity_on_hand, quantity_out=0,
            opening_balance=balance, closing_balance=balance + batch.quantity_on_hand,
            pharmacist_id=register.pharmacist_id, pharmacist_name=register.pharmacist_name,
            pharmacist_reg_no=register.pharmacist_reg_no, notes=register.notes)
        balance = row.closing_balance
        db.session.add(row)
        rows.append(row)
    db.session.flush()
    return rows


def dispense_entries(transaction_id, item_id):
    """{batch_id: entry} for a sale's dispense rows of one drug."""
    rows = (ControlledDrugEntry.query
            .filter_by(transaction_id=transaction_id, inventory_item_id=item_id,
                       entry_type="dispense").all())
    return {r.batch_id: r for r in rows}


def pharmacist(user_id):
    return db.session.get(User, user_id) if user_id else None
