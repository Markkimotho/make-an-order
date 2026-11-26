"""Cashier reconciliation: does the money counted match what the system says?

Per shift:

* Cash: opening float + cash sales + cash in - cash out = expected cash;
  counted cash (``closing_cash``) - expected = variance. Cash sales are
  completed cash sales plus the cash part of split sales (the same figure
  the shift close uses, ``cash_sales``). A closed shift reports the
  expected cash and variance stored when it closed, which is what the
  cashier was measured against.
* M-Pesa: the system's M-Pesa sales (sales marked M-Pesa plus the M-Pesa
  part of split sales) against confirmed M-Pesa payments (STK or Paybill,
  verified) and codes typed at the till still unverified. ``unexplained``
  is what neither covers (for example a code that was rejected).
* Bank: references recorded on the shift's sales, and how many are not yet
  checked against the statement.

Per day: the same, for shifts opened on that Nairobi day, grouped by
cashier. A variance whose size is over the business's
``cash_variance_alert`` is flagged (and texted to the admins on close).
"""
import logging
from collections import defaultdict
from decimal import Decimal

from auth.tenancy import current_tenant_id
from models import (
    CashMovement, Invoice, Payment, PaymentAllocation, Shift, Tenant, Transaction, User,
    ROLE_ADMIN, ROLE_SUPERUSER, SHIFT_CLOSED, TXN_COMPLETED, db,
)
from services.reports.profit import d, money, utc_bounds

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
CONFIRMED_MPESA = ("mpesa_stk", "mpesa_c2b")
MPESA = ("mpesa_stk", "mpesa_c2b", "mpesa_manual")


def variance_alert():
    """The business's alert threshold (KES)."""
    tenant = db.session.get(Tenant, current_tenant_id())
    return d(tenant.cash_variance_alert if tenant is not None else 500)


def _sales(shift_id):
    return (Transaction.query.filter(Transaction.shift_id == shift_id,
                                     Transaction.status == TXN_COMPLETED).all())


def _cash_taken_sales(shift_id):
    """Every sale this shift took money for, including ones refunded or
    voided later: cash handed back is a cash-out on the drawer it left (see
    api/pos.py _hand_back), so the cash taken here stays counted in full."""
    return (Transaction.query.filter(Transaction.shift_id == shift_id,
                                     Transaction.payment_method.isnot(None),
                                     Transaction.status.in_((TXN_COMPLETED, "refunded",
                                                             "voided"))).all())


def _handed_back(payment_ids):
    """Cash already returned from these payments (refunds and voids lower
    a payment's amount and log it as payment.refund)."""
    from models import AuditLog
    out = defaultdict(lambda: ZERO)
    if not payment_ids:
        return out
    for row in AuditLog.query.filter(AuditLog.action == "payment.refund",
                                     AuditLog.entity_type == "Payment",
                                     AuditLog.entity_id.in_(list(payment_ids))):
        out[row.entity_id] += d((row.new_value or {}).get("amount", 0))
    return out


def _live_payments(sales):
    """{sale id: [(payment, live allocation amount)]} for confirmed payments
    on these sales' (non-void) invoices."""
    out = defaultdict(list)
    ids = [t.id for t in sales]
    if not ids:
        return out
    invoices = {i.id: i.transaction_id for i in Invoice.query.filter(
        Invoice.transaction_id.in_(ids), Invoice.status != "void").all()}
    if not invoices:
        return out
    allocations = (PaymentAllocation.query
                   .filter(PaymentAllocation.invoice_id.in_(list(invoices)),
                           PaymentAllocation.voided_at.is_(None))
                   .order_by(PaymentAllocation.id.asc()).all())
    payments = {p.id: p for p in Payment.query.filter(
        Payment.id.in_({a.payment_id for a in allocations})).all()} if allocations else {}
    for a in allocations:
        p = payments.get(a.payment_id)
        if p is not None and p.status == "confirmed":
            out[invoices[a.invoice_id]].append((p, d(a.amount)))
    return out


def _cash_sales(shift_id):
    """Cash the shift's sales put in the drawer, as taken at the sale: cash
    sales at their total, plus the cash part of split sales (what is still
    allocated plus what was handed back since)."""
    sales = _cash_taken_sales(shift_id)
    cash = sum((d(t.total) for t in sales if t.payment_method == "cash"), ZERO)
    split = [t.id for t in sales if t.payment_method == "split"]
    if not split:
        return cash
    # Cash payments ever put on these sales' invoices, at what they were
    # when taken: today's amount plus anything handed back since.
    invoice_ids = [i.id for i in Invoice.query.filter(Invoice.transaction_id.in_(split))]
    payment_ids = {a.payment_id for a in PaymentAllocation.query.filter(
        PaymentAllocation.invoice_id.in_(invoice_ids))} if invoice_ids else set()
    payments = [p for p in Payment.query.filter(Payment.id.in_(payment_ids))
                if p.method == "cash"] if payment_ids else []
    back = _handed_back({p.id for p in payments})
    return cash + sum((d(p.amount) + back[p.id] for p in payments), ZERO)


def cash_sales(shift):
    """Cash a shift's sales put in the drawer (see _cash_sales)."""
    return _cash_sales(shift.id)


def _movements(shift_id):
    cash_in = cash_out = ZERO
    for m in CashMovement.query.filter(CashMovement.shift_id == shift_id).all():
        if m.movement_type == "cash_in":
            cash_in += d(m.amount)
        elif m.movement_type == "cash_out":
            cash_out += d(m.amount)
    return cash_in, cash_out


def _shift_numbers(shift):
    """Decimal figures for one shift (see the module docstring)."""
    sales = _sales(shift.id)
    paid = _live_payments(sales)
    cash = _cash_sales(shift.id)
    cash_in, cash_out = _movements(shift.id)
    computed = d(shift.opening_float) + cash + cash_in - cash_out
    closed = shift.status == SHIFT_CLOSED
    expected = d(shift.expected_cash) if closed and shift.expected_cash is not None else computed
    counted = d(shift.closing_cash) if shift.closing_cash is not None else None
    variance = (d(shift.cash_variance) if shift.cash_variance is not None
                else (counted - expected if counted is not None else None))

    # M-Pesa and bank money on sales rung up as such (a credit sale paid off
    # later by M-Pesa is debt collected, not this shift's M-Pesa sales).
    mpesa_paid = [(p, a) for t in sales if t.payment_method in ("mpesa", "split")
                  for p, a in paid[t.id] if p.method in MPESA]
    mpesa_system = (sum((d(t.total) for t in sales if t.payment_method == "mpesa"), ZERO)
                    + sum((a for t in sales if t.payment_method == "split"
                           for p, a in paid[t.id] if p.method in MPESA), ZERO))
    confirmed = sum((a for p, a in mpesa_paid
                     if p.method in CONFIRMED_MPESA and p.verified), ZERO)
    unverified = [(p, a) for p, a in mpesa_paid
                  if not (p.method in CONFIRMED_MPESA and p.verified)]
    unverified_amount = sum((a for _, a in unverified), ZERO)
    bank = [(p, a) for t in sales if t.payment_method == "bank"
            for p, a in paid[t.id] if p.method == "bank"]
    bank_unverified = [(p, a) for p, a in bank if not p.verified]
    return {
        "opening_float": d(shift.opening_float), "cash_sales": cash, "cash_in": cash_in,
        "cash_out": cash_out, "expected_cash": expected, "counted_cash": counted,
        "variance": variance, "sales_count": len(sales),
        "mpesa_system": mpesa_system, "mpesa_confirmed": confirmed,
        "mpesa_unverified": unverified_amount,
        "mpesa_unexplained": mpesa_system - confirmed - unverified_amount,
        "mpesa_unverified_codes": [{"payment_id": p.id, "receipt": p.mpesa_receipt,
                                    "method": p.method, "amount": money(a)}
                                   for p, a in unverified],
        "bank_total": sum((a for _, a in bank), ZERO), "bank_refs": len(bank),
        "bank_unverified_count": len(bank_unverified),
        "bank_unverified": sum((a for _, a in bank_unverified), ZERO),
        "bank_unverified_refs": [{"payment_id": p.id, "reference": p.bank_reference,
                                  "bank": p.bank_name, "amount": money(a)}
                                 for p, a in bank_unverified],
    }


_MONEY_KEYS = ("opening_float", "cash_sales", "cash_in", "cash_out", "expected_cash",
               "mpesa_system", "mpesa_confirmed", "mpesa_unverified", "mpesa_unexplained",
               "bank_total", "bank_unverified")


def _out(numbers, threshold):
    out = dict(numbers)
    for k in _MONEY_KEYS:
        out[k] = money(numbers[k])
    for k in ("counted_cash", "variance"):
        out[k] = money(numbers[k]) if numbers[k] is not None else None
    v = numbers["variance"]
    out["over_threshold"] = v is not None and abs(v) > threshold
    return out


def _cashier_name(user_id):
    user = db.session.get(User, user_id) if user_id else None
    return (user.name or user.email) if user is not None else None


def shift_report(shift, threshold=None):
    """Reconciliation for one shift (open or closed)."""
    threshold = variance_alert() if threshold is None else threshold
    out = _out(_shift_numbers(shift), threshold)
    out.update({"shift_id": shift.id, "cashier_id": shift.cashier_id,
                "cashier_name": _cashier_name(shift.cashier_id), "status": shift.status,
                "opened_at": shift.opened_at.isoformat() if shift.opened_at else None,
                "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
                "variance_alert": money(threshold)})
    return out


_SUM_KEYS = ("opening_float", "cash_sales", "cash_in", "cash_out", "expected_cash",
             "mpesa_system", "mpesa_confirmed", "mpesa_unverified", "mpesa_unexplained",
             "bank_total", "bank_refs", "bank_unverified_count", "bank_unverified", "sales_count")


def day_report(day):
    """Every shift opened on Nairobi ``day``, grouped by cashier, with totals."""
    threshold = variance_alert()
    start, end = utc_bounds(day, day)
    shifts = (Shift.query.filter(Shift.opened_at >= start, Shift.opened_at < end)
              .order_by(Shift.opened_at.asc(), Shift.id.asc()).all())
    groups = defaultdict(list)
    for shift in shifts:
        groups[shift.cashier_id].append((shift, _shift_numbers(shift)))

    def total(rows):
        t = {k: ZERO for k in _SUM_KEYS}
        counted = variance = None
        for _, n in rows:
            for k in _SUM_KEYS:
                t[k] += n[k]
            if n["counted_cash"] is not None:
                counted = (counted or ZERO) + n["counted_cash"]
            if n["variance"] is not None:
                variance = (variance or ZERO) + n["variance"]
        t["counted_cash"], t["variance"] = counted, variance
        t["mpesa_unverified_codes"] = [c for _, n in rows for c in n["mpesa_unverified_codes"]]
        t["bank_unverified_refs"] = [c for _, n in rows for c in n["bank_unverified_refs"]]
        out = _out(t, threshold)
        for k in ("bank_refs", "bank_unverified_count", "sales_count"):
            out[k] = int(t[k])
        out["open_shifts"] = sum(1 for s, _ in rows if s.status != SHIFT_CLOSED)
        # A single shift over the line flags its cashier, even if another
        # shift's opposite variance cancels it out in the sum.
        out["over_threshold"] = out["over_threshold"] or any(
            n["variance"] is not None and abs(n["variance"]) > threshold for _, n in rows)
        return out

    cashiers = []
    for cashier_id, rows in groups.items():
        cashiers.append({"cashier_id": cashier_id, "cashier_name": _cashier_name(cashier_id),
                         **total(rows),
                         "shifts": [shift_report(s, threshold) for s, _ in rows]})
    cashiers.sort(key=lambda c: (c["cashier_name"] or "", c["cashier_id"] or 0))
    all_rows = [r for rows in groups.values() for r in rows]
    return {"date": day.isoformat(), "variance_alert": money(threshold),
            "cashiers": cashiers, "totals": total(all_rows)}


def alert_variance(shift):
    """Text the admins when a closed shift's variance is over the business's
    alert level. Once per shift and admin (dedupe ``variance:<shift>:<phone>``).
    Call after the commit: it goes through the SMS outbox, which commits.
    Returns how many admins were texted (0 when under the line)."""
    from services.sms_service import send

    if shift.cash_variance is None:
        return 0
    variance = d(shift.cash_variance)
    threshold = variance_alert()
    if abs(variance) <= threshold:
        return 0
    tenant = db.session.get(Tenant, current_tenant_id())
    phones = [u.phone_number for u in
              User.query.filter(User.role.in_((ROLE_ADMIN, ROLE_SUPERUSER)),
                                User.phone_number.isnot(None)).all()]
    phones = [p for p in dict.fromkeys(phones) if p]
    word = "SHORT" if variance < 0 else "OVER"
    body = (f"CASH {word}: {_cashier_name(shift.cashier_id) or 'cashier'} closed shift "
            f"#{shift.id} with KES {money(d(shift.closing_cash)):,.2f} counted vs "
            f"KES {money(d(shift.expected_cash)):,.2f} expected "
            f"({'-' if variance < 0 else '+'}KES {money(abs(variance)):,.2f}). "
            f"-{tenant.name if tenant else 'POS'}")
    for phone in phones:
        send(phone, body, "cash_variance", dedupe_key=f"variance:{shift.id}:{phone}")
    if not phones:
        logger.warning("cash variance on shift %s: no admin phone to text", shift.id)
    return len(phones)
