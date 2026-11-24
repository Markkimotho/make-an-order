"""Invoices: numbering, recomputation, credit notes and voiding.

Every function runs inside the bound tenant and leaves committing to the
caller, so a sale, its invoice and its payment commit together.
"""
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from auth.tenancy import current_tenant_id
from models import Invoice, InvoiceAdjustment, PaymentAllocation, Tenant, db
from services import audit_service

CENT = Decimal("0.01")
OWING = ("open", "partial")


class BillingError(ValueError):
    """A billing operation that cannot be done (bad amount, void invoice...)."""


def money(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def lock_tenant():
    """Serialise money-moving work within one business.

    Invoice numbering and receipt de-duplication read-then-write; holding
    the tenant row lock makes those steps safe under concurrent requests
    (MySQL row lock; SQLite already allows one writer at a time).
    """
    db.session.execute(select(Tenant.id).where(Tenant.id == current_tenant_id())
                       .with_for_update())


def lock_invoice(invoice):
    """Reload ``invoice`` from the database under a row lock."""
    return db.session.execute(
        select(Invoice).where(Invoice.id == invoice.id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one()


def _next_number():
    last = (db.session.query(Invoice.invoice_number)
            .order_by(Invoice.id.desc()).limit(1).scalar())
    n = int(last[3:]) + 1 if last and last[3:].isdigit() else 1
    return f"INV{n:06d}"


def create_invoice(*, total, kind="sale", customer_id=None, transaction_id=None,
                   order_id=None, due_date=None, notes=None):
    total = money(total)
    if total <= 0:
        raise BillingError("invoice total must be greater than zero")
    lock_tenant()
    invoice = Invoice(invoice_number=_next_number(), kind=kind, customer_id=customer_id,
                      transaction_id=transaction_id, order_id=order_id, total=total,
                      amount_paid=Decimal("0"), amount_adjusted=Decimal("0"),
                      status="open", due_date=due_date, notes=notes)
    db.session.add(invoice)
    db.session.flush()
    return invoice


def recompute(invoice):
    """Recalculate paid, adjusted and status from live rows, under lock."""
    invoice = lock_invoice(invoice)
    paid = sum((money(a.amount) for a in PaymentAllocation.query.filter(
        PaymentAllocation.invoice_id == invoice.id,
        PaymentAllocation.voided_at.is_(None))), Decimal("0"))
    adjusted = sum((money(a.amount) for a in InvoiceAdjustment.query.filter_by(
        invoice_id=invoice.id)), Decimal("0"))
    invoice.amount_paid = paid
    invoice.amount_adjusted = adjusted
    if invoice.status != "void":
        owed = money(invoice.total) - adjusted
        if paid > 0 and paid >= owed:
            invoice.status = "paid"
        elif paid > 0:
            invoice.status = "partial"
        elif owed <= 0:
            invoice.status = "paid"            # fully credited
        elif invoice.status != "expired":
            invoice.status = "open"
    db.session.flush()
    return invoice


def live_allocations(invoice_id, newest_first=False):
    q = PaymentAllocation.query.filter(PaymentAllocation.invoice_id == invoice_id,
                                       PaymentAllocation.voided_at.is_(None))
    order = PaymentAllocation.id.desc() if newest_first else PaymentAllocation.id.asc()
    return q.order_by(order).all()


def release(invoice, amount, user_id=None):
    """Take ``amount`` of money back off ``invoice``, newest allocation first.

    Returns the payments whose money was released, so the caller can roll
    it on to the payer's other invoices.
    """
    from models import Payment
    remaining = money(amount)
    touched = []
    now = datetime.utcnow()
    for alloc in live_allocations(invoice.id, newest_first=True):
        if remaining <= 0:
            break
        take = min(money(alloc.amount), remaining)
        alloc.voided_at, alloc.voided_by = now, user_id
        if money(alloc.amount) > take:
            db.session.add(PaymentAllocation(payment_id=alloc.payment_id, invoice_id=invoice.id,
                                             amount=money(alloc.amount) - take))
        payment = db.session.get(Payment, alloc.payment_id)
        payment.unallocated_amount = money(payment.unallocated_amount) + take
        remaining -= take
        if payment not in touched:
            touched.append(payment)
    db.session.flush()
    recompute(invoice)
    return touched


def adjust(invoice, amount, reason, note=None, user_id=None, roll=True):
    """Credit note. Money already paid above the new amount owed is released.

    With ``roll`` (the default) it moves on to the payer's next invoices or
    stays as their credit. With ``roll=False`` the caller decides (the till
    hands cash back). Returns (adjustment, payments whose money was released).
    """
    from services.billing.payments import allocate, refresh_match_status
    amount = money(amount)
    invoice = lock_invoice(invoice)
    if invoice.status == "void":
        raise BillingError("invoice is void")
    if amount <= 0:
        raise BillingError("adjustment must be greater than zero")
    if amount > money(invoice.total) - money(invoice.amount_adjusted):
        raise BillingError("adjustment is larger than the amount still invoiced")
    row = InvoiceAdjustment(invoice_id=invoice.id, amount=amount, reason=reason, note=note,
                            created_by=user_id)
    db.session.add(row)
    db.session.flush()
    invoice = recompute(invoice)
    excess = money(invoice.amount_paid) - (money(invoice.total) - money(invoice.amount_adjusted))
    released = release(invoice, excess, user_id) if excess > 0 else []
    if roll:
        for payment in released:
            allocate(payment, None, exclude=(invoice.id,))
            refresh_match_status(payment)
    audit_service.log(user_id, "invoice.adjust", "Invoice", invoice.id, None,
                      {"amount": str(amount), "reason": reason, "note": note})
    return row, released


def void_invoice(invoice, user_id=None, reason="void", roll=True):
    """Void an invoice. Money already on it is released: with ``roll`` it
    becomes the payer's credit and moves on to their other open invoices;
    with ``roll=False`` the caller decides. Returns (invoice, released)."""
    from services.billing.payments import allocate, refresh_match_status
    invoice = lock_invoice(invoice)
    if invoice.status == "void":
        return invoice, []
    invoice.status = "void"
    db.session.flush()
    released = release(invoice, invoice.amount_paid, user_id) if money(invoice.amount_paid) else []
    if roll:
        for payment in released:
            allocate(payment, None, exclude=(invoice.id,))
            refresh_match_status(payment)
    audit_service.log(user_id, "invoice.void", "Invoice", invoice.id, None, {"reason": reason})
    return invoice, released
