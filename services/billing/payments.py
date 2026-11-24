"""Payments: recording money once, allocating it, moving and reversing it."""
from datetime import datetime
from decimal import Decimal

from models import Invoice, Payment, PaymentAllocation, db
from services import audit_service
from services.billing.invoices import (BillingError, live_allocations, lock_invoice,
                                       lock_tenant, money, recompute)
from services.billing.matching import match_reference

OWING = ("open", "partial")


class DuplicateReceipt(BillingError):
    """A typed M-Pesa code that is already recorded."""


def _normalise_receipt(receipt):
    return (receipt or "").strip().upper() or None


def refresh_match_status(payment):
    allocated = money(payment.amount) - money(payment.unallocated_amount)
    if payment.status == "reversed":
        return
    if allocated <= 0:
        payment.match_status = "unmatched"
    elif money(payment.unallocated_amount) > 0:
        payment.match_status = "partial"
    else:
        payment.match_status = "matched"


def _payer_invoices(customer_id, exclude=()):
    q = Invoice.query.filter(Invoice.customer_id == customer_id, Invoice.status.in_(OWING))
    if exclude:
        q = q.filter(Invoice.id.notin_(exclude))
    return q.order_by(Invoice.issued_at.asc(), Invoice.id.asc()).all()


def allocate(payment, target=None, exclude=()):
    """Spend ``payment.unallocated_amount``: ``target`` first (up to its
    balance), then the payer's other owing invoices, oldest first. Whatever
    is left stays unallocated, as the payer's credit or for review.
    Expired and void invoices are only paid when named as ``target``.
    """
    if payment.status != "confirmed":
        return payment
    remaining = money(payment.unallocated_amount)
    if target is not None and payment.customer_id is None:
        payment.customer_id = target.customer_id
    queue = []
    if target is not None and target.status != "void":
        queue.append(target)
    if payment.customer_id:
        skip = set(exclude) | ({target.id} if target is not None else set())
        queue.extend(_payer_invoices(payment.customer_id, exclude=tuple(skip)))
    for invoice in queue:
        if remaining <= 0:
            break
        invoice = recompute(invoice)
        due = invoice.balance
        if invoice.status == "void" or due <= 0:
            continue
        amount = min(due, remaining)
        db.session.add(PaymentAllocation(payment_id=payment.id, invoice_id=invoice.id,
                                         amount=amount))
        db.session.flush()
        remaining -= amount
        recompute(invoice)
    payment.unallocated_amount = remaining
    refresh_match_status(payment)
    db.session.flush()
    return payment


def record_payment(*, method, amount, receipt=None, intent=None, invoice=None, bill_ref=None,
                   payer_phone=None, payer_name=None, customer_id=None,
                   org_account_balance=None, raw=None, recorded_by=None, verified=True,
                   received_at=None):
    """Record money once and allocate it.

    The M-Pesa receipt is the key: the same receipt arriving again (STK
    callback, Paybill confirmation, Daraja retry, a typed code) updates the
    one existing payment instead of creating another. Target invoice, in
    order: ``invoice``, the intent's invoice, the account reference.
    """
    amount = money(amount)
    if amount <= 0:
        raise BillingError("payment amount must be greater than zero")
    lock_tenant()
    receipt = _normalise_receipt(receipt)
    existing = Payment.query.filter_by(mpesa_receipt=receipt).first() if receipt else None
    if existing is not None:
        return _merge(existing, method=method, amount=amount, intent=intent, invoice=invoice,
                      bill_ref=bill_ref, payer_phone=payer_phone, payer_name=payer_name,
                      org_account_balance=org_account_balance, raw=raw,
                      recorded_by=recorded_by, verified=verified)

    payment = Payment(method=method, amount=amount, unallocated_amount=amount,
                      mpesa_receipt=receipt, bill_ref_raw=(bill_ref or None),
                      payer_phone=payer_phone, payer_name=payer_name, customer_id=customer_id,
                      org_account_balance=org_account_balance, raw_payload=raw,
                      recorded_by=recorded_by, verified=verified,
                      received_at=received_at or datetime.utcnow())
    db.session.add(payment)
    db.session.flush()

    target = invoice or (intent.invoice if intent is not None else None)
    if target is None and bill_ref:
        match = match_reference(bill_ref)
        target = match.invoice
        if payment.customer_id is None:
            payment.customer_id = match.customer_id
    if intent is not None:
        intent.payment_id = payment.id
        intent.status = "completed"
    allocate(payment, target)
    return payment


def _merge(existing, *, method, amount, intent, invoice, bill_ref, payer_phone, payer_name,
           org_account_balance, raw, recorded_by, verified):
    if method == "mpesa_manual":
        raise DuplicateReceipt(f"M-Pesa code {existing.mpesa_receipt} is already recorded")

    for field, value in (("bill_ref_raw", bill_ref), ("payer_phone", payer_phone),
                         ("payer_name", payer_name),
                         ("org_account_balance", org_account_balance), ("raw_payload", raw)):
        if getattr(existing, field) is None and value is not None:
            setattr(existing, field, value)

    if verified and not existing.verified:
        if money(existing.amount) == amount:
            existing.verified = True
        else:
            _correct_typed_amount(existing, amount)

    target = invoice or (intent.invoice if intent is not None else None)
    if intent is not None:
        intent.payment_id = existing.id
        intent.status = "completed"
    if target is not None and money(existing.unallocated_amount) > 0:
        allocate(existing, target)
    db.session.flush()
    return existing


def _correct_typed_amount(payment, true_amount):
    """Staff typed a code with the wrong amount; Daraja's amount wins."""
    targets = [db.session.get(Invoice, a.invoice_id) for a in live_allocations_of(payment)]
    now = datetime.utcnow()
    for alloc in live_allocations_of(payment):
        alloc.voided_at = now
    db.session.flush()
    for invoice in targets:
        recompute(invoice)
    typed = money(payment.amount)
    payment.claimed_amount = typed
    payment.amount = true_amount
    payment.unallocated_amount = true_amount
    payment.verified = True
    audit_service.log(payment.recorded_by, "payment.amount_mismatch", "Payment", payment.id,
                      {"typed_amount": str(typed)},
                      {"daraja_amount": str(true_amount), "receipt": payment.mpesa_receipt,
                       "typed_by": payment.recorded_by})
    allocate(payment, targets[0] if targets else None)


def live_allocations_of(payment):
    return (PaymentAllocation.query
            .filter(PaymentAllocation.payment_id == payment.id,
                    PaymentAllocation.voided_at.is_(None))
            .order_by(PaymentAllocation.id.asc()).all())


def _void_allocations(payment, user_id):
    now = datetime.utcnow()
    invoices = []
    for alloc in live_allocations_of(payment):
        alloc.voided_at, alloc.voided_by = now, user_id
        invoices.append(db.session.get(Invoice, alloc.invoice_id))
    db.session.flush()
    for invoice in invoices:
        recompute(invoice)
    return invoices


def reallocate(payment, to_invoice, user_id=None):
    """Move all of a payment to another invoice (wrong account number)."""
    if payment.status != "confirmed":
        raise BillingError("payment is reversed")
    to_invoice = lock_invoice(to_invoice)
    if to_invoice.status == "void":
        raise BillingError("invoice is void")
    before = [a.invoice_id for a in live_allocations_of(payment)]
    _void_allocations(payment, user_id)
    payment.unallocated_amount = money(payment.amount)
    payment.customer_id = to_invoice.customer_id
    allocate(payment, to_invoice)
    audit_service.log(user_id, "payment.reallocate", "Payment", payment.id,
                      {"invoices": before}, {"invoice": to_invoice.id})
    return payment


def reverse_payment(payment, user_id=None, reason=None):
    """Safaricom reversed it: the money is gone, its invoices reopen."""
    if payment.status == "reversed":
        return payment
    _void_allocations(payment, user_id)
    payment.status = "reversed"
    payment.unallocated_amount = Decimal("0")
    audit_service.log(user_id, "payment.reverse", "Payment", payment.id, None,
                      {"reason": reason, "receipt": payment.mpesa_receipt})
    db.session.flush()
    return payment


def refund_unallocated(payment, amount=None, user_id=None, note=None):
    """Money handed back to the payer (cash at the till, card reversal on
    the terminal): it leaves the payment, and the business's books."""
    take = money(payment.unallocated_amount if amount is None else amount)
    if take <= 0:
        return payment
    if take > money(payment.unallocated_amount):
        raise BillingError("cannot refund more than the payment's unapplied money")
    payment.amount = money(payment.amount) - take
    payment.unallocated_amount = money(payment.unallocated_amount) - take
    if money(payment.amount) == 0:
        payment.status = "reversed"
    refresh_match_status(payment)
    audit_service.log(user_id, "payment.refund", "Payment", payment.id, None,
                      {"amount": str(take), "method": payment.method, "note": note})
    db.session.flush()
    return payment


HANDED_BACK = ("cash", "card")


def settle_released(payments, user_id=None, note=None):
    """After a sale is voided or refunded: cash and card money goes back to
    the customer; M-Pesa money stays as their credit (it cannot be handed
    back at the till) and rolls on to their other invoices, or waits in the
    review queue for a walk-in."""
    for payment in payments:
        if payment.method in HANDED_BACK:
            refund_unallocated(payment, user_id=user_id, note=note)
        else:
            allocate(payment, None)
            refresh_match_status(payment)
