"""Who received a recalled batch, and texting them.

Reads only. The recall itself (hold the batch for good) and any write-off
of its units are in services/stock.py, the only writer of stock.

A buyer is anyone a batch's units went to and stayed with: LineBatch rows
of completed or refunded counter sales, and of remote orders that were not
cancelled, counted net of units returned (``returned_quantity``). A sale
with no customer on it is a walk-in: counted, but nobody to text.
"""
from auth.tenancy import current_tenant_id
from models import (TXN_COMPLETED, TXN_REFUNDED, Customer, InventoryItem, LineBatch, Order,
                    OrderItem, SmsMessage, Tenant, Transaction, TransactionItem, db)
from services import audit_service, sms_service

SMS_KIND = "recall"


class RecallError(ValueError):
    """A recall action that cannot be done."""


def dedupe_key(batch, customer_id):
    return f"recall:{batch.id}:{customer_id}"


def _sale_rows(batch):
    return (db.session.query(LineBatch, Transaction)
            .join(TransactionItem, TransactionItem.id == LineBatch.transaction_item_id)
            .join(Transaction, Transaction.id == TransactionItem.transaction_id)
            .filter(LineBatch.batch_id == batch.id,
                    Transaction.status.in_((TXN_COMPLETED, TXN_REFUNDED)))
            .order_by(Transaction.created_at, LineBatch.id).all())


def _order_rows(batch):
    return (db.session.query(LineBatch, Order)
            .join(OrderItem, OrderItem.id == LineBatch.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .filter(LineBatch.batch_id == batch.id, Order.status != "cancelled")
            .order_by(Order.time, LineBatch.id).all())


def recall_buyers(batch):
    """Every sale or order that kept units of ``batch``, oldest first:
    dicts with source, ref, date, customer_id, customer_name, phone and
    quantity (net of returns)."""
    rows = []
    for lb, txn in _sale_rows(batch):
        rows.append(("sale", txn.transaction_ref, txn.created_at, txn.customer_id, lb))
    for lb, order in _order_rows(batch):
        rows.append(("order", order.order_number or f"#{order.id}", order.time,
                     order.customer_id, lb))
    ids = {r[3] for r in rows if r[3]}
    customers = ({c.id: c for c in Customer.query.filter(Customer.id.in_(ids)).all()}
                 if ids else {})
    out = []
    for source, ref, when, customer_id, lb in rows:
        net = int(lb.quantity or 0) - int(lb.returned_quantity or 0)
        if net <= 0:
            continue
        customer = customers.get(customer_id)
        out.append({"source": source, "ref": ref,
                    "date": when.isoformat() if when else None,
                    "customer_id": customer.id if customer else None,
                    "customer_name": customer.name if customer else "Walk-in",
                    "phone": (customer.phone_number or None) if customer else None,
                    "quantity": net})
    out.sort(key=lambda r: r["date"] or "")
    return out


def buyers_summary(batch):
    """The buyer rows plus totals: units out with customers, with walk-ins,
    and how many distinct customers can be texted."""
    rows = recall_buyers(batch)
    customers = {r["customer_id"]: r for r in rows if r["customer_id"]}
    return {"buyers": rows,
            "units_out": sum(r["quantity"] for r in rows),
            "walk_in_units": sum(r["quantity"] for r in rows if not r["customer_id"]),
            "customers": len(customers),
            "customers_with_phone": sum(1 for r in customers.values() if r["phone"])}


def message(batch):
    """The recall text: business, medicine, batch, what to do."""
    tenant = db.session.get(Tenant, current_tenant_id())
    item = db.session.get(InventoryItem, batch.inventory_item_id)
    business = (tenant.name if tenant else "Your pharmacy")[:40]
    name = (item.name if item else "a medicine")[:40]
    return (f"{business}: {name} batch {batch.batch_number} has been recalled. "
            "Please stop using it and bring it back to the pharmacy.")


def notify_recall(batch, user_id=None):
    """Text every customer who has units of a recalled batch, once each
    (dedupe key ``recall:<batch_id>:<customer_id>``). Returns counts:
    sent, failed (provider refused or bad number), already_sent, no_phone
    (buyers with no number to text: customers without one, and each sale
    with no customer on it) and walk_in (how many of no_phone were sales
    with no customer).

    Commits (sms_service.send does); call it with a clean session.
    """
    if batch.recalled_at is None:
        raise RecallError(f"batch {batch.batch_number} is not recalled")
    rows = recall_buyers(batch)
    walk_in = sum(1 for r in rows if not r["customer_id"])
    counts = {"sent": 0, "failed": 0, "no_phone": walk_in, "already_sent": 0,
              "walk_in": walk_in}
    body = message(batch)
    seen = set()
    for row in rows:
        customer_id = row["customer_id"]
        if not customer_id or customer_id in seen:
            continue
        seen.add(customer_id)
        if not row["phone"]:
            counts["no_phone"] += 1
            continue
        key = dedupe_key(batch, customer_id)
        if SmsMessage.query.filter_by(dedupe_key=key).first() is not None:
            counts["already_sent"] += 1
            continue
        msg = sms_service.send(row["phone"], body, SMS_KIND, dedupe_key=key)
        counts["sent" if msg is not None and msg.status == "sent" else "failed"] += 1
    audit_service.log(user_id, "stock.recall_notify", "Batch", batch.id, None, counts)
    db.session.commit()
    return counts
