"""The online shop: what a customer can buy at /o/<slug>/shop, the order
they place there (or from WhatsApp), and the staff side of those orders.

Prices and stock always come from the database, never from the browser.
Orders go through services/orders.create_order as pay-now orders, so they
hold stock for 15 minutes and the payments sweep releases unpaid holds.

Not sold online: controlled drugs and prescription-only items (the
pharmacist has to see the prescription), items with no price, and stock
that a counter sale could not take either (expired, quarantined,
recalled).

Staff fulfilment (``set_fulfilment``): ready, dispatched and collected
need a paid invoice, or a credit invoice for credit orders. Cancelling goes
through services/orders.cancel, which returns the stock and voids or
credits the invoice. SMS to the customer is sent after the caller commits
(``notify``), with a dedupe key so a second click texts nobody twice.
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import func, select

from models import (Batch, Category, Customer, InventoryItem, Invoice, ItemVariant, Order,
                    OrderItem, Tenant, db)
from services import billing, stock
from services import orders as order_service
from services.billing.invoices import lock_tenant, money
from services.clock import today_nairobi
from services.phone import InvalidPhone, normalize_ke_phone

logger = logging.getLogger(__name__)

SHOP_CHANNELS = ("shop", "whatsapp")
REMOTE_CHANNELS = ("shop", "whatsapp", "public_link", "staff")
FEW_LEFT = 5
MAX_LINES = 50
MAX_QTY = 999
DEFAULT_ICON = "fa-box"
_ICON = re.compile(r"^fa-[a-z0-9-]{1,40}$")
_ICON_STYLES = {"fa-solid", "fa-regular", "fa-brands", "fa-light", "fa-thin", "fa-duotone"}


class ShopError(ValueError):
    """An order the shop refuses, with a message for the customer."""


class OutOfStock(ShopError):
    """A quantity is more than the shop can sell right now."""


# --- links ------------------------------------------------------------------------

def _base_url():
    from flask import current_app, has_request_context, request
    base = current_app.config.get("PUBLIC_BASE_URL")
    if not base and has_request_context():
        base = request.host_url
    return (base or "").rstrip("/")


def shop_url(tenant):
    """Absolute link to the business's shop, for sharing on WhatsApp."""
    return f"{_base_url()}/o/{tenant.slug}/shop"


def absolute(path):
    return _base_url() + path


def order_path(slug, invoice):
    return f"/o/{slug}/shop/order/{invoice.pay_token}"


def wa_link(number, text):
    """https://wa.me link; ``number`` empty means "pick a contact"."""
    return f"https://wa.me/{number or ''}?text={quote(text, safe='')}"


def qr_svg(data):
    """The QR code of ``data`` as an inline SVG string (segno)."""
    import segno
    return segno.make(data, error="m").svg_inline(scale=5, border=2, dark="#111")


# --- catalogue --------------------------------------------------------------------

def icon_of(item):
    """A safe Font Awesome class from the item's stored icon, else the default."""
    for token in (item.icon or "").lower().split():
        if token in _ICON_STYLES:
            continue
        if _ICON.match(token):
            return token
    return DEFAULT_ICON


def sold_online(item):
    return not (item.is_controlled or item.requires_prescription) and money(item.price) > 0


def _sellable_by_item(ids, today):
    """{item_id: units a sale could take now} for batch-tracked items."""
    if not ids:
        return {}
    rows = (db.session.query(Batch.inventory_item_id, func.sum(Batch.quantity_on_hand))
            .filter(Batch.inventory_item_id.in_(ids), Batch.quantity_on_hand > 0,
                    Batch.quarantined.is_(False), Batch.recalled_at.is_(None),
                    db.or_(Batch.expiry_date.is_(None), Batch.expiry_date >= today))
            .group_by(Batch.inventory_item_id).all())
    return {item_id: int(total or 0) for item_id, total in rows}


def _stock_label(units):
    return "few" if units <= FEW_LEFT else "in"


def variant_price(item, variant):
    if variant is not None and variant.price_override is not None:
        return money(variant.price_override)
    return money(item.price)


def catalogue(today=None):
    """What the shop shows: in-stock items with price, category, icon and a
    stock label ("in" or "few"), never a count. Variant items list their
    sizes that are in stock."""
    today = today or today_nairobi()
    items = (InventoryItem.query
             .filter(InventoryItem.is_controlled.is_(False),
                     InventoryItem.requires_prescription.is_(False),
                     InventoryItem.price > 0, InventoryItem.stock > 0)
             .order_by(InventoryItem.name).all())
    plain = [i.id for i in items if not i.has_variants]
    sized = [i.id for i in items if i.has_variants]
    sellable = _sellable_by_item(plain, today)
    variants = {}
    if sized:
        for v in (ItemVariant.query.filter(ItemVariant.inventory_item_id.in_(sized),
                                           ItemVariant.stock > 0)
                  .order_by(ItemVariant.inventory_item_id, ItemVariant.id)):
            variants.setdefault(v.inventory_item_id, []).append(v)
    category_names = {c.id: c.name for c in Category.query.all()}
    out = []
    for item in items:
        category = (category_names.get(item.category_id) if item.category_id else None) \
            or (item.category or "").strip() or "Other"
        entry = {"id": item.id, "name": item.name, "description": item.description or "",
                 "unit": item.unit or "", "category": category, "icon": icon_of(item),
                 "price": float(money(item.price))}
        if item.has_variants:
            sizes = variants.get(item.id) or []
            if not sizes:
                continue
            entry["sizes"] = [{"id": v.id, "label": v.label,
                               "price": float(variant_price(item, v)),
                               "stock": _stock_label(int(v.stock or 0))} for v in sizes]
            entry["price"] = min(s["price"] for s in entry["sizes"])
            entry["stock"] = "few" if all(s["stock"] == "few" for s in entry["sizes"]) else "in"
        else:
            units = sellable.get(item.id, 0)
            if units <= 0:
                continue
            entry["stock"] = _stock_label(units)
        out.append(entry)
    return out


def has_prescription_items():
    return (db.session.query(InventoryItem.id)
            .filter(db.or_(InventoryItem.requires_prescription.is_(True),
                           InventoryItem.is_controlled.is_(True)))
            .first() is not None)


# --- placing an order --------------------------------------------------------------

@dataclass
class Checkout:
    name: str
    phone: str
    fulfilment: str
    address: str | None
    note: str | None


def _int(value, what):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ShopError(f"{what} must be a whole number")
    return number


def parse_lines(raw_items):
    """[services.orders.Line] from the browser's cart, priced here. Same
    item and size twice are added together."""
    if not isinstance(raw_items, list) or not raw_items:
        raise ShopError("Your cart is empty")
    if len(raw_items) > MAX_LINES:
        raise ShopError(f"At most {MAX_LINES} different items per order")
    wanted = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ShopError("Your cart could not be read. Refresh the page.")
        item_id = _int(raw.get("id"), "item")
        variant_id = raw.get("variant_id")
        variant_id = _int(variant_id, "size") if variant_id not in (None, "") else None
        qty = _int(raw.get("quantity"), "quantity")
        if qty <= 0:
            raise ShopError("Quantities must be 1 or more")
        key = (item_id, variant_id)
        wanted[key] = wanted.get(key, 0) + qty
    lines = []
    for (item_id, variant_id), qty in wanted.items():
        item = db.session.get(InventoryItem, item_id)
        if item is None:
            raise ShopError("An item in your cart is no longer sold. Refresh the page.")
        if not sold_online(item):
            raise ShopError(f"{item.name} is not sold online. Ask at the counter.")
        if qty > MAX_QTY:
            raise ShopError(f"At most {MAX_QTY} of {item.name} per order")
        variant = None
        if item.has_variants:
            if variant_id is None:
                raise ShopError(f"Choose a size for {item.name}")
            variant = db.session.get(ItemVariant, variant_id)
            if variant is None or variant.inventory_item_id != item.id:
                raise ShopError(f"That size of {item.name} is no longer sold. Refresh the page.")
        elif variant_id is not None:
            raise ShopError(f"{item.name} has no sizes. Refresh the page.")
        lines.append(order_service.Line(item=item, quantity=qty,
                                        unit_price=variant_price(item, variant),
                                        variant=variant))
    return lines


def _label(line):
    return line.item.name + (f" ({line.variant.label})" if line.variant else "")


def quote_lines(lines):
    """Server prices for the cart: lines and the total, all Decimal."""
    out = [{"id": l.item.id, "variant_id": l.variant.id if l.variant else None,
            "name": _label(l), "quantity": l.quantity, "unit_price": l.unit_price,
            "subtotal": l.unit_price * l.quantity} for l in lines]
    return out, sum((row["subtotal"] for row in out), Decimal("0"))


def parse_checkout(body, tenant):
    name = " ".join(str(body.get("name") or "").split())[:80]
    if len(name) < 2:
        raise ShopError("Enter your name")
    try:
        phone = normalize_ke_phone(str(body.get("phone") or "").strip())
    except InvalidPhone:
        raise ShopError("Enter a Kenyan mobile number, like 0712 345 678")
    fulfilment = str(body.get("fulfilment") or "pickup").strip()
    if fulfilment not in ("pickup", "delivery"):
        raise ShopError("Choose pickup or delivery")
    address = " ".join(str(body.get("address") or "").split())[:255] or None
    if fulfilment == "delivery":
        if not tenant.shop_delivery:
            raise ShopError("This shop does not deliver. Choose pickup.")
        if not address or len(address) < 3:
            raise ShopError("Enter where to deliver: area, street or a landmark")
    else:
        address = None
    note = " ".join(str(body.get("note") or "").split())[:300] or None
    return Checkout(name=name, phone=phone, fulfilment=fulfilment, address=address, note=note)


def find_or_create_customer(name, phone):
    """The customer with this phone, or a new one. An existing customer's
    name is never changed from a public page. Call under lock_tenant()."""
    customer = Customer.query.filter_by(phone_number=phone).first()
    if customer is not None:
        return customer
    n = (db.session.query(func.max(Customer.id)).scalar() or 0) + 1
    while Customer.query.filter_by(code=f"S{n:05d}").first() is not None:
        n += 1
    customer = Customer(name=name, phone_number=phone, code=f"S{n:05d}")
    db.session.add(customer)
    db.session.flush()
    return customer


def place_order(body, tenant, channel="shop"):
    """Place a shop order from the request body. Returns (order, invoice,
    lines). Raises ShopError with a message for the customer; the caller
    rolls back."""
    if channel not in SHOP_CHANNELS:
        raise ShopError("unknown channel")
    lines = parse_lines(body.get("items"))
    checkout = parse_checkout(body, tenant)
    lock_tenant()               # one writer per business: no duplicate customer
    customer = find_or_create_customer(checkout.name, checkout.phone)
    via = "WhatsApp" if channel == "whatsapp" else "online shop"
    notes = f"Ordered on the {via} for {checkout.fulfilment}"
    if checkout.note:
        notes += f". Customer note: {checkout.note}"
    try:
        order, invoice = order_service.create_order(
            lines, customer_id=customer.id, channel=channel, pay_now=True,
            delivery_address=checkout.address, notes=notes)
    except stock.InsufficientStock as e:
        raise OutOfStock(f"Not enough {e.item_name} in stock for {e.wanted}. "
                        "Lower the quantity or remove it.")
    except (order_service.OrderError, stock.StockError) as e:
        raise ShopError(str(e))
    return order, invoice, lines


def whatsapp_text(business, order, lines, total, address, pay_link, status_link):
    rows = [f"Hello {business}, I have placed order {order.order_number}:"]
    for line in lines:
        rows.append(f"- {_label(line)} x{line.quantity}: KES {line.unit_price * line.quantity:,.2f}")
    rows.append(f"Total: KES {total:,.2f}")
    rows.append(f"Delivery to: {address}" if address else "I will pick it up.")
    rows.append(f"Pay with M-Pesa: {pay_link}")
    rows.append(f"Order status: {status_link}")
    return "\n".join(rows)


# --- the customer's order page -------------------------------------------------------

STATUS_TEXT = {
    "pending": "Waiting for payment",
    "paid_pending": "Paid. The shop is preparing your order.",
    "ready": "Ready for pickup",
    "ready_delivery": "Packed and ready to go out",
    "dispatched": "Out for delivery",
    "collected": "Collected. Thank you!",
    "delivered": "Delivered. Thank you!",
    "released": "Not paid in time, so the order was cancelled",
    "restock_needed": "Paid after the hold ended. The shop will contact you.",
    "cancelled": "Cancelled",
}


def public_status(order, invoice):
    status = order.fulfillment_status
    delivery = bool(order.delivery_address)
    if status == "pending":
        key = "paid_pending" if invoice is not None and invoice.status == "paid" else "pending"
    elif status == "ready" and delivery:
        key = "ready_delivery"
    elif status == "collected" and delivery:
        key = "delivered"
    else:
        key = status
    return {"key": key, "text": STATUS_TEXT.get(key, status),
            "final": status in ("collected", "released", "cancelled")}


def order_for_token(token):
    """(order, invoice) for a shop order's pay token, or (None, None)."""
    invoice = Invoice.query.filter_by(pay_token=token).first()
    if invoice is None or invoice.pay_token != token or invoice.order_id is None:
        return None, None
    order = db.session.get(Order, invoice.order_id)
    if order is None or order.channel not in SHOP_CHANNELS:
        return None, None
    return order, invoice


# --- staff: the online orders list and fulfilment ------------------------------------

FULFILMENT_ACTIONS = ("ready", "dispatched", "collected", "delivered", "cancelled")
OPEN_STATES = ("pending", "ready", "dispatched")


class FulfilmentError(ValueError):
    pass


class FulfilmentForbidden(FulfilmentError):
    """The user's role may not make this move (403, not 409)."""


def _paid_order_ids():
    return select(Invoice.order_id).where(Invoice.order_id.isnot(None),
                                          Invoice.status == "paid")


def list_orders(status=None, channel=None, paid=None, customer_id=None, page=1, per_page=50):
    """Remote orders, newest first. Campaign orders have their own page."""
    q = Order.query.filter(Order.campaign_id.is_(None))
    if status == "open":
        q = q.filter(Order.fulfillment_status.in_(OPEN_STATES))
    elif status:
        q = q.filter(Order.fulfillment_status == status)
    if channel:
        q = q.filter(Order.channel == channel)
    if paid == "paid":
        q = q.filter(Order.id.in_(_paid_order_ids()))
    elif paid == "unpaid":
        q = q.filter(Order.id.notin_(_paid_order_ids()))
    if customer_id:
        q = q.filter(Order.customer_id == customer_id)
    total = q.count()
    orders = q.order_by(Order.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return orders, total


def waiting_query():
    """Paid orders nobody has started on yet."""
    return (Order.query.filter(Order.campaign_id.is_(None), Order.fulfillment_status == "pending",
                               Order.id.in_(_paid_order_ids())))


def waiting_summary():
    q = waiting_query()
    return {"count": q.count(),
            "latest_id": db.session.query(func.max(Order.id)).filter(
                Order.campaign_id.is_(None), Order.fulfillment_status == "pending",
                Order.id.in_(_paid_order_ids())).scalar()}


# Medicine on a prescription leaves the pharmacy only through a pharmacist
# (the pharmacy hand-over route has the same rule); anyone on the till may
# still cancel it.
PRESCRIPTION_ROLES = ("pharmacist", "admin", "superuser")


def may_move(order, role):
    """Whether ``role`` may mark this order ready, dispatched or collected."""
    return order.prescription_id is None or role in PRESCRIPTION_ROLES


def _actions(order, invoice, role=None):
    status = order.fulfillment_status
    if status in ("collected", "cancelled"):
        return []
    actions = []
    if _may_hand_over(invoice) and may_move(order, role):
        if status == "pending":
            actions.append("ready")
        if order.delivery_address and status in ("pending", "ready"):
            actions.append("dispatched")
        if status in OPEN_STATES:
            actions.append("delivered" if order.delivery_address else "collected")
    actions.append("cancelled")
    return actions


def _may_hand_over(invoice):
    if invoice is None or invoice.status == "void":
        return False
    return invoice.status == "paid" or invoice.kind == "credit"


def order_row(order, now=None, role=None):
    now = now or datetime.utcnow()
    invoice = order_service.invoice_for(order)
    customer = db.session.get(Customer, order.customer_id) if order.customer_id else None
    items = OrderItem.query.filter_by(order_id=order.id).order_by(OrderItem.id).all()
    age = int((now - order.time).total_seconds() // 60) if order.time else None
    return {
        "id": order.id, "order_number": order.order_number or f"#{order.id}",
        "customer_name": customer.name if customer else None,
        "customer_phone": customer.phone_number if customer else None,
        "items": [{"name": i.item_name, "quantity": i.quantity,
                   "subtotal": float(i.subtotal)} for i in items] or
                 [{"name": order.item, "quantity": 1, "subtotal": float(order.amount)}],
        "total": float(order.amount),
        "paid": bool(invoice is not None and invoice.status == "paid"),
        "invoice_status": invoice.status if invoice else None,
        "invoice_kind": invoice.kind if invoice else None,
        "notes": invoice.notes if invoice else None,
        "fulfilment": "delivery" if order.delivery_address else "pickup",
        "delivery_address": order.delivery_address,
        "channel": order.channel,
        "fulfillment_status": order.fulfillment_status,
        "prescription_id": order.prescription_id,
        "time": order.time.isoformat() if order.time else None,
        "age_minutes": age,
        "actions": _actions(order, invoice, role),
    }


def set_fulfilment(order, status, user_id=None, now=None, role=None):
    """Move a remote order on. Returns the order. Raises FulfilmentError, or
    FulfilmentForbidden when ``role`` may not move a prescription order.
    The caller commits, then calls :func:`notify`."""
    if status == "delivered":
        status = "collected"
    if status not in ("ready", "dispatched", "collected", "cancelled"):
        raise FulfilmentError("status must be ready, dispatched, collected, delivered or cancelled")
    if status != "cancelled" and not may_move(order, role):
        raise FulfilmentForbidden("A pharmacist hands over prescription orders.")
    if order.campaign_id is not None:
        raise FulfilmentError("campaign orders are handed over with their pickup code")
    if status == "cancelled":
        try:
            return order_service.cancel(order, user_id=user_id)
        except order_service.OrderError as e:
            raise FulfilmentError(f"cannot cancel: {e}")
    if status == "collected" and order.prescription_id is not None:
        # A refill: hand over through the pharmacy, which counts the refill.
        from verticals.pharmacy import service as pharmacy
        try:
            pharmacy.hand_over_refill(order, user_id, now)
        except pharmacy.PharmacyError as e:
            raise FulfilmentError(str(e))
        return order
    lock_tenant()
    order = order_service.lock_order(order)
    current = order.fulfillment_status
    invoice = order_service.invoice_for(order)
    if invoice is not None:
        invoice = billing.recompute(invoice)
    if not _may_hand_over(invoice):
        raise FulfilmentError("This order is not paid yet. Wait for the M-Pesa payment.")
    allowed = {"ready": ("pending",), "dispatched": ("pending", "ready"),
               "collected": OPEN_STATES}[status]
    if current not in allowed:
        raise FulfilmentError(f"The order is {current}; it cannot be marked {status}.")
    if status == "dispatched" and not order.delivery_address:
        raise FulfilmentError("This is a pickup order; mark it collected instead.")
    order.fulfillment_status = status
    order.reserved_until = None
    if status == "collected":
        order.picked_up_at = now or datetime.utcnow()
        order.picked_up_by = user_id
    db.session.flush()
    return order


def notify(order):
    """Text the customer that the order is ready or on its way. Call after
    commit. Returns the SmsMessage, or None when there is nothing to say."""
    from services import sms_service
    if order.customer_id is None:
        return None
    if order.fulfillment_status == "ready" and not order.delivery_address:
        kind, what = "order_ready", "is ready for pickup"
    elif order.fulfillment_status == "dispatched":
        kind, what = "order_dispatched", "is out for delivery"
    else:
        return None
    customer = db.session.get(Customer, order.customer_id)
    tenant = db.session.get(Tenant, order.tenant_id)
    if customer is None:
        return None
    body = f"Your order {order.order_number} {what}. -{tenant.name if tenant else ''}".rstrip(" -")
    return sms_service.send(customer.phone_number, body, kind,
                            dedupe_key=f"{kind}:{order.id}")

