from flask import Flask, request, Response, render_template, redirect, url_for, session, jsonify
from config import Config
from models import db, Customer, Order, OrderItem, StockMovement  # Import models
from api.customers import customers_bp
from api.orders import orders_bp
from api.inventory import inventory_bp
from api.users import users_bp
from api.reports import reports_bp
from services.database_service import create_database
from services.seed_service import seed_inventory, seed_customers
from authlib.integrations.flask_client import OAuth # type: ignore
from datetime import timedelta
from dotenv import load_dotenv
from auth.auth_routes import create_auth_blueprint
from auth.auth_middleware import login_required, admin_required, superuser_required
import os
import logging
from logging.handlers import RotatingFileHandler

# dotenv setup
load_dotenv()

app = Flask(__name__)
app.config.from_object(Config)

app.secret_key = os.getenv("APP_SECRET_KEY")
app.config['SESSION_COOKIE_NAME'] = 'google-login-session'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)

# Configure logging
if not app.debug:
    if not os.path.exists('logs'):
        os.mkdir('logs')
    file_handler = RotatingFileHandler('logs/make-an-order.log', maxBytes=10240, backupCount=10)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info('Make-An-Order startup')
else:
    # For debug mode, log to console
    logging.basicConfig(level=logging.DEBUG)


# Create the database if it doesn't exist (only for local dev, skipped for prod DB)
import sys; print("[app.py] calling create_database...", flush=True)
create_database()
print("[app.py] create_database done, init_app...", flush=True)

# Initialize the database
db.init_app(app)

def migrate_schema():
    """Add new columns to existing tables without dropping data."""
    from sqlalchemy import text, inspect as sa_inspect
    inspector = sa_inspect(db.engine)

    def add_missing_columns(table, additions):
        existing = {c['name'] for c in inspector.get_columns(table)}
        stmts = [sql for col, sql in additions if col not in existing]
        if stmts:
            with db.engine.begin() as conn:
                for sql in stmts:
                    conn.execute(text(sql))
            print(f"[app.py] migrated {table}: {stmts}", flush=True)

    add_missing_columns('users', [
        ('phone_number',  "ALTER TABLE users ADD COLUMN phone_number VARCHAR(20) NULL"),
        ('status',        "ALTER TABLE users ADD COLUMN `status` VARCHAR(20) NOT NULL DEFAULT 'pending'"),
        ('password_hash', "ALTER TABLE users ADD COLUMN password_hash VARCHAR(255) NULL"),
    ])

    add_missing_columns('customers', [
        ('email', "ALTER TABLE customers ADD COLUMN email VARCHAR(255) NULL"),
    ])

    add_missing_columns('orders', [
        ('status',            "ALTER TABLE orders ADD COLUMN `status` VARCHAR(20) NOT NULL DEFAULT 'pending'"),
        ('payment_method',    "ALTER TABLE orders ADD COLUMN payment_method VARCHAR(20) NULL"),
        ('payment_reference', "ALTER TABLE orders ADD COLUMN payment_reference VARCHAR(100) NULL"),
    ])

    add_missing_columns('inventory_items', [
        ('sku', "ALTER TABLE inventory_items ADD COLUMN sku VARCHAR(50) UNIQUE NULL"),
    ])

with app.app_context():
    print("[app.py] db.create_all()...", flush=True)
    db.create_all()
    migrate_schema()
    print("[app.py] seeding...", flush=True)
    seed_inventory()
    seed_customers()
    print("[app.py] startup complete", flush=True)

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
app.register_blueprint(inventory_bp, url_prefix='/inventory')
app.register_blueprint(users_bp, url_prefix='/users')
app.register_blueprint(reports_bp, url_prefix='/reports')
app.register_blueprint(create_auth_blueprint(oauth))


# --- User context helper ---
def get_user_context():
    profile = session.get('profile', {})
    return {
        'user_email': profile.get('email', 'Guest'),
        'user_name':  profile.get('name', ''),
        'user_role':  session.get('role', 'user'),
    }


# --- Frontend Routes ---
@app.route('/')
def index():
    if 'profile' in session:
        return redirect(url_for('dashboard')) # Redirect to dashboard if logged in
    return render_template('index.html') # Landing page with login button

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', **get_user_context())

@app.route('/customers-ui')
@admin_required
def customers_page():
    return render_template('customers.html', **get_user_context())

@app.route('/orders-ui')
@admin_required
def orders_page():
    return render_template('orders.html', **get_user_context())

@app.route('/inventory-ui')
@login_required
def inventory_page():
    return render_template('inventory.html', **get_user_context())

@app.route('/users-ui')
@admin_required
def users_page():
    return render_template('users.html', **get_user_context())

@app.route('/reports-ui')
@admin_required
def reports_page():
    return render_template('reports.html', **get_user_context())

@app.route('/orders/<int:order_id>/receipt')
@login_required
def order_receipt_page(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        return render_template('404.html', message="Order not found"), 404
    # Normal users can only view receipts for their own orders
    if session.get('role', 'user') not in ('admin', 'superuser'):
        from models import Customer as _Customer
        cust = _Customer.query.filter_by(code=f"USR-{session.get('user_id')}").first()
        if not cust or order.customer_id != cust.id:
            return render_template('404.html', message="Receipt not found"), 404
    return render_template('receipt.html', order=order, **get_user_context())


# --- Customer Detail/Edit Pages ---
@app.route('/customers/<int:customer_id>')
@admin_required
def customer_detail_page(customer_id):
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return render_template('404.html', message="Customer not found"), 404
    return render_template('customer_detail.html', customer=customer, **get_user_context())

@app.route('/customers/<int:customer_id>/edit')
@admin_required
def customer_edit_page(customer_id):
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return render_template('404.html', message="Customer not found"), 404
    return render_template('customer_edit.html', customer=customer, **get_user_context())

# --- Order Edit Page ---
@app.route('/orders/<int:order_id>/edit')
@admin_required
def order_edit_page(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        return render_template('404.html', message="Order not found"), 404
    return render_template('order_edit.html', order=order, **get_user_context())


# --- Centralized Error Handlers ---
@app.errorhandler(400)
def bad_request_error(error):
    app.logger.error(f"Bad Request: {request.url} - {str(error)}")
    return jsonify({"error": "Bad Request", "message": str(error)}), 400

@app.errorhandler(401)
def unauthorized_error(error):
    app.logger.warning(f"Unauthorized Access: {request.url}")
    return jsonify({"error": "Unauthorized", "message": "Authentication required or invalid credentials"}), 401

@app.errorhandler(404)
def not_found_error(error):
    app.logger.warning(f"Not Found: {request.url}")
    # Render a more user-friendly 404 page for UI
    return render_template('404.html', message="The page you are looking for does not exist."), 404

@app.errorhandler(500)
def internal_server_error(error):
    app.logger.exception(f"Internal Server Error: {request.url}") # Logs traceback
    return jsonify({"error": "Internal Server Error", "message": "An unexpected error occurred."}), 500


# Route to handle incoming messages
@app.route('/incoming-messages', methods=['POST'])
def incoming_messages():
    """
    This route handles incoming messages sent to your shortcode.
    Africa's Talking will send POST requests to this URL.
    """
    data = request.get_json(force=True)  # Get the incoming message data
    app.logger.info(f'Incoming message: {data}') # Use app.logger

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
    app.logger.info(f'Delivery report: {data}') # Use app.logger

    # Handle the delivery report here, e.g., log the delivery status or update the database
    return Response(status=200)  # Return 200 OK to acknowledge receipt

if __name__ == '__main__':
    app.run(debug=True, port=5001)
