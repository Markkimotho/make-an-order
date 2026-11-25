"""Public refill link: /o/<slug>/r/<refill_token>.

From the reminder SMS a chronic patient confirms their refill, picks
pickup or delivery, and is sent to the invoice's pay page. The order holds
the stock for 15 minutes like any pay-now order.

A prescription with a controlled drug cannot be ordered or paid here: the
page offers "Ask the pharmacist to call me", which puts it on the
pharmacy dashboard. The counter rules for controlled drugs are not
bypassed by a link.
"""
import logging
import math
from datetime import datetime

from flask import abort, g, jsonify, render_template, request

from models import InventoryItem, Invoice, Prescription, db
from public.limits import limiter
from public.routes import _error, public_bp, public_feature, public_tenant
from services import orders as order_service
from services import stock
from verticals.pharmacy import service as pharmacy

logger = logging.getLogger(__name__)


def masked_name(name):
    """'Wanjiku Kamau' -> 'Wanjiku K.'"""
    parts = (name or "").split()
    if not parts:
        return "Patient"
    return parts[0] + (f" {parts[-1][0]}." if len(parts) > 1 else "")


def _prescription(token):
    rx = Prescription.query.filter_by(refill_token=token).first()
    if rx is None or rx.refill_token != token or not rx.is_chronic:
        abort(404)
    return rx


def _lines(rx):
    lines = []
    for it in rx.items:
        item = db.session.get(InventoryItem, it.inventory_item_id) if it.inventory_item_id else None
        if item is None:
            continue
        lines.append(order_service.Line(item=item, quantity=max(1, math.ceil(it.quantity_prescribed)),
                                        unit_price=item.price))
    return lines


def _pay_path(slug, invoice):
    return f"/o/{slug}/pay/{invoice.pay_token}"


@public_bp.route("/<slug>/r/<refill_token>", methods=["GET"])
@limiter.limit("60 per minute")
@public_tenant
@public_feature("pharmacy")
def refill_page(slug, refill_token):
    rx = _prescription(refill_token)
    lines = _lines(rx)
    return render_template(
        "public/refill.html", business=g.public_business_name, slug=slug, token=refill_token,
        patient=masked_name(rx.customer.name if rx.customer else None),
        lines=[{"name": l.item.name, "quantity": l.quantity, "amount": float(l.unit_price) * l.quantity}
               for l in lines],
        total=sum(float(l.unit_price) * l.quantity for l in lines),
        due=rx.refill_due_date, active=rx.refill_status == "active",
        controlled=pharmacy.has_controlled(rx),
        callback_requested=rx.callback_requested_at is not None)


@public_bp.route("/<slug>/r/<refill_token>/order", methods=["POST"])
@limiter.limit("5 per minute")
@public_tenant
@public_feature("pharmacy")
def refill_order(slug, refill_token):
    rx = _prescription(refill_token)
    if pharmacy.has_controlled(rx):
        return _error("This prescription has a controlled medicine. Ask the pharmacist to "
                      "call you.", 409)
    # One order per cycle, decided under the prescription's lock so two
    # taps (or two phones) cannot both order the same refill.
    from services.billing.invoices import lock_tenant
    lock_tenant()
    rx = pharmacy.lock_prescription(rx)
    if not pharmacy.refills_left(rx):
        db.session.rollback()
        return _error("This prescription has no refills left. Contact the pharmacy.", 409)
    pending = pharmacy.open_refill_order(rx)
    if pending is not None:
        invoice = order_service.invoice_for(pending)
        db.session.rollback()
        if invoice is not None and invoice.status in ("open", "partial"):
            return jsonify({"pay_url": _pay_path(slug, invoice), "order": pending.order_number}), 200
        return _error("This refill is already ordered and paid; the pharmacy is preparing it.",
                      409)
    body = request.get_json(silent=True) or request.form
    fulfilment = (body.get("fulfilment") or "pickup").strip()
    address = (body.get("address") or "").strip()
    if fulfilment not in ("pickup", "delivery"):
        return _error("Choose pickup or delivery", 400)
    if fulfilment == "delivery" and not address:
        return _error("Enter the delivery address", 400)
    lines = _lines(rx)
    if not lines:
        return _error("Nothing on this prescription can be ordered online", 409)
    try:
        order, invoice = order_service.create_order(
            lines, customer_id=rx.customer_id, channel="public_link", pay_now=True,
            prescription_id=rx.id, delivery_address=address if fulfilment == "delivery" else None,
            notes=f"Refill of prescription #{rx.id} ({fulfilment})")
    except (order_service.OrderError, stock.StockError) as e:
        db.session.rollback()
        logger.info("refill order refused for rx %s: %s", rx.id, e)
        return _error("Some of this refill is out of stock. Call the pharmacy.", 409)
    db.session.commit()
    logger.info("refill order %s for prescription %s", order.order_number, rx.id)
    return jsonify({"pay_url": _pay_path(slug, invoice), "order": order.order_number}), 201


@public_bp.route("/<slug>/r/<refill_token>/callback", methods=["POST"])
@limiter.limit("3 per minute")
@public_tenant
@public_feature("pharmacy")
def refill_callback(slug, refill_token):
    rx = _prescription(refill_token)
    if rx.callback_requested_at is None:
        rx.callback_requested_at = datetime.utcnow()
        db.session.commit()
    return jsonify({"message": "The pharmacist will call you."}), 200
