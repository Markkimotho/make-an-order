"""Pharmacy / dispensary blueprint.

URL prefix: ``/pharmacy``.

Endpoints
---------
* ``POST /pharmacy/prescriptions``                 — capture a new prescription
* ``GET  /pharmacy/prescriptions``                 — list (filterable)
* ``GET  /pharmacy/prescriptions/<id>``            — detail with dispensing status
* ``POST /pharmacy/prescriptions/<id>/dispense``   — dispense items against an open POS txn
* ``GET  /pharmacy/controlled_drugs``              — controlled drug ledger

All routes require pharmacist role or higher.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, jsonify, request, session

from auth.auth_middleware import pharmacist_api_required
from models import (
    db, Customer, InventoryItem, Prescription, PrescriptionItem,
    Transaction, TransactionItem, TXN_OPEN,
)
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
    )
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
    if rx.transaction_id and rx.transaction_id != txn.id:
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
             .filter(InventoryItem.is_drug.is_(True),
                     InventoryItem.requires_prescription.is_(True))
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
