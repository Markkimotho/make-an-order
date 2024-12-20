# services/sms_services.py
from config import Config
import africastalking # type: ignore

# Load environment variables
username = Config.AT_USERNAME
api_key = Config.AT_API_KEY
sender_id = Config.AT_SENDER_ID

# Initialize Africa's Talking SDK
africastalking.initialize(username, api_key)
sms = africastalking.SMS

class SendSMS:
    @staticmethod
    def send_order_confirmation(phone_number, customer_name, order_details):
        """
        Sends an order confirmation SMS to the customer.
        """
        message = f"Hello {customer_name}, your order has been placed. Details: {order_details}"
        sender = sender_id
        
        try:
            response = sms.send(message, [phone_number], sender)
            print(f"SMS sent successfully: {response}")
            return response
        except Exception as e:
            print(f"Error sending SMS: {e}")
            return None
