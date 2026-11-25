"""Staff payment endpoints.

URL prefix: ``/payments``. Safaricom's own callbacks are in api/daraja.py.

M-Pesa
* ``POST /payments/mpesa/stk_push``   push a payment prompt for a sale or invoice
* ``GET  /payments/intents/<id>``     poll a push (asks Daraja if it has gone quiet)
* ``POST /payments/manual``           record a typed M-Pesa code
* ``GET  /payments/review``           what needs a person (admin)
* ``POST /payments/<id>/assign``      move a payment to another invoice (owner)
* ``POST /payments/<id>/reverse``     Safaricom reversed it (owner)

Cards (off unless CARD_PAYMENTS_ENABLED)
* ``POST /payments/card/initiate``, ``POST /payments/card/webhook`` (public),
  ``GET /payments/card/verify/<ref>``
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, current_app, g, jsonify, request, session

from auth.auth_middleware import (admin_api_required, cashier_api_required,
                                  finance_api_required, superuser_api_required)
from auth.tenancy import tenant_scope, unscoped
from models import (
    db, Customer, Invoice, Payment, PaymentIntent, Tenant, Transaction,
    TXN_OPEN,
)
from services import audit_service, billing, flutterwave_service
from services.billing import stk
from services.daraja import DarajaBusy, DarajaError, DarajaNotConfigured, DarajaUnavailable
from services.phone import InvalidPhone

payments_bp = Blueprint('payments', __name__)
logger = logging.getLogger(__name__)


def _user_id():
    return session.get('user_id')


def _invoice_for_request(body):
    """The invoice a staff request is about: ``invoice_id``, or the open
    sale ``transaction_id`` (its invoice is created, or brought in line
    with the cart if nothing has been paid yet). Returns (invoice, error)."""
    if body.get('invoice_id'):
        invoice = db.session.get(Invoice, body.get('invoice_id'))
        return (invoice, None) if invoice else (None, (jsonify({'error': 'Invoice not found'}), 404))
    txn = db.session.get(Transaction, body.get('transaction_id')) if body.get('transaction_id') else None
    if txn is None:
        return None, (jsonify({'error': 'transaction_id or invoice_id is required'}), 400)
    # Same lock as checkout: concurrent requests cannot make two invoices.
    from sqlalchemy import select
    from services.billing.invoices import lock_tenant
    lock_tenant()
    txn = db.session.execute(select(Transaction).where(Transaction.id == txn.id)
                             .with_for_update()
                             .execution_options(populate_existing=True)).scalar_one()
    if txn.status != TXN_OPEN:
        return None, (jsonify({'error': f'Transaction is {txn.status}'}), 400)
    if billing.money(txn.total) <= 0:
        return None, (jsonify({'error': 'Cart is empty'}), 400)
    invoice = billing.open_invoice_for_transaction(txn.id)
    if invoice is None:
        invoice = billing.create_invoice(total=txn.total, transaction_id=txn.id,
                                         customer_id=txn.customer_id)
    elif billing.money(invoice.total) != billing.money(txn.total):
        if billing.money(invoice.amount_paid) > 0:
            return None, (jsonify({'error': 'The cart changed after a payment was received'}), 409)
        invoice.total = billing.money(txn.total)
        billing.recompute(invoice)
    return invoice, None


# ===========================================================================
# M-Pesa
# ===========================================================================

@payments_bp.route('/mpesa/stk_push', methods=['POST'])
@cashier_api_required
def mpesa_stk_push():
    """Push an M-Pesa prompt for the balance of a sale or invoice.

    Body: ``{"transaction_id": 41 | "invoice_id": 7, "phone_number": "0712..."}``.
    """
    body = request.get_json(silent=True) or {}
    phone = (body.get('phone_number') or '').strip()
    if not phone:
        return jsonify({'error': 'phone_number is required'}), 400
    invoice, err = _invoice_for_request(body)
    if err:
        db.session.rollback()
        return err
    try:
        intent = stk.start_stk(invoice, phone, user_id=_user_id())
    except InvalidPhone:
        db.session.rollback()
        return jsonify({'error': 'Enter a Kenyan mobile number'}), 400
    except stk.PromptPending as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 409
    except DarajaBusy:
        return jsonify({'error': 'The customer already has an M-Pesa prompt open. '
                                 'Wait a minute and try again.'}), 409
    except DarajaUnavailable:
        return jsonify({'error': 'No reply from M-Pesa. If the customer gets the prompt '
                                 'and pays, it will still be recorded; wait before '
                                 'sending another.'}), 502
    except DarajaNotConfigured as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    except DarajaError as e:
        logger.error('STK push failed: %s', e)
        return jsonify({'error': 'M-Pesa could not send the prompt. Try again.'}), 502
    except billing.BillingError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    audit_service.log(_user_id(), 'payment.stk_push', 'Invoice', invoice.id, None,
                      {'intent': intent.id, 'amount': str(intent.amount)})
    db.session.commit()
    return jsonify({'intent': intent.to_dict(), 'invoice': invoice.to_dict()}), 201


@payments_bp.route('/intents/<int:intent_id>', methods=['GET'])
@cashier_api_required
def intent_status(intent_id):
    """Where a push stands. If it has been quiet for 30 s this asks Daraja."""
    intent = db.session.get(PaymentIntent, intent_id)
    if intent is None:
        return jsonify({'error': 'Not found'}), 404
    intent = stk.refresh_intent(intent, min_age=stk.POLL_QUERY_AFTER)
    invoice = db.session.get(Invoice, intent.invoice_id)
    return jsonify({'intent': intent.to_dict(), 'invoice': invoice.to_dict()}), 200


@payments_bp.route('/manual', methods=['POST'])
@cashier_api_required
def manual_payment():
    """Record a typed M-Pesa code against a sale or invoice.

    Body: ``{"transaction_id"|"invoice_id", "receipt": "SJK82910PL", "amount": 500}``.
    Recorded unverified until Daraja reports the same receipt.
    """
    tenant = db.session.get(Tenant, g.current_tenant.id)
    if not tenant.manual_mpesa_enabled:
        return jsonify({'error': 'Typed M-Pesa codes are switched off for this business'}), 403
    body = request.get_json(silent=True) or {}
    receipt = (body.get('receipt') or '').strip()
    if len(receipt) < 8:
        return jsonify({'error': 'Enter the M-Pesa code from the SMS'}), 400
    try:
        amount = billing.money(body.get('amount'))
    except Exception:
        return jsonify({'error': 'amount must be numeric'}), 400
    if amount <= 0:
        return jsonify({'error': 'amount must be > 0'}), 400
    invoice, err = _invoice_for_request(body)
    if err:
        db.session.rollback()
        return err
    try:
        payment = billing.record_payment(method='mpesa_manual', amount=amount, receipt=receipt,
                                         invoice=invoice, recorded_by=_user_id(), verified=False)
    except billing.DuplicateReceipt as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 409
    audit_service.log(_user_id(), 'payment.manual', 'Payment', payment.id, None,
                      {'receipt': payment.mpesa_receipt, 'amount': str(amount),
                       'invoice': invoice.id})
    db.session.commit()
    return jsonify({'payment': payment.to_dict(),
                    'invoice': db.session.get(Invoice, invoice.id).to_dict()}), 201


@payments_bp.route('/bank', methods=['POST'])
@cashier_api_required
def bank_payment():
    """Record a bank transfer or deposit against a sale or invoice.

    Body: ``{"transaction_id"|"invoice_id", "bank_name": "Equity",
    "reference": "FT24330X9", "amount": 1500}``. The cashier copies the
    reference from the customer's bank SMS or slip; it is unverified until
    an admin or accountant checks the bank statement (Payments to verify).
    """
    body = request.get_json(silent=True) or {}
    bank_name = (body.get('bank_name') or '').strip()[:64]
    if not bank_name:
        return jsonify({'error': 'Which bank? (e.g. Equity, KCB)'}), 400
    try:
        amount = billing.money(body.get('amount'))
    except Exception:
        return jsonify({'error': 'amount must be numeric'}), 400
    if amount <= 0:
        return jsonify({'error': 'amount must be > 0'}), 400
    reference = (body.get('reference') or '').strip()
    if len(''.join(reference.split())) < 4:
        return jsonify({'error': 'Enter the bank reference from the slip or SMS'}), 400
    invoice, err = _invoice_for_request(body)
    if err:
        db.session.rollback()
        return err
    try:
        payment = billing.record_payment(method='bank', amount=amount, invoice=invoice,
                                         bank_name=bank_name, bank_reference=reference,
                                         recorded_by=_user_id(), verified=False)
    except billing.DuplicateReceipt as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 409
    except billing.BillingError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    audit_service.log(_user_id(), 'payment.bank', 'Payment', payment.id, None,
                      {'bank': bank_name, 'reference': payment.bank_reference,
                       'amount': str(amount), 'invoice': invoice.id})
    db.session.commit()
    return jsonify({'payment': payment.to_dict(),
                    'invoice': db.session.get(Invoice, invoice.id).to_dict()}), 201


@payments_bp.route('/unverified', methods=['GET'])
@finance_api_required
def unverified():
    """Typed M-Pesa codes and bank references nobody has checked yet,
    oldest first, with who typed them."""
    from models import UNVERIFIED_METHODS, User
    rows = (Payment.query.filter(Payment.verified.is_(False),
                                 Payment.status == 'confirmed',
                                 Payment.method.in_(UNVERIFIED_METHODS))
            .order_by(Payment.received_at, Payment.id).all())
    users = {u.id: (u.name or u.email) for u in
             User.query.filter(User.id.in_({p.recorded_by for p in rows if p.recorded_by}))}
    out = []
    for p in rows:
        allocations = billing.live_allocations_of(p)
        invoices = [db.session.get(Invoice, a.invoice_id) for a in allocations]
        out.append({**p.to_dict(), 'recorded_by_name': users.get(p.recorded_by),
                    'invoices': [i.invoice_number for i in invoices if i is not None]})
    total = sum((billing.money(p.amount) for p in rows), Decimal('0'))
    return jsonify({'payments': out, 'count': len(out), 'total': float(total)}), 200


def _unverified_or_404(payment_id):
    from models import UNVERIFIED_METHODS
    payment = db.session.get(Payment, payment_id)
    if payment is None or payment.method not in UNVERIFIED_METHODS:
        return None, (jsonify({'error': 'Not found'}), 404)
    billing.lock_tenant()                       # lock order: billing, then the payment
    payment = billing.lock_payment(payment)
    if payment.verified or payment.status != 'confirmed':
        db.session.rollback()
        return None, (jsonify({'error': 'Already checked'}), 409)
    return payment, None


@payments_bp.route('/<int:payment_id>/verify', methods=['POST'])
@finance_api_required
def verify_payment(payment_id):
    """The money is on the statement: mark the typed payment verified."""
    payment, err = _unverified_or_404(payment_id)
    if err:
        return err
    payment.verified = True
    payment.verified_by = _user_id()
    payment.verified_at = datetime.utcnow()
    audit_service.log(_user_id(), 'payment.verify', 'Payment', payment.id, None,
                      {'method': payment.method,
                       'reference': payment.bank_reference or payment.mpesa_receipt,
                       'amount': str(payment.amount)})
    db.session.commit()
    return jsonify({'payment': payment.to_dict()}), 200


@payments_bp.route('/<int:payment_id>/reject', methods=['POST'])
@finance_api_required
def reject_payment(payment_id):
    """The money never arrived: reverse the payment, so the invoices it
    paid open again and show as owed."""
    body = request.get_json(silent=True) or {}
    reason = (body.get('reason') or '').strip() or 'not on the statement'
    payment, err = _unverified_or_404(payment_id)
    if err:
        return err
    billing.reverse_payment(payment, user_id=_user_id(), reason=reason)
    audit_service.log(_user_id(), 'payment.reject', 'Payment', payment.id, None,
                      {'method': payment.method,
                       'reference': payment.bank_reference or payment.mpesa_receipt,
                       'amount': str(payment.amount), 'reason': reason,
                       'recorded_by': payment.recorded_by})
    db.session.commit()
    return jsonify({'payment': payment.to_dict()}), 200


@payments_bp.route('/review', methods=['GET'])
@admin_api_required
def review():
    return jsonify(billing.review_queue()), 200


def _payment_or_404(payment_id):
    payment = db.session.get(Payment, payment_id)
    if payment is None:
        return None, (jsonify({'error': 'Payment not found'}), 404)
    return payment, None


@payments_bp.route('/<int:payment_id>/assign', methods=['POST'])
@superuser_api_required
def assign(payment_id):
    """Move a payment to the right invoice (wrong account number typed)."""
    payment, err = _payment_or_404(payment_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    invoice = db.session.get(Invoice, body.get('invoice_id')) if body.get('invoice_id') else None
    if invoice is None:
        return jsonify({'error': 'Invoice not found'}), 404
    try:
        billing.reallocate(payment, invoice, user_id=_user_id())
    except billing.BillingError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    db.session.commit()
    return jsonify({'payment': payment.to_dict(),
                    'invoice': db.session.get(Invoice, invoice.id).to_dict()}), 200


@payments_bp.route('/<int:payment_id>/reverse', methods=['POST'])
@superuser_api_required
def reverse(payment_id):
    """Safaricom reversed this payment: its invoices reopen."""
    payment, err = _payment_or_404(payment_id)
    if err:
        return err
    reason = ((request.get_json(silent=True) or {}).get('reason') or '').strip() or None
    billing.reverse_payment(payment, user_id=_user_id(), reason=reason)
    db.session.commit()
    return jsonify({'payment': payment.to_dict()}), 200


# ===========================================================================
# Flutterwave (cards; off unless CARD_PAYMENTS_ENABLED)
# ===========================================================================

def _owning_tenant(**match):
    """Tenant id of the one transaction matching ``match``, else None.

    The Flutterwave webhook arrives with no session. Its reference names a
    transaction; this finds which business owns it, and the handler runs
    inside that tenant only. None when nothing or more than one matches.
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


def _record_card_payment(txn, flw_ref, amount):
    """A verified card payment pays the sale's invoice; the till completes
    the sale (method card) once it is paid. Keyed on the provider ref, so a
    retried webhook is recorded once."""
    invoice = billing.open_invoice_for_transaction(txn.id) or billing.create_invoice(
        total=txn.total, transaction_id=txn.id, customer_id=txn.customer_id)
    billing.record_payment(method='card', amount=amount or invoice.balance,
                           receipt=f'FLW{flw_ref}', invoice=invoice,
                           customer_id=txn.customer_id)
    txn.card_ref = flw_ref


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
        _record_card_payment(txn, verified.get('flw_ref') or str(flw_id or ''),
                             verified.get('amount'))
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
