"""Public pay link: /o/<slug>/pay/<pay_token>.

A customer opens the link from an SMS, sees the invoice and pays its
balance by M-Pesa STK push. No login: the slug picks the business, the
invoice's random pay_token picks the invoice. An unknown slug, a lapsed
business, a wrong token or a token from another business is 404.

Responses are never cached, never indexed and send no Referer, so the
token does not leak to the CDN or search engines.
"""
import logging
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, abort, g, jsonify, render_template, request

from auth.tenancy import tenant_scope, unscoped
from models import (Invoice, OrderItem, PaymentIntent, Tenant, TransactionItem, db)
from public.limits import limiter
from services import billing
from services.billing import stk
from services.daraja import DarajaBusy, DarajaError, DarajaNotConfigured, DarajaUnavailable
from services.phone import InvalidPhone

logger = logging.getLogger(__name__)

public_bp = Blueprint("public", __name__)

# A poll asks Daraja at most this often per intent, whatever the client does.
QUERY_EVERY = timedelta(seconds=15)
# M-Pesa prompts one invoice may send in a window, from any number of IPs.
PROMPTS_PER_INVOICE = 3
PROMPT_WINDOW = timedelta(minutes=10)

STATUS_TEXT = {
    "initiating": "Sending the M-Pesa prompt...",
    "pending": "Check your phone and enter your M-Pesa PIN.",
    "unknown": "Waiting for M-Pesa. If you got the prompt and paid, it will be recorded.",
    "callback_received": "Payment received, confirming with M-Pesa...",
    "paid_unconfirmed": "M-Pesa confirms the payment; the receipt is on its way.",
    "completed": "Paid. Thank you!",
    "failed": "The payment did not go through.",
    "timeout": "No answer from M-Pesa. You can try again.",
}


def _business(slug):
    """The business for a public slug, or None. Lapsed businesses are hidden."""
    with unscoped():
        tenant = Tenant.query.filter_by(slug=slug).first()
        # MySQL compares case-insensitively; only the exact slug is this page.
        if tenant is None or tenant.slug != slug or not tenant.is_active_subscription():
            return None
        return tenant.id, tenant.name, tenant.vertical


def public_tenant(view):
    """Bind the business named by <slug> for this request, or 404."""
    @wraps(view)
    def wrapped(slug, *args, **kwargs):
        found = _business(slug)
        if found is None:
            abort(404)
        g.public_tenant_id, g.public_business_name, g.public_vertical = found
        with tenant_scope(found[0]):
            return view(slug, *args, **kwargs)
    return wrapped


def public_feature(name):
    """Inner decorator (below @public_tenant): 404 unless the business's
    vertical has the feature, so a duka link never works for a school."""
    from verticals.features import features_for

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if name not in features_for(g.get("public_vertical")):
                abort(404)
            return view(*args, **kwargs)
        return wrapped
    return decorator


@public_bp.after_request
def _private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _invoice(pay_token):
    invoice = Invoice.query.filter_by(pay_token=pay_token).first()
    if invoice is None or invoice.pay_token != pay_token or invoice.status == "void":
        abort(404)
    return invoice


def _lines(invoice):
    if invoice.transaction_id:
        rows = TransactionItem.query.filter_by(transaction_id=invoice.transaction_id).all()
        return [{"name": r.item_name, "quantity": float(r.quantity),
                 "amount": float(r.line_total)} for r in rows]
    if invoice.order_id:
        rows = OrderItem.query.filter_by(order_id=invoice.order_id).all()
        return [{"name": r.item_name, "quantity": float(r.quantity),
                 "amount": float(r.subtotal)} for r in rows]
    label = "Opening balance" if invoice.kind == "opening_balance" else "Amount due"
    return [{"name": label, "quantity": 1, "amount": float(invoice.total)}]


def _public_invoice(invoice):
    """What the page may show: no customer, phone or internal ids."""
    return {"invoice_number": invoice.invoice_number,
            "issued_at": invoice.issued_at.date().isoformat() if invoice.issued_at else None,
            "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
            "total": float(invoice.total), "paid": float(invoice.amount_paid or 0),
            "adjusted": float(invoice.amount_adjusted or 0),
            "balance": float(invoice.balance), "status": invoice.status}


def _public_intent(intent):
    return {"id": intent.id, "status": intent.status, "amount": float(intent.amount),
            "message": STATUS_TEXT.get(intent.status, intent.status),
            "detail": intent.result_desc if intent.status in ("failed", "timeout") else None,
            "done": intent.status in ("completed", "failed", "timeout")}


def _error(message, status):
    return jsonify({"error": message}), status


@public_bp.route("/<slug>/pay/<pay_token>", methods=["GET"])
@limiter.limit("60 per minute")
@public_tenant
def pay_page(slug, pay_token):
    invoice = billing.recompute(_invoice(pay_token))
    from verticals.institution import service as institution
    instalments, minimum = institution.instalment_terms(invoice)
    db.session.commit()
    return render_template("public/pay.html", business=g.public_business_name,
                           invoice=_public_invoice(invoice), lines=_lines(invoice),
                           slug=slug, pay_token=pay_token, instalments=instalments,
                           min_instalment=minimum)


@public_bp.route("/<slug>/pay/<pay_token>/stk", methods=["POST"])
@limiter.limit("5 per minute")
@public_tenant
def pay_stk(slug, pay_token):
    invoice = _invoice(pay_token)
    body = request.get_json(silent=True) or request.form
    phone = (body.get("phone") or "").strip()
    if not phone:
        return _error("Enter the M-Pesa phone number to pay from", 400)
    if invoice.status == "paid":
        return _error("This invoice is already paid", 409)
    amount = None
    if body.get("amount") not in (None, ""):
        from verticals.institution import service as institution
        try:
            amount = institution.check_instalment(billing.recompute(invoice), body.get("amount"))
        except (institution.InstitutionError, ValueError, ArithmeticError) as e:
            db.session.rollback()
            return _error(str(e), 400)
    # The per-invoice cap counts prompts actually attempted, under the invoice
    # row lock: start_stk commits its intent before calling Daraja, so a
    # concurrent request waits here and then sees it.
    invoice = billing.lock_invoice(invoice)
    since = datetime.utcnow() - PROMPT_WINDOW
    recent = PaymentIntent.query.filter(PaymentIntent.invoice_id == invoice.id,
                                        PaymentIntent.created_at > since).count()
    if recent >= PROMPTS_PER_INVOICE:
        db.session.rollback()
        return _error("Too many payment prompts for this invoice. Try again in a few "
                      "minutes.", 429)
    try:
        intent = stk.start_stk(invoice, phone, amount=amount)
    except InvalidPhone:
        db.session.rollback()
        return _error("Enter a Kenyan mobile number, like 0712 345 678", 400)
    except stk.PromptPending:
        db.session.rollback()
        return _error("We already sent a prompt to that phone. Check it, or wait a "
                      "minute and try again.", 409)
    except DarajaBusy:
        return _error("That phone already has an M-Pesa prompt open. Wait a minute "
                      "and try again.", 409)
    except DarajaUnavailable:
        return _error("No reply from M-Pesa. If you get the prompt and pay, it will "
                      "be recorded; wait before trying again.", 502)
    except DarajaNotConfigured:
        db.session.rollback()
        return _error("This business cannot take M-Pesa online yet. Pay at the counter.",
                      400)
    except DarajaError:
        logger.warning("public STK push failed for invoice %s", invoice.id, exc_info=True)
        return _error("M-Pesa could not send the prompt. Try again.", 502)
    except billing.BillingError as e:
        db.session.rollback()
        return _error(str(e), 409)
    logger.info("public STK push: invoice %s intent %s", invoice.id, intent.id)
    return jsonify({"intent": _public_intent(intent),
                    "invoice": _public_invoice(intent.invoice)}), 201


@public_bp.route("/<slug>/pay/<pay_token>/status", methods=["GET"])
@limiter.limit("60 per minute")
@public_tenant
def pay_status(slug, pay_token):
    invoice = _invoice(pay_token)
    try:
        intent_id = int(request.args.get("intent", ""))
    except ValueError:
        abort(404)
    intent = db.session.get(PaymentIntent, intent_id)
    if intent is None or intent.invoice_id != invoice.id:
        abort(404)
    now = datetime.utcnow()
    if intent.last_queried_at is None or intent.last_queried_at < now - QUERY_EVERY:
        intent = stk.refresh_intent(intent, now=now, min_age=stk.POLL_QUERY_AFTER)
    invoice = billing.recompute(db.session.get(Invoice, invoice.id))
    if invoice.status == "paid" and invoice.order_id:
        # Campaign orders: the pickup code goes out as soon as it is paid.
        from verticals.institution import service as institution
        db.session.commit()
        institution.issue_pickup_codes(order_id=invoice.order_id)
        invoice = db.session.get(Invoice, invoice.id)
    db.session.commit()
    return jsonify({"intent": _public_intent(intent), "invoice": _public_invoice(invoice)}), 200
