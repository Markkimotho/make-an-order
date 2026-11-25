"""Distributor rules and queries. Runs in the bound business; callers commit.

Credit ceiling: placing a credit order takes the billing lock, then the
duka's row, then checks what it owes on credit (less its unapplied money)
plus the new order against its credit limit. Two orders at once cannot
both slip under the ceiling. Over the ceiling the same order can still be
paid now by M-Pesa: only more deni is refused, never the sale.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from models import (ContainerMovement, Customer, CustomerPhone, InventoryItem, Invoice,
                    ItemVariant, TierPrice, db)
from services import billing, sms_service
from services import orders as order_service
from services.billing.invoices import lock_tenant, money
from services.billing.matching import C2B_MATCHERS, Match
from services.clock import today_nairobi
from services.phone import InvalidPhone, normalize_ke_phone

AGING_BUCKETS = ("not_due", "0_7", "8_14", "15_plus")


class CreditCeiling(order_service.OrderError):
    def __init__(self, customer, owed, wanted):
        self.limit = money(customer.credit_limit)
        self.owed = money(owed)
        self.wanted = money(wanted)
        self.available = max(self.limit - self.owed, Decimal("0"))
        super().__init__(f"This order (KES {self.wanted:,.2f}) is over {customer.name}'s credit: "
                         f"KES {self.available:,.2f} of {self.limit:,.2f} left. Pay now instead.")


def lock_customer(customer_id):
    return db.session.execute(select(Customer).where(Customer.id == customer_id)
                              .with_for_update().execution_options(populate_existing=True)
                              ).scalar_one_or_none()


def price_for(customer, item):
    """The duka's tier price for ``item``, else the item's price."""
    if customer is not None and customer.price_tier_id:
        row = TierPrice.query.filter_by(price_tier_id=customer.price_tier_id,
                                        inventory_item_id=item.id).first()
        if row is not None:
            return money(row.price)
    return money(item.price)


def lines_for(customer, cart):
    """Order lines from ``[{"inventory_item_id"|"id", "quantity", "variant_id"?}]``
    at the duka's prices. Quantities of one item are added up."""
    wanted = {}
    for entry in cart or []:
        item_id = entry.get("inventory_item_id") or entry.get("id")
        try:
            qty = int(entry.get("quantity", 0))
        except (TypeError, ValueError):
            raise order_service.OrderError("quantities must be whole numbers")
        if not item_id or qty <= 0:
            continue
        key = (int(item_id), entry.get("variant_id"))
        wanted[key] = wanted.get(key, 0) + qty
    lines = []
    for (item_id, variant_id), qty in wanted.items():
        item = db.session.get(InventoryItem, item_id)
        if item is None:
            raise order_service.OrderError(f"item {item_id} not found")
        variant = db.session.get(ItemVariant, int(variant_id)) if variant_id else None
        if variant is not None and variant.inventory_item_id != item.id:
            raise order_service.OrderError("that size belongs to another item")
        price = (money(variant.price_override) if variant is not None
                 and variant.price_override is not None else price_for(customer, item))
        lines.append(order_service.Line(item=item, quantity=qty, unit_price=price,
                                        variant=variant))
    if not lines:
        raise order_service.OrderError("the order is empty")
    return lines


def place_order(customer_id, cart, *, pay_now, channel="staff", user_id=None, now=None,
                delivery_address=None):
    """A duka's order: on credit within the ceiling, or paid now."""
    lock_tenant()
    customer = lock_customer(customer_id)
    if customer is None:
        raise order_service.OrderError("customer not found")
    lines = lines_for(customer, cart)
    total = sum((money(l.unit_price) * l.quantity for l in lines), Decimal("0"))
    due = None
    if not pay_now:
        owed = billing.customer_exposure(customer.id)
        if owed + total > money(customer.credit_limit):
            raise CreditCeiling(customer, owed, total)
        due = billing.credit_due_date(customer, today_nairobi(now))
    order, invoice = order_service.create_order(
        lines, customer_id=customer.id, channel=channel, pay_now=pay_now, due_date=due,
        user_id=user_id, now=now, delivery_address=delivery_address)
    for line in lines:
        if line.item.is_returnable_container:
            db.session.add(ContainerMovement(customer_id=customer.id, inventory_item_id=line.item.id,
                                             delta=line.quantity, order_id=order.id,
                                             note=f"issued with {order.order_number}",
                                             created_by=user_id))
    db.session.flush()
    return order, invoice


def credit_summary(customer):
    owed = billing.customer_exposure(customer.id)
    limit = money(customer.credit_limit)
    return {"limit": float(limit), "owed": float(owed),
            "available": float(max(limit - owed, Decimal("0")))}


# --- containers ---------------------------------------------------------------------

def record_containers(customer_id, item_id, delta, note=None, user_id=None):
    item = db.session.get(InventoryItem, item_id)
    if item is None or not item.is_returnable_container:
        raise order_service.OrderError("not a returnable container item")
    if db.session.get(Customer, customer_id) is None:
        raise order_service.OrderError("customer not found")
    delta = int(delta)
    if delta == 0:
        raise order_service.OrderError("delta must not be zero")
    balance = container_balance(customer_id, item_id)
    if balance + delta < 0:
        raise order_service.OrderError(f"the duka only holds {balance}")
    row = ContainerMovement(customer_id=customer_id, inventory_item_id=item_id, delta=delta,
                            note=note, created_by=user_id)
    db.session.add(row)
    db.session.flush()
    return row


def container_balance(customer_id, item_id):
    return int(db.session.query(db.func.coalesce(db.func.sum(ContainerMovement.delta), 0))
               .filter(ContainerMovement.customer_id == customer_id,
                       ContainerMovement.inventory_item_id == item_id).scalar() or 0)


def container_balances(customer_id=None):
    q = (db.session.query(ContainerMovement.customer_id, ContainerMovement.inventory_item_id,
                          db.func.sum(ContainerMovement.delta))
         .group_by(ContainerMovement.customer_id, ContainerMovement.inventory_item_id))
    if customer_id is not None:
        q = q.filter(ContainerMovement.customer_id == customer_id)
    out = []
    for cid, item_id, balance in q.all():
        if not balance:
            continue
        customer, item = db.session.get(Customer, cid), db.session.get(InventoryItem, item_id)
        out.append({"customer_id": cid, "customer": customer.name if customer else None,
                    "inventory_item_id": item_id, "item": item.name if item else None,
                    "balance": int(balance)})
    return sorted(out, key=lambda r: (r["customer"] or "", r["item"] or ""))


# --- deni aging, reminders, eTIMS ----------------------------------------------------

def _bucket(days_past):
    if days_past < 0:
        return "not_due"
    if days_past <= 7:
        return "0_7"
    if days_past <= 14:
        return "8_14"
    return "15_plus"


def aging(today=None):
    """Credit invoices with a balance, by days past their due date. Pay-now
    and expired invoices are not debt and are left out."""
    today = today or today_nairobi()
    invoices = (Invoice.query
                .filter(Invoice.kind.in_(billing.CREDIT_KINDS),
                        Invoice.status.in_(("open", "partial")), Invoice.due_date.isnot(None))
                .order_by(Invoice.due_date, Invoice.id).all())
    buckets = {b: [] for b in AGING_BUCKETS}
    totals = defaultdict(Decimal)
    per_customer = defaultdict(lambda: defaultdict(Decimal))
    for inv in invoices:
        balance = money(inv.balance)
        if balance <= 0:
            continue
        days = (today - inv.due_date).days
        bucket = _bucket(days)
        customer = db.session.get(Customer, inv.customer_id) if inv.customer_id else None
        buckets[bucket].append({"invoice_id": inv.id, "invoice_number": inv.invoice_number,
                                "customer_id": inv.customer_id,
                                "customer": customer.name if customer else None,
                                "due_date": inv.due_date.isoformat(), "days_past": days,
                                "balance": float(balance), "etims": etims_badge(inv)})
        totals[bucket] += balance
        if inv.customer_id:
            per_customer[inv.customer_id][bucket] += balance
    return {"buckets": buckets, "totals": {b: float(totals[b]) for b in AGING_BUCKETS},
            "by_customer": {cid: {b: float(v) for b, v in row.items()}
                            for cid, row in per_customer.items()}}


def remind(invoice, today=None):
    """Text the duka its balance and pay link. Once per invoice per day."""
    today = today or today_nairobi()
    invoice = billing.recompute(invoice)
    if invoice.status not in ("open", "partial") or money(invoice.balance) <= 0:
        raise order_service.OrderError(f"invoice is {invoice.status}")
    customer = db.session.get(Customer, invoice.customer_id) if invoice.customer_id else None
    if customer is None:
        raise order_service.OrderError("invoice has no customer to remind")
    link = billing.pay_url(invoice)
    db.session.commit()                       # sms_service.send commits; nothing may be pending
    body = (f"{customer.name}: invoice {invoice.invoice_number} has KES "
            f"{money(invoice.balance):,.2f} due"
            + (f" (was due {invoice.due_date:%d %b})" if invoice.due_date
               and invoice.due_date < today else "")
            + f". Pay by M-Pesa: {link}")
    return sms_service.send(customer.phone_number, body, "credit_reminder",
                            dedupe_key=f"reminder:{invoice.id}:{today.isoformat()}")


def etims_badge(invoice):
    """Until live KRA submission exists: never imply the invoice is fiscalised."""
    if invoice.external_etims_ref:
        return {"status": "external", "label": f"CUIN {invoice.external_etims_ref}"}
    return {"status": "not_fiscalised", "label": "Not fiscalised"}


def add_phone(customer_id, phone, label=None):
    try:
        phone = normalize_ke_phone(phone)
    except InvalidPhone as e:
        raise order_service.OrderError(str(e))
    taken = (Customer.query.filter_by(phone_number=phone).first()
             or CustomerPhone.query.filter_by(phone=phone).first())
    if taken is not None:
        raise order_service.OrderError("that number already belongs to a customer")
    row = CustomerPhone(customer_id=customer_id, phone=phone, label=label)
    db.session.add(row)
    db.session.flush()
    return row


def customer_for_phone(phone):
    """The duka a number pays for: its own number or an alternate."""
    try:
        phone = normalize_ke_phone(phone)
    except InvalidPhone:
        return None
    customer = Customer.query.filter_by(phone_number=phone).first()
    if customer is not None:
        return customer
    alt = CustomerPhone.query.filter_by(phone=phone).first()
    return db.session.get(Customer, alt.customer_id) if alt else None


# --- Paybill: a duka pays with its account code --------------------------------------

def _match_account_ref(ref):
    customer = Customer.query.filter_by(account_ref=ref).first()
    if customer is None:
        return None
    return Match(customer_id=customer.id)


if _match_account_ref not in C2B_MATCHERS:
    C2B_MATCHERS.append(_match_account_ref)
