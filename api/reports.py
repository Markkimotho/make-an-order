from flask import Blueprint, jsonify, request, Response
from models import db, Order, OrderItem, StockMovement
from auth.auth_middleware import admin_api_required
from sqlalchemy import func
from datetime import datetime, timedelta
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
@admin_api_required
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
@admin_api_required
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
@admin_api_required
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
@admin_api_required
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
@admin_api_required
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
@admin_api_required
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
