from config import Config
import africastalking # type: ignore
import logging
# import time # Uncomment if implementing blocking in-request retry (not recommended)

logger = logging.getLogger(__name__)

# Load environment variables
username = Config.AT_USERNAME
api_key = Config.AT_API_KEY
sender_id = Config.AT_SENDER_ID

# Initialize Africa's Talking SDK
africastalking.initialize(username, api_key)
sms = africastalking.SMS


def _send(message: str, recipients):
    """Internal helper. Always returns True/False; never raises.

    ``recipients`` may be a single phone string or a list. Empty / falsy
    entries are filtered. Configured via ``AT_SENDER_ID`` if present.
    """
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [r for r in (recipients or []) if r]
    if not recipients:
        logger.warning("SMS skipped — no recipients (msg=%r)", message[:60])
        return False
    try:
        if sender_id:
            response = sms.send(message, recipients, sender_id)
        else:
            response = sms.send(message, recipients)
        logger.info("SMS sent to %s: %s", recipients, response)
        return True
    except Exception as e:
        # Graceful degradation — never fail a checkout because AT is down.
        logger.error("SMS send failed for %s: %s", recipients, e, exc_info=True)
        return False


class SendSMS:
    @staticmethod
    # Added max_retries parameter, though full retry logic for production would be async
    def send_order_confirmation(phone_number, customer_name, order_details): # Removed max_retries parameter for simplicity
        """
        Sends an order confirmation SMS to the customer.
        Returns True on success, False on failure.
        """
        message = f"Hello {customer_name}, your order has been placed. Details: {order_details}"
        return _send(message, phone_number)

    # ----------------------------------------------------------------------
    # POS notification triggers
    # ----------------------------------------------------------------------

    @staticmethod
    def send_payment_confirmation(phone_number, total, transaction_ref):
        """Sent to the customer once a POS transaction is completed."""
        biz = Config.BUSINESS_NAME or "POS"
        message = (
            f"Payment of KES {float(total):.2f} received. "
            f"Ref: {transaction_ref}. Thank you for shopping at {biz}."
        )
        return _send(message, phone_number)

    @staticmethod
    def send_low_stock_alert(admin_phones, item_name, stock, reorder_level):
        """Sent to admins/superusers when an item drops to or below its reorder level."""
        message = (
            f"LOW STOCK: {item_name} has {stock} units remaining "
            f"(reorder level: {reorder_level})."
        )
        return _send(message, admin_phones)

    @staticmethod
    def send_prescription_ready(phone_number, prescription_id):
        """Sent to the customer when a prescription is fully dispensed."""
        biz = Config.BUSINESS_NAME or "POS"
        message = (
            f"Your prescription (Rx #{prescription_id}) has been dispensed. "
            f"Collect at our pharmacy. -{biz}"
        )
        return _send(message, phone_number)

    @staticmethod
    def send_credit_reminder(phone_number, credit_balance):
        """Reminder to a credit customer about their outstanding balance."""
        biz = Config.BUSINESS_NAME or "POS"
        message = (
            f"Reminder: Your account balance is KES {float(credit_balance):.2f}. "
            f"Please make a payment at your earliest convenience. -{biz}"
        )
        return _send(message, phone_number)

    @staticmethod
    def send_manager_otp(phone_number, otp, action_label="manager override"):
        """Sent to a manager when a cashier requests an override OTP."""
        message = (
            f"{Config.BUSINESS_NAME or 'POS'} {action_label} code: {otp}. "
            f"Expires in {int(Config.MANAGER_OTP_TTL/60) or 5} minutes. "
            f"Do not share unless you authorised this action."
        )
        return _send(message, phone_number)
