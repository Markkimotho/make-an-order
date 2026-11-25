"""Remote orders: an order, its invoice and its stock, created together.

``create_order`` writes the Order, its lines and its Invoice and takes the
stock (first-expiry-first-out, through services/stock.py) in one go:

* pay-now orders (public links, refills, campaigns) get a ``sale`` invoice
  with no due date and hold their stock until ``reserved_until``
  (15 minutes; campaigns extend it). An unpaid hold is released by
  :func:`release_expired` (run by ``flask payments-sweep``): stock goes back
  to the batches it came from and the invoice becomes ``expired``, so an
  abandoned checkout never looks like debt.
* credit orders get a ``credit`` invoice with a due date and keep their
  stock: no payment is awaited.

Money that arrives after a release still counts (billing reopens an expired
invoice); the order is then marked ``restock_needed`` for a person to fill.
A paid order is never released.

Lock order: billing lock (tenant), then the order, then the invoice, then
stock. Runs in the bound business; callers commit.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from models import (ContainerMovement, InventoryItem, Invoice, ItemVariant, LineBatch, Order,
                    OrderItem, db)
from services import billing, stock
from services.billing.invoices import lock_tenant, money

logger = logging.getLogger(__name__)

HOLD = timedelta(minutes=15)
OPEN_FULFILMENT = ("pending", "ready", "dispatched")


class OrderError(ValueError):
    """An order that cannot be placed or changed."""


@dataclass
class Line:
    item: InventoryItem
    quantity: int
    unit_price: Decimal
    variant: ItemVariant | None = None


def _next_number():
    last = (db.session.query(Order.order_number).filter(Order.order_number.like("ORD%"))
            .order_by(Order.id.desc()).limit(1).scalar())
    n = int(last[3:]) + 1 if last and last[3:].isdigit() else 1
    return f"ORD{n:06d}"


def _summary(lines):
    parts = [line.item.name + (f" ({line.variant.label})" if line.variant else "")
             + (f" ×{line.quantity}" if line.quantity != 1 else "") for line in lines]
    text = ", ".join(parts)
    return text if len(text) <= 255 else text[:252] + "..."


def create_order(lines, *, customer_id=None, channel="staff", pay_now, due_date=None,
                 delivery_address=None, prescription_id=None, hold=HOLD, now=None,
                 user_id=None, notes=None, member_id=None, campaign_id=None):
    """Place an order. Returns (order, invoice). Raises OrderError, or
    stock.StockError when a line cannot be filled (nothing is written that
    the caller's rollback will not undo)."""
    now = now or datetime.utcnow()
    if not lines:
        raise OrderError("an order needs at least one item")
    for line in lines:
        if int(line.quantity) <= 0:
            raise OrderError("quantities must be whole numbers above zero")
        if money(line.unit_price) < 0:
            raise OrderError("prices cannot be negative")
        if line.item.has_variants and line.variant is None:
            raise OrderError(f"{line.item.name} comes in sizes; choose one")
        if line.item.is_controlled:
            raise OrderError(f"{line.item.name} is a controlled drug; it is dispensed only "
                             "at the counter by a pharmacist")
    if not pay_now and due_date is None:
        raise OrderError("a credit order needs a due date")
    total = sum((money(line.unit_price) * int(line.quantity) for line in lines), Decimal("0"))
    if total <= 0:
        raise OrderError("order total must be greater than zero")

    lock_tenant()
    order = Order(customer_id=customer_id, item=_summary(lines), amount=total, status="pending",
                  order_number=_next_number(), channel=channel, fulfillment_status="pending",
                  delivery_address=delivery_address, prescription_id=prescription_id,
                  member_id=member_id, campaign_id=campaign_id,
                  reserved_until=now + hold if pay_now else None)
    db.session.add(order)
    db.session.flush()
    for line in lines:
        oi = OrderItem(order_id=order.id, inventory_item_id=line.item.id,
                       item_name=line.item.name + (f" ({line.variant.label})" if line.variant else ""),
                       quantity=int(line.quantity), unit_price=money(line.unit_price),
                       subtotal=money(line.unit_price) * int(line.quantity),
                       variant_id=line.variant.id if line.variant else None)
        db.session.add(oi)
        db.session.flush()
        stock.take(line.item, int(line.quantity), order_item=oi, variant=line.variant,
                   reason="reserve" if pay_now else "sale", user_id=user_id,
                   note=f"order {order.order_number}")
    invoice = billing.create_invoice(total=total, kind="sale" if pay_now else "credit",
                                     customer_id=customer_id, order_id=order.id,
                                     due_date=None if pay_now else due_date, notes=notes,
                                     member_id=member_id)
    return order, invoice


def lock_order(order):
    return db.session.execute(select(Order).where(Order.id == order.id).with_for_update()
                              .execution_options(populate_existing=True)).scalar_one()


def invoice_for(order):
    return (Invoice.query.filter_by(order_id=order.id).order_by(Invoice.id.desc()).first())


def _give_stock_back(order, reason, user_id=None):
    """Return every unit the order still has out, to its batches or size."""
    for oi in OrderItem.query.filter_by(order_id=order.id).order_by(OrderItem.id):
        if not oi.inventory_item_id:
            continue
        item = db.session.get(InventoryItem, oi.inventory_item_id)
        if item is None:
            continue
        if item.has_variants:
            variant = db.session.get(ItemVariant, oi.variant_id) if oi.variant_id else None
            if variant is not None:
                stock.give_back(item, oi.quantity, variant=variant, reason=reason,
                                user_id=user_id, note=f"order {order.order_number}")
            continue
        records = LineBatch.query.filter_by(order_item_id=oi.id).all()
        if not records:
            # Ordered before batches existed: no record of where it came
            # from, so it goes back on hold (services/stock.py quarantine).
            if oi.quantity > 0:
                stock.give_back(item, oi.quantity, reason=reason, user_id=user_id,
                                note=f"order {order.order_number or order.id}")
            continue
        out = sum(lb.quantity - (lb.returned_quantity or 0) for lb in records)
        if out > 0:
            stock.give_back(item, out, order_item=oi, reason=reason, user_id=user_id,
                            note=f"order {order.order_number or order.id}")


def _reverse_containers(order, user_id=None):
    """Containers issued with an order that did not go out come back off
    the duka's balance."""
    issued = (db.session.query(ContainerMovement.customer_id, ContainerMovement.inventory_item_id,
                               db.func.sum(ContainerMovement.delta))
              .filter(ContainerMovement.order_id == order.id)
              .group_by(ContainerMovement.customer_id, ContainerMovement.inventory_item_id).all())
    for customer_id, item_id, total in issued:
        if total:
            db.session.add(ContainerMovement(customer_id=customer_id, inventory_item_id=item_id,
                                             delta=-int(total), order_id=order.id,
                                             note=f"{order.order_number} did not go out",
                                             created_by=user_id))


def release(order, *, reason="release", user_id=None, force=False):
    """Give an unpaid order's held stock back and expire its invoice.

    Refuses (returns False) when money has been paid, unless ``force``
    (a campaign closing with part-paid orders: the money stays as the
    payer's credit). Safe to call twice.
    """
    lock_tenant()
    order = lock_order(order)
    if order.fulfillment_status in ("released", "restock_needed", "cancelled", "collected"):
        return False
    invoice = invoice_for(order)
    if invoice is not None:
        invoice = billing.recompute(invoice)
        if money(invoice.amount_paid) > 0 and not force:
            order.reserved_until = None          # paid: the stock is theirs
            return False
    _give_stock_back(order, reason, user_id)
    _reverse_containers(order, user_id)
    if invoice is not None and invoice.status in ("open", "partial"):
        if money(invoice.amount_paid) > 0:
            billing.void_invoice(invoice, user_id, reason="void", roll=False)
        else:
            invoice.status = "expired"
    order.fulfillment_status = "released"
    order.reserved_until = None
    db.session.flush()
    return True


def cancel(order, *, user_id=None, reason="cancel"):
    """Staff cancel: stock back, invoice voided (money paid becomes the
    customer's credit)."""
    lock_tenant()
    order = lock_order(order)
    if order.fulfillment_status in ("cancelled", "collected"):
        raise OrderError(f"order is {order.fulfillment_status}")
    if order.fulfillment_status not in ("released", "restock_needed"):
        _give_stock_back(order, reason, user_id)     # released orders already gave it back
        _reverse_containers(order, user_id)
    invoice = invoice_for(order)
    if invoice is not None and invoice.status != "void":
        billing.void_invoice(invoice, user_id, reason="void")
    order.fulfillment_status = "cancelled"
    order.status = "cancelled"
    order.reserved_until = None
    db.session.flush()
    return order


def extend_hold(order, until):
    order = lock_order(order)
    if order.fulfillment_status == "pending":
        order.reserved_until = until
    return order


def release_expired(now=None):
    """Release every unpaid hold past its time, and flag released orders
    that were paid afterwards. Commits per order. Returns counts."""
    now = now or datetime.utcnow()
    counts = {"released": 0, "kept_paid": 0, "restock_needed": 0}
    due = [o.id for o in Order.query.filter(Order.reserved_until.isnot(None),
                                            Order.reserved_until < now,
                                            Order.fulfillment_status == "pending")
           .order_by(Order.id)]
    for order_id in due:
        order = db.session.get(Order, order_id)
        if release(order):
            counts["released"] += 1
        else:
            counts["kept_paid"] += 1
        db.session.commit()
    for order in Order.query.filter(Order.fulfillment_status == "released").order_by(Order.id):
        invoice = invoice_for(order)
        if invoice is not None and money(invoice.amount_paid) > 0 and invoice.status != "void":
            order = lock_order(order)
            if order.fulfillment_status == "released":
                order.fulfillment_status = "restock_needed"
                counts["restock_needed"] += 1
                logger.warning("order %s paid after its stock was released", order.order_number)
            db.session.commit()
    return counts
