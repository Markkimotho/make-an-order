"""Patients and their medication history.

A patient is a customer of the pharmacy (same ``customers`` table). What a
patient received comes from three places:

* counter sales: completed POS lines for the patient (voided sales are left
  out; refunded quantities are taken off),
* prescriptions: the same POS lines, when the sale was the one a
  prescription was dispensed against (the line then carries the prescriber),
* refills: paid refill orders (``Order.prescription_id``) handed over.

Months are Nairobi calendar months (services/clock.py). Runs in the bound
business: every query here is tenant scoped.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func

from models import (AuditLog, Batch, Customer, LineBatch, Order, OrderItem, Prescription,
                    Transaction, TransactionItem, User, db)
from models.core import TXN_COMPLETED, TXN_REFUNDED
from services.clock import NAIROBI, now_nairobi, today_nairobi
from verticals.pharmacy.service import it_name

MAX_MONTHS = 36
SALE_STATUSES = (TXN_COMPLETED, TXN_REFUNDED)     # a refunded sale is a completed one returned
REFILL_DONE = ("collected",)                      # what hand-over leaves an order in
ORDER_UNDONE = ("cancelled", "refunded")          # Order.status of an order taken back
SOURCE_LABELS = {"counter": "Counter sale", "prescription": "Prescription",
                 "refill": "Refill order"}
ZERO = Decimal("0")


class HistoryError(ValueError):
    pass


# --- dates ------------------------------------------------------------------

def _utc_naive(local_day):
    """Nairobi midnight of ``local_day`` as a naive UTC datetime (how the app
    stores times)."""
    local = datetime.combine(local_day, time.min, tzinfo=NAIROBI)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _add_months(day, n):
    """First day of the month ``n`` months after ``day``'s month."""
    index = day.year * 12 + (day.month - 1) + n
    return date(index // 12, index % 12 + 1, 1)


def month_window(months, today=None):
    """(first day, first day after) of the last ``months`` Nairobi months,
    this month included."""
    today = today or today_nairobi()
    return _add_months(today, -(months - 1)), _add_months(today, 1)


def _local(dt):
    return now_nairobi(dt) if dt is not None else None


# --- search -----------------------------------------------------------------

def search_conditions(q):
    """Name, code or phone. Phones are stored 2547XXXXXXXX, so a typed 07...,
    +254... or 254... prefix is dropped before matching (as at the till)."""
    from services.phone import InvalidPhone, normalize_ke_phone
    q = (q or "").strip()
    conditions = [Customer.name.ilike(f"%{q}%"), Customer.code.ilike(f"{q}%")]
    digits = "".join(ch for ch in q if ch.isdigit())
    for prefix in ("254", "0"):
        if digits.startswith(prefix):
            digits = digits[len(prefix):]
            break
    if len(digits) >= 3:
        conditions.append(Customer.phone_number.like(f"%{digits}%"))
        try:
            conditions.append(Customer.phone_number == normalize_ke_phone(q))
        except InvalidPhone:
            pass
    return conditions


# --- the patient list ---------------------------------------------------------

def _max_by_customer(column, customer_col, filters, ids):
    q = db.session.query(customer_col, func.max(column)).filter(*filters, column.isnot(None))
    if ids is not None:
        q = q.filter(customer_col.in_(ids))
    return dict(q.group_by(customer_col).all())


def _last_visits(ids=None):
    """customer id -> latest naive UTC time they were served: a completed
    sale, a dispensed prescription or a refill handed over."""
    found = {}
    for source in (
        _max_by_customer(Transaction.created_at, Transaction.customer_id,
                         [Transaction.status.in_(SALE_STATUSES)], ids),
        _max_by_customer(Prescription.last_dispensed_at, Prescription.customer_id, [], ids),
        _max_by_customer(Order.picked_up_at, Order.customer_id,
                         [Order.prescription_id.isnot(None),
                          Order.fulfillment_status.in_(REFILL_DONE)], ids),
    ):
        for cid, when in source.items():
            if cid is not None and (cid not in found or when > found[cid]):
                found[cid] = when
    return found


def _active_chronic(ids):
    rows = (Prescription.query
            .filter(Prescription.customer_id.in_(ids), Prescription.is_chronic.is_(True),
                    Prescription.refill_status == "active")
            .all()) if ids else []
    out = {}
    for rx in rows:
        count, due = out.get(rx.customer_id, (0, None))
        if rx.refill_due_date is not None and (due is None or rx.refill_due_date < due):
            due = rx.refill_due_date
        out[rx.customer_id] = (count + 1, due)
    return out


def patient_list(q=None, limit=50, today=None):
    """Patients with their last visit, active chronic prescriptions and the
    next refill due. With ``q``: matches by name, phone or code, by name.
    Without: most recently served first, then everyone else by name."""
    limit = max(1, min(int(limit or 50), 200))
    today = today or today_nairobi()
    q = (q or "").strip()
    if q:
        customers = (Customer.query.filter(db.or_(*search_conditions(q)))
                     .order_by(Customer.name, Customer.id).limit(limit).all())
        visits = _last_visits([c.id for c in customers])
    else:
        visits = _last_visits()
        recent = sorted(visits, key=lambda cid: (visits[cid], cid), reverse=True)[:limit]
        by_id = {c.id: c for c in Customer.query.filter(Customer.id.in_(recent)).all()} \
            if recent else {}
        customers = [by_id[cid] for cid in recent if cid in by_id]
        if len(customers) < limit:
            customers += (Customer.query.filter(Customer.id.notin_(list(visits) or [0]))
                          .order_by(Customer.name, Customer.id)
                          .limit(limit - len(customers)).all())
    chronic = _active_chronic([c.id for c in customers])
    out = []
    for c in customers:
        count, due = chronic.get(c.id, (0, None))
        last = visits.get(c.id)
        out.append({"id": c.id, "name": c.name, "phone_number": c.phone_number,
                    "code": c.code,
                    "last_visit": _local(last).date().isoformat() if last else None,
                    "chronic_count": count,
                    "next_refill_due": due.isoformat() if due else None,
                    "days_to_refill": (due - today).days if due else None})
    return out


# --- medication history ---------------------------------------------------------

def _user_names(ids):
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: (u.name or u.email) for u in User.query.filter(User.id.in_(ids)).all()}


def _batches(column, line_ids):
    """line id -> ([{batch_number, expiry_date, quantity}], units returned)."""
    if not line_ids:
        return {}
    rows = (db.session.query(LineBatch, Batch)
            .join(Batch, Batch.id == LineBatch.batch_id)
            .filter(column.in_(line_ids))
            .order_by(LineBatch.id).all())
    out = {}
    for lb, batch in rows:
        entries, returned = out.get(getattr(lb, column.key), ([], 0))
        entries.append({"batch_number": batch.batch_number,
                        "expiry_date": batch.expiry_date.isoformat() if batch.expiry_date else None,
                        "quantity": lb.quantity})
        out[getattr(lb, column.key)] = (entries, returned + (lb.returned_quantity or 0))
    return out


def _dispensed_against(rx_list):
    """sale id -> prescriptions dispensed against it. The current cycle's
    sale is on the prescription; earlier refill cycles are in the audit log
    (``prescription.dispense`` records the sale each time)."""
    by_id = {rx.id: rx for rx in rx_list}
    links = {}
    for rx in rx_list:
        if rx.transaction_id:
            links.setdefault(rx.transaction_id, []).append(rx)
    if by_id:
        logged = (AuditLog.query
                  .filter(AuditLog.action == "prescription.dispense",
                          AuditLog.entity_type == "Prescription",
                          AuditLog.entity_id.in_(list(by_id)))
                  .all())
        for entry in logged:
            txn_id = (entry.new_value or {}).get("transaction_id") \
                if isinstance(entry.new_value, dict) else None
            rx = by_id.get(entry.entity_id)
            if isinstance(txn_id, int) and rx is not None and rx not in links.get(txn_id, []):
                links.setdefault(txn_id, []).append(rx)
    return links


def _rx_for_line(line, rxs):
    for rx in rxs or ():
        for it in rx.items:
            if it.inventory_item_id is not None and it.inventory_item_id == line.inventory_item_id:
                return rx, it
    return None, None


def _money(value):
    return float(Decimal(value).quantize(Decimal("0.01")))


def _sale_rows(customer_id, rx_links, start, end, names):
    txn_ids = list(rx_links)
    who = Transaction.customer_id == customer_id
    if txn_ids:
        who = db.or_(who, Transaction.id.in_(txn_ids))
    txns = (Transaction.query
            .filter(who, Transaction.status.in_(SALE_STATUSES),
                    Transaction.created_at >= start, Transaction.created_at < end)
            .all())
    lines = (TransactionItem.query
             .filter(TransactionItem.transaction_id.in_([t.id for t in txns]))
             .order_by(TransactionItem.id).all()) if txns else []
    by_txn = {t.id: t for t in txns}
    batches = _batches(LineBatch.transaction_item_id, [li.id for li in lines])
    names.update(_user_names([t.served_by_id or t.cashier_id for t in txns]))
    rows = []
    for li in lines:
        txn = by_txn[li.transaction_id]
        sold = Decimal(str(li.quantity))
        refunded = min(Decimal(str(li.refunded_quantity or 0)), sold)
        net = sold - refunded
        subtotal = Decimal(str(txn.subtotal or 0))
        ratio = Decimal(str(txn.total or 0)) / subtotal if subtotal > 0 else ZERO
        paid = Decimal(str(li.line_total)) * ratio
        spent = paid * net / sold if sold > 0 else ZERO
        rx, rx_item = _rx_for_line(li, rx_links.get(txn.id))
        entries, _ = batches.get(li.id, ([], 0))
        if not entries and li.batch_number:
            entries = [{"batch_number": li.batch_number, "expiry_date": None,
                        "quantity": None}]
        server = txn.served_by_id or txn.cashier_id
        rows.append({
            "when": txn.created_at, "source": "prescription" if rx else "counter",
            "item_name": li.item_name, "inventory_item_id": li.inventory_item_id,
            "quantity": float(net), "quantity_sold": float(sold),
            "refunded_quantity": float(refunded), "refunded": refunded > 0,
            "batches": entries,
            "prescription_id": rx.id if rx else None,
            "prescriber": rx.doctor_name if rx else None,
            "dosage": rx_item.dosage_instructions if rx_item else None,
            "served_by": names.get(server), "served_by_id": server,
            "reference": txn.transaction_ref, "amount": _money(spent),
        })
    return rows


def _refill_rows(customer_id, rx_by_id, start, end, names):
    who = Order.customer_id == customer_id
    if rx_by_id:
        who = db.or_(who, Order.prescription_id.in_(list(rx_by_id)))
    when = func.coalesce(Order.picked_up_at, Order.time)
    orders = (Order.query
              .filter(who, Order.prescription_id.isnot(None),
                      Order.fulfillment_status.in_(REFILL_DONE),
                      Order.status.notin_(ORDER_UNDONE),
                      when >= start, when < end)
              .all())
    items = (OrderItem.query.filter(OrderItem.order_id.in_([o.id for o in orders]))
             .order_by(OrderItem.id).all()) if orders else []
    by_order = {o.id: o for o in orders}
    batches = _batches(LineBatch.order_item_id, [oi.id for oi in items])
    names.update(_user_names([o.picked_up_by for o in orders]))
    rows = []
    for oi in items:
        order = by_order[oi.order_id]
        rx = rx_by_id.get(order.prescription_id) or db.session.get(Prescription,
                                                                   order.prescription_id)
        entries, returned = batches.get(oi.id, ([], 0))
        sold = int(oi.quantity or 0)
        returned = min(returned, sold)
        net = sold - returned
        rx_item = next((it for it in (rx.items if rx else ())
                        if it.inventory_item_id == oi.inventory_item_id), None)
        rows.append({
            "when": order.picked_up_at or order.time, "source": "refill",
            "item_name": oi.item_name, "inventory_item_id": oi.inventory_item_id,
            "quantity": float(net), "quantity_sold": float(sold),
            "refunded_quantity": float(returned), "refunded": returned > 0,
            "batches": entries,
            "prescription_id": order.prescription_id,
            "prescriber": rx.doctor_name if rx else None,
            "dosage": rx_item.dosage_instructions if rx_item else None,
            "served_by": names.get(order.picked_up_by), "served_by_id": order.picked_up_by,
            "reference": order.order_number,
            "amount": _money(Decimal(str(oi.subtotal)) * net / sold if sold else ZERO),
        })
    return rows


def current_medicines(customer_id, today=None):
    """Active chronic prescriptions with their items and refill dates."""
    today = today or today_nairobi()
    rows = (Prescription.query
            .filter(Prescription.customer_id == customer_id, Prescription.is_chronic.is_(True),
                    Prescription.refill_status == "active")
            .order_by(Prescription.refill_due_date.is_(None), Prescription.refill_due_date,
                      Prescription.id).all())
    out = []
    for rx in rows:
        due = rx.refill_due_date
        out.append({
            "prescription_id": rx.id, "prescriber": rx.doctor_name,
            "prescription_date": rx.prescription_date.isoformat() if rx.prescription_date else None,
            "duration_days": rx.duration_days, "refills_remaining": rx.refills_remaining,
            "next_refill_due": due.isoformat() if due else None,
            "days_to_refill": (due - today).days if due else None,
            "last_dispensed": (_local(rx.last_dispensed_at).date().isoformat()
                               if rx.last_dispensed_at else None),
            "items": [{"name": it_name(it), "dosage": it.dosage_instructions,
                       "quantity": float(it.quantity_prescribed)} for it in rx.items],
        })
    return out


def patient_card(customer):
    return {"id": customer.id, "name": customer.name, "phone_number": customer.phone_number,
            "email": customer.email, "code": customer.code,
            "physical_address": customer.physical_address}


def medication_history(customer_id, months=12, today=None):
    """Everything the patient received in the last ``months`` Nairobi months,
    grouped by month (newest first), plus their current medicines.

    Raises HistoryError for a months value outside 1..36. Returns None for
    an unknown patient (or one of another business)."""
    try:
        months = int(months)
    except (TypeError, ValueError):
        raise HistoryError("months must be a whole number") from None
    if not 1 <= months <= MAX_MONTHS:
        raise HistoryError(f"months must be between 1 and {MAX_MONTHS}")
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return None
    today = today or today_nairobi()
    first, after = month_window(months, today)
    start, end = _utc_naive(first), _utc_naive(after)

    rx_list = Prescription.query.filter(Prescription.customer_id == customer.id).all()
    rx_by_id = {rx.id: rx for rx in rx_list}
    names = {}
    rows = (_sale_rows(customer.id, _dispensed_against(rx_list), start, end, names)
            + _refill_rows(customer.id, rx_by_id, start, end, names))
    rows.sort(key=lambda r: (r["when"], r["reference"] or ""), reverse=True)

    grouped = {}
    for row in rows:
        local = _local(row.pop("when"))
        row["date"] = local.date().isoformat()
        row["time"] = local.strftime("%H:%M")
        row["source_label"] = SOURCE_LABELS[row["source"]]
        key = local.strftime("%Y-%m")
        month = grouped.setdefault(key, {"month": key, "label": local.strftime("%B %Y"),
                                         "items": 0, "total_spent": ZERO, "rows": []})
        month["rows"].append(row)
        if row["quantity"] > 0:
            month["items"] += 1
        month["total_spent"] += Decimal(str(row["amount"]))
    out_months = []
    for key in sorted(grouped, reverse=True):
        month = grouped[key]
        month["total_spent"] = _money(month["total_spent"])
        out_months.append(month)
    return {
        "patient": patient_card(customer),
        "window": {"months": months, "from": first.isoformat(),
                   "to": (after - timedelta(days=1)).isoformat()},
        "months": out_months,
        "total_spent": _money(sum((Decimal(str(m["total_spent"])) for m in out_months), ZERO)),
        "current_medicines": current_medicines(customer.id, today),
    }
