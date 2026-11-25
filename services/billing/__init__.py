"""Billing service: the only code that writes invoices and payments.

Everything runs in the bound tenant and leaves committing to the caller.
See models/billing.py for the money model.
"""
from datetime import timedelta
from decimal import Decimal

from models import Invoice, Payment
from services.billing.invoices import (BillingError, adjust, create_invoice,  # noqa: F401
                                       lock_invoice, lock_payment, lock_tenant, money,
                                       recompute, void_invoice)
from services.billing.matching import C2B_MATCHERS, Match, match_reference  # noqa: F401
from services.billing.payments import (DuplicateReceipt, allocate, live_allocations_of,  # noqa: F401
                                       record_payment, reallocate, refund_unallocated,
                                       reverse_payment, settle_released)

CREDIT_KINDS = ("credit", "opening_balance")


def customer_exposure(customer_id):
    """What a customer owes on credit, less money of theirs not yet applied."""
    owed = sum((inv.balance for inv in Invoice.query.filter(
        Invoice.customer_id == customer_id, Invoice.kind.in_(CREDIT_KINDS),
        Invoice.status.in_(("open", "partial")))), Decimal("0"))
    credit = sum((money(p.unallocated_amount) for p in Payment.query.filter(
        Payment.customer_id == customer_id, Payment.status == "confirmed",
        Payment.unallocated_amount > 0)), Decimal("0"))
    return money(owed - credit)


def review_queue():
    """What a person needs to look at."""
    unmatched = (Payment.query.filter_by(status="confirmed", match_status="unmatched")
                 .order_by(Payment.received_at.desc()).all())
    unverified = (Payment.query.filter_by(method="mpesa_manual", verified=False,
                                          status="confirmed")
                  .order_by(Payment.received_at.desc()).all())
    mismatches = (Payment.query.filter(Payment.claimed_amount.isnot(None))
                  .order_by(Payment.received_at.desc()).all())
    return {
        "unmatched": [p.to_dict() for p in unmatched],
        "unverified_manual": [p.to_dict() for p in unverified],
        "amount_mismatches": [p.to_dict() for p in mismatches],
        "possible_missed_payments": possible_missed_payments(),
    }


def possible_missed_payments():
    """Paybill confirmations carry the account balance after the payment.
    If the balance rose by more than the payment since the previous
    confirmation, a confirmation in between probably never arrived. A hint,
    not proof: withdrawals lower the balance and are ignored, and
    out-of-order delivery can trip it."""
    rows = (Payment.query.filter(Payment.method == "mpesa_c2b",
                                 Payment.org_account_balance.isnot(None))
            .order_by(Payment.received_at.asc(), Payment.id.asc()).all())
    hints = []
    for before, after in zip(rows, rows[1:]):
        rise = money(after.org_account_balance) - money(before.org_account_balance)
        gap = rise - money(after.amount)
        if gap > Decimal("0.01"):
            hints.append({"after_receipt": before.mpesa_receipt,
                          "before_receipt": after.mpesa_receipt,
                          "unexplained_amount": float(gap),
                          "window_start": before.received_at.isoformat(),
                          "window_end": after.received_at.isoformat()})
    return hints


def pay_url(invoice):
    """The absolute public link where a customer pays ``invoice`` by M-Pesa.

    Uses PUBLIC_BASE_URL; inside a request it falls back to the request's
    host. Outside a request (SMS jobs) without PUBLIC_BASE_URL it raises,
    since a relative link in a text message is useless.
    """
    from flask import current_app, has_request_context, request
    from models import Tenant, db
    tenant = db.session.get(Tenant, invoice.tenant_id)
    base = current_app.config.get("PUBLIC_BASE_URL")
    if not base:
        if not has_request_context():
            raise BillingError("PUBLIC_BASE_URL is not set; cannot build a pay link")
        base = request.host_url
    return f"{base.rstrip('/')}/o/{tenant.slug}/pay/{invoice.pay_token}"


def credit_due_date(customer, today):
    return today + timedelta(days=int(customer.credit_term_days or 30))


def open_invoice_for_transaction(transaction_id):
    """The sale's current (non-void) invoice, or None."""
    return (Invoice.query.filter(Invoice.transaction_id == transaction_id,
                                 Invoice.status != "void")
            .order_by(Invoice.id.desc()).first())
