"""STK push: ask the customer's phone for the PIN, then record the money.

An intent is written (and committed) before Daraja is called, so a crash
mid-call leaves a trace. The callback is verified with Daraja's STK query
before money is recorded; if the query is unavailable the intent waits as
'callback_received' and the sweep finishes it. Missed callbacks are found by
the sweep querying pending intents.

Money is only ever recorded with its M-Pesa receipt (the key that stops one
transfer being counted twice). If Daraja's query says paid but no callback
ever brought the receipt, the intent becomes 'paid_unconfirmed' and waits
for the receipt from the Paybill confirmation or the code on the customer's
SMS; it closes by itself once the invoice is paid.

Intent states: initiating -> pending -> callback_received -> completed, or
failed / timeout / paid_unconfirmed; 'unknown' when the push request itself
got no reply (Safaricom may or may not have sent the prompt).
"""
import logging
from datetime import datetime, timedelta

from auth.tenancy import current_tenant_id
from models import Payment, PaymentIntent, Tenant, db
from services.billing.invoices import BillingError, money, recompute
from services.billing.payments import record_payment
from services.billing.invoices import lock_tenant
from services.daraja import (DarajaBusy, DarajaClient, DarajaError, DarajaUnavailable,
                             stk_amount)
from services.mpesa_service import parse_stk_callback
from services.phone import normalize_ke_phone

logger = logging.getLogger(__name__)

PROMPT_WINDOW = timedelta(seconds=90)     # one open prompt per phone
QUERY_AFTER = timedelta(seconds=90)       # ask Daraja about silent intents
POLL_QUERY_AFTER = timedelta(seconds=30)  # the till's own polling may ask sooner
TIMEOUT_AFTER = timedelta(minutes=10)
OPEN = ("initiating", "pending", "unknown")
WAITING = ("pending", "callback_received", "paid_unconfirmed")
MATCH_UNKNOWN_WITHIN = timedelta(minutes=10)


class PromptPending(BillingError):
    """This phone already has an M-Pesa prompt from us in the last 90 s."""


def _tenant():
    return db.session.get(Tenant, current_tenant_id())


def client():
    return DarajaClient(_tenant())


def start_stk(invoice, phone, user_id=None, now=None, amount=None):
    """Send an STK push for the invoice's balance, or for ``amount`` (an
    instalment; never more than the balance). Commits."""
    now = now or datetime.utcnow()
    invoice = recompute(invoice)
    if invoice.status in ("paid", "void"):
        raise BillingError(f"invoice is {invoice.status}")
    due = invoice.balance if amount is None else min(money(amount), invoice.balance)
    if due <= 0:
        raise BillingError("amount must be greater than zero")
    phone = normalize_ke_phone(phone)
    recent = (PaymentIntent.query
              .filter(PaymentIntent.phone == phone, PaymentIntent.status.in_(OPEN),
                      PaymentIntent.created_at > now - PROMPT_WINDOW).first())
    if recent is not None:
        raise PromptPending("Check the phone for the M-Pesa prompt already sent")
    daraja = client()                       # fail fast if Daraja is not set up
    intent = PaymentIntent(invoice_id=invoice.id, phone=phone,
                           amount=stk_amount(due), status="initiating",
                           initiated_by=user_id, created_at=now)
    db.session.add(intent)
    db.session.commit()
    try:
        data = daraja.stk_push(phone, intent.amount, invoice.invoice_number,
                               f"Inv {invoice.invoice_number[-6:]}")
    except DarajaBusy:
        intent.status = "failed"
        intent.result_desc = ("The customer already has an M-Pesa prompt open. "
                              "Wait a minute and try again.")
        db.session.commit()
        raise
    except DarajaUnavailable:
        # No reply: Safaricom may still have sent the prompt. Keep the intent
        # open (it blocks a second prompt for 90 s) so a callback can be
        # matched to it by phone and amount.
        intent.status = "unknown"
        intent.result_desc = ("No reply from M-Pesa. If the customer got the prompt and "
                              "paid, the payment will still be recorded.")
        db.session.commit()
        raise
    except DarajaError as e:
        intent.status = "failed"
        intent.result_desc = str(e)[:255]
        db.session.commit()
        raise
    intent.checkout_request_id = data.get("CheckoutRequestID")
    intent.merchant_request_id = data.get("MerchantRequestID")
    intent.status = "pending"
    db.session.commit()
    return intent


def _record_from_callback(intent):
    parsed = parse_stk_callback(intent.raw_callback or {})
    if not parsed.get("mpesa_receipt") or not parsed.get("amount"):
        return None
    payment = record_payment(method="mpesa_stk", amount=parsed["amount"],
                             receipt=parsed["mpesa_receipt"], intent=intent,
                             payer_phone=parsed.get("phone_number"), raw=intent.raw_callback)
    intent.status = "completed"
    return payment


def _lock_intent(intent):
    from sqlalchemy import select
    lock_tenant()
    return db.session.execute(select(PaymentIntent).where(PaymentIntent.id == intent.id)
                              .with_for_update()
                              .execution_options(populate_existing=True)).scalar_one()


def _intent_for_unknown_checkout(parsed, now):
    """A push whose request timed out has no checkout id stored. Its result
    is matched by the phone and amount we asked for, within 10 minutes."""
    phone, amount = parsed.get("phone_number"), parsed.get("amount")
    if not phone or amount is None:
        return None
    try:
        phone = normalize_ke_phone(phone)
    except ValueError:
        return None
    return (PaymentIntent.query
            .filter(PaymentIntent.status.in_(("initiating", "unknown")),
                    PaymentIntent.checkout_request_id.is_(None),
                    PaymentIntent.phone == phone,
                    PaymentIntent.amount == money(amount),
                    PaymentIntent.created_at > now - MATCH_UNKNOWN_WITHIN)
            .order_by(PaymentIntent.id.desc()).first())


def handle_stk_callback(payload):
    """Daraja's STK result. Commits."""
    parsed = parse_stk_callback(payload)
    cri = parsed.get("checkout_request_id")
    intent = PaymentIntent.query.filter_by(checkout_request_id=cri).first() if cri else None
    if intent is None and cri:
        intent = _intent_for_unknown_checkout(parsed, datetime.utcnow())
        if intent is not None:
            intent.checkout_request_id = cri
            db.session.flush()
    if intent is None:
        if parsed.get("success") and parsed.get("mpesa_receipt") and parsed.get("amount"):
            # Money we cannot tie to a request is still money: record it for review.
            logger.warning("STK callback for unknown CheckoutRequestID %s; recorded unmatched", cri)
            record_payment(method="mpesa_stk", amount=parsed["amount"],
                           receipt=parsed["mpesa_receipt"],
                           payer_phone=parsed.get("phone_number"), raw=payload)
            db.session.commit()
        else:
            logger.warning("STK callback for unknown CheckoutRequestID %s", cri)
        return
    intent = _lock_intent(intent)
    if intent.status == "completed":
        db.session.commit()
        return
    intent.raw_callback = payload
    intent.result_desc = (parsed.get("result_desc") or "")[:255] or None
    if not parsed.get("success"):
        intent.status = "failed"
        db.session.commit()
        return
    intent.status = "callback_received"
    db.session.commit()
    try:
        result = client().query(cri, timeout=5)
    except DarajaError as e:
        logger.warning("STK verify unavailable for %s (%s); the sweep will retry", cri, e)
        return
    _apply_query(intent, result)
    db.session.commit()


def _apply_query(intent, result):
    intent.last_queried_at = datetime.utcnow()
    if result.state == "paid":
        if intent.raw_callback and _record_from_callback(intent):
            return
        invoice = recompute(intent.invoice)
        if invoice.status == "paid":
            intent.status = "completed"       # the money came in another way
            return
        intent.status = "paid_unconfirmed"
        intent.result_desc = ("M-Pesa confirms the payment, but its receipt has not arrived. "
                              "Enter the code from the customer's SMS to finish.")
    elif result.state == "failed":
        if intent.status == "callback_received":
            # The callback said paid, the query says not: a person decides.
            logger.error("STK %s: callback success but query %s", intent.checkout_request_id,
                         result.result_code)
            intent.result_desc = f"Query says {result.result_desc}; check before trusting"
            return
        intent.status = "failed"
        intent.result_desc = (result.result_desc or "")[:255] or None


def refresh_intent(intent, now=None, min_age=QUERY_AFTER):
    """Ask Daraja about a silent intent. Used by the sweep and by the till's
    polling. Returns the intent. Commits."""
    now = now or datetime.utcnow()
    if intent.status not in WAITING or not intent.checkout_request_id:
        return intent
    if intent.created_at > now - min_age:
        return intent
    intent = _lock_intent(intent)
    if intent.status == "paid_unconfirmed":
        if recompute(intent.invoice).status == "paid":
            intent.status = "completed"
        db.session.commit()
        return intent
    if intent.status not in WAITING:
        db.session.commit()
        return intent
    try:
        result = client().query(intent.checkout_request_id, timeout=5)
    except DarajaError as e:
        logger.info("STK query unavailable for %s: %s", intent.checkout_request_id, e)
        result = None
    if result is not None and result.state != "processing":
        _apply_query(intent, result)
    else:
        intent.last_queried_at = now
        if intent.status == "pending" and intent.created_at < now - TIMEOUT_AFTER:
            intent.status = "timeout"
            intent.result_desc = "No answer from M-Pesa after 10 minutes"
    db.session.commit()
    return intent


def handle_c2b(payload):
    """A Paybill/Till confirmation. Commits."""
    first, middle, last = (payload.get("FirstName"), payload.get("MiddleName"),
                           payload.get("LastName"))
    name = " ".join(p for p in (first, middle, last) if p) or None
    balance = payload.get("OrgAccountBalance")
    record_payment(method="mpesa_c2b", amount=payload.get("TransAmount"),
                   receipt=payload.get("TransID"), bill_ref=payload.get("BillRefNumber"),
                   payer_phone=payload.get("MSISDN"), payer_name=name,
                   org_account_balance=money(balance) if balance not in (None, "") else None,
                   raw=payload)
    db.session.commit()


def sweep_tenant(now=None):
    """Finish every silent intent of the bound tenant. Returns counts."""
    now = now or datetime.utcnow()
    counts = {"checked": 0, "completed": 0, "failed": 0, "timeout": 0}
    intents = (PaymentIntent.query
               .filter(PaymentIntent.status.in_(WAITING),
                       PaymentIntent.created_at <= now - QUERY_AFTER)
               .order_by(PaymentIntent.id).all())
    for intent in intents:
        counts["checked"] += 1
        intent = refresh_intent(intent, now=now)
        if intent.status in counts:
            counts[intent.status] += 1
    return counts

