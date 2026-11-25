"""POS checkout blueprint.

URL prefix: ``/pos`` (registered in app.py).

Endpoints
---------
Shifts
~~~~~~
* ``POST   /pos/shift/open``                           — open a shift; stores ``shift_id`` in session
* ``POST   /pos/shift/close``                          — close current shift, return Z-report
* ``POST   /pos/shift/cash_movement``                  — log petty cash in/out
* ``GET    /pos/shift/current``                        — current shift summary

Transactions (carts)
~~~~~~~~~~~~~~~~~~~~
* ``POST   /pos/transaction/new``                      — create open transaction
* ``GET    /pos/transaction/<id>``                     — fetch with items & totals
* ``POST   /pos/transaction/<id>/add_item``            — add line item
* ``DELETE /pos/transaction/<id>/remove_item/<iid>``   — remove a line item
* ``POST   /pos/transaction/<id>/apply_discount``      — apply transaction-level discount
* ``POST   /pos/transaction/<id>/complete``            — checkout: deduct stock, award loyalty, SMS
* ``POST   /pos/transaction/<id>/void``                — void; manager OTP required if completed
* ``POST   /pos/transaction/<id>/refund``              — refund items, restock, reverse loyalty
* ``GET    /pos/transaction/<id>/receipt``             — receipt JSON
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from datetime import date

from flask import Blueprint, jsonify, request, session
from sqlalchemy import func, select

from auth.auth_middleware import cashier_api_required
from config import Config
from models import (
    db, User, Customer, InventoryItem, StockMovement,
    Shift, CashMovement, Transaction, TransactionItem,
    Prescription, LoyaltyTransaction,
    SHIFT_OPEN, SHIFT_CLOSED,
    TXN_OPEN, TXN_COMPLETED, TXN_VOIDED, TXN_REFUNDED,
    PAY_CASH, PAY_MPESA, PAY_CARD, PAY_CREDIT, PAY_SPLIT, PAYMENT_METHODS,
    ROLE_ADMIN, ROLE_SUPERUSER,
)
from services import audit_service, billing, loyalty_service, otp_service, stock
from services.receipt_service import build_receipt
from services.clock import today_nairobi
from services.sms_service import SendSMS

pos_bp = Blueprint('pos', __name__)
logger = logging.getLogger(__name__)

# Session key used to remember the cashier's open shift.
SHIFT_SESSION_KEY = 'pos_shift_id'

# Session key for "manager has authorised the next sensitive action".
# (We use a per-action OTP scope; this is just a convenience flag.)
MONEY = Decimal('0.01')


# ===========================================================================
# Helpers
# ===========================================================================

def _q(value) -> Decimal:
    """Quantise a value to 2 dp (cents)."""
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _user_id() -> int | None:
    return session.get('user_id')


def _current_shift() -> Shift | None:
    """Return the cashier's current open shift (from session, then DB)."""
    sid = session.get(SHIFT_SESSION_KEY)
    if sid:
        s = db.session.get(Shift, sid)
        if s and s.status == SHIFT_OPEN:
            return s
        # Stale session — clear.
        session.pop(SHIFT_SESSION_KEY, None)

    uid = _user_id()
    if not uid:
        return None
    s = (Shift.query.filter_by(cashier_id=uid, status=SHIFT_OPEN)
         .order_by(Shift.opened_at.desc()).first())
    if s:
        session[SHIFT_SESSION_KEY] = s.id
    return s


def _generate_transaction_ref() -> str:
    """``TXN-<tenant>-YYYYMMDD-NNNN``, sequential per business per UTC day.

    The tenant id makes refs unique across businesses: payment providers
    echo the ref back, and a ref two businesses shared could not be matched
    to either.
    """
    from auth.tenancy import current_tenant_id
    today = datetime.utcnow().strftime('%Y%m%d')
    prefix = f"TXN-{current_tenant_id()}-{today}-"
    last = (Transaction.query
            .filter(Transaction.transaction_ref.like(f"{prefix}%"))
            .order_by(Transaction.id.desc())
            .first())
    seq = 1
    if last and last.transaction_ref.startswith(prefix):
        try:
            seq = int(last.transaction_ref.rsplit('-', 1)[1]) + 1
        except (ValueError, IndexError):
            seq = 1
    return f"{prefix}{seq:04d}"


def _recalc_totals(txn: Transaction, *, discount_override: Decimal | None = None) -> None:
    """Recompute subtotal/tax/total for ``txn`` from its items.

    The item list is reloaded first: lines added or deleted in this request
    are not in an already-loaded ``txn.items`` (that made the running total
    lag one line behind every add and remove).

    Tax = ``Config.TAX_RATE`` applied to (subtotal - discount). Inclusive
    pricing not assumed — tax is added on top. Caller flushes/commits.
    """
    db.session.flush()
    db.session.expire(txn, ['items'])
    subtotal = Decimal('0')
    for item in txn.items:
        subtotal += Decimal(str(item.line_total or 0))
    txn.subtotal = _q(subtotal)
    if discount_override is not None:
        txn.discount_amount = _q(discount_override)
    discount = Decimal(str(txn.discount_amount or 0))
    taxable = max(subtotal - discount, Decimal('0'))
    tax_rate = Decimal(str(Config.TAX_RATE or 0))
    txn.tax_amount = _q(taxable * tax_rate)
    txn.total      = _q(taxable + txn.tax_amount)


def _stock_units_for(qty: Decimal) -> int:
    """How many integer stock units a sale of ``qty`` consumes.

    Stock is tracked as integers; for fractional sales (0.5 kg from a 1 kg
    pack) we round UP — opening a pack consumes the whole unit from stock.
    """
    f = float(qty)
    if f <= 0:
        return 0
    return int(math.ceil(f))


def _ensure_open_shift_or_403():
    shift = _current_shift()
    if shift is None:
        return None, (jsonify({'error': 'No open shift. Open a shift first.'}), 403)
    return shift, None


def _units_returned(line, refunded=None):
    """Whole stock units already back on the shelf for a line that has had
    ``refunded`` (default: its refunded_quantity) returned. Never more than
    the line took, so repeated fractional refunds or a void after a partial
    refund cannot put back more than was sold."""
    refunded = Decimal(str(line.refunded_quantity or 0)) if refunded is None else refunded
    return min(_stock_units_for(refunded), _stock_units_for(Decimal(str(line.quantity))))


def _lock_txn(txn):
    """Serialise money work on this sale: the business's billing lock, then
    the sale row reloaded under lock (two taps on 'complete' cannot both
    create an invoice and take payment)."""
    from services.billing.invoices import lock_tenant
    lock_tenant()
    return db.session.execute(select(Transaction).where(Transaction.id == txn.id)
                              .with_for_update()
                              .execution_options(populate_existing=True)).scalar_one()


def _invoice_to_credit(txn):
    """The invoice a void or refund of this sale credits: its own, or, for a
    credit sale made before invoices existed, the customer's opening-balance
    invoice (which holds that debt since migration 0004)."""
    invoice = billing.open_invoice_for_transaction(txn.id)
    if invoice is not None or txn.payment_method != PAY_CREDIT or not txn.customer_id:
        return invoice
    from models import Invoice
    return (Invoice.query.filter(Invoice.customer_id == txn.customer_id,
                                 Invoice.kind == 'opening_balance',
                                 Invoice.status.in_(('open', 'partial')))
            .order_by(Invoice.id.asc()).first())


def _online_cards():
    from flask import current_app
    return bool(current_app.config.get('CARD_PAYMENTS_ENABLED'))


def _invoice_payments(invoice):
    from models import Payment, PaymentAllocation
    ids = [a.payment_id for a in PaymentAllocation.query.filter(
        PaymentAllocation.invoice_id == invoice.id,
        PaymentAllocation.voided_at.is_(None))]
    return Payment.query.filter(Payment.id.in_(ids)).all() if ids else []


def _txn_or_404(txn_id: int):
    txn = db.session.get(Transaction, txn_id)
    if txn is None:
        return None, (jsonify({'error': 'Transaction not found'}), 404)
    return txn, None


# ===========================================================================
# Shifts
# ===========================================================================

@pos_bp.route('/shift/open', methods=['POST'])
@cashier_api_required
def open_shift():
    """Open a new shift for the logged-in cashier.

    Body: ``{"opening_float": 5000.00, "branch_id": 1?}``.
    Refuses if the cashier already has an open shift.
    """
    uid = _user_id()
    if uid is None:
        return jsonify({'error': 'Unauthorized'}), 401

    if Shift.query.filter_by(cashier_id=uid, status=SHIFT_OPEN).first():
        return jsonify({'error': 'You already have an open shift'}), 400

    body = request.get_json(silent=True) or {}
    try:
        opening_float = _q(body.get('opening_float', 0))
        if opening_float < 0:
            raise ValueError
    except Exception:
        return jsonify({'error': 'opening_float must be a non-negative number'}), 400

    shift = Shift(
        cashier_id=uid,
        branch_id=body.get('branch_id'),
        opening_float=opening_float,
        status=SHIFT_OPEN,
    )
    db.session.add(shift)
    db.session.flush()
    audit_service.log(uid, 'shift.open', 'Shift', shift.id,
                      None, {'opening_float': float(opening_float)})
    db.session.commit()
    session[SHIFT_SESSION_KEY] = shift.id
    return jsonify({'message': 'Shift opened', 'shift': shift.to_dict()}), 201


@pos_bp.route('/shift/current', methods=['GET'])
@cashier_api_required
def current_shift_endpoint():
    shift = _current_shift()
    if shift is None:
        return jsonify({'shift': None}), 200
    return jsonify({'shift': shift.to_dict()}), 200


@pos_bp.route('/shift/close', methods=['POST'])
@cashier_api_required
def close_shift():
    """Close the current shift. Body: ``{"closing_cash": 4850.00}``.

    Computes expected cash = opening_float + cash sales + cash_in - cash_out
    and the variance against the counted ``closing_cash``.
    Returns full Z-report data.
    """
    shift, err = _ensure_open_shift_or_403()
    if err: return err

    body = request.get_json(silent=True) or {}
    try:
        closing_cash = _q(body.get('closing_cash', 0))
    except Exception:
        return jsonify({'error': 'closing_cash must be a number'}), 400

    z = _build_z_report(shift)
    expected = _q(z['expected_cash'])
    shift.closing_cash  = closing_cash
    shift.expected_cash = expected
    shift.cash_variance = _q(closing_cash - expected)
    shift.closed_at     = datetime.utcnow()
    shift.status        = SHIFT_CLOSED

    audit_service.log(_user_id(), 'shift.close', 'Shift', shift.id,
                      None, {
                          'closing_cash': float(closing_cash),
                          'expected_cash': float(expected),
                          'cash_variance': float(shift.cash_variance),
                      })
    db.session.commit()
    session.pop(SHIFT_SESSION_KEY, None)

    z['shift'] = shift.to_dict()
    return jsonify({'message': 'Shift closed', 'z_report': z}), 200


@pos_bp.route('/shift/cash_movement', methods=['POST'])
@cashier_api_required
def shift_cash_movement():
    """Log a petty cash in/out.

    Body: ``{"type": "cash_in"|"cash_out", "amount": 200, "reason": "..."}``.
    """
    shift, err = _ensure_open_shift_or_403()
    if err: return err
    body = request.get_json(silent=True) or {}
    mtype = (body.get('type') or '').strip()
    if mtype not in ('cash_in', 'cash_out'):
        return jsonify({'error': "type must be 'cash_in' or 'cash_out'"}), 400
    try:
        amount = _q(body.get('amount', 0))
        if amount <= 0:
            raise ValueError
    except Exception:
        return jsonify({'error': 'amount must be a positive number'}), 400

    mv = CashMovement(
        shift_id=shift.id,
        user_id=_user_id(),
        movement_type=mtype,
        amount=amount,
        reason=(body.get('reason') or '').strip() or None,
    )
    db.session.add(mv)
    db.session.flush()
    audit_service.log(_user_id(), f'shift.{mtype}', 'CashMovement', mv.id,
                      None, mv.to_dict())
    db.session.commit()
    return jsonify({'message': 'Cash movement logged', 'movement': mv.to_dict()}), 201


def _build_z_report(shift: Shift) -> dict:
    """Z-report numbers for a (possibly still-open) shift."""
    # Sales by payment method, completed only.
    rows = (db.session.query(Transaction.payment_method,
                             func.coalesce(func.sum(Transaction.total), 0))
            .filter(Transaction.shift_id == shift.id,
                    Transaction.status == TXN_COMPLETED)
            .group_by(Transaction.payment_method)
            .all())
    by_method = {m or 'unspecified': float(t) for m, t in rows}
    cash_sales  = Decimal(str(by_method.get(PAY_CASH, 0)))
    mpesa_sales = Decimal(str(by_method.get(PAY_MPESA, 0)))
    card_sales  = Decimal(str(by_method.get(PAY_CARD, 0)))

    refund_total = (db.session.query(func.coalesce(func.sum(Transaction.total), 0))
                    .filter(Transaction.shift_id == shift.id,
                            Transaction.status == TXN_REFUNDED)
                    .scalar() or 0)
    refund_total = Decimal(str(refund_total))

    cash_in  = (db.session.query(func.coalesce(func.sum(CashMovement.amount), 0))
                .filter(CashMovement.shift_id == shift.id,
                        CashMovement.movement_type == 'cash_in').scalar() or 0)
    cash_out = (db.session.query(func.coalesce(func.sum(CashMovement.amount), 0))
                .filter(CashMovement.shift_id == shift.id,
                        CashMovement.movement_type == 'cash_out').scalar() or 0)
    cash_in, cash_out = Decimal(str(cash_in)), Decimal(str(cash_out))

    expected_cash = (Decimal(str(shift.opening_float or 0))
                     + cash_sales + cash_in - cash_out)

    return {
        'shift_id':       shift.id,
        'opening_float':  float(shift.opening_float or 0),
        'cash_sales':     float(cash_sales),
        'mpesa_sales':    float(mpesa_sales),
        'card_sales':     float(card_sales),
        'sales_by_method': {k: float(v) for k, v in by_method.items()},
        'cash_in':        float(cash_in),
        'cash_out':       float(cash_out),
        'refund_total':   float(refund_total),
        'expected_cash':  float(expected_cash),
        'closing_cash':   float(shift.closing_cash) if shift.closing_cash is not None else None,
        'cash_variance':  float(shift.cash_variance) if shift.cash_variance is not None else None,
    }


# ===========================================================================
# Transactions
# ===========================================================================

@pos_bp.route('/transaction/new', methods=['POST'])
@cashier_api_required
def transaction_new():
    """Create a new open transaction (an empty cart).

    Body (all optional): ``{"customer_id": 1, "notes": "..."}``.
    """
    shift, err = _ensure_open_shift_or_403()
    if err: return err

    body = request.get_json(silent=True) or {}
    customer_id = body.get('customer_id')
    if customer_id is not None:
        if db.session.get(Customer, customer_id) is None:
            return jsonify({'error': 'Customer not found'}), 404

    txn = Transaction(
        transaction_ref=_generate_transaction_ref(),
        customer_id=customer_id,
        cashier_id=_user_id(),
        shift_id=shift.id,
        status=TXN_OPEN,
        notes=(body.get('notes') or None),
    )
    db.session.add(txn)
    db.session.flush()
    db.session.commit()
    return jsonify({
        'message': 'Transaction opened',
        'transaction_id': txn.id,
        'transaction_ref': txn.transaction_ref,
        'transaction': txn.to_dict(),
    }), 201


@pos_bp.route('/transaction/<int:txn_id>', methods=['GET'])
@cashier_api_required
def transaction_get(txn_id):
    txn, err = _txn_or_404(txn_id)
    if err: return err
    return jsonify(txn.to_dict()), 200


@pos_bp.route('/transaction/<int:txn_id>/add_item', methods=['POST'])
@cashier_api_required
def transaction_add_item(txn_id):
    """Add a line item to an open transaction."""
    txn, err = _txn_or_404(txn_id)
    if err: return err
    if txn.status != TXN_OPEN:
        return jsonify({'error': f'Transaction is {txn.status}, cannot modify'}), 400

    body = request.get_json(silent=True) or {}
    item_id = body.get('inventory_item_id')
    if not item_id:
        return jsonify({'error': 'inventory_item_id is required'}), 400
    item = db.session.get(InventoryItem, item_id)
    if not item:
        return jsonify({'error': 'Inventory item not found'}), 404

    try:
        qty = Decimal(str(body.get('quantity', 1)))
    except Exception:
        return jsonify({'error': 'quantity must be numeric'}), 400
    if qty <= 0:
        return jsonify({'error': 'quantity must be > 0'}), 400

    # Validate stock against existing line allocations + new qty.
    already = sum((Decimal(str(li.quantity)) for li in txn.items
                   if li.inventory_item_id == item_id), Decimal('0'))
    needed_units = _stock_units_for(already + qty)
    if needed_units > int(item.stock or 0):
        return jsonify({'error': f'Insufficient stock for {item.name} '
                                  f'(have {item.stock}, need {needed_units})'}), 400

    # Prescription gating happens at /complete time so the cashier can build
    # the cart freely first; the dispensary endpoint links a Prescription.

    discount = _q(body.get('discount', 0))
    unit_price = Decimal(str(item.price))
    line_total = _q((unit_price * qty) - discount)
    if line_total < 0:
        return jsonify({'error': 'discount cannot exceed line subtotal'}), 400

    line = TransactionItem(
        transaction_id=txn.id,
        inventory_item_id=item.id,
        item_name=item.name,
        quantity=qty,
        unit_price=unit_price,
        discount=discount,
        line_total=line_total,
        batch_number=None,          # set at checkout from the batches actually taken
    )
    db.session.add(line)
    db.session.flush()
    _recalc_totals(txn)
    db.session.commit()
    return jsonify({
        'message': 'Item added',
        'line': line.to_dict(),
        'transaction': txn.to_dict(),
    }), 201


@pos_bp.route('/transaction/<int:txn_id>/remove_item/<int:line_id>', methods=['DELETE'])
@cashier_api_required
def transaction_remove_item(txn_id, line_id):
    txn, err = _txn_or_404(txn_id)
    if err: return err
    if txn.status != TXN_OPEN:
        return jsonify({'error': f'Transaction is {txn.status}, cannot modify'}), 400
    line = db.session.get(TransactionItem, line_id)
    if not line or line.transaction_id != txn.id:
        return jsonify({'error': 'Line item not found on this transaction'}), 404
    db.session.delete(line)
    db.session.flush()
    _recalc_totals(txn)
    db.session.commit()
    return jsonify({'message': 'Item removed', 'transaction': txn.to_dict()}), 200


@pos_bp.route('/transaction/<int:txn_id>/apply_discount', methods=['POST'])
@cashier_api_required
def transaction_apply_discount(txn_id):
    """Apply a transaction-level discount.

    Body: ``{"type": "percent"|"flat", "value": 10}``.
    """
    txn, err = _txn_or_404(txn_id)
    if err: return err
    if txn.status != TXN_OPEN:
        return jsonify({'error': f'Transaction is {txn.status}, cannot modify'}), 400

    body = request.get_json(silent=True) or {}
    dtype = (body.get('type') or '').lower()
    try:
        value = Decimal(str(body.get('value', 0)))
    except Exception:
        return jsonify({'error': 'value must be numeric'}), 400
    if value < 0:
        return jsonify({'error': 'value must be >= 0'}), 400

    subtotal = sum((Decimal(str(li.line_total)) for li in txn.items), Decimal('0'))
    if dtype == 'percent':
        if value > 100:
            return jsonify({'error': 'percent must be <= 100'}), 400
        discount = (subtotal * value / Decimal('100'))
    elif dtype == 'flat':
        discount = value
    else:
        return jsonify({'error': "type must be 'percent' or 'flat'"}), 400
    if discount > subtotal:
        return jsonify({'error': 'discount cannot exceed subtotal'}), 400

    _recalc_totals(txn, discount_override=discount)
    db.session.commit()
    return jsonify({'message': 'Discount applied', 'transaction': txn.to_dict()}), 200


@pos_bp.route('/transaction/<int:txn_id>/complete', methods=['POST'])
@cashier_api_required
def transaction_complete(txn_id):
    """Finalise the sale.

    Body::

        {
          "payment_method": "cash" | "mpesa" | "card" | "credit" | "split",
          "cash_amount": 0, "mpesa_amount": 0, "card_amount": 0,
          "mpesa_ref": "...", "card_ref": "..."
        }

    For ``"split"`` at least two of the *_amount values must be > 0 and
    they must sum to the transaction total.
    For ``"credit"`` the customer must exist and have sufficient credit limit.
    """
    txn, err = _txn_or_404(txn_id)
    if err: return err
    txn = _lock_txn(txn)
    if txn.status != TXN_OPEN:
        db.session.rollback()
        return jsonify({'error': f'Transaction is {txn.status}'}), 400
    if not txn.items:
        db.session.rollback()
        return jsonify({'error': 'Cannot complete an empty cart'}), 400

    body = request.get_json(silent=True) or {}
    method = (body.get('payment_method') or '').lower()
    if method not in PAYMENT_METHODS:
        return jsonify({'error': f'payment_method must be one of {list(PAYMENT_METHODS)}'}), 400

    _recalc_totals(txn)
    total = Decimal(str(txn.total))
    cash_a = mpesa_a = card_a = Decimal('0')

    # Validate split amounts
    if method == PAY_SPLIT:
        cash_a  = Decimal(str(body.get('cash_amount', 0) or 0))
        mpesa_a = Decimal(str(body.get('mpesa_amount', 0) or 0))
        card_a  = Decimal(str(body.get('card_amount', 0) or 0))
        if any(a < 0 for a in (cash_a, mpesa_a, card_a)):
            return jsonify({'error': 'split amounts must be non-negative'}), 400
        non_zero = [a for a in (cash_a, mpesa_a, card_a) if a > 0]
        if len(non_zero) < 2:
            return jsonify({'error': 'split must have at least two non-zero amounts'}), 400
        if _q(cash_a + mpesa_a + card_a) != _q(total):
            return jsonify({'error': 'split amounts must sum to total'}), 400

    # Prescription gating: any line that requires prescription must already be
    # linked to a dispensed PrescriptionItem on this transaction.
    rx_required = [li for li in txn.items
                   if li.inventory_item_id
                   and (db.session.get(InventoryItem, li.inventory_item_id) or
                        type('x', (), {'requires_prescription': False})()).requires_prescription]
    if rx_required:
        dispensed_item_ids = set()
        for rx in (Prescription.query
                   .filter(Prescription.transaction_id == txn.id).all()):
            for it in rx.items:
                if it.is_dispensed:
                    dispensed_item_ids.add(it.inventory_item_id)
        missing = [li.item_name for li in rx_required
                   if li.inventory_item_id not in dispensed_item_ids]
        if missing:
            return jsonify({'error': 'Prescription required for: ' + ', '.join(missing),
                            'requires_prescription': missing}), 403

    # ----- Payment checks (nothing is written until all of them pass) -----
    customer = db.session.get(Customer, txn.customer_id) if txn.customer_id else None
    invoice = billing.open_invoice_for_transaction(txn.id)
    paid_so_far = billing.money(invoice.amount_paid) if invoice else Decimal('0')
    if invoice is not None and billing.money(invoice.total) != _q(total) and paid_so_far > 0:
        return jsonify({'error': 'The cart changed after a payment was received; '
                                 'void and start again'}), 409
    # Money that arrives by itself (M-Pesa; online cards when switched on)
    # must already be recorded on the invoice; the till does not record it.
    awaits_provider = method == PAY_MPESA or (method == PAY_CARD and _online_cards())
    if awaits_provider and (invoice is None or paid_so_far < _q(total)):
        db.session.rollback()
        return jsonify({'error': f'{"M-Pesa" if method == PAY_MPESA else "Card"} payment '
                                 'not received yet',
                        'invoice': invoice.to_dict() if invoice else None}), 409
    if method == PAY_SPLIT and paid_so_far != _q(mpesa_a):
        # The tender must match what M-Pesa actually brought in, or cash
        # taken at the till would silently go unrecorded.
        db.session.rollback()
        return jsonify({'error': f'M-Pesa received is {paid_so_far}; the split says '
                                 f'{_q(mpesa_a)}. Adjust the cash/card amounts.'}), 409
    if method == PAY_CREDIT:
        if customer is None:
            return jsonify({'error': 'credit payment requires a customer'}), 400
        # Lock the customer so two credit sales cannot both pass the ceiling.
        customer = db.session.execute(select(Customer).where(Customer.id == customer.id)
                                      .with_for_update()
                                      .execution_options(populate_existing=True)).scalar_one()
        exposure = billing.customer_exposure(customer.id)
        limit = Decimal(str(customer.credit_limit or 0))
        if exposure + _q(total) - paid_so_far > limit:
            db.session.rollback()
            return jsonify({'error': f'Customer credit limit exceeded '
                                     f'(limit {limit}, would be {exposure + _q(total)})'}), 400

    # ----- Mutate stock & log movements -----
    uid = _user_id()
    low_stock = []                       # alerted after the commit
    for li in txn.items:
        if not li.inventory_item_id:
            continue
        item = db.session.get(InventoryItem, li.inventory_item_id)
        if not item:
            continue
        units = _stock_units_for(li.quantity)
        try:
            taken = stock.take(item, units, transaction_item=li, reason='sale', user_id=uid,
                               note=f'TXN {txn.transaction_ref}')
        except stock.StockError as e:
            # Nothing is committed yet: no stock, invoice or payment changes.
            db.session.rollback()
            return jsonify({'error': str(e)}), 409
        li.batch_number = stock.batch_label(taken) if taken else li.batch_number
        item = db.session.get(InventoryItem, li.inventory_item_id)
        if int(item.stock or 0) <= int(item.reorder_level or 0):
            low_stock.append((item.id, item.name, int(item.stock or 0),
                              int(item.reorder_level or 0)))

    # ----- Invoice and payments -----
    if invoice is None:
        invoice = billing.create_invoice(
            total=total, kind='credit' if method == PAY_CREDIT else 'sale',
            customer_id=customer.id if customer else None, transaction_id=txn.id)
    elif billing.money(invoice.total) != _q(total):
        invoice.total = _q(total)          # cart edited after an unpaid STK attempt
        billing.recompute(invoice)
    if method == PAY_CREDIT:
        invoice.kind = 'credit'
        invoice.customer_id = customer.id
        invoice.due_date = billing.credit_due_date(customer, date.today())
    customer_id = customer.id if customer else None
    to_record = []
    if method == PAY_CASH or (method == PAY_CARD and not _online_cards()):
        to_record.append((method, None))
    elif method == PAY_SPLIT:
        to_record += [(PAY_CASH, cash_a), (PAY_CARD, card_a)]
    for pay_method, amount in to_record:
        invoice = billing.recompute(invoice)
        amount = invoice.balance if amount is None else min(amount, invoice.balance)
        if amount > 0:
            billing.record_payment(method=pay_method, amount=amount, invoice=invoice,
                                   customer_id=customer_id, recorded_by=_user_id())
    invoice = billing.recompute(invoice)
    if method != PAY_CREDIT and invoice.status != 'paid':
        db.session.rollback()
        return jsonify({'error': 'Payment does not cover the total'}), 409

    # ----- Update transaction header -----
    txn.payment_method = method
    receipts = [p.mpesa_receipt for p in _invoice_payments(invoice) if p.mpesa_receipt]
    txn.mpesa_ref = receipts[0] if receipts else txn.mpesa_ref
    txn.card_ref  = (body.get('card_ref')  or txn.card_ref)
    txn.status    = TXN_COMPLETED
    txn.updated_at = datetime.utcnow()

    # ----- Loyalty points -----
    if customer and method != PAY_CREDIT:
        try:
            loyalty_service.award_points(customer.id, txn.id, total)
        except Exception:
            logger.warning('loyalty award failed', exc_info=True)

    audit_service.log(uid, 'transaction.complete', 'Transaction', txn.id,
                      None, {'total': float(total), 'method': method})
    db.session.commit()

    # ----- SMS receipt and low-stock alerts (best-effort, after the commit) -----
    try:
        if customer and customer.phone_number:
            SendSMS.send_payment_confirmation(customer.phone_number,
                                              total, txn.transaction_ref)
    except Exception:
        db.session.rollback()
        logger.warning('payment SMS failed', exc_info=True)
    if low_stock:
        try:
            admin_phones = [u.phone_number for u in
                            User.query.filter(User.role.in_((ROLE_ADMIN, ROLE_SUPERUSER)),
                                              User.phone_number.isnot(None)).all()]
            for item_id, name, left, reorder in low_stock:
                SendSMS.send_low_stock_alert(admin_phones, name, left, reorder,
                                             item_id=item_id, today=today_nairobi())
        except Exception:
            db.session.rollback()
            logger.warning('low-stock SMS failed', exc_info=True)

    return jsonify({
        'message': 'Transaction completed',
        'transaction': txn.to_dict(),
        'invoice': invoice.to_dict(),
        'receipt': build_receipt(txn.id),
    }), 200


@pos_bp.route('/transaction/<int:txn_id>/void', methods=['POST'])
@cashier_api_required
def transaction_void(txn_id):
    """Void a transaction.

    Open transactions can be voided directly. Completed transactions require
    a manager OTP — first call returns ``202 {"otp_required": true}`` and
    triggers an SMS to all admins. The cashier re-posts with ``"otp": "..."``.
    """
    txn, err = _txn_or_404(txn_id)
    if err: return err
    if txn.status in (TXN_VOIDED, TXN_REFUNDED):
        return jsonify({'error': f'Transaction already {txn.status}'}), 400

    body = request.get_json(silent=True) or {}
    reason = (body.get('reason') or '').strip() or 'unspecified'
    uid = _user_id()

    scope = f'void:txn:{txn.id}'
    provided_otp = (body.get('otp') or '').strip()
    if txn.status == TXN_COMPLETED and not provided_otp:
        otp = otp_service.generate_otp(scope)
        admins = User.query.filter(User.role.in_((ROLE_ADMIN, ROLE_SUPERUSER)),
                                   User.phone_number.isnot(None)).all()
        for a in admins:
            SendSMS.send_manager_otp(a.phone_number, otp, 'void')
        return jsonify({'otp_required': True,
                        'message': 'Manager OTP sent. Re-submit with "otp".'}), 202

    # Decide on the locked row: a checkout finishing between the read above
    # and here must not turn this into a completed-sale void without the OTP
    # or without its stock coming back. Lock order: billing, sale, stock.
    txn = _lock_txn(txn)
    if txn.status in (TXN_VOIDED, TXN_REFUNDED):
        db.session.rollback()
        return jsonify({'error': f'Transaction already {txn.status}'}), 400
    if txn.status == TXN_COMPLETED:
        if not provided_otp:
            db.session.rollback()
            return jsonify({'error': 'This sale was completed meanwhile; voiding it needs '
                                     'a manager OTP. Void it again.'}), 409
        if not otp_service.verify_otp(scope, provided_otp):
            db.session.rollback()
            return jsonify({'error': 'Invalid or expired OTP'}), 403
        # Restore what is still out (a partial refund already put some back).
        for li in txn.items:
            if not li.inventory_item_id:
                continue
            item = db.session.get(InventoryItem, li.inventory_item_id)
            if not item:
                continue
            units = _stock_units_for(Decimal(str(li.quantity))) - _units_returned(li)
            if units > 0:
                stock.give_back(item, units, transaction_item=li, reason='cancel', user_id=uid,
                                note=f'VOID {txn.transaction_ref}')
        # Reverse loyalty earned on the original sale
        if txn.customer_id:
            earned = (db.session.query(func.coalesce(func.sum(LoyaltyTransaction.points_earned), 0))
                      .filter(LoyaltyTransaction.transaction_id == txn.id).scalar() or 0)
            if earned:
                cust = db.session.get(Customer, txn.customer_id)
                if cust:
                    cust.loyalty_points = max(int(cust.loyalty_points or 0) - int(earned), 0)
                    db.session.add(LoyaltyTransaction(
                        customer_id=cust.id, transaction_id=txn.id,
                        points_earned=0, points_redeemed=int(earned),
                        balance_after=cust.loyalty_points,
                        note=f'reversal: void {txn.transaction_ref}',
                    ))

    # The invoice goes too. Cash/card already taken is handed back; M-Pesa
    # money (including an STK paid on a sale still open) becomes credit.
    invoice = _invoice_to_credit(txn)
    if invoice is not None and invoice.transaction_id == txn.id:
        _, released = billing.void_invoice(invoice, uid, reason=reason, roll=False)
        billing.settle_released(released, uid, note=f'VOID {txn.transaction_ref}')
    elif invoice is not None and txn.status == TXN_COMPLETED:
        # A pre-invoice credit sale: take its amount off the opening balance.
        credit = min(_q(Decimal(str(txn.total or 0))), billing.money(invoice.balance))
        if credit > 0:
            _, released = billing.adjust(invoice, credit, 'void', note=f'VOID {txn.transaction_ref}',
                                         user_id=uid, roll=False)
            billing.settle_released(released, uid, note=f'VOID {txn.transaction_ref}')

    txn.status = TXN_VOIDED
    txn.notes = ((txn.notes or '') + f'\nVOID: {reason}').strip()
    audit_service.log(uid, 'transaction.void', 'Transaction', txn.id,
                      None, {'reason': reason})
    db.session.commit()
    return jsonify({'message': 'Transaction voided', 'transaction': txn.to_dict()}), 200


@pos_bp.route('/transaction/<int:txn_id>/refund', methods=['POST'])
@cashier_api_required
def transaction_refund(txn_id):
    """Refund selected items from a completed transaction.

    Body::

        { "items": [{"transaction_item_id": 5, "quantity": 1}, ...],
          "reason": "Defective" }

    Restocks the refunded units, reverses loyalty earned proportionally,
    lowers the sale's invoice with a credit note (cash/card handed back, M-Pesa kept as credit).
    Marks the transaction ``refunded`` only when fully refunded;
    otherwise leaves it ``completed`` and records the partial refund in notes.
    """
    txn, err = _txn_or_404(txn_id)
    if err: return err
    if txn.status != TXN_COMPLETED:
        return jsonify({'error': f'Can only refund completed transactions (status={txn.status})'}), 400

    body = request.get_json(silent=True) or {}
    refund_items = body.get('items') or []
    reason = (body.get('reason') or '').strip() or 'unspecified'
    if not refund_items:
        return jsonify({'error': 'items list is required'}), 400

    uid = _user_id()
    txn = _lock_txn(txn)
    if txn.status != TXN_COMPLETED:
        db.session.rollback()
        return jsonify({'error': f'Can only refund completed transactions (status={txn.status})'}), 400

    # Add up the request per line, then check against what is still unreturned
    # (a replayed or duplicated request cannot return the same goods twice).
    wanted = {}
    for entry in refund_items:
        lid = entry.get('transaction_item_id')
        try:
            qty = Decimal(str(entry.get('quantity', 0)))
        except Exception:
            db.session.rollback()
            return jsonify({'error': 'item quantity must be numeric'}), 400
        if qty <= 0:
            continue
        line = db.session.get(TransactionItem, lid) if lid else None
        if not line or line.transaction_id != txn.id:
            db.session.rollback()
            return jsonify({'error': f'Line {lid} not on this transaction'}), 404
        wanted[line.id] = (line, wanted.get(line.id, (line, Decimal('0')))[1] + qty)
    if not wanted:
        db.session.rollback()
        return jsonify({'error': 'nothing to refund'}), 400
    for line, qty in wanted.values():
        left = Decimal(str(line.quantity)) - Decimal(str(line.refunded_quantity or 0))
        if qty > left:
            db.session.rollback()
            return jsonify({'error': f'Only {left} of {line.item_name} left to refund'}), 400

    # Each line's share of what the customer actually paid: tax and any
    # sale-wide discount are spread in proportion to the line totals.
    subtotal = Decimal(str(txn.subtotal or 0))
    ratio = (Decimal(str(txn.total or 0)) / subtotal) if subtotal > 0 else Decimal('0')
    refunded_value = Decimal('0')
    for line, qty in wanted.values():
        line_qty = Decimal(str(line.quantity))
        unit_value = Decimal(str(line.line_total)) / line_qty if line_qty > 0 else Decimal('0')
        refunded_value += _q(unit_value * qty * ratio)
        before = _units_returned(line)
        line.refunded_quantity = Decimal(str(line.refunded_quantity or 0)) + qty
        units = _units_returned(line) - before
        if line.inventory_item_id and units > 0:
            item = db.session.get(InventoryItem, line.inventory_item_id)
            if item:
                stock.give_back(item, units, transaction_item=line,
                                reason='refund', user_id=uid,
                                note=f'REFUND {txn.transaction_ref}')
    fully_refunded = all(Decimal(str(li.refunded_quantity or 0)) >= Decimal(str(li.quantity))
                         for li in txn.items)

    # Loyalty reversal proportional to refunded fraction of total
    total = Decimal(str(txn.total or 0))
    if txn.customer_id and total > 0 and refunded_value > 0:
        earned = (db.session.query(func.coalesce(func.sum(LoyaltyTransaction.points_earned), 0))
                  .filter(LoyaltyTransaction.transaction_id == txn.id).scalar() or 0)
        if earned:
            reverse_pts = int((Decimal(str(earned)) * refunded_value / total)
                              .to_integral_value(rounding=ROUND_HALF_UP))
            if reverse_pts > 0:
                cust = db.session.get(Customer, txn.customer_id)
                if cust:
                    cust.loyalty_points = max(int(cust.loyalty_points or 0) - reverse_pts, 0)
                    db.session.add(LoyaltyTransaction(
                        customer_id=cust.id, transaction_id=txn.id,
                        points_earned=0, points_redeemed=reverse_pts,
                        balance_after=cust.loyalty_points,
                        note=f'reversal: refund {txn.transaction_ref}',
                    ))

    # Credit note on the sale's invoice (or, for a credit sale made before
    # invoices existed, the customer's opening balance). It lowers what is
    # owed; money already paid above that goes back (cash/card) or stays as
    # the customer's credit (M-Pesa). The last return takes whatever is left,
    # so rounding never strands a few cents.
    invoice = _invoice_to_credit(txn)
    if invoice is not None and refunded_value > 0:
        still_invoiced = billing.money(invoice.total) - billing.money(invoice.amount_adjusted)
        if fully_refunded and invoice.transaction_id == txn.id:
            refunded_value = still_invoiced
        credit = min(refunded_value, still_invoiced,
                     billing.money(invoice.balance) if invoice.transaction_id != txn.id
                     else still_invoiced)
        if credit > 0:
            _, released = billing.adjust(invoice, credit, 'return', note=reason, user_id=uid,
                                         roll=False)
            billing.settle_released(released, uid, note=f'REFUND {txn.transaction_ref}')

    txn.notes = ((txn.notes or '') +
                 f'\nREFUND {refunded_value} ({reason})').strip()
    if fully_refunded:
        txn.status = TXN_REFUNDED

    audit_service.log(uid, 'transaction.refund', 'Transaction', txn.id,
                      None, {'reason': reason, 'value': float(refunded_value),
                             'full': fully_refunded})
    db.session.commit()
    return jsonify({'message': 'Refund processed',
                    'refunded_value': float(refunded_value),
                    'fully_refunded': fully_refunded,
                    'transaction': txn.to_dict()}), 200


@pos_bp.route('/transaction/<int:txn_id>/receipt', methods=['GET'])
@cashier_api_required
def transaction_receipt(txn_id):
    txn, err = _txn_or_404(txn_id)
    if err: return err
    return jsonify(build_receipt(txn.id)), 200
