from flask import Blueprint, jsonify, request, Response
from models import (
    db, Batch, Order, OrderItem, StockMovement, InventoryItem, User,
    Transaction, TransactionItem, Shift,
    TXN_COMPLETED, TXN_REFUNDED, PAYMENT_METHODS,
)
from auth.auth_middleware import finance_api_required
from services.clock import today_nairobi
from sqlalchemy import func
from datetime import datetime, timedelta, date
from decimal import Decimal
import logging
import csv
import io

reports_bp = Blueprint('reports', __name__)
logger = logging.getLogger(__name__)

# Statuses that count as real revenue (exclude cancelled/refunded)
REVENUE_STATUSES = ('pending', 'confirmed', 'completed')


def _since(period: str):
    """Return the start datetime for the requested period, or None for 'all'."""
    now = datetime.utcnow()
    return {
        'today': now.replace(hour=0, minute=0, second=0, microsecond=0),
        'week':  now - timedelta(days=7),
        'month': now - timedelta(days=30),
    }.get(period)


# ---------------------------------------------------------------------------
# Summary KPIs
# ---------------------------------------------------------------------------

@reports_bp.route('/summary', methods=['GET'])
@finance_api_required
def summary():
    """Return order count, total revenue, avg order value for a given period."""
    period = request.args.get('period', 'month')
    since  = _since(period)

    q = (db.session.query(
             func.count(Order.id).label('n'),
             func.coalesce(func.sum(Order.amount), 0).label('rev'),
         )
         .filter(Order.status.in_(REVENUE_STATUSES)))
    if since:
        q = q.filter(Order.time >= since)

    row     = q.one()
    count   = row.n
    revenue = float(row.rev)
    return jsonify({
        'period':          period,
        'order_count':     count,
        'total_revenue':   revenue,
        'avg_order_value': round(revenue / count, 2) if count else 0,
    })


# ---------------------------------------------------------------------------
# Revenue over time (for line chart)
# ---------------------------------------------------------------------------

@reports_bp.route('/by_day', methods=['GET'])
@finance_api_required
def by_day():
    """Daily revenue + order count for the last N days (max 90)."""
    days  = min(int(request.args.get('days', 30)), 90)
    since = datetime.utcnow() - timedelta(days=days)

    rows = (db.session.query(
                func.date(Order.time).label('day'),
                func.sum(Order.amount).label('revenue'),
                func.count(Order.id).label('count'),
            )
            .filter(Order.time >= since)
            .filter(Order.status.in_(REVENUE_STATUSES))
            .group_by(func.date(Order.time))
            .order_by(func.date(Order.time))
            .all())

    return jsonify([{
        'date':    str(r.day),
        'revenue': float(r.revenue),
        'count':   r.count,
    } for r in rows])


# ---------------------------------------------------------------------------
# Top-selling items (from OrderItems)
# ---------------------------------------------------------------------------

@reports_bp.route('/top_items', methods=['GET'])
@finance_api_required
def top_items():
    """Top N items by revenue, excluding cancelled/refunded orders."""
    limit = min(int(request.args.get('limit', 10)), 50)

    rows = (db.session.query(
                OrderItem.item_name,
                func.sum(OrderItem.quantity).label('qty'),
                func.sum(OrderItem.subtotal).label('revenue'),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .filter(Order.status.in_(REVENUE_STATUSES))
            .group_by(OrderItem.item_name)
            .order_by(func.sum(OrderItem.subtotal).desc())
            .limit(limit)
            .all())

    return jsonify([{
        'name':    r.item_name,
        'qty':     int(r.qty),
        'revenue': float(r.revenue),
    } for r in rows])


# ---------------------------------------------------------------------------
# Order status breakdown (for doughnut chart)
# ---------------------------------------------------------------------------

@reports_bp.route('/status_breakdown', methods=['GET'])
@finance_api_required
def status_breakdown():
    """Count of orders in each status."""
    rows = (db.session.query(Order.status, func.count(Order.id).label('n'))
            .group_by(Order.status)
            .all())
    return jsonify({r.status: r.n for r in rows})


# ---------------------------------------------------------------------------
# Payment method breakdown
# ---------------------------------------------------------------------------

@reports_bp.route('/payment_breakdown', methods=['GET'])
@finance_api_required
def payment_breakdown():
    """Revenue and count broken down by payment method."""
    rows = (db.session.query(
                Order.payment_method,
                func.count(Order.id).label('n'),
                func.sum(Order.amount).label('revenue'),
            )
            .filter(Order.status.in_(REVENUE_STATUSES))
            .group_by(Order.payment_method)
            .all())

    return jsonify([{
        'method':  r.payment_method or 'unspecified',
        'count':   r.n,
        'revenue': float(r.revenue or 0),
    } for r in rows])


# ---------------------------------------------------------------------------
# CSV export (admin+)
# ---------------------------------------------------------------------------

@reports_bp.route('/export', methods=['GET'])
@finance_api_required
def export_report():
    """Download a revenue-by-day summary as a CSV file."""
    days  = min(int(request.args.get('days', 30)), 365)
    since = datetime.utcnow() - timedelta(days=days)

    rows = (db.session.query(
                func.date(Order.time).label('day'),
                func.count(Order.id).label('count'),
                func.sum(Order.amount).label('revenue'),
            )
            .filter(Order.time >= since)
            .filter(Order.status.in_(REVENUE_STATUSES))
            .group_by(func.date(Order.time))
            .order_by(func.date(Order.time))
            .all())

    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(['Date', 'Order Count', 'Revenue'])
    for r in rows:
        w.writerow([str(r.day), r.count, f'{float(r.revenue):.2f}'])

    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=revenue_report.csv'},
    )


# ===========================================================================
# POS-era reports (run on Transaction / TransactionItem)
# ===========================================================================

def _parse_date_arg(name, default=None):
    raw = request.args.get(name)
    if not raw:
        return default
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return default


def _date_window():
    """Resolve ``?from=&to=`` date params into a (start, end_exclusive) datetime
    pair. Defaults: last 30 days."""
    today = date.today()
    d_from = _parse_date_arg('from', today - timedelta(days=30))
    d_to   = _parse_date_arg('to',   today)
    start = datetime.combine(d_from, datetime.min.time())
    end   = datetime.combine(d_to + timedelta(days=1), datetime.min.time())
    return start, end


def _csv_response(rows, fieldnames, filename):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, '') for k in fieldnames})
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


@reports_bp.route('/sales_summary', methods=['GET'])
@finance_api_required
def sales_summary():
    """POS sales summary over a date window with optional cashier / method filter.

    Query params: ``from`` ``to`` (YYYY-MM-DD), ``cashier_id``, ``payment_method``,
    ``format=csv``.
    """
    start, end = _date_window()
    cashier_id = request.args.get('cashier_id', type=int)
    method = (request.args.get('payment_method') or '').strip().lower()

    q = (Transaction.query
         .filter(Transaction.created_at >= start,
                 Transaction.created_at < end,
                 Transaction.status == TXN_COMPLETED))
    if cashier_id:
        q = q.filter(Transaction.cashier_id == cashier_id)
    if method:
        if method not in PAYMENT_METHODS:
            return jsonify({'error': f'payment_method must be one of '
                                      f'{list(PAYMENT_METHODS)}'}), 400
        q = q.filter(Transaction.payment_method == method)

    txns = q.all()
    n = len(txns)
    subtotal = sum((Decimal(str(t.subtotal or 0)) for t in txns), Decimal('0'))
    tax      = sum((Decimal(str(t.tax_amount or 0)) for t in txns), Decimal('0'))
    discount = sum((Decimal(str(t.discount_amount or 0)) for t in txns), Decimal('0'))
    total    = sum((Decimal(str(t.total or 0)) for t in txns), Decimal('0'))
    avg      = (total / n) if n else Decimal('0')

    by_method = {}
    for t in txns:
        by_method[t.payment_method or 'unspecified'] = (
            by_method.get(t.payment_method or 'unspecified', Decimal('0'))
            + Decimal(str(t.total or 0))
        )

    payload = {
        'from': start.date().isoformat(),
        'to':   (end - timedelta(days=1)).date().isoformat(),
        'transaction_count': n,
        'subtotal': float(subtotal),
        'tax':      float(tax),
        'discount': float(discount),
        'total':    float(total),
        'average_basket': float(avg),
        'by_method': {k: float(v) for k, v in by_method.items()},
    }
    if (request.args.get('format') or '').lower() == 'csv':
        rows = [{'metric': k, 'value': v} for k, v in payload.items()
                if not isinstance(v, dict)]
        for k, v in payload['by_method'].items():
            rows.append({'metric': f'method_{k}', 'value': v})
        return _csv_response(rows, ['metric', 'value'], 'sales_summary.csv')
    return jsonify(payload), 200


@reports_bp.route('/shift/<int:shift_id>', methods=['GET'])
@finance_api_required
def shift_report(shift_id):
    """Z-report for a single shift (open or closed)."""
    shift = db.session.get(Shift, shift_id)
    if not shift:
        return jsonify({'error': 'Shift not found'}), 404
    from api.pos import _build_z_report  # local import to avoid cycle
    z = _build_z_report(shift)
    z['shift'] = shift.to_dict()
    return jsonify(z), 200


@reports_bp.route('/inventory_valuation', methods=['GET'])
@finance_api_required
def inventory_valuation():
    """Sum of ``stock * cost_price`` (and ``stock * price`` for retail value)."""
    rows = InventoryItem.query.all()
    cost_value   = Decimal('0')
    retail_value = Decimal('0')
    item_rows = []
    for it in rows:
        cost = Decimal(str(it.cost_price or 0))
        price = Decimal(str(it.price or 0))
        units = Decimal(int(it.stock or 0))
        cost_value   += cost * units
        retail_value += price * units
        item_rows.append({
            'id': it.id, 'name': it.name, 'stock': int(it.stock or 0),
            'cost_price': float(cost), 'retail_price': float(price),
            'cost_value': float(cost * units),
            'retail_value': float(price * units),
        })
    payload = {
        'item_count':    len(rows),
        'total_units':   sum(r['stock'] for r in item_rows),
        'cost_value':    float(cost_value),
        'retail_value':  float(retail_value),
        'projected_margin': float(retail_value - cost_value),
        'items': item_rows,
    }
    if (request.args.get('format') or '').lower() == 'csv':
        return _csv_response(item_rows,
                             ['id', 'name', 'stock', 'cost_price', 'retail_price',
                              'cost_value', 'retail_value'],
                             'inventory_valuation.csv')
    return jsonify(payload), 200


@reports_bp.route('/stock_movement', methods=['GET'])
@finance_api_required
def stock_movement_report():
    """Stock movement history with date / item / reason filters."""
    start, end = _date_window()
    item_id = request.args.get('item_id', type=int)
    reason  = (request.args.get('reason') or '').strip().lower()
    q = (StockMovement.query
         .filter(StockMovement.created_at >= start,
                 StockMovement.created_at < end))
    if item_id:
        q = q.filter(StockMovement.item_id == item_id)
    if reason:
        q = q.filter(StockMovement.reason == reason)
    rows = q.order_by(StockMovement.created_at.desc()).limit(2000).all()
    return jsonify([m.to_dict() for m in rows]), 200


@reports_bp.route('/low_stock', methods=['GET'])
@finance_api_required
def low_stock_report():
    """Items where ``stock <= reorder_level`` (formatted report)."""
    rows = (InventoryItem.query
            .filter(InventoryItem.stock <= InventoryItem.reorder_level)
            .order_by(InventoryItem.stock.asc()).all())
    return jsonify([{
        'id': it.id, 'name': it.name, 'stock': int(it.stock or 0),
        'reorder_level': int(it.reorder_level or 0),
        'unit': it.unit, 'category': it.category,
    } for it in rows]), 200


@reports_bp.route('/expiry', methods=['GET'])
@finance_api_required
def expiry_report():
    """Items expiring within ``?days=`` (default 30) plus cost-at-risk."""
    try:
        days = int(request.args.get('days', 30))
    except ValueError:
        return jsonify({'error': 'days must be an integer'}), 400
    today = today_nairobi()
    cutoff = today + timedelta(days=max(days, 0))
    rows = (db.session.query(Batch, InventoryItem)
            .join(InventoryItem, InventoryItem.id == Batch.inventory_item_id)
            .filter(Batch.quantity_on_hand > 0, Batch.expiry_date.isnot(None),
                    Batch.expiry_date <= cutoff)
            .order_by(Batch.expiry_date.asc(), Batch.id.asc()).all())
    out = []
    cost_at_risk = Decimal('0')
    for batch, it in rows:
        unit_cost = batch.unit_cost if batch.unit_cost is not None else (it.cost_price or 0)
        cost = Decimal(str(unit_cost)) * Decimal(int(batch.quantity_on_hand))
        cost_at_risk += cost
        out.append({
            'id': it.id, 'name': it.name,
            'stock': int(batch.quantity_on_hand), 'unit': it.unit,
            'batch_id': batch.id, 'batch_number': batch.batch_number,
            'expiry_date': batch.expiry_date.isoformat(),
            'days_to_expiry': (batch.expiry_date - today).days,
            'expired': batch.expiry_date < today,
            'cost_at_risk': float(cost),
        })
    return jsonify({'cutoff': cutoff.isoformat(),
                    'cost_at_risk': float(cost_at_risk),
                    'items': out}), 200


@reports_bp.route('/top_products', methods=['GET'])
@finance_api_required
def top_products_pos():
    """Top selling products from completed transactions."""
    start, end = _date_window()
    limit = request.args.get('limit', default=10, type=int)
    rows = (db.session.query(
                TransactionItem.item_name.label('item'),
                func.coalesce(func.sum(TransactionItem.quantity), 0).label('qty'),
                func.coalesce(func.sum(TransactionItem.line_total), 0).label('revenue'),
            )
            .join(Transaction, Transaction.id == TransactionItem.transaction_id)
            .filter(Transaction.status == TXN_COMPLETED,
                    Transaction.created_at >= start,
                    Transaction.created_at < end)
            .group_by(TransactionItem.item_name)
            .order_by(func.sum(TransactionItem.line_total).desc())
            .limit(limit).all())
    payload = [{'item': r.item, 'qty': float(r.qty), 'revenue': float(r.revenue)}
               for r in rows]
    if (request.args.get('format') or '').lower() == 'csv':
        return _csv_response(payload, ['item', 'qty', 'revenue'], 'top_products.csv')
    return jsonify(payload), 200


@reports_bp.route('/cashier_performance', methods=['GET'])
@finance_api_required
def cashier_performance():
    """Per-cashier transaction count, total sales, and refund total."""
    start, end = _date_window()
    sales_rows = (db.session.query(
                      Transaction.cashier_id,
                      func.count(Transaction.id).label('txn_count'),
                      func.coalesce(func.sum(Transaction.total), 0).label('sales'),
                  )
                  .filter(Transaction.status == TXN_COMPLETED,
                          Transaction.created_at >= start,
                          Transaction.created_at < end)
                  .group_by(Transaction.cashier_id).all())
    refund_rows = dict((cid, total) for (cid, total) in
                       db.session.query(
                           Transaction.cashier_id,
                           func.coalesce(func.sum(Transaction.total), 0))
                       .filter(Transaction.status == TXN_REFUNDED,
                               Transaction.created_at >= start,
                               Transaction.created_at < end)
                       .group_by(Transaction.cashier_id).all())
    out = []
    for cid, count, sales in sales_rows:
        u = db.session.get(User, cid) if cid else None
        out.append({
            'cashier_id': cid,
            'cashier_name': (u.name if u else None),
            'transaction_count': int(count or 0),
            'sales_total': float(sales or 0),
            'refund_total': float(refund_rows.get(cid, 0) or 0),
            'net': float(Decimal(str(sales or 0)) - Decimal(str(refund_rows.get(cid, 0) or 0))),
        })
    out.sort(key=lambda r: r['net'], reverse=True)
    if (request.args.get('format') or '').lower() == 'csv':
        return _csv_response(out,
                             ['cashier_id', 'cashier_name', 'transaction_count',
                              'sales_total', 'refund_total', 'net'],
                             'cashier_performance.csv')
    return jsonify(out), 200


import api.finance_reports  # noqa: E402,F401  daily profit, expenses, reconciliation routes
