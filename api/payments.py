"""Payment integrations (M-Pesa Daraja, Flutterwave).

URL prefix: ``/payments``.

Endpoints
---------
M-Pesa STK Push
~~~~~~~~~~~~~~~
* ``POST /payments/mpesa/stk_push``                    — initiate STK Push
* ``POST /payments/mpesa/callback``                    — Daraja webhook (PUBLIC)
* ``GET  /payments/mpesa/status/<checkout_request_id>``— poll status

Flutterwave
~~~~~~~~~~~
* ``POST /payments/card/initiate``                     — create hosted payment link
* ``POST /payments/card/webhook``                      — Flutterwave webhook (PUBLIC)
* ``GET  /payments/card/verify/<flw_ref>``             — verify by Flutterwave id
"""
from __future__ import annotations

import logging
from decimal import Decimal

from flask import Blueprint, current_app, jsonify, request

from auth.auth_middleware import cashier_api_required
from auth.tenancy import tenant_scope, unscoped
from models import (
    db, Customer, Transaction,
    TXN_OPEN, TXN_COMPLETED,
    PAY_MPESA, PAY_CARD,
)
from services import (
    mpesa_service, flutterwave_service, audit_service, loyalty_service,
)
from services.sms_service import SendSMS

payments_bp = Blueprint('payments', __name__)
logger = logging.getLogger(__name__)


def _owning_tenant(**match):
    """Tenant id of the one transaction matching ``match``, else None.

    Payment-provider webhooks arrive with no session, so no tenant is bound.
    The provider's reference identifies the transaction; this finds which
    business owns it, and the handler then runs inside that tenant only.
    None when there is no match, or more than one (references are unique
    per tenant, not globally), in which case nothing is changed.
    """
    with unscoped():
        owners = {tid for (tid,) in db.session.query(Transaction.tenant_id).filter_by(**match)}
    if len(owners) != 1:
        if len(owners) > 1:
            logger.error("Webhook reference %s matches transactions in %d tenants; ignored",
                         match, len(owners))
        return None
    return owners.pop()


def _card_payments_disabled():
    """Card payments use the platform's Flutterwave account, so money would
    not settle to the business directly. Off unless CARD_PAYMENTS_ENABLED."""
    if current_app.config.get('CARD_PAYMENTS_ENABLED'):
        return None
    return jsonify({'error': 'Card payments are not available'}), 404


def _txn_or_404(txn_id):
    txn = db.session.get(Transaction, txn_id)
    if txn is None:
        return None, (jsonify({'error': 'Transaction not found'}), 404)
    return txn, None


def _mark_paid(txn: Transaction, *, method: str, ref_field: str, ref_value: str,
               amount=None) -> None:
    """Mark a transaction as completed after async confirmation. Caller commits."""
    if txn.status == TXN_COMPLETED:
        # Idempotent — webhook may fire twice.
        return
    setattr(txn, ref_field, ref_value)
    txn.payment_method = method
    txn.status = TXN_COMPLETED
    if amount is not None:
        # Trust our own total over the gateway-reported amount; only log mismatch.
        try:
            if Decimal(str(amount)) != Decimal(str(txn.total)):
                logger.warning("Payment amount %s != txn total %s for %s",
                               amount, txn.total, txn.transaction_ref)
        except Exception:
            pass

    # Loyalty + SMS, best-effort
    if txn.customer_id:
        try:
            loyalty_service.award_points(txn.customer_id, txn.id, txn.total)
        except Exception:
            logger.warning('loyalty award failed', exc_info=True)
        cust = db.session.get(Customer, txn.customer_id)
        if cust and cust.phone_number:
            try:
                SendSMS.send_payment_confirmation(cust.phone_number,
                                                  txn.total, txn.transaction_ref)
            except Exception:
                logger.warning('payment SMS failed', exc_info=True)


# ===========================================================================
# M-Pesa
# ===========================================================================

@payments_bp.route('/mpesa/stk_push', methods=['POST'])
@cashier_api_required
def mpesa_stk_push():
    """Initiate an STK Push for ``transaction_id``.

    Body: ``{"phone_number": "...", "amount": ..., "transaction_id": ...}``.
    The amount is informational — we always push for the txn's stored total.
    """
    body = request.get_json(silent=True) or {}
    txn_id = body.get('transaction_id')
    phone  = (body.get('phone_number') or '').strip()
    if not txn_id or not phone:
        return jsonify({'error': 'transaction_id and phone_number are required'}), 400

    txn, err = _txn_or_404(txn_id)
    if err: return err
    if txn.status != TXN_OPEN:
        return jsonify({'error': f'Transaction is {txn.status}, cannot push'}), 400
    amount = Decimal(str(body.get('amount') or txn.total or 0))
    if amount <= 0:
        return jsonify({'error': 'amount must be > 0'}), 400

    try:
        result = mpesa_service.stk_push(
            phone=phone,
            amount=amount,
            account_ref=txn.transaction_ref[-12:],
            description=f'TXN {txn.id}',
        )
    except mpesa_service.MpesaError as e:
        logger.error('STK push failed: %s', e)
        return jsonify({'error': str(e)}), 502
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    txn.checkout_request_id = result.get('CheckoutRequestID')
    audit_service.log(None, 'payment.mpesa.stk_push', 'Transaction', txn.id,
                      None, {'checkout_request_id': txn.checkout_request_id,
                             'phone': phone})
    db.session.commit()
    return jsonify({
        'checkout_request_id': result.get('CheckoutRequestID'),
        'merchant_request_id': result.get('MerchantRequestID'),
        'status': 'pending',
    }), 200


@payments_bp.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """Daraja STK Push callback. PUBLIC endpoint — must accept any IP.

    Idempotent: Daraja may retry. We commit only on the first success.
    Always returns Daraja's expected ack body.
    """
    payload = request.get_json(silent=True) or {}
    parsed = mpesa_service.parse_stk_callback(payload)
    cri = parsed.get('checkout_request_id')
    logger.info('M-Pesa callback: %s', parsed)

    if not cri:
        return jsonify({'ResultCode': 1, 'ResultDesc': 'missing CheckoutRequestID'}), 200

    tenant_id = _owning_tenant(checkout_request_id=cri)
    if tenant_id is None:
        logger.warning('M-Pesa callback for unknown CheckoutRequestID %s', cri)
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted (no matching txn)'}), 200

    with tenant_scope(tenant_id):
        _apply_stk_result(cri, parsed)
    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200


def _apply_stk_result(cri, parsed):
    """Record an STK result. Runs inside the owning tenant's scope."""
    txn = Transaction.query.filter_by(checkout_request_id=cri).first()
    if parsed['success']:
        _mark_paid(txn, method=PAY_MPESA, ref_field='mpesa_ref',
                   ref_value=parsed.get('mpesa_receipt') or '',
                   amount=parsed.get('amount'))
        audit_service.log(None, 'payment.mpesa.confirmed', 'Transaction', txn.id,
                          None, parsed)
    else:
        # Append failure to notes; don't change status (cashier can retry).
        txn.notes = ((txn.notes or '') +
                     f"\nM-Pesa failed: {parsed.get('result_desc')}").strip()
        audit_service.log(None, 'payment.mpesa.failed', 'Transaction', txn.id,
                          None, parsed)

    db.session.commit()


@payments_bp.route('/mpesa/status/<checkout_request_id>', methods=['GET'])
@cashier_api_required
def mpesa_status(checkout_request_id):
    """Query Daraja for the current state of one of this business's STK pushes.

    The id must belong to a transaction of the current tenant: otherwise any
    business could read another's payment status through the provider.
    """
    txn = Transaction.query.filter_by(checkout_request_id=checkout_request_id).first()
    if txn is None:
        return jsonify({'error': 'Transaction not found'}), 404
    try:
        result = mpesa_service.query_stk_status(checkout_request_id)
    except mpesa_service.MpesaError as e:
        return jsonify({'error': str(e)}), 502
    return jsonify({
        'daraja': result,
        'transaction': txn.to_dict() if txn else None,
    }), 200


# ===========================================================================
# Flutterwave (cards)
# ===========================================================================

@payments_bp.route('/card/initiate', methods=['POST'])
@cashier_api_required
def card_initiate():
    """Create a Flutterwave hosted-checkout link for ``transaction_id``."""
    disabled = _card_payments_disabled()
    if disabled:
        return disabled
    body = request.get_json(silent=True) or {}
    txn_id = body.get('transaction_id')
    if not txn_id:
        return jsonify({'error': 'transaction_id is required'}), 400
    txn, err = _txn_or_404(txn_id)
    if err: return err
    if txn.status != TXN_OPEN:
        return jsonify({'error': f'Transaction is {txn.status}'}), 400

    customer = db.session.get(Customer, txn.customer_id) if txn.customer_id else None
    email = (body.get('customer_email')
             or (customer.email if customer else None)
             or 'walkin@example.com')
    name = body.get('customer_name') or (customer.name if customer else 'Walk-in')
    phone = body.get('customer_phone') or (customer.phone_number if customer else None)
    amount = Decimal(str(body.get('amount') or txn.total or 0))
    currency = (body.get('currency') or 'KES').upper()

    try:
        result = flutterwave_service.create_payment_link(
            tx_ref=txn.transaction_ref,
            amount=amount,
            currency=currency,
            customer_email=email,
            customer_name=name,
            customer_phone=phone,
            redirect_url=body.get('redirect_url'),
            description=f'TXN {txn.transaction_ref}',
        )
    except flutterwave_service.FlutterwaveError as e:
        logger.error('Flutterwave initiate failed: %s', e)
        return jsonify({'error': str(e)}), 502

    audit_service.log(None, 'payment.card.initiate', 'Transaction', txn.id,
                      None, {'tx_ref': txn.transaction_ref})
    db.session.commit()
    return jsonify({'payment_link': result['payment_link']}), 200


@payments_bp.route('/card/webhook', methods=['POST'])
def card_webhook():
    """Flutterwave webhook. Verifies the ``verif-hash`` header, then
    re-verifies the transaction server-side via Flutterwave's verify API.
    """
    disabled = _card_payments_disabled()
    if disabled:
        return disabled
    sig = request.headers.get('verif-hash')
    raw = request.get_data() or b''
    if not flutterwave_service.validate_webhook_signature(raw, sig):
        logger.warning('Flutterwave webhook signature mismatch')
        return jsonify({'status': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    data = payload.get('data') or payload  # Flutterwave nests under "data"
    tx_ref = data.get('tx_ref') or data.get('txRef')
    flw_id = data.get('id')
    if not tx_ref:
        return jsonify({'status': 'ignored', 'reason': 'missing tx_ref'}), 200

    tenant_id = _owning_tenant(transaction_ref=tx_ref)
    if tenant_id is None:
        logger.warning('Flutterwave webhook for unknown or ambiguous tx_ref %s', tx_ref)
        return jsonify({'status': 'unknown_txn'}), 200
    with tenant_scope(tenant_id):
        return _apply_card_result(tx_ref, data, flw_id)


def _apply_card_result(tx_ref, data, flw_id):
    """Verify and record a card payment. Runs inside the owning tenant's scope."""
    txn = Transaction.query.filter_by(transaction_ref=tx_ref).first()

    # Re-verify before trusting the webhook.
    if flw_id:
        try:
            verified = flutterwave_service.verify_transaction(flw_id)
        except flutterwave_service.FlutterwaveError as e:
            logger.error('Flutterwave verify failed: %s', e)
            return jsonify({'status': 'verify_failed', 'error': str(e)}), 502
    else:
        verified = {'status': data.get('status'),
                    'amount': data.get('amount'),
                    'flw_ref': data.get('flw_ref')}

    # The provider's record must be for this transaction: a signed event
    # could otherwise pair this ref with someone else's successful payment.
    if flw_id and verified.get('tx_ref') != tx_ref:
        logger.error('Flutterwave verify for %s returned tx_ref %s; ignored',
                     tx_ref, verified.get('tx_ref'))
        return jsonify({'status': 'mismatch'}), 200

    if (verified.get('status') or '').lower() == 'successful':
        _mark_paid(txn, method=PAY_CARD, ref_field='card_ref',
                   ref_value=verified.get('flw_ref') or str(flw_id or ''),
                   amount=verified.get('amount'))
        audit_service.log(None, 'payment.card.confirmed', 'Transaction', txn.id,
                          None, verified)
    else:
        txn.notes = ((txn.notes or '') +
                     f"\nCard payment {verified.get('status')}").strip()
        audit_service.log(None, 'payment.card.failed', 'Transaction', txn.id,
                          None, verified)

    db.session.commit()
    return jsonify({'status': 'ok'}), 200


@payments_bp.route('/card/verify/<flw_ref>', methods=['GET'])
@cashier_api_required
def card_verify(flw_ref):
    disabled = _card_payments_disabled()
    if disabled:
        return disabled
    return _card_verify(flw_ref)


def _card_verify(flw_ref):
    """Verify a Flutterwave transaction by its numeric ID (NB: the param is
    named ``flw_ref`` per the spec but Flutterwave's verify endpoint expects
    the numeric ``id`` — accept both)."""
    # Only references of this business's own transactions may be looked up.
    owned = (Transaction.query
             .filter((Transaction.card_ref == flw_ref) | (Transaction.transaction_ref == flw_ref))
             .first())
    if owned is None:
        return jsonify({'error': 'Transaction not found'}), 404
    try:
        result = flutterwave_service.verify_transaction(flw_ref)
    except flutterwave_service.FlutterwaveError as e:
        return jsonify({'error': str(e)}), 502
    return jsonify(result), 200
