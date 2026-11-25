"""Pharmacy / dispensary blueprint.

URL prefix: ``/pharmacy``.

Endpoints
---------
* ``POST /pharmacy/prescriptions``                 — capture a new prescription
* ``GET  /pharmacy/prescriptions``                 — list (filterable)
* ``GET  /pharmacy/prescriptions/<id>``            — detail with dispensing status
* ``POST /pharmacy/prescriptions/<id>/dispense``   — dispense items against an open POS txn
* ``GET  /pharmacy/controlled_drugs``              — controlled drug ledger
* ``PUT  /pharmacy/prescriptions/<id>/refill``     — pause / resume / end refills, clear a call-back
* ``GET  /pharmacy/dashboard``                     — expiry radar, refill schedule, call-backs
* ``GET  /pharmacy/register``                      — controlled-drug register (JSON)
* ``GET  /pharmacy/register.pdf``                  — the register for a PPB inspection
* ``POST /pharmacy/orders/<id>/hand-over``         — a paid refill order collected or delivered

All routes require pharmacist role or higher.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, Response, jsonify, request, session

from auth.auth_middleware import pharmacist_api_required
from models import (
    db, Customer, InventoryItem, Order, Prescription, PrescriptionItem, Tenant,
    Transaction, TransactionItem, TXN_OPEN,
)
from auth.tenancy import current_tenant_id
from services.clock import now_nairobi
from verticals.pharmacy import register_pdf
from verticals.pharmacy import service as pharmacy
from services import audit_service
from services.sms_service import SendSMS

pharmacy_bp = Blueprint('pharmacy', __name__)
logger = logging.getLogger(__name__)


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


# ===========================================================================
# Create / list / get
# ===========================================================================

@pharmacy_bp.route('/prescriptions', methods=['POST'])
@pharmacist_api_required
def create_prescription():
    body = request.get_json(silent=True) or {}
    customer_id = body.get('customer_id')
    if not customer_id:
        return jsonify({'error': 'customer_id is required'}), 400
    if db.session.get(Customer, customer_id) is None:
        return jsonify({'error': 'Customer not found'}), 404

    items = body.get('items') or []
    if not items:
        return jsonify({'error': 'items list is required'}), 400

    rx = Prescription(
        customer_id=customer_id,
        pharmacist_id=session.get('user_id'),
        doctor_name=(body.get('doctor_name') or '').strip() or None,
        doctor_phone=(body.get('doctor_phone') or '').strip() or None,
        prescription_date=_parse_date(body.get('prescription_date')) or date.today(),
        notes=(body.get('notes') or '').strip() or None,
        prescriber_reg_no=(body.get('prescriber_reg_no') or '').strip() or None,
    )
    if body.get('is_chronic'):
        try:
            pharmacy.make_chronic(rx, body.get('duration_days') or 30,
                                  body.get('refills_remaining'))
        except (pharmacy.PharmacyError, ValueError, TypeError) as e:
            return jsonify({'error': str(e)}), 400
    db.session.add(rx)
    db.session.flush()

    for entry in items:
        iid = entry.get('inventory_item_id')
        if not iid:
            return jsonify({'error': 'each item needs inventory_item_id'}), 400
        item = db.session.get(InventoryItem, iid)
        if not item:
            return jsonify({'error': f'Inventory item {iid} not found'}), 404
        try:
            qty = Decimal(str(entry.get('quantity_prescribed', 0)))
        except Exception:
            return jsonify({'error': 'quantity_prescribed must be numeric'}), 400
        if qty <= 0:
            return jsonify({'error': 'quantity_prescribed must be > 0'}), 400
        db.session.add(PrescriptionItem(
            prescription_id=rx.id,
            inventory_item_id=iid,
            dosage_instructions=(entry.get('dosage_instructions') or '').strip() or None,
            quantity_prescribed=qty,
            quantity_dispensed=Decimal('0'),
            is_dispensed=False,
        ))

    audit_service.log(session.get('user_id'), 'prescription.create',
                      'Prescription', rx.id, None, rx.to_dict())
    db.session.commit()
    return jsonify({'message': 'Prescription captured', 'prescription': rx.to_dict()}), 201


@pharmacy_bp.route('/prescriptions', methods=['GET'])
@pharmacist_api_required
def list_prescriptions():
    """List prescriptions. Optional filters: ``customer_id``, ``open=true``."""
    q = Prescription.query.order_by(Prescription.created_at.desc())
    cid = request.args.get('customer_id')
    if cid:
        q = q.filter_by(customer_id=int(cid))
    if (request.args.get('open') or '').lower() in ('1', 'true', 'yes'):
        q = q.filter(Prescription.transaction_id.is_(None))
    return jsonify([rx.to_dict() for rx in q.limit(200).all()]), 200


@pharmacy_bp.route('/prescriptions/<int:rx_id>', methods=['GET'])
@pharmacist_api_required
def get_prescription(rx_id):
    rx = db.session.get(Prescription, rx_id)
    if not rx:
        return jsonify({'error': 'Prescription not found'}), 404
    out = rx.to_dict()
    out['fully_dispensed'] = all(it.is_dispensed for it in rx.items)
    return jsonify(out), 200


# ===========================================================================
# Dispense
# ===========================================================================

@pharmacy_bp.route('/prescriptions/<int:rx_id>/dispense', methods=['POST'])
@pharmacist_api_required
def dispense_prescription(rx_id):
    """Dispense items against an open POS transaction.

    Body::

        { "transaction_id": 41,
          "items": [{"prescription_item_id": 3, "quantity_dispensed": 30}] }

    Marks the matching :class:`PrescriptionItem` rows as dispensed and links
    the prescription to the transaction so checkout can pass the
    requires-prescription gate. Sends "prescription ready" SMS once fully
    dispensed.
    """
    rx = db.session.get(Prescription, rx_id)
    if not rx:
        return jsonify({'error': 'Prescription not found'}), 404

    body = request.get_json(silent=True) or {}
    txn_id = body.get('transaction_id')
    if not txn_id:
        return jsonify({'error': 'transaction_id is required'}), 400
    txn = db.session.get(Transaction, txn_id)
    if not txn:
        return jsonify({'error': 'Transaction not found'}), 404
    if txn.status != TXN_OPEN:
        return jsonify({'error': f'Transaction is {txn.status}, cannot dispense'}), 400
    # A finished cycle (dispensed at the counter or handed over online) and a
    # new sale: a refill. Decided on the locked prescription.
    rx = pharmacy.lock_prescription(rx)
    if pharmacy.cycle_complete(rx) and rx.transaction_id != txn.id:
        if not rx.is_chronic:
            db.session.rollback()
            return jsonify({'error': 'Prescription already dispensed'}), 400
        try:
            pharmacy.start_new_cycle(rx)            # a refill of a chronic prescription
        except pharmacy.PharmacyError as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400
    elif rx.transaction_id and rx.transaction_id != txn.id:
        # Part-dispensed against another sale: finish it there.
        db.session.rollback()
        return jsonify({'error': 'Prescription already linked to another transaction'}), 400

    items_payload = body.get('items') or []
    if not items_payload:
        return jsonify({'error': 'items list is required'}), 400

    rx_items_by_id = {it.id: it for it in rx.items}
    for entry in items_payload:
        pid = entry.get('prescription_item_id')
        rx_item = rx_items_by_id.get(pid)
        if not rx_item:
            return jsonify({'error': f'prescription_item {pid} not on this Rx'}), 404
        try:
            qty = Decimal(str(entry.get('quantity_dispensed', 0)))
        except Exception:
            return jsonify({'error': 'quantity_dispensed must be numeric'}), 400
        if qty <= 0:
            return jsonify({'error': 'quantity_dispensed must be > 0'}), 400
        new_total = Decimal(str(rx_item.quantity_dispensed or 0)) + qty
        if new_total > Decimal(str(rx_item.quantity_prescribed)):
            return jsonify({
                'error': f'cannot dispense more than prescribed for item {pid}',
            }), 400
        rx_item.quantity_dispensed = new_total
        rx_item.is_dispensed = (new_total >= Decimal(str(rx_item.quantity_prescribed)))

        # Sanity: the txn must contain this drug.
        in_cart = any(li.inventory_item_id == rx_item.inventory_item_id for li in txn.items)
        if not in_cart:
            return jsonify({
                'error': f'inventory item {rx_item.inventory_item_id} '
                          'is not in the linked transaction cart',
            }), 400

    rx.transaction_id = txn.id
    if pharmacy.cycle_complete(rx):
        pharmacy.mark_dispensed(rx)
    audit_service.log(session.get('user_id'), 'prescription.dispense',
                      'Prescription', rx.id, None,
                      {'transaction_id': txn.id, 'items': items_payload})
    db.session.commit()

    # SMS once fully dispensed
    if all(it.is_dispensed for it in rx.items):
        cust = db.session.get(Customer, rx.customer_id)
        if cust and cust.phone_number:
            try:
                SendSMS.send_prescription_ready(cust.phone_number, rx.id)
            except Exception:
                logger.warning('Rx-ready SMS failed', exc_info=True)

    return jsonify({'message': 'Dispensed', 'prescription': rx.to_dict()}), 200


# ===========================================================================
# Controlled drugs ledger
# ===========================================================================

@pharmacy_bp.route('/controlled_drugs', methods=['GET'])
@pharmacist_api_required
def controlled_drugs():
    """List controlled drugs (is_drug=True AND requires_prescription=True) with
    the full dispensing history per item: who, when, how much, to whom."""
    items = (InventoryItem.query
             .filter(db.or_(InventoryItem.is_controlled.is_(True),
                            db.and_(InventoryItem.is_drug.is_(True),
                                    InventoryItem.requires_prescription.is_(True))))
             .order_by(InventoryItem.name).all())

    out = []
    for item in items:
        history_q = (db.session.query(
                         TransactionItem, Transaction, Customer)
                     .join(Transaction, Transaction.id == TransactionItem.transaction_id)
                     .outerjoin(Customer, Customer.id == Transaction.customer_id)
                     .filter(TransactionItem.inventory_item_id == item.id)
                     .order_by(Transaction.created_at.desc())
                     .limit(200).all())
        history = [{
            'transaction_ref': t.transaction_ref,
            'date': t.created_at.isoformat() if t.created_at else None,
            'customer': c.name if c else None,
            'customer_phone': c.phone_number if c else None,
            'pharmacist_id': t.cashier_id,
            'quantity': float(li.quantity),
            'batch_number': li.batch_number,
        } for (li, t, c) in history_q]
        d = item.to_dict()
        d['dispensing_history'] = history
        out.append(d)
    return jsonify(out), 200


# ===========================================================================
# Refills, dashboard, register
# ===========================================================================

@pharmacy_bp.route('/prescriptions/<int:rx_id>/refill', methods=['PUT'])
@pharmacist_api_required
def update_refill(rx_id):
    """Body: ``{"status": "active|paused|ended"}``, ``{"callback_done": true}``,
    or ``{"is_chronic": true, "duration_days": 30, "refills_remaining": 5}``."""
    rx = db.session.get(Prescription, rx_id)
    if not rx:
        return jsonify({'error': 'Prescription not found'}), 404
    body = request.get_json(silent=True) or {}
    try:
        if body.get('is_chronic'):
            pharmacy.make_chronic(rx, body.get('duration_days') or rx.duration_days or 30,
                                  body.get('refills_remaining', rx.refills_remaining))
        status = body.get('status')
        if status:
            if status not in pharmacy.REFILL_STATUSES:
                raise pharmacy.PharmacyError(f'status must be one of {pharmacy.REFILL_STATUSES}')
            if not rx.is_chronic:
                raise pharmacy.PharmacyError('not a chronic prescription')
            rx.refill_status = status
    except (pharmacy.PharmacyError, ValueError, TypeError) as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    if body.get('callback_done'):
        rx.callback_requested_at = None
    audit_service.log(session.get('user_id'), 'prescription.refill', 'Prescription', rx.id,
                      None, {k: body[k] for k in body})
    db.session.commit()
    return jsonify({'prescription': rx.to_dict()}), 200


@pharmacy_bp.route('/dashboard', methods=['GET'])
@pharmacist_api_required
def dashboard_data():
    return jsonify({'expiry': pharmacy.expiry_radar(),
                    'refills': pharmacy.refill_schedule(),
                    'callbacks': pharmacy.callbacks_requested()}), 200


def _register_query():
    try:
        item_id = int(request.args['item_id']) if request.args.get('item_id') else None
    except ValueError:
        return None, (jsonify({'error': 'item_id must be a number'}), 400)
    date_from = _parse_date(request.args.get('from'))
    date_to = _parse_date(request.args.get('to'))
    return pharmacy.register_rows(item_id, date_from, date_to), None


@pharmacy_bp.route('/register', methods=['GET'])
@pharmacist_api_required
def register_json():
    rows, err = _register_query()
    if err:
        return err
    return jsonify({'entries': [r.to_dict() for r in rows],
                    'breaks': pharmacy.register_breaks(rows)}), 200


@pharmacy_bp.route('/register.pdf', methods=['GET'])
@pharmacist_api_required
def register_pdf_view():
    rows, err = _register_query()
    if err:
        return err
    business = db.session.get(Tenant, current_tenant_id())
    period = ' to '.join(filter(None, [request.args.get('from'), request.args.get('to')])) or None
    pdf = register_pdf.build(business, rows, now_nairobi(), period=period,
                             breaks=set(pharmacy.register_breaks(rows)))
    audit_service.log(session.get('user_id'), 'pharmacy.register_pdf', 'Tenant',
                      current_tenant_id(), None, {'entries': len(rows)})
    db.session.commit()
    return Response(pdf, mimetype='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="controlled-register-{business.slug}.pdf"'})


@pharmacy_bp.route('/orders/<int:order_id>/hand-over', methods=['POST'])
@pharmacist_api_required
def hand_over_refill(order_id):
    """A paid refill order goes to the patient: the refill is counted and the
    next due date set, without ringing the stock up again at the till."""
    order = db.session.get(Order, order_id)
    if order is None:
        return jsonify({'error': 'Order not found'}), 404
    try:
        rx = pharmacy.hand_over_refill(order, session.get('user_id'))
    except pharmacy.PharmacyError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 409
    audit_service.log(session.get('user_id'), 'prescription.refill_handed_over', 'Order',
                      order_id, None, {'prescription_id': rx.id})
    db.session.commit()
    return jsonify({'prescription': rx.to_dict()}), 200
