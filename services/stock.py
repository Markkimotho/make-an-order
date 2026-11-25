"""The only code that changes stock.

An item's stock lives in batches (each with an expiry date) or, for sized
goods, in variants. ``InventoryItem.stock`` is their total and is set here
and nowhere else (tests/test_no_bypass.py checks the source).

Sales take first-expiry-first-out from batches that have not expired by
today in Nairobi and are not quarantined; expired or quarantined stock
leaves only through an adjustment or write-off. Every unit taken for a sale
or order line is recorded in LineBatch, so a void or return puts it back in
the batch it came from, and never more than was taken. Goods returned with
no such record go to the batch named on the line if it exists, else into a
quarantined batch an admin must check before it can be sold.

Locking: the item row first, then its batches (in id order), then the
variant, so concurrent sales of the last unit cannot both succeed.
Callers commit.
"""
from datetime import datetime

from sqlalchemy import select

from models import Batch, InventoryItem, ItemVariant, LineBatch, StockMovement, db
from services.clock import today_nairobi

DEFAULT_BATCH = "UNBATCHED"
QUARANTINE_BATCH = "QUARANTINE"


class StockError(ValueError):
    """A stock change that cannot be made."""


class InsufficientStock(StockError):
    def __init__(self, item, wanted, sellable, expired=0):
        self.item_name, self.wanted, self.sellable, self.expired = (
            item.name, wanted, sellable, expired)
        extra = f" ({expired} more expired or on hold)" if expired else ""
        super().__init__(f"Only {sellable} of {item.name} in stock{extra}; {wanted} wanted")


def _lock_item(item):
    db.session.flush()        # never overwrite unsaved edits with the reload
    return db.session.execute(select(InventoryItem).where(InventoryItem.id == item.id)
                              .with_for_update()
                              .execution_options(populate_existing=True)).scalar_one()


def _locked_batches(item):
    return db.session.execute(select(Batch).where(Batch.inventory_item_id == item.id)
                              .order_by(Batch.id).with_for_update()
                              .execution_options(populate_existing=True)).scalars().all()


def _locked_variant(item, variant):
    if variant is None:
        raise StockError(f"{item.name} comes in sizes; choose one")
    row = db.session.execute(select(ItemVariant).where(ItemVariant.id == variant.id)
                             .with_for_update()
                             .execution_options(populate_existing=True)).scalar_one()
    if row.inventory_item_id != item.id:
        raise StockError("that size belongs to another item")
    return row


def _resync(item):
    """Set the item's total from its batches or variants."""
    model = ItemVariant if item.has_variants else Batch
    column = model.stock if item.has_variants else model.quantity_on_hand
    total = (db.session.query(db.func.coalesce(db.func.sum(column), 0))
             .filter(model.inventory_item_id == item.id).scalar())
    item.stock = int(total or 0)


def _movement(item, delta, reason, user_id, note):
    db.session.add(StockMovement(item_id=item.id, item_name=item.name, delta=delta,
                                 reason=reason, user_id=user_id, note=note))


def _units(qty):
    qty = int(qty)
    if qty <= 0:
        raise StockError("quantity must be a positive whole number")
    return qty


def receive(item, qty, *, batch_number=None, expiry_date=None, unit_cost=None, variant=None,
            reason="restock", note=None, user_id=None):
    """Add stock: into a batch (new or existing) or a variant."""
    qty = _units(qty)
    item = _lock_item(item)
    if item.has_variants:
        row = _locked_variant(item, variant)
        row.stock = int(row.stock or 0) + qty
    else:
        number = (batch_number or "").strip() or DEFAULT_BATCH
        if number.upper().startswith(QUARANTINE_BATCH):
            raise StockError(f"batch numbers starting {QUARANTINE_BATCH} are reserved for "
                             "returned goods on hold")
        batches = {b.batch_number: b for b in _locked_batches(item)}
        batch = batches.get(number)
        if batch is None:
            batch = Batch(inventory_item_id=item.id, batch_number=number,
                          expiry_date=expiry_date, quantity_on_hand=0, unit_cost=unit_cost,
                          received_at=datetime.utcnow())
            db.session.add(batch)
        elif expiry_date and batch.expiry_date and expiry_date != batch.expiry_date:
            raise StockError(f"batch {number} already has expiry {batch.expiry_date}")
        elif expiry_date and not batch.expiry_date:
            batch.expiry_date = expiry_date
        batch.quantity_on_hand = int(batch.quantity_on_hand or 0) + qty
    db.session.flush()
    _resync(item)
    _movement(item, qty, reason, user_id, note)
    db.session.flush()
    return item


def _fefo_order(batch):
    return (batch.expiry_date is None, batch.expiry_date or datetime.max.date(), batch.id)


def take(item, qty, *, transaction_item=None, order_item=None, variant=None,
         allow_expired=False, reason="sale", note=None, user_id=None, today=None):
    """Remove stock, soonest expiry first. Returns [(batch, units)].

    Expired batches are skipped unless ``allow_expired`` (write-offs).
    Raises InsufficientStock before changing anything.
    """
    qty = _units(qty)
    item = _lock_item(item)
    taken = []
    if item.has_variants:
        row = _locked_variant(item, variant)
        if int(row.stock or 0) < qty:
            raise InsufficientStock(item, qty, int(row.stock or 0))
        row.stock = int(row.stock) - qty
    else:
        today = today or today_nairobi()
        batches = [b for b in _locked_batches(item) if (b.quantity_on_hand or 0) > 0]
        expired = [b for b in batches
                   if b.quarantined or (b.expiry_date and b.expiry_date < today)]
        usable = batches if allow_expired else [b for b in batches if b not in expired]
        sellable = sum(b.quantity_on_hand for b in usable)
        if sellable < qty:
            raise InsufficientStock(item, qty, sellable,
                                    0 if allow_expired else sum(b.quantity_on_hand for b in expired))
        remaining = qty
        for batch in sorted(usable, key=_fefo_order):
            if remaining == 0:
                break
            units = min(batch.quantity_on_hand, remaining)
            batch.quantity_on_hand -= units
            remaining -= units
            taken.append((batch, units))
            if transaction_item is not None or order_item is not None:
                db.session.add(LineBatch(
                    transaction_item_id=transaction_item.id if transaction_item is not None else None,
                    order_item_id=order_item.id if order_item is not None else None,
                    batch_id=batch.id, quantity=units, returned_quantity=0))
    db.session.flush()
    _resync(item)
    _movement(item, -qty, reason, user_id, note)
    db.session.flush()
    return taken


def _free_quarantine_number(batches):
    """QUARANTINE, or QUARANTINE-2, -3... if a batch already uses the name
    (compared case-insensitively, as MySQL does)."""
    taken = {(b.batch_number or "").upper() for b in batches}
    number, n = QUARANTINE_BATCH, 1
    while number.upper() in taken:
        n += 1
        number = f"{QUARANTINE_BATCH}-{n}"
    return number


def _line_batches(transaction_item, order_item):
    if transaction_item is not None:
        return LineBatch.query.filter(LineBatch.transaction_item_id == transaction_item.id)
    if order_item is not None:
        return LineBatch.query.filter(LineBatch.order_item_id == order_item.id)
    return None


def give_back(item, qty, *, transaction_item=None, order_item=None, variant=None,
              reason="refund", note=None, user_id=None):
    """Return units to stock. Returns [(batch, units)].

    A line with batch records gets its units back in those batches (latest
    taken first), and asking for more than it has out raises StockError.
    A line without them (sold before batches existed) returns to the batch
    named on the line if there is one; anything else, and returns with no
    line at all, goes to a quarantined batch that is not sold until checked.
    """
    qty = _units(qty)
    item = _lock_item(item)
    restored = []
    if item.has_variants:
        row = _locked_variant(item, variant)
        row.stock = int(row.stock or 0) + qty
    else:
        batches = {b.id: b for b in _locked_batches(item)}
        query = _line_batches(transaction_item, order_item)
        records = query.order_by(LineBatch.id.desc()).all() if query is not None else []
        if records:
            out = sum(lb.quantity - (lb.returned_quantity or 0) for lb in records)
            if qty > out:
                raise StockError(f"only {out} of {item.name} from this line can come back")
            remaining = qty
            for lb in records:
                units = min(lb.quantity - (lb.returned_quantity or 0), remaining)
                if units <= 0:
                    continue
                lb.returned_quantity = (lb.returned_quantity or 0) + units
                batch = batches[lb.batch_id]
                batch.quantity_on_hand += units
                restored.append((batch, units))
                remaining -= units
                if remaining == 0:
                    break
        else:
            named = getattr(transaction_item, "batch_number", None)
            batch = next((b for b in batches.values()
                          if named and b.batch_number == named and not b.quarantined), None)
            if batch is None:
                batch = next((b for b in batches.values() if b.quarantined), None)
            if batch is None:
                batch = Batch(inventory_item_id=item.id,
                              batch_number=_free_quarantine_number(batches.values()),
                              quantity_on_hand=0, quarantined=True,
                              received_at=datetime.utcnow())
                db.session.add(batch)
            batch.quantity_on_hand = int(batch.quantity_on_hand or 0) + qty
            restored.append((batch, qty))
    db.session.flush()
    _resync(item)
    _movement(item, qty, reason, user_id, note)
    db.session.flush()
    return restored


def adjust(item, delta, *, batch_number=None, expiry_date=None, variant=None,
           reason="adjustment", note=None, user_id=None):
    """Manual correction. Positive adds (to a named or default batch);
    negative removes soonest-expiry first, expired stock included."""
    delta = int(delta)
    if delta == 0:
        raise StockError("adjustment must not be zero")
    if delta > 0:
        return receive(item, delta, batch_number=batch_number, expiry_date=expiry_date,
                       variant=variant, reason=reason, note=note, user_id=user_id)
    take(item, -delta, variant=variant, allow_expired=True, reason=reason, note=note,
         user_id=user_id)
    return item


def batch_label(taken):
    """'B12 x4, B13 x6' for a sale line's batch snapshot."""
    if len(taken) == 1:
        return taken[0][0].batch_number
    return ", ".join(f"{b.batch_number} x{n}" for b, n in taken)[:64]
