"""Public online shop: /o/<slug>/shop.

Customers (mostly on a phone, from a WhatsApp link) browse what is in
stock, fill a cart kept in their browser, and check out with name, phone
and pickup or delivery. The order is a pay-now order: it holds the stock
for 15 minutes and the customer goes straight to the invoice's pay page
(M-Pesa STK push). "Order on WhatsApp" places the same order with channel
'whatsapp' and opens a chat with the shop carrying the order number, items,
total and pay link.

404 for an unknown or lapsed business, a business type without the online
shop, or a shop the owner has closed. The customer's order page is found
by the invoice's random pay token, never by an order id.
"""
import logging

from flask import abort, g, jsonify, render_template, request

from models import Tenant, db
from public.limits import limiter
from public.routes import _error, public_bp, public_feature, public_tenant
from services import billing
from services import online_shop as shop

logger = logging.getLogger(__name__)


def _open_shop():
    tenant = db.session.get(Tenant, g.public_tenant_id)
    if tenant is None or not tenant.shop_open:
        abort(404)
    return tenant


@public_bp.route("/<slug>/shop", methods=["GET"])
@limiter.limit("60 per minute")
@public_tenant
@public_feature("online_shop")
def shop_page(slug):
    tenant = _open_shop()
    return render_template(
        "public/shop.html", business=g.public_business_name, slug=slug,
        catalogue=shop.catalogue(), delivery=bool(tenant.shop_delivery),
        whatsapp=tenant.whatsapp_number or "",
        rx_note=g.public_vertical == "pharmacy" and shop.has_prescription_items(),
        chat_url=shop.wa_link(tenant.whatsapp_number,
                              f"Hello {tenant.name}, I have a question about an order.")
        if tenant.whatsapp_number else None)


@public_bp.route("/<slug>/shop/quote", methods=["POST"])
@limiter.limit("30 per minute")
@public_tenant
@public_feature("online_shop")
def shop_quote(slug):
    """Server prices for the cart. Nothing is written."""
    _open_shop()
    body = request.get_json(silent=True) or {}
    try:
        lines = shop.parse_lines(body.get("items"))
    except shop.ShopError as e:
        return _error(str(e), 400)
    rows, total = shop.quote_lines(lines)
    return jsonify({"lines": [{**r, "unit_price": float(r["unit_price"]),
                               "subtotal": float(r["subtotal"])} for r in rows],
                    "total": float(total)}), 200


@public_bp.route("/<slug>/shop/order", methods=["POST"])
@limiter.limit("5 per minute")
@public_tenant
@public_feature("online_shop")
def shop_order(slug):
    tenant = _open_shop()
    body = request.get_json(silent=True) or {}
    channel = "whatsapp" if body.get("channel") == "whatsapp" else "shop"
    if channel == "whatsapp" and not tenant.whatsapp_number:
        return _error("This shop does not take WhatsApp orders. Use Place order.", 400)
    try:
        order, invoice, lines = shop.place_order(body, tenant, channel=channel)
    except shop.ShopError as e:
        db.session.rollback()
        return _error(str(e), 409 if isinstance(e, shop.OutOfStock) else 400)
    db.session.commit()
    pay_link = billing.pay_url(invoice)
    status_path = shop.order_path(slug, invoice)
    out = {"order": order.order_number, "total": float(invoice.total),
           "pay_url": pay_link, "status_url": status_path}
    if channel == "whatsapp":
        text = shop.whatsapp_text(tenant.name, order, lines, invoice.total,
                                  order.delivery_address, pay_link, shop.absolute(status_path))
        out["whatsapp_url"] = shop.wa_link(tenant.whatsapp_number, text)
    logger.info("shop order %s via %s", order.order_number, channel)
    return jsonify(out), 201


@public_bp.route("/<slug>/shop/order/<token>", methods=["GET"])
@limiter.limit("60 per minute")
@public_tenant
@public_feature("online_shop")
def shop_order_page(slug, token):
    order, invoice = shop.order_for_token(token)
    if order is None:
        abort(404)
    invoice = billing.recompute(invoice)
    db.session.commit()
    from models import OrderItem
    items = OrderItem.query.filter_by(order_id=order.id).order_by(OrderItem.id).all()
    return render_template(
        "public/shop_order.html", business=g.public_business_name, slug=slug,
        order_number=order.order_number, status=shop.public_status(order, invoice),
        lines=[{"name": i.item_name, "quantity": i.quantity, "amount": float(i.subtotal)}
               for i in items],
        total=float(invoice.total), paid=invoice.status == "paid",
        can_pay=invoice.status in ("open", "partial") and order.fulfillment_status == "pending",
        pay_url=f"/o/{slug}/pay/{invoice.pay_token}",
        delivery_address=order.delivery_address)
