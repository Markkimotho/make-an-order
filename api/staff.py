"""Staff performance: what each person sold, as cashier and as salesperson.

A sale has two people: the cashier who took the money (``cashier_id``) and
the salesperson who served the customer (``served_by_id``, often the same
person). Each gets credited in their own column. Days are Nairobi days.

Definitions, for one person in one capacity over [from, to]:
  sales_count / sales_value  sales completed (or completed and later fully
                             refunded) that were opened in the range;
                             value is the sale total after discounts
  average_basket             sales_value / sales_count
  discounts                  line discounts + sale discount on those sales
  voids_count / voids_value  'transaction.void' audit events in the range,
                             on that person's sales (value: the sale total)
  refunds_count / _value     'transaction.refund' audit events in the range,
                             on that person's sales (value: what was refunded)
  shifts_closed, shifts_with_variance, cash_variance_total
                             cashier only: their shifts closed in the range
                             and the sum of counted minus expected cash
"""
import csv
import io
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from flask import Blueprint, Response, jsonify, request
from sqlalchemy import func

from auth.auth_middleware import finance_api_required
from models import (TXN_COMPLETED, TXN_REFUNDED, AuditLog, Shift, Transaction,
                    TransactionItem, User, db)
from services.clock import NAIROBI, today_nairobi

staff_bp = Blueprint('staff', __name__)

MAX_RANGE_DAYS = 366
ZERO = Decimal('0')
SALE_FIELDS = ('sales_count', 'sales_value', 'average_basket', 'discounts',
               'voids_count', 'voids_value', 'refunds_count', 'refunds_value')
SHIFT_FIELDS = ('shifts_closed', 'shifts_with_variance', 'cash_variance_total')


class BadRange(ValueError):
    pass


def nairobi_day_bounds(d_from, d_to):
    """Naive UTC [start, end) covering Nairobi days d_from..d_to inclusive."""
    def utc(day):
        return (datetime.combine(day, time.min, tzinfo=NAIROBI)
                .astimezone(timezone.utc).replace(tzinfo=None))
    return utc(d_from), utc(d_to + timedelta(days=1))


def parse_range(args, default_days=30):
    """(from_date, to_date) from ?from=&to= (YYYY-MM-DD); default the last
    ``default_days`` Nairobi days ending today."""
    today = today_nairobi()
    try:
        d_to = datetime.strptime(args['to'], '%Y-%m-%d').date() if args.get('to') else today
        d_from = (datetime.strptime(args['from'], '%Y-%m-%d').date() if args.get('from')
                  else d_to - timedelta(days=default_days - 1))
    except ValueError:
        raise BadRange('Dates must be YYYY-MM-DD')
    if d_from > d_to:
        raise BadRange('"from" is after "to"')
    if (d_to - d_from).days >= MAX_RANGE_DAYS:
        raise BadRange(f'Choose at most {MAX_RANGE_DAYS} days')
    return d_from, d_to


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def _blank(cashier):
    row = {'sales_count': 0, 'sales_value': ZERO, 'discounts': ZERO,
           'voids_count': 0, 'voids_value': ZERO, 'refunds_count': 0, 'refunds_value': ZERO}
    if cashier:
        row.update(shifts_closed=0, shifts_with_variance=0, cash_variance_total=ZERO)
    return row


def performance(d_from, d_to):
    """The report as plain data (Decimals for money)."""
    start, end = nairobi_day_bounds(d_from, d_to)
    people = {}

    def person(user_id):
        if user_id not in people:
            people[user_id] = {'as_cashier': _blank(True), 'as_salesperson': _blank(False)}
        return people[user_id]

    def credit(txn_row, field, amount=None, count_field=None):
        for uid, capacity in ((txn_row.cashier_id, 'as_cashier'),
                              (txn_row.served_by_id, 'as_salesperson')):
            stats = person(uid)[capacity]
            if count_field:
                stats[count_field] += 1
            if amount is not None:
                stats[field] += amount

    sales = (db.session.query(Transaction.id, Transaction.cashier_id, Transaction.served_by_id,
                              Transaction.total, Transaction.discount_amount)
             .filter(Transaction.status.in_((TXN_COMPLETED, TXN_REFUNDED)),
                     Transaction.created_at >= start, Transaction.created_at < end)
             .all())
    line_discounts = dict(
        db.session.query(TransactionItem.transaction_id,
                         func.coalesce(func.sum(TransactionItem.discount), 0))
        .join(Transaction, Transaction.id == TransactionItem.transaction_id)
        .filter(Transaction.status.in_((TXN_COMPLETED, TXN_REFUNDED)),
                Transaction.created_at >= start, Transaction.created_at < end)
        .group_by(TransactionItem.transaction_id).all())
    for t in sales:
        credit(t, 'sales_value', _money(t.total), 'sales_count')
        credit(t, 'discounts', _money(t.discount_amount) + _money(line_discounts.get(t.id)))

    events = (AuditLog.query
              .filter(AuditLog.action.in_(('transaction.void', 'transaction.refund')),
                      AuditLog.entity_type == 'Transaction',
                      AuditLog.created_at >= start, AuditLog.created_at < end)
              .all())
    txn_ids = {e.entity_id for e in events if e.entity_id is not None}
    txns = {t.id: t for t in (
        db.session.query(Transaction.id, Transaction.cashier_id, Transaction.served_by_id,
                         Transaction.total)
        .filter(Transaction.id.in_(txn_ids)).all())} if txn_ids else {}
    voided = set()
    for e in events:
        t = txns.get(e.entity_id)
        if t is None:
            continue
        if e.action == 'transaction.void':
            if t.id in voided:
                continue
            voided.add(t.id)
            credit(t, 'voids_value', _money(t.total), 'voids_count')
        else:
            value = (e.new_value or {}).get('value') if isinstance(e.new_value, dict) else None
            credit(t, 'refunds_value', _money(value), 'refunds_count')

    shifts = (db.session.query(Shift.cashier_id, Shift.cash_variance)
              .filter(Shift.closed_at.isnot(None),
                      Shift.closed_at >= start, Shift.closed_at < end).all())
    for cashier_id, variance in shifts:
        stats = person(cashier_id)['as_cashier']
        stats['shifts_closed'] += 1
        if variance is not None and _money(variance) != ZERO:
            stats['shifts_with_variance'] += 1
            stats['cash_variance_total'] += _money(variance)

    ids = [uid for uid in people if uid is not None]
    users = {u.id: u for u in User.query.filter(User.id.in_(ids)).all()} if ids else {}
    rows = []
    for uid, data in people.items():
        user = users.get(uid)
        for stats in data.values():
            n = stats['sales_count']
            stats['average_basket'] = (stats['sales_value'] / n).quantize(Decimal('0.01')) \
                if n else ZERO
        rows.append({
            'user_id': uid,
            'name': (user.name or user.email) if user else
                    ('Not recorded' if uid is None else 'Former staff'),
            'role': user.role if user else None,
            **data,
        })
    rows.sort(key=lambda r: (-(r['as_salesperson']['sales_value']
                               + r['as_cashier']['sales_value']), r['name'].lower()))
    return {'from': d_from.isoformat(), 'to': d_to.isoformat(), 'people': rows}


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _csv(report):
    buf = io.StringIO()
    fields = ['person', 'role', 'capacity', *SALE_FIELDS, *SHIFT_FIELDS]
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for r in report['people']:
        for capacity, label in (('as_salesperson', 'salesperson'), ('as_cashier', 'cashier')):
            stats = r[capacity]
            w.writerow({'person': r['name'], 'role': r['role'] or '', 'capacity': label,
                        **{k: stats.get(k, '') for k in (*SALE_FIELDS, *SHIFT_FIELDS)}})
    name = f"staff_performance_{report['from']}_{report['to']}.csv"
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={name}'})


@staff_bp.route('/performance', methods=['GET'])
@finance_api_required
def performance_report():
    """``GET /staff/performance?from=YYYY-MM-DD&to=YYYY-MM-DD[&format=csv]``"""
    try:
        d_from, d_to = parse_range(request.args)
    except BadRange as e:
        return jsonify({'error': str(e)}), 400
    report = performance(d_from, d_to)
    if (request.args.get('format') or '').lower() == 'csv':
        return _csv(report)
    return jsonify(_jsonable(report))
