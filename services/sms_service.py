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

class SendSMS:
    @staticmethod
    # Added max_retries parameter, though full retry logic for production would be async
    def send_order_confirmation(phone_number, customer_name, order_details): # Removed max_retries parameter for simplicity
        """
        Sends an order confirmation SMS to the customer.
        Returns True on success, False on failure.
        """
        message = f"Hello {customer_name}, your order has been placed. Details: {order_details}"
        sender = sender_id # Make sure this is correctly configured

        try:
            response = sms.send(message, [phone_number], sender)
            logger.info(f"SMS sent successfully to {phone_number} for customer {customer_name}. Response: {response}")
            return True # Indicate success
        except Exception as e:
            logger.error(f"Error sending SMS to {phone_number} for customer {customer_name}: {e}", exc_info=True)
            # In a real application, this failure will be logged persistently
            # (e.g., to a dedicated SMS log table in the DB with retry attempts).
            # I would also implement a proper retry mechanism here, ideally
            # using a background task queue like Celery.
            return False # Indicate failure