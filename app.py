from flask import Flask, request, Response # type: ignore
from config import Config
from models import db
from api.customers import customers_bp
from api.orders import orders_bp
from services.database_service import create_database
from services.sms_service import SendSMS

app = Flask(__name__)
app.config.from_object(Config)

# Create the database if it doesn't exist
create_database()

# Initialize the database
db.init_app(app)
with app.app_context():
    db.create_all()

# Register the endpoint blueprints
app.register_blueprint(customers_bp, url_prefix='/customers')
app.register_blueprint(orders_bp, url_prefix='/orders')

# Route to handle incoming messages
@app.route('/incoming-messages', methods=['POST'])
def incoming_messages():
    """
    This route handles incoming messages sent to your shortcode.
    Africa's Talking will send POST requests to this URL.
    """
    data = request.get_json(force=True)  # Get the incoming message data
    print(f'Incoming message: {data}')
    
    # Handle the incoming message here if needed, e.g., respond to the user or log it
    return Response(status=200)  # Return 200 OK to acknowledge receipt

# Route to handle delivery reports
@app.route('/delivery-reports', methods=['POST'])
def delivery_reports():
    """
    This route handles the delivery reports for messages sent.
    Africa's Talking will send POST requests with delivery status updates.
    """
    data = request.get_json(force=True)
    print(f'Delivery report: {data}')
    
    # Handle the delivery report here, e.g., log the delivery status or update the database
    return Response(status=200)  # Return 200 OK to acknowledge receipt

if __name__ == '__main__':
    app.run(debug=True, port=5001)
