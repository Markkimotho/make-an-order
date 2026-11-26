"""Daily sales and profit, expenses and cashier reconciliation.

Routes on ``reports_bp`` (URL prefix ``/reports``, finance feature, finance
roles: accountant, admin, owner). The numbers come from services/reports.

* ``GET    /reports/daily?from=&to=[&format=csv]``  per-day profit, totals, top items
* ``GET    /reports/monthly?months=12``             monthly net profit
* ``GET    /reports/expenses?from=&to=&category=&method=``
* ``POST   /reports/expenses``                      record an expense
* ``DELETE /reports/expenses/<id>``
* ``GET    /reports/reconciliation?date=``          per cashier for a Nairobi day
* ``GET    /reports/reconciliation/shift/<id>``     one shift
"""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import jsonify, request, session

from api.reports import _csv_response, reports_bp
from auth.auth_middleware import finance_api_required
from models import (
    CashMovement, Expense, EXPENSE_CATEGORIES, EXPENSE_METHODS, Shift, SHIFT_OPEN, User, db,
)
from services import audit_service
from services.clock import today_nairobi
from services.reports import profit, reconciliation
from verticals.features import feature_required

MAX_EXPENSE = Decimal('10000000000')    # Numeric(12, 2)


def _date_arg(name, default):
    """``?name=YYYY-MM-DD`` as a date, ``default`` if absent; ValueError if malformed."""
    raw = (request.args.get(name) or '').strip()
    if not raw:
        return default
    return datetime.strptime(raw, '%Y-%m-%d').date()


def _report_range():
    """(from, to, error response). Default: the last 30 Nairobi days."""
    try:
        d_to = _date_arg('to', today_nairobi())
        d_from = _date_arg('from', d_to - timedelta(days=29))
        profit.check_range(d_from, d_to)
    except profit.RangeError as e:
        return None, None, (jsonify({'error': str(e)}), 400)
    except ValueError:
        return None, None, (jsonify({'error': 'dates must be YYYY-MM-DD'}), 400)
    return d_from, d_to, None


DAILY_CSV_FIELDS = ['date', 'sales_count', 'gross_sales', 'refunds', 'revenue', 'discounts',
                    'tax', 'cogs', 'gross_profit', 'gross_margin_pct', 'expenses', 'net_profit',
                    'cost_missing_lines', 'revenue_without_cost',
                    *profit.METHODS]


def _flat(row):
    return {**{k: v for k, v in row.items() if k != 'by_method'}, **row['by_method']}


@reports_bp.route('/daily', methods=['GET'])
@finance_api_required
def daily_report():
    """Sales, gross and net profit per Nairobi day (``?from=&to=``, at most
    366 days, default the last 30), with totals, top items and categories.
    ``format=csv`` downloads the daily rows and a total row."""
    d_from, d_to, err = _report_range()
    if err:
        return err
    data = profit.report(d_from, d_to)
    if (request.args.get('format') or '').lower() == 'csv':
        rows = [_flat(r) for r in data['days']]
        rows.append({**_flat(data['totals']), 'date': 'TOTAL'})
        return _csv_response(rows, DAILY_CSV_FIELDS,
                             f'daily_report_{d_from.isoformat()}_{d_to.isoformat()}.csv')
    return jsonify(data), 200


@reports_bp.route('/monthly', methods=['GET'])
@finance_api_required
def monthly_report():
    """The last ``?months=`` calendar months (default and most 12)."""
    months = request.args.get('months', default=12, type=int) or 12
    return jsonify({'months': profit.monthly(months)}), 200


# --- expenses -------------------------------------------------------------------

@reports_bp.route('/expenses', methods=['GET'])
@finance_api_required
@feature_required('expenses')
def expenses_list():
    """Expenses in ``?from=&to=`` (default last 30 days), optional
    ``category`` and ``method``, with totals by category and by month."""
    d_from, d_to, err = _report_range()
    if err:
        return err
    q = Expense.query.filter(Expense.spent_on >= d_from, Expense.spent_on <= d_to)
    category = (request.args.get('category') or '').strip()
    method = (request.args.get('method') or '').strip()
    if category:
        q = q.filter(Expense.category == category)
    if method:
        q = q.filter(Expense.method == method)
    rows = q.order_by(Expense.spent_on.desc(), Expense.id.desc()).all()
    creators = {e.created_by for e in rows if e.created_by}
    names = ({u.id: (u.name or u.email) for u in User.query.filter(User.id.in_(creators)).all()}
             if creators else {})
    by_category, by_month = {}, {}
    for e in rows:
        amount = Decimal(str(e.amount))
        by_category[e.category] = by_category.get(e.category, Decimal('0')) + amount
        month = by_month.setdefault(e.spent_on.strftime('%Y-%m'), {})
        month[e.category] = month.get(e.category, Decimal('0')) + amount
    return jsonify({
        'from': d_from.isoformat(), 'to': d_to.isoformat(),
        'categories': list(EXPENSE_CATEGORIES), 'methods': list(EXPENSE_METHODS),
        'expenses': [{**e.to_dict(), 'created_by_name': names.get(e.created_by)} for e in rows],
        'total': profit.money(sum(by_category.values(), Decimal('0'))),
        'by_category': {k: profit.money(v) for k, v in sorted(by_category.items())},
        'by_month': [{'month': m, 'total': profit.money(sum(c.values(), Decimal('0'))),
                      'by_category': {k: profit.money(v) for k, v in sorted(c.items())}}
                     for m, c in sorted(by_month.items(), reverse=True)],
    }), 200


def _expense_reason(expense):
    """The cash_out reason; also how a delete finds its movement."""
    note = f': {expense.note}' if expense.note else ''
    return f'Expense #{expense.id} {expense.category}{note}'[:255]


@reports_bp.route('/expenses', methods=['POST'])
@finance_api_required
@feature_required('expenses')
def expense_create():
    """Record an expense.

    Body: ``{"spent_on": "2026-11-20", "category": "rent", "amount": 15000,
    "method": "cash"|"mpesa"|"bank", "note": "...", "pay_from_drawer": true}``.
    Cash paid from the drawer by someone with an open shift is also a
    cash_out on that shift, so the drawer still reconciles. Without an open
    shift it is recorded with a warning and no drawer movement.
    """
    body = request.get_json(silent=True) or {}
    category = str(body.get('category') or '').strip()
    method = str(body.get('method') or 'cash').strip()
    if category not in EXPENSE_CATEGORIES:
        return jsonify({'error': f'category must be one of {list(EXPENSE_CATEGORIES)}'}), 400
    if method not in EXPENSE_METHODS:
        return jsonify({'error': f'method must be one of {list(EXPENSE_METHODS)}'}), 400
    try:
        amount = Decimal(str(body.get('amount')))
        if not amount.is_finite():
            raise InvalidOperation
        amount = amount.quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        return jsonify({'error': 'amount must be a number greater than 0'}), 400
    if amount <= 0:
        return jsonify({'error': 'amount must be a number greater than 0'}), 400
    if amount >= MAX_EXPENSE:
        return jsonify({'error': 'amount is too large'}), 400
    today = today_nairobi()
    raw = str(body.get('spent_on') or '').strip()
    try:
        spent_on = datetime.strptime(raw, '%Y-%m-%d').date() if raw else today
    except ValueError:
        return jsonify({'error': 'spent_on must be YYYY-MM-DD'}), 400
    if spent_on > today:
        return jsonify({'error': 'spent_on cannot be in the future'}), 400
    note = str(body.get('note') or '').strip()[:255] or None

    uid = session.get('user_id')
    shift, warning = None, None
    if method == 'cash' and body.get('pay_from_drawer') is True:
        shift = (Shift.query.filter_by(cashier_id=uid, status=SHIFT_OPEN)
                 .order_by(Shift.opened_at.desc()).first()) if uid else None
        if shift is None:
            warning = 'You have no open shift, so it was not taken from a drawer.'
    expense = Expense(spent_on=spent_on, category=category, amount=amount, method=method,
                      note=note, created_by=uid, shift_id=shift.id if shift else None)
    db.session.add(expense)
    db.session.flush()
    movement = None
    if shift is not None:
        movement = CashMovement(shift_id=shift.id, user_id=uid, movement_type='cash_out',
                                amount=amount, reason=_expense_reason(expense))
        db.session.add(movement)
        db.session.flush()
        audit_service.log(uid, 'shift.cash_out', 'CashMovement', movement.id, None,
                          movement.to_dict())
    audit_service.log(uid, 'expense.create', 'Expense', expense.id, None, expense.to_dict())
    db.session.commit()
    return jsonify({'expense': expense.to_dict(),
                    'cash_movement': movement.to_dict() if movement else None,
                    'warning': warning}), 201


@reports_bp.route('/expenses/<int:expense_id>', methods=['DELETE'])
@finance_api_required
@feature_required('expenses')
def expense_delete(expense_id):
    """Delete an expense. One paid from a drawer takes its cash_out with it
    while that shift is open; once the shift has closed (the drawer was
    counted against it) it cannot be deleted."""
    expense = db.session.get(Expense, expense_id)
    if expense is None:
        return jsonify({'error': 'Expense not found'}), 404
    if expense.shift_id:
        shift = db.session.get(Shift, expense.shift_id)
        if shift is not None and shift.status != SHIFT_OPEN:
            return jsonify({'error': 'It was paid from a drawer whose shift has closed; '
                                     'record a correction instead'}), 409
        movement = (CashMovement.query
                    .filter(CashMovement.shift_id == expense.shift_id,
                            CashMovement.movement_type == 'cash_out',
                            CashMovement.reason == _expense_reason(expense)).first())
        if movement is not None:
            db.session.delete(movement)
    before = expense.to_dict()
    db.session.delete(expense)
    audit_service.log(session.get('user_id'), 'expense.delete', 'Expense', expense_id,
                      before, None)
    db.session.commit()
    return jsonify({'message': 'Expense deleted'}), 200


# --- reconciliation ---------------------------------------------------------------

@reports_bp.route('/reconciliation', methods=['GET'])
@finance_api_required
def reconciliation_day():
    """Cash, M-Pesa and bank reconciliation for ``?date=`` (a Nairobi day,
    default today), per cashier with their shifts."""
    try:
        day = _date_arg('date', today_nairobi())
    except ValueError:
        return jsonify({'error': 'date must be YYYY-MM-DD'}), 400
    return jsonify(reconciliation.day_report(day)), 200


@reports_bp.route('/reconciliation/shift/<int:shift_id>', methods=['GET'])
@finance_api_required
def reconciliation_shift(shift_id):
    shift = db.session.get(Shift, shift_id)
    if shift is None:
        return jsonify({'error': 'Shift not found'}), 404
    return jsonify(reconciliation.shift_report(shift)), 200
