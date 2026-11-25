"""Text messages through Africa's Talking, recorded in the sms_messages outbox.

``send(to, body, kind, dedupe_key=None)`` is the one way to text someone:

* It runs in the bound business and uses its ``at_sender_id`` (else the
  platform's ``AT_SENDER_ID``). All businesses share one Africa's Talking
  account.
* Every call leaves a row: ``sent`` (Africa's Talking accepted it),
  ``failed`` (refused, not configured, or down) or ``skipped`` (not a
  Kenyan mobile number). Delivery reports later fill ``delivery_status``.
* A ``dedupe_key`` already used in this business returns the earlier row
  and sends nothing, so a rerun of a daily job texts nobody twice.
* It never raises for a provider problem; checkouts do not fail on SMS.
* It commits. Call it after your own commit: it refuses to run with
  unsaved changes in the session rather than commit them for you.

The row is committed as ``queued`` before the provider call, so a crash
during the call leaves evidence and the dedupe key still blocks a resend.
"""
import hmac
import logging
from datetime import datetime

import africastalking  # type: ignore
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.tenancy import current_tenant_id, tenant_scope, unscoped
from config import Config
from models import SmsMessage, Tenant, db
from services.phone import InvalidPhone, normalize_ke_phone

logger = logging.getLogger(__name__)

# The SDK is initialised on first use, not at import: importing this module
# must not need credentials or a network, so the app and tests can load
# without Africa's Talking configured.
_sms = None
_initialised = False

# Africa's Talking per-recipient statusCode values meaning "accepted".
_ACCEPTED_CODES = {100, 101, 102}           # Processed, Sent, Queued

# Delivery report states, in the order they can happen. A report never moves
# a message backwards, and a final state is never replaced.
_DELIVERY_RANK = {"Sent": 1, "Submitted": 1, "Buffered": 2}
_FINAL_DELIVERY = {"Success", "Failed", "Rejected", "AbsentSubscriber", "Expired"}


class UnsavedChanges(RuntimeError):
    """send() was called with uncommitted work in the session."""


def _client():
    """Return the Africa's Talking SMS client, or None if it is unavailable.

    Initialises once. Missing credentials or an SDK error give None (logged),
    so callers degrade to "SMS not sent" instead of failing a checkout.
    """
    global _sms, _initialised
    if _initialised:
        return _sms
    _initialised = True
    username, api_key = Config.AT_USERNAME, Config.AT_API_KEY
    if not username or not api_key:
        logger.warning("SMS disabled: AT_USERNAME / AT_API_KEY not configured")
        return None
    try:
        africastalking.initialize(username, api_key)
        _sms = africastalking.SMS
    except Exception as e:
        logger.error("Africa's Talking initialisation failed: %s", e)
        _sms = None
    return _sms


# Whether the current transaction has flushed writes. Pending objects alone
# are not enough: after a flush (stock.take() flushes) they are gone from
# session.new/dirty but would still be committed by send().
_WROTE = "sms_service.flushed_writes"


@event.listens_for(Session, "after_flush")
def _note_flush(session, flush_context):
    session.info[_WROTE] = True


@event.listens_for(Session, "after_transaction_end")
def _forget_flush(session, transaction):
    # Only the outermost transaction ending settles the writes; a savepoint
    # committing or rolling back leaves the outer transaction's writes pending.
    if transaction.parent is None:
        session.info.pop(_WROTE, None)


def _require_clean_session():
    s = db.session
    if (s.info.get(_WROTE) or s.new or s.deleted
            or any(s.is_modified(o) for o in s.dirty)):
        raise UnsavedChanges("sms_service.send() commits; commit your own changes first")


def _existing(dedupe_key):
    return SmsMessage.query.filter_by(dedupe_key=dedupe_key).first() if dedupe_key else None


def _parse_reply(reply):
    """(accepted, provider_message_id, error) from Africa's Talking's reply."""
    try:
        recipient = reply["SMSMessageData"]["Recipients"][0]
    except (TypeError, KeyError, IndexError):
        message = (reply or {}).get("SMSMessageData", {}).get("Message") if isinstance(reply, dict) else None
        return False, None, (message or "no recipient in reply")[:255]
    code = recipient.get("statusCode")
    status = recipient.get("status") or ""
    accepted = code in _ACCEPTED_CODES if code is not None else status == "Success"
    if not accepted:
        # Refusals carry messageId "None" (a string); keeping it would
        # collide on the unique index with the next refusal.
        return False, None, (status or str(code))[:255]
    return True, recipient.get("messageId") or None, None


def send(to, body, kind, dedupe_key=None, stored_body=None):
    """Text ``body`` to ``to`` in the bound business. Returns the SmsMessage.

    ``stored_body`` replaces the text kept in the outbox, for messages
    carrying a secret (one-time codes).
    """
    _require_clean_session()
    earlier = _existing(dedupe_key)
    if earlier is not None:
        return earlier

    try:
        phone = normalize_ke_phone(to)
    except InvalidPhone:
        phone = None
    msg = SmsMessage(to_phone=(phone or str(to or ""))[:32] or "-",
                     body=body if stored_body is None else stored_body, kind=kind,
                     dedupe_key=dedupe_key, status="queued" if phone else "skipped",
                     error=None if phone else "not a Kenyan mobile number")
    db.session.add(msg)
    try:
        db.session.commit()
    except IntegrityError:                  # the same key, sent at the same moment
        db.session.rollback()
        return _existing(dedupe_key)
    if not phone:
        logger.info("SMS %s skipped: bad number %r", kind, to)
        return msg

    tenant = db.session.get(Tenant, current_tenant_id())
    sender_id = (tenant.at_sender_id if tenant else None) or Config.AT_SENDER_ID
    client = _client()
    if client is None:
        msg.status, msg.error = "failed", "SMS not configured"
    else:
        try:
            reply = (client.send(body, ["+" + phone], sender_id) if sender_id
                     else client.send(body, ["+" + phone]))
            accepted, message_id, error = _parse_reply(reply)
            msg.status = "sent" if accepted else "failed"
            msg.provider_message_id, msg.error = message_id, error
            if accepted:
                msg.sent_at = datetime.utcnow()
        except Exception as e:
            # Never fail a checkout because Africa's Talking is down.
            logger.error("SMS %s to %s failed: %s", kind, phone, e, exc_info=True)
            msg.status, msg.error = "failed", str(e)[:255]
    try:
        db.session.commit()
    except IntegrityError:                  # provider reused a message id
        db.session.rollback()
        logger.error("SMS %s: duplicate provider message id", kind)
    return msg


def _send_many(recipients, body, kind, dedupe_prefix=None, stored_body=None):
    """One row per recipient. True when at least one was accepted."""
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [r for r in dict.fromkeys(recipients or []) if r]
    if not recipients:
        logger.warning("SMS %s skipped: no recipients", kind)
        return False
    results = [send(r, body, kind,
                    dedupe_key=f"{dedupe_prefix}:{r}" if dedupe_prefix else None,
                    stored_body=stored_body)
               for r in recipients]
    return any(m is not None and m.status == "sent" for m in results)


# --- delivery reports ----------------------------------------------------------

def _message_tenant(message_id):
    """Which business sent this message. Reports arrive on one global URL."""
    with unscoped():
        row = (db.session.query(SmsMessage.tenant_id)
               .filter(SmsMessage.provider_message_id == message_id).first())
    return row[0] if row else None


def record_delivery(message_id, status, failure_reason=None):
    """Apply an Africa's Talking delivery report. Returns True if a row changed.

    Unknown ids are ignored. Status only moves forward: a final state
    (Success, Failed, Rejected, ...) is never replaced, and a late
    Sent/Submitted cannot overwrite Buffered.
    """
    if not message_id or not status:
        return False
    tenant_id = _message_tenant(message_id)
    if tenant_id is None:
        logger.info("delivery report for unknown message %s", message_id)
        return False
    with tenant_scope(tenant_id):
        # Locked, so two reports for one message apply one after the other
        # and the second sees the first's status.
        msg = db.session.execute(
            select(SmsMessage).where(SmsMessage.provider_message_id == message_id)
            .with_for_update().execution_options(populate_existing=True)).scalar_one()
        current = msg.delivery_status
        if current in _FINAL_DELIVERY:
            return False
        rank = 3 if status in _FINAL_DELIVERY else _DELIVERY_RANK.get(status)
        if rank is None or rank <= _DELIVERY_RANK.get(current, 0):
            return False
        msg.delivery_status = status
        if status in _FINAL_DELIVERY:
            msg.delivered_at = datetime.utcnow()
            if status != "Success" and failure_reason:
                msg.error = str(failure_reason)[:255]
        db.session.commit()
        return True


def delivery_token_ok(given):
    """Optional shared secret on the delivery-report URL (AT_DELIVERY_TOKEN)."""
    expected = Config.AT_DELIVERY_TOKEN
    if not expected:
        return True
    return hmac.compare_digest(str(given or ""), expected)


# --- the app's messages -----------------------------------------------------------

def _business_name():
    tenant = db.session.get(Tenant, current_tenant_id())
    return tenant.name if tenant else (Config.BUSINESS_NAME or "POS")


class SendSMS:
    """The messages the app sends. Each returns True when Africa's Talking
    accepted it (for several recipients: at least one)."""

    @staticmethod
    def send_order_confirmation(phone_number, customer_name, order_details, order_id=None):
        message = f"Hello {customer_name}, your order has been placed. Details: {order_details}"
        return _send_many(phone_number, message, "order_confirmation",
                          dedupe_prefix=f"order:{order_id}" if order_id else None)

    @staticmethod
    def send_payment_confirmation(phone_number, total, transaction_ref):
        """Sent to the customer once a POS transaction is completed."""
        message = (
            f"Payment of KES {float(total):.2f} received. "
            f"Ref: {transaction_ref}. Thank you for shopping at {_business_name()}."
        )
        return _send_many(phone_number, message, "payment_receipt",
                          dedupe_prefix=f"receipt:{transaction_ref}")

    @staticmethod
    def send_low_stock_alert(admin_phones, item_name, stock, reorder_level, item_id=None,
                             today=None):
        """To admins when an item drops to its reorder level: once per item per day."""
        message = (
            f"LOW STOCK: {item_name} has {stock} units remaining "
            f"(reorder level: {reorder_level})."
        )
        prefix = f"low_stock:{item_id}:{today}" if item_id and today else None
        return _send_many(admin_phones, message, "low_stock", dedupe_prefix=prefix)

    @staticmethod
    def send_prescription_ready(phone_number, prescription_id):
        """Sent to the customer when a prescription is fully dispensed."""
        message = (
            f"Your prescription (Rx #{prescription_id}) has been dispensed. "
            f"Collect at our pharmacy. -{_business_name()}"
        )
        return _send_many(phone_number, message, "rx_ready",
                          dedupe_prefix=f"rx_ready:{prescription_id}")

    @staticmethod
    def send_credit_reminder(phone_number, credit_balance):
        """Reminder to a credit customer about their outstanding balance."""
        message = (
            f"Reminder: Your account balance is KES {float(credit_balance):.2f}. "
            f"Please make a payment at your earliest convenience. -{_business_name()}"
        )
        return _send_many(phone_number, message, "credit_reminder")

    @staticmethod
    def send_manager_otp(phone_number, otp, action_label="manager override"):
        """Sent to a manager when a cashier requests an override OTP."""
        def compose(code):
            return (f"{_business_name()} {action_label} code: {code}. "
                    f"Expires in {int(Config.MANAGER_OTP_TTL/60) or 5} minutes. "
                    f"Do not share unless you authorised this action.")
        return _send_many(phone_number, compose(otp), "manager_otp",
                          stored_body=compose("*" * len(str(otp))))
