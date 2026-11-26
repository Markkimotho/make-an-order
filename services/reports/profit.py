"""Sales, gross profit and net profit per Nairobi day.

Definitions (the owner's questions, answered one way everywhere):

* A sale is a POS transaction that was completed. Fully refunded sales
  (status ``refunded``) count as a sale on their day and a refund on the
  refund's day. Voided sales never count.
* Revenue of a sale = its total net of tax (``total - tax_amount``). Tax is
  owed to KRA, so it is reported on its own, never as revenue. Revenue is
  already after line and sale discounts; ``discounts`` shows how much was
  given.
* Refunds reduce revenue on the Nairobi day the refund happened: the date
  of the credit note (``InvoiceAdjustment``, reason ``return``) the refund
  wrote on the sale's invoice. A sale with returned goods but no credit
  note (a credit sale made before invoices existed) has its refund on the
  sale's own day. The refunded amount is split into revenue and tax in the
  sale's proportions.
* COGS of a line = units taken from each batch (``LineBatch.quantity``) x
  that batch's ``unit_cost``, else the item's ``cost_price``. Lines with no
  batches (variants, older sales) use ``cost_price`` x quantity. Returned
  units come off COGS on the refund's day. A line with no cost anywhere is
  counted in ``cost_missing_lines`` and its revenue in
  ``revenue_without_cost``, so a gross profit that is too high says so.
* Gross profit = revenue - COGS. Net profit = gross profit - expenses
  (``Expense.spent_on`` in the range).
* Revenue by payment method splits each sale's revenue in the proportions
  it was paid (the first allocation of each payment to the sale's invoice:
  ``mpesa_stk``/``mpesa_c2b``/``mpesa_manual`` are ``mpesa``). Credit sales
  are ``credit`` whatever later pays them off. Refunds come off the same
  methods, so the methods always add up to revenue.
* Top items and profit by category cover sales made in the range, net of
  their refunds whenever those happened. The daily rows put refunds on the
  refund's day, so the two can differ when a refund crosses the range edge.

Days are Nairobi calendar days (services/clock): a sale at 22:30 UTC is on
the next day. Bucketing happens in Python, so SQLite and MySQL agree.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from models import (
    Batch, Category, Expense, InventoryItem, Invoice, InvoiceAdjustment, LineBatch, Payment,
    PaymentAllocation, StockMovement, Transaction, TransactionItem, TXN_COMPLETED,
    TXN_REFUNDED,
)
from models.finance import EXPENSE_CATEGORIES
from services.clock import NAIROBI, now_nairobi, today_nairobi

ZERO = Decimal("0")
CENT = Decimal("0.01")
MAX_DAYS = 366
METHODS = ("cash", "mpesa", "bank", "credit", "card", "other")
SALE_STATUSES = (TXN_COMPLETED, TXN_REFUNDED)

_PAYMENT_METHOD = {"mpesa_stk": "mpesa", "mpesa_c2b": "mpesa", "mpesa_manual": "mpesa",
                   "cash": "cash", "card": "card", "bank": "bank"}
_TXN_METHOD = {"cash": "cash", "mpesa": "mpesa", "card": "card", "bank": "bank",
               "credit": "credit", "split": "cash"}
_CHUNK = 500


class RangeError(ValueError):
    """A date range the reports refuse (reversed, too long)."""


def d(value):
    return Decimal(str(value or 0))


def money(value):
    """Decimal to a float rounded to 2 dp, for JSON."""
    return float(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP))


def nairobi_day(dt):
    """The Nairobi calendar day of a naive UTC datetime."""
    return now_nairobi(dt).date()


def utc_bounds(d_from, d_to):
    """[start, end) naive UTC datetimes covering Nairobi days d_from..d_to."""
    def utc(day):
        local = datetime.combine(day, time.min, tzinfo=NAIROBI)
        return local.astimezone(timezone.utc).replace(tzinfo=None)
    return utc(d_from), utc(d_to + timedelta(days=1))


def check_range(d_from, d_to, max_days=MAX_DAYS):
    if d_from > d_to:
        raise RangeError("from must be on or before to")
    if (d_to - d_from).days + 1 > max_days:
        raise RangeError(f"at most {max_days} days at a time")


def _chunks(ids):
    ids = list(ids)
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


def _fetch(model, column, ids, *criteria):
    """All rows of ``model`` whose ``column`` is in ``ids`` (chunked IN)."""
    out = []
    for part in _chunks(set(ids)):
        out.extend(model.query.filter(column.in_(part), *criteria).all())
    return out


def _split(value, mix):
    """Split ``value`` over ``mix`` [(key, weight)]; the last key takes the
    rounding remainder, so the parts add up to ``value`` exactly."""
    total = sum((w for _, w in mix), ZERO)
    out, given = {}, ZERO
    for i, (key, weight) in enumerate(mix):
        part = value - given if i == len(mix) - 1 else value * weight / total
        out[key] = out.get(key, ZERO) + part
        given += part
    return out


class _Line:
    __slots__ = ("key", "name", "category", "qty", "refunded_qty", "revenue", "cogs",
                 "returned_cogs", "cost_missing")


class _Sale:
    """One sale with everything the reports need, in Decimal."""
    __slots__ = ("txn", "day", "revenue", "tax", "total", "discounts", "lines", "mix",
                 "refunds", "returned_cogs", "cogs", "cost_missing_lines",
                 "revenue_without_cost", "returned_events")


def _empty_day():
    return {"sales_count": 0, "gross_sales": ZERO, "refunds": ZERO, "revenue": ZERO,
            "discounts": ZERO, "tax": ZERO, "cogs": ZERO, "expenses": ZERO,
            "cost_missing_lines": 0, "revenue_without_cost": ZERO,
            "by_method": {m: ZERO for m in METHODS}}


class _Loader:
    """Loads a set of sales and what hangs off them in a fixed number of
    queries (chunked IN lists), however many sales there are."""

    def __init__(self, txns):
        self.txns = {t.id: t for t in txns}
        ids = list(self.txns)
        self.lines = defaultdict(list)
        for li in _fetch(TransactionItem, TransactionItem.transaction_id, ids):
            self.lines[li.transaction_id].append(li)
        line_ids = [li.id for rows in self.lines.values() for li in rows]
        self.line_batches = defaultdict(list)
        for lb in _fetch(LineBatch, LineBatch.transaction_item_id, line_ids):
            self.line_batches[lb.transaction_item_id].append(lb)
        self.batches = {b.id: b for b in _fetch(
            Batch, Batch.id, {lb.batch_id for rows in self.line_batches.values() for lb in rows})}
        self.items = {i.id: i for i in _fetch(
            InventoryItem, InventoryItem.id,
            {li.inventory_item_id for rows in self.lines.values() for li in rows
             if li.inventory_item_id})}
        self.categories = {c.id: c.name for c in _fetch(
            Category, Category.id, {i.category_id for i in self.items.values() if i.category_id})}
        invoices = _fetch(Invoice, Invoice.transaction_id, ids, Invoice.status != "void")
        self.invoice_txn = {inv.id: inv.transaction_id for inv in invoices}
        allocations = _fetch(PaymentAllocation, PaymentAllocation.invoice_id, self.invoice_txn)
        payments = {p.id: p for p in _fetch(Payment, Payment.id,
                                            {a.payment_id for a in allocations})}
        # The first allocation of each payment to the sale's invoice is what
        # was tendered; later rows are what was left after a refund.
        first = {}
        for a in sorted(allocations, key=lambda a: a.id):
            first.setdefault((a.invoice_id, a.payment_id), a)
        self.tendered = defaultdict(list)
        for (invoice_id, payment_id), a in first.items():
            payment = payments.get(payment_id)
            if payment is not None:
                self.tendered[self.invoice_txn[invoice_id]].append(
                    (_PAYMENT_METHOD.get(payment.method, "other"), d(a.amount)))
        # Refunded units, one stock movement per item per refund, with its
        # date: each refund's cost comes off the day it happened.
        by_ref = {t.transaction_ref: t.id for t in self.txns.values()}
        self.refund_moves = defaultdict(list)
        notes = [f"REFUND {ref}" for ref in by_ref]
        for m in _fetch(StockMovement, StockMovement.note, notes,
                        StockMovement.reason == "refund"):
            txn_id = by_ref.get((m.note or "")[len("REFUND "):])
            if txn_id is not None:
                self.refund_moves[txn_id].append(m)
        self.credit_notes = defaultdict(list)
        for adj in _fetch(InvoiceAdjustment, InvoiceAdjustment.invoice_id, self.invoice_txn,
                          InvoiceAdjustment.reason == "return"):
            self.credit_notes[self.invoice_txn[adj.invoice_id]].append(adj)

    def _mix(self, txn):
        if txn.payment_method == "credit":
            return [("credit", Decimal(1))]
        tendered = [(m, a) for m, a in self.tendered.get(txn.id, []) if a > 0]
        if tendered:
            return tendered
        return [(_TXN_METHOD.get(txn.payment_method or "", "other"), Decimal(1))]

    def _unit_cost(self, batch, item, snapshot=None):
        """A batch's own cost; else the item's cost as it was at the sale
        (TransactionItem.unit_cost, kept since migration 0013); else, for
        older sales, today's item cost."""
        if batch is not None and batch.unit_cost is not None:
            return d(batch.unit_cost)
        if snapshot is not None:
            return d(snapshot)
        if item is not None and item.cost_price is not None:
            return d(item.cost_price)
        return None

    def _line(self, li, ratio):
        item = self.items.get(li.inventory_item_id)
        line = _Line()
        line.key = li.inventory_item_id or f"name:{li.item_name}"
        line.name = item.name if item is not None else li.item_name
        if item is not None and item.category_id in self.categories:
            line.category = self.categories[item.category_id]
        else:
            line.category = (item.category if item is not None else None) or "Uncategorised"
        line.qty = d(li.quantity)
        line.refunded_qty = d(li.refunded_quantity)
        line.revenue = d(li.line_total) * ratio
        line.cogs = line.returned_cogs = ZERO
        line.cost_missing = False
        batches = self.line_batches.get(li.id)
        if batches:
            for lb in batches:
                cost = self._unit_cost(self.batches.get(lb.batch_id), item, li.unit_cost)
                if cost is None:
                    line.cost_missing = True
                    continue
                line.cogs += Decimal(int(lb.quantity or 0)) * cost
                line.returned_cogs += Decimal(int(lb.returned_quantity or 0)) * cost
        else:
            cost = self._unit_cost(None, item, li.unit_cost)
            if cost is None:
                line.cost_missing = True
            else:
                line.cogs = line.qty * cost
                line.returned_cogs = line.refunded_qty * cost
        return line

    def _return_order(self, txn_id):
        """{item id: [(units, unit cost)]} in the order a refund puts units
        back (services/stock.give_back: the most recently taken batch first)."""
        out = defaultdict(list)
        for li in self.lines.get(txn_id, []):
            item = self.items.get(li.inventory_item_id)
            rows = sorted(self.line_batches.get(li.id, []), key=lambda lb: -lb.id)
            if rows:
                for lb in rows:
                    cost = self._unit_cost(self.batches.get(lb.batch_id), item, li.unit_cost)
                    out[li.inventory_item_id].append((int(lb.quantity or 0), cost))
            else:
                out[li.inventory_item_id].append((int(d(li.quantity)),
                                                  self._unit_cost(None, item, li.unit_cost)))
        return out

    def _returned_cost_events(self, txn_id, lines):
        """[(day, cost)] for each refund's units, oldest refund first, each
        costed at the batches its units went back to."""
        moves = sorted(self.refund_moves.get(txn_id, []), key=lambda m: (m.created_at, m.id))
        if not moves:
            return []
        queues = {item_id: list(rows) for item_id, rows in self._return_order(txn_id).items()}
        events = []
        for m in moves:
            left, cost = int(m.delta or 0), ZERO
            queue = queues.get(m.item_id, [])
            while left > 0 and queue:
                units, unit_cost = queue[0]
                take = min(units, left)
                if unit_cost is not None:
                    cost += Decimal(take) * unit_cost
                left -= take
                if take == units:
                    queue.pop(0)
                else:
                    queue[0] = (units - take, unit_cost)
            events.append((nairobi_day(m.created_at), cost))
        return events

    def sale(self, txn):
        s = _Sale()
        s.txn = txn
        s.day = nairobi_day(txn.created_at)
        s.total, s.tax = d(txn.total), d(txn.tax_amount)
        s.revenue = s.total - s.tax
        subtotal = d(txn.subtotal)
        ratio = s.revenue / subtotal if subtotal > 0 else ZERO
        lines = self.lines.get(txn.id, [])
        s.discounts = d(txn.discount_amount) + sum((d(li.discount) for li in lines), ZERO)
        s.lines = [self._line(li, ratio) for li in lines]
        s.cogs = sum((li.cogs for li in s.lines), ZERO)
        s.returned_cogs = sum((li.returned_cogs for li in s.lines), ZERO)
        missing = [li for li in s.lines if li.cost_missing]
        s.cost_missing_lines = len(missing)
        s.revenue_without_cost = sum((li.revenue for li in missing), ZERO)
        s.mix = self._mix(txn)
        s.returned_events = self._returned_cost_events(txn.id, s.lines)
        # Refund events: (day, amount the customer got back incl. tax).
        notes = self.credit_notes.get(txn.id, [])
        if notes:
            s.refunds = [(nairobi_day(a.created_at), d(a.amount)) for a in notes]
        elif any(li.refunded_qty > 0 for li in s.lines):
            gross = (s.total / subtotal) if subtotal > 0 else ZERO
            amount = sum((d(li.line_total) / d(li.quantity) * d(li.refunded_quantity) * gross
                          for li in lines if d(li.quantity) > 0), ZERO)
            s.refunds = [(s.day, amount)]
        else:
            s.refunds = []
        return s


def _sales_for(d_from, d_to):
    """Sales made in the range plus older sales refunded in the range."""
    start, end = utc_bounds(d_from, d_to)
    made = (Transaction.query
            .filter(Transaction.status.in_(SALE_STATUSES),
                    Transaction.created_at >= start, Transaction.created_at < end).all())
    refunded_ids = {row.transaction_id for row in
                    Invoice.query.join(InvoiceAdjustment,
                                       InvoiceAdjustment.invoice_id == Invoice.id)
                    .filter(InvoiceAdjustment.reason == "return",
                            InvoiceAdjustment.created_at >= start,
                            InvoiceAdjustment.created_at < end,
                            Invoice.transaction_id.isnot(None)).all()}
    have = {t.id for t in made}
    later = _fetch(Transaction, Transaction.id, refunded_ids - have,
                   Transaction.status.in_(SALE_STATUSES))
    loader = _Loader(made + later)
    return [loader.sale(t) for t in made + later]


def _days(d_from, d_to):
    out, day = {}, d_from
    while day <= d_to:
        out[day] = _empty_day()
        day += timedelta(days=1)
    return out


def _compute(d_from, d_to):
    check_range(d_from, d_to)
    sales = _sales_for(d_from, d_to)
    days = _days(d_from, d_to)
    items, categories = {}, defaultdict(lambda: {"revenue": ZERO, "cogs": ZERO})
    for s in sales:
        row = days.get(s.day)
        if row is not None:
            row["sales_count"] += 1
            row["gross_sales"] += s.revenue
            row["discounts"] += s.discounts
            row["tax"] += s.tax
            row["cogs"] += s.cogs
            row["cost_missing_lines"] += s.cost_missing_lines
            row["revenue_without_cost"] += s.revenue_without_cost
            for m, part in _split(s.revenue, s.mix).items():
                row["by_method"][m] += part
            for li in s.lines:
                share = li.refunded_qty / li.qty if li.qty > 0 else ZERO
                net_rev = li.revenue * (1 - share)
                net_cogs = li.cogs - li.returned_cogs
                it = items.setdefault(li.key, {"name": li.name, "category": li.category,
                                               "qty": ZERO, "revenue": ZERO, "cogs": ZERO,
                                               "cost_missing": False})
                it["qty"] += li.qty - li.refunded_qty
                it["revenue"] += net_rev
                it["cogs"] += net_cogs
                it["cost_missing"] = it["cost_missing"] or li.cost_missing
                categories[li.category]["revenue"] += net_rev
                categories[li.category]["cogs"] += net_cogs
        refunded = sum((a for _, a in s.refunds), ZERO)
        for day, amount in s.refunds:
            row = days.get(day)
            if row is None or amount <= 0:
                continue
            net = amount * s.revenue / s.total if s.total > 0 else ZERO
            row["refunds"] += net
            row["tax"] -= amount - net
            if not s.returned_events:          # older refunds: spread by value
                row["cogs"] -= s.returned_cogs * amount / refunded
            for m, part in _split(net, s.mix).items():
                row["by_method"][m] -= part
        # Cost of returned goods comes off the day each refund happened.
        for day, cost in s.returned_events:
            if day in days:
                days[day]["cogs"] -= cost
        if not s.refunds and not s.returned_events and s.returned_cogs and s.day in days:
            days[s.day]["cogs"] -= s.returned_cogs
    exp_by_category = defaultdict(lambda: ZERO)
    for e in Expense.query.filter(Expense.spent_on >= d_from, Expense.spent_on <= d_to).all():
        if e.spent_on in days:
            days[e.spent_on]["expenses"] += d(e.amount)
            exp_by_category[e.category] += d(e.amount)
    for row in days.values():
        row["revenue"] = row["gross_sales"] - row["refunds"]
    return days, items, categories, exp_by_category


def _margin(gp, revenue):
    return float((gp / revenue * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)) \
        if revenue else None


def _row_out(day, row):
    gp = row["revenue"] - row["cogs"]
    out = {"date": day.isoformat() if day is not None else None,
           "sales_count": row["sales_count"],
           "gross_sales": money(row["gross_sales"]),
           "refunds": money(row["refunds"]),
           "revenue": money(row["revenue"]),
           "discounts": money(row["discounts"]),
           "tax": money(row["tax"]),
           "cogs": money(row["cogs"]),
           "gross_profit": money(gp),
           "gross_margin_pct": _margin(gp, row["revenue"]),
           "expenses": money(row["expenses"]),
           "net_profit": money(gp - row["expenses"]),
           "cost_missing_lines": row["cost_missing_lines"],
           "revenue_without_cost": money(row["revenue_without_cost"]),
           "by_method": {m: money(v) for m, v in row["by_method"].items()}}
    return out


def _total(days):
    tot = _empty_day()
    for row in days.values():
        for k, v in row.items():
            if k == "by_method":
                for m, x in v.items():
                    tot["by_method"][m] += x
            else:
                tot[k] += v
    return tot


def daily(d_from, d_to):
    """One row per Nairobi day from ``d_from`` to ``d_to`` (empty days too)."""
    days, _, _, _ = _compute(d_from, d_to)
    return [_row_out(day, row) for day, row in sorted(days.items())]


def report(d_from, d_to, top=10):
    """Daily rows, totals, top items by gross profit, profit by category and
    expenses by category, from one pass over the data."""
    days, items, categories, expenses = _compute(d_from, d_to)
    totals = _row_out(None, _total(days))
    totals.pop("date")
    top_items = sorted(
        ({"name": it["name"], "category": it["category"], "qty": float(it["qty"]),
          "revenue": money(it["revenue"]), "cogs": money(it["cogs"]),
          "gross_profit": money(it["revenue"] - it["cogs"]),
          "gross_margin_pct": _margin(it["revenue"] - it["cogs"], it["revenue"]),
          "cost_missing": it["cost_missing"]} for it in items.values()),
        key=lambda r: (-r["gross_profit"], r["name"]))[:top]
    by_category = sorted(
        ({"category": name, "revenue": money(c["revenue"]), "cogs": money(c["cogs"]),
          "gross_profit": money(c["revenue"] - c["cogs"]),
          "gross_margin_pct": _margin(c["revenue"] - c["cogs"], c["revenue"])}
         for name, c in categories.items()),
        key=lambda r: (-r["gross_profit"], r["category"]))
    return {"from": d_from.isoformat(), "to": d_to.isoformat(),
            "days": [_row_out(day, row) for day, row in sorted(days.items())],
            "totals": totals, "top_items": top_items, "by_category": by_category,
            "expenses_by_category": {k: money(v) for k, v in sorted(expenses.items())}}


def summary(d_from, d_to, top=10):
    """Totals over the range plus top items and profit by category."""
    out = report(d_from, d_to, top=top)
    out.pop("days")
    return out


def _month_start(day, back):
    y, m = day.year, day.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def monthly(months=12, today=None):
    """The last ``months`` calendar months (this one so far included)."""
    months = max(1, min(int(months), 12))
    today = today or today_nairobi()
    d_from = _month_start(today, months - 1)
    days, _, _, _ = _compute(d_from, today)
    buckets = {}
    for day, row in sorted(days.items()):
        key = day.strftime("%Y-%m")
        b = buckets.setdefault(key, _empty_day())
        for k, v in row.items():
            if k != "by_method":
                b[k] += v
    out = []
    for key, row in buckets.items():
        r = _row_out(None, row)
        r.pop("date")
        r.pop("by_method")
        out.append({"month": key, **r})
    return out


def expense_totals(d_from, d_to):
    """{month 'YYYY-MM': {category: Decimal}} for expenses in the range."""
    out = defaultdict(lambda: {c: ZERO for c in EXPENSE_CATEGORIES})
    for e in Expense.query.filter(Expense.spent_on >= d_from, Expense.spent_on <= d_to).all():
        month = out[e.spent_on.strftime("%Y-%m")]
        month[e.category] = month.get(e.category, ZERO) + d(e.amount)
    return dict(out)
