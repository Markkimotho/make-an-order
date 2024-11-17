from flask import Flask, request, Response  # type: ignore
from config import Config
from models import db
from api.customers import customers_bp
from api.orders import orders_bp
from services.database_service import create_database
from authlib.integrations.flask_client import OAuth
from datetime import timedelta
from dotenv import load_dotenv
from auth.auth_routes import create_auth_blueprint
import os

# dotenv setup
load_dotenv()

app = Flask(__name__)
app.config.from_object(Config)

app.secret_key = os.getenv("APP_SECRET_KEY")
app.config['SESSION_COOKIE_NAME'] = 'google-login-session'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)

# Create the database if it doesn't exist
create_database()

# Initialize the database
db.init_app(app)
with app.app_context():
    db.create_all()

# OAuth Setup
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    access_token_url='https://accounts.google.com/o/oauth2/token',
    authorize_url='https://accounts.google.com/o/oauth2/auth',
    api_base_url='https://www.googleapis.com/oauth2/v1/',
    userinfo_endpoint='https://openidconnect.googleapis.com/v1/userinfo',
    client_kwargs={'scope': 'email profile'},
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration'
)

# Register the endpoint blueprints
app.register_blueprint(customers_bp, url_prefix='/customers')
app.register_blueprint(orders_bp, url_prefix='/orders')
app.register_blueprint(create_auth_blueprint(oauth))

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
