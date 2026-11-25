"""Public duka order link: /o/<slug>/d/<public_token>.

A duka owner orders from the distributor's catalogue at their tier price,
sees what they owe and how much credit is left, and orders on credit
within the ceiling. Over the ceiling the same order can be paid now by
M-Pesa (the page goes to the invoice's pay page): only more deni is
refused, never the sale.
"""
import logging

from flask import abort, g, jsonify, render_template, request

from models import Customer, InventoryItem, Invoice, db
from public.limits import limiter
from public.routes import _error, public_bp, public_tenant
from services import billing, stock
from services import orders as order_service
from verticals.distributor import service as distributor

logger = logging.getLogger(__name__)


def _duka(token):
    customer = Customer.query.filter_by(public_token=token).first()
    if customer is None or customer.public_token != token:
        abort(404)
    return customer


def _catalogue(customer):
    items = (InventoryItem.query
             .filter(InventoryItem.stock > 0, InventoryItem.is_controlled.is_(False),
                     InventoryItem.has_variants.is_(False))
             .order_by(InventoryItem.category, InventoryItem.name).all())
    return [{"id": i.id, "name": i.name, "unit": i.unit, "category": i.category,
             "price": float(distributor.price_for(customer, i)), "in_stock": int(i.stock or 0)}
            for i in items]


@public_bp.route("/<slug>/d/<duka_token>", methods=["GET"])
@limiter.limit("60 per minute")
@public_tenant
def duka_page(slug, duka_token):
    customer = _duka(duka_token)
    open_invoices = (Invoice.query
                     .filter(Invoice.customer_id == customer.id,
                             Invoice.status.in_(("open", "partial")))
                     .order_by(Invoice.issued_at).all())
    return render_template(
        "public/duka.html", business=g.public_business_name, slug=slug, token=duka_token,
        duka=customer.name, credit=distributor.credit_summary(customer),
        catalogue=_catalogue(customer),
        invoices=[{"number": inv.invoice_number, "balance": float(inv.balance),
                   "due": inv.due_date.isoformat() if inv.due_date else None,
                   "pay_url": f"/o/{slug}/pay/{inv.pay_token}"}
                  for inv in open_invoices if inv.balance > 0])


@public_bp.route("/<slug>/d/<duka_token>/order", methods=["POST"])
@limiter.limit("5 per minute")
@public_tenant
def duka_order(slug, duka_token):
    customer = _duka(duka_token)
    body = request.get_json(silent=True) or {}
    pay_now = bool(body.get("pay_now"))
    try:
        order, invoice = distributor.place_order(customer.id, body.get("items"), pay_now=pay_now,
                                                 channel="public_link")
    except distributor.CreditCeiling as e:
        db.session.rollback()
        return jsonify({"error": str(e), "can_pay_now": True,
                        "available": float(e.available)}), 409
    except stock.InsufficientStock as e:
        db.session.rollback()
        return _error(f"Only {e.sellable} of {e.item_name} available", 409)
    except (order_service.OrderError, stock.StockError) as e:
        db.session.rollback()
        return _error(str(e), 400)
    db.session.commit()
    logger.info("duka order %s (%s)", order.order_number, "pay now" if pay_now else "credit")
    return jsonify({"order": order.order_number, "invoice": invoice.invoice_number,
                    "total": float(invoice.total), "on_credit": not pay_now,
                    "pay_url": f"/o/{slug}/pay/{invoice.pay_token}"}), 201
