"""Pharmacy queries and rules: refill cycles, the refill schedule, the
expiry radar and the controlled-drug register report.

Dates are Nairobi dates (services/clock.py). Runs in the bound business.
"""
import secrets
from datetime import datetime, timedelta

from models import (Batch, ControlledDrugEntry, InventoryItem, Order, Prescription, db)
from services.clock import today_nairobi

REFILL_STATUSES = ("active", "paused", "ended")
DURATIONS = (14, 30, 90)
EXPIRY_BUCKETS = (30, 60, 90)


class PharmacyError(ValueError):
    pass


def new_refill_token():
    return secrets.token_urlsafe(16)[:22]


def make_chronic(rx, duration_days, refills_remaining=None):
    if int(duration_days) not in DURATIONS:
        raise PharmacyError(f"duration_days must be one of {DURATIONS}")
    rx.is_chronic = True
    rx.duration_days = int(duration_days)
    rx.refills_remaining = None if refills_remaining in (None, "") else int(refills_remaining)
    rx.refill_status = "active"
    rx.refill_token = rx.refill_token or new_refill_token()
    return rx


def cycle_complete(rx):
    return bool(rx.items) and all(it.is_dispensed for it in rx.items)


def start_new_cycle(rx):
    """A chronic prescription dispensed again (a refill): reset what was
    dispensed and use up one refill. ``refills_remaining`` counts refills
    after the first fill; None means no limit."""
    if not rx.is_chronic:
        raise PharmacyError("prescription already dispensed")
    if rx.refill_status != "active":
        raise PharmacyError(f"refills are {rx.refill_status}")
    if rx.refills_remaining is not None:
        if rx.refills_remaining <= 0:
            raise PharmacyError("no refills left on this prescription")
        rx.refills_remaining -= 1
    for it in rx.items:
        it.quantity_dispensed = 0
        it.is_dispensed = False
    rx.transaction_id = None
    return rx


def mark_dispensed(rx, now=None):
    """A cycle finished: set the next due date, or end the refills when
    none are left."""
    now = now or datetime.utcnow()
    rx.last_dispensed_at = now
    rx.callback_requested_at = None
    if not (rx.is_chronic and rx.duration_days):
        return rx
    if rx.refills_remaining is not None and rx.refills_remaining <= 0:
        rx.refill_status = "ended"
        rx.refill_due_date = None
    else:
        rx.refill_due_date = today_nairobi(now) + timedelta(days=rx.duration_days)
    return rx


def refill_schedule(today=None):
    """Active chronic prescriptions: overdue, due today, due in 7 days."""
    today = today or today_nairobi()
    rows = (Prescription.query
            .filter(Prescription.is_chronic.is_(True), Prescription.refill_status == "active",
                    Prescription.refill_due_date.isnot(None),
                    Prescription.refill_due_date <= today + timedelta(days=7))
            .order_by(Prescription.refill_due_date, Prescription.id).all())
    out = {"overdue": [], "today": [], "next_7_days": []}
    for rx in rows:
        key = ("overdue" if rx.refill_due_date < today
               else "today" if rx.refill_due_date == today else "next_7_days")
        out[key].append(_rx_summary(rx, today))
    return out


def callbacks_requested():
    rows = (Prescription.query.filter(Prescription.callback_requested_at.isnot(None))
            .order_by(Prescription.callback_requested_at).all())
    return [_rx_summary(rx, today_nairobi()) for rx in rows]


def _rx_summary(rx, today):
    customer = rx.customer
    return {"id": rx.id, "customer_id": rx.customer_id,
            "customer": customer.name if customer else None,
            "phone": customer.phone_number if customer else None,
            "refill_due_date": rx.refill_due_date.isoformat() if rx.refill_due_date else None,
            "days": (rx.refill_due_date - today).days if rx.refill_due_date else None,
            "refills_remaining": rx.refills_remaining,
            "controlled": has_controlled(rx),
            "callback_requested_at": (rx.callback_requested_at.isoformat()
                                      if rx.callback_requested_at else None),
            "items": [{"name": it_name(it), "quantity": float(it.quantity_prescribed)}
                      for it in rx.items]}


def it_name(prescription_item):
    item = (db.session.get(InventoryItem, prescription_item.inventory_item_id)
            if prescription_item.inventory_item_id else None)
    return item.name if item else "(removed item)"


def has_controlled(rx):
    for it in rx.items:
        item = db.session.get(InventoryItem, it.inventory_item_id) if it.inventory_item_id else None
        if item is not None and item.is_controlled:
            return True
    return False


def open_refill_order(rx):
    """A refill order for this prescription still waiting for payment."""
    return (Order.query.filter(Order.prescription_id == rx.id,
                               Order.fulfillment_status == "pending",
                               Order.reserved_until.isnot(None))
            .order_by(Order.id.desc()).first())


def expiry_radar(today=None):
    """Stocked batches by days to expiry: expired, <=30, 31-60, 61-90."""
    today = today or today_nairobi()
    rows = (db.session.query(Batch, InventoryItem)
            .join(InventoryItem, InventoryItem.id == Batch.inventory_item_id)
            .filter(Batch.quantity_on_hand > 0, Batch.expiry_date.isnot(None),
                    Batch.expiry_date <= today + timedelta(days=EXPIRY_BUCKETS[-1]))
            .order_by(Batch.expiry_date, Batch.id).all())
    out = {"expired": [], "30": [], "60": [], "90": []}
    for batch, item in rows:
        days = (batch.expiry_date - today).days
        key = "expired" if days < 0 else next(str(b) for b in EXPIRY_BUCKETS if days <= b)
        cost = batch.unit_cost if batch.unit_cost is not None else (item.cost_price or 0)
        out[key].append({"item_id": item.id, "name": item.name, "batch_id": batch.id,
                         "batch_number": batch.batch_number,
                         "expiry_date": batch.expiry_date.isoformat(), "days": days,
                         "quantity": batch.quantity_on_hand,
                         "cost_at_risk": float(cost) * batch.quantity_on_hand})
    return out


def register_rows(item_id=None, date_from=None, date_to=None):
    q = ControlledDrugEntry.query
    if item_id:
        q = q.filter(ControlledDrugEntry.inventory_item_id == int(item_id))
    if date_from:
        q = q.filter(ControlledDrugEntry.entry_date >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        q = q.filter(ControlledDrugEntry.entry_date
                     < datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
    return q.order_by(ControlledDrugEntry.drug_name, ControlledDrugEntry.inventory_item_id,
                      ControlledDrugEntry.seq).all()


def register_breaks(rows):
    """Entries whose opening balance is not the previous closing balance
    (should never happen; shown so an inspection finds it first)."""
    breaks, last = [], {}
    for row in rows:
        prev = last.get(row.inventory_item_id)
        if prev is not None and (row.seq != prev.seq + 1
                                 or row.opening_balance != prev.closing_balance):
            breaks.append(row.id)
        last[row.inventory_item_id] = row
    return breaks
