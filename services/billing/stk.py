"""STK push: ask the customer's phone for the PIN, then record the money.

An intent is written (and committed) before Daraja is called, so a crash
mid-call leaves a trace. The callback is verified with Daraja's STK query
before money is recorded; if the query is unavailable the intent waits as
'callback_received' and the sweep finishes it. Missed callbacks are found by
the sweep querying pending intents.
"""
import logging
from datetime import datetime, timedelta

from auth.tenancy import current_tenant_id
from models import Payment, PaymentIntent, Tenant, db
from services.billing.invoices import BillingError, money, recompute
from services.billing.payments import record_payment
from services.daraja import DarajaBusy, DarajaClient, DarajaError, stk_amount
from services.mpesa_service import parse_stk_callback
from services.phone import normalize_ke_phone

logger = logging.getLogger(__name__)

PROMPT_WINDOW = timedelta(seconds=90)     # one open prompt per phone
QUERY_AFTER = timedelta(seconds=90)       # ask Daraja about silent intents
POLL_QUERY_AFTER = timedelta(seconds=30)  # the till's own polling may ask sooner
TIMEOUT_AFTER = timedelta(minutes=10)
OPEN = ("initiating", "pending")
WAITING = ("pending", "callback_received")


class PromptPending(BillingError):
    """This phone already has an M-Pesa prompt from us in the last 90 s."""


def _tenant():
    return db.session.get(Tenant, current_tenant_id())


def client():
    return DarajaClient(_tenant())


def start_stk(invoice, phone, user_id=None, now=None):
    """Send an STK push for the invoice's balance. Commits."""
    now = now or datetime.utcnow()
    invoice = recompute(invoice)
    if invoice.status in ("paid", "void"):
        raise BillingError(f"invoice is {invoice.status}")
    phone = normalize_ke_phone(phone)
    recent = (PaymentIntent.query
              .filter(PaymentIntent.phone == phone, PaymentIntent.status.in_(OPEN),
                      PaymentIntent.created_at > now - PROMPT_WINDOW).first())
    if recent is not None:
        raise PromptPending("Check the phone for the M-Pesa prompt already sent")
    daraja = client()                       # fail fast if Daraja is not set up
    intent = PaymentIntent(invoice_id=invoice.id, phone=phone,
                           amount=stk_amount(invoice.balance), status="initiating",
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


def _record_without_receipt(intent):
    """Daraja's query says paid but its callback never came, so there is no
    receipt. Record the money now; if the receipt turns up later (callback
    or Paybill confirmation for this invoice and amount) it is attached to
    this payment instead of counting twice."""
    payment = record_payment(method="mpesa_stk", amount=intent.amount, intent=intent,
                             raw={"source": "stk_query", "checkout_request_id":
                                  intent.checkout_request_id})
    intent.status = "completed"
    return payment


def handle_stk_callback(payload):
    """Daraja's STK result. Commits."""
    parsed = parse_stk_callback(payload)
    cri = parsed.get("checkout_request_id")
    intent = PaymentIntent.query.filter_by(checkout_request_id=cri).first() if cri else None
    if intent is None:
        logger.warning("STK callback for unknown CheckoutRequestID %s", cri)
        return
    if intent.status == "completed":
        _attach_late_receipt(intent, parsed)
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


def _attach_late_receipt(intent, parsed):
    payment = db.session.get(Payment, intent.payment_id) if intent.payment_id else None
    receipt = parsed.get("mpesa_receipt")
    if payment is not None and payment.mpesa_receipt is None and receipt:
        payment.mpesa_receipt = receipt
        payment.raw_payload = parsed


def _apply_query(intent, result):
    intent.last_queried_at = datetime.utcnow()
    if result.state == "paid":
        if intent.raw_callback and _record_from_callback(intent):
            return
        _record_without_receipt(intent)
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
        refresh_intent(intent, now=now)
        if intent.status in counts:
            counts[intent.status] += 1
    return counts

