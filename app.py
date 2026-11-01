from flask import Flask, request, Response, render_template, redirect, url_for, session, jsonify
from config import Config
from models import db, Customer, Order, OrderItem, StockMovement, SystemConfig
from api.customers import customers_bp
from api.orders import orders_bp
from api.inventory import inventory_bp
from api.users import users_bp
from api.reports import reports_bp
from api.pos import pos_bp
from api.payments import payments_bp
from api.pharmacy import pharmacy_bp
from services.database_service import create_database
from services.seed_service import seed_inventory
from authlib.integrations.flask_client import OAuth # type: ignore
from flask_migrate import Migrate
from datetime import timedelta
from dotenv import load_dotenv
from auth.auth_routes import create_auth_blueprint
from auth.auth_middleware import login_required, admin_required, superuser_required
from auth.auth_middleware import (
    cashier_required, pharmacist_required, stock_clerk_required,
)
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

# Handle proxy (for HTTPS behind reverse proxy like Nginx/Heroku)
# This tells Flask to trust X-Forwarded-Proto header
app.config['PREFERRED_URL_SCHEME'] = 'https' if os.getenv("APP_URL", "").startswith("https") else 'http'
# For production deployments behind a proxy, uncomment this:
# from werkzeug.middleware.proxy_fix import ProxyFix
# app = ProxyFix(app, x_for=1, x_proto=1, x_host=1, x_port=1)

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
migrate = Migrate(app, db)

def migrate_schema():
    """Add new columns to existing tables without dropping data.

    This is a lightweight in-process migrator kept from the original project
    so that `python app.py` still produces a working DB on first run.
    For schema changes going forward prefer Flask-Migrate (Alembic):

        flask db init        # one-off
        flask db migrate -m "add pos models"
        flask db upgrade
    """
    from sqlalchemy import text, inspect as sa_inspect
    inspector = sa_inspect(db.engine)

    def add_missing_columns(table, additions):
        if not inspector.has_table(table):
            return
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
        ('email',          "ALTER TABLE customers ADD COLUMN email VARCHAR(255) NULL"),
        ('loyalty_points', "ALTER TABLE customers ADD COLUMN loyalty_points INT NOT NULL DEFAULT 0"),
        ('credit_limit',   "ALTER TABLE customers ADD COLUMN credit_limit DECIMAL(12,2) NOT NULL DEFAULT 0"),
        ('credit_balance', "ALTER TABLE customers ADD COLUMN credit_balance DECIMAL(12,2) NOT NULL DEFAULT 0"),
        ('customer_type',  "ALTER TABLE customers ADD COLUMN customer_type VARCHAR(20) NOT NULL DEFAULT 'retail'"),
        ('created_at',     "ALTER TABLE customers ADD COLUMN created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP"),
    ])

    add_missing_columns('orders', [
        ('status',            "ALTER TABLE orders ADD COLUMN `status` VARCHAR(20) NOT NULL DEFAULT 'pending'"),
        ('payment_method',    "ALTER TABLE orders ADD COLUMN payment_method VARCHAR(20) NULL"),
        ('payment_reference', "ALTER TABLE orders ADD COLUMN payment_reference VARCHAR(100) NULL"),
    ])

    add_missing_columns('inventory_items', [
        ('sku',                   "ALTER TABLE inventory_items ADD COLUMN sku VARCHAR(50) NULL"),
        ('barcode',               "ALTER TABLE inventory_items ADD COLUMN barcode VARCHAR(64) NULL"),
        ('cost_price',            "ALTER TABLE inventory_items ADD COLUMN cost_price DECIMAL(12,2) NULL"),
        ('reorder_level',         "ALTER TABLE inventory_items ADD COLUMN reorder_level INT NOT NULL DEFAULT 5"),
        ('expiry_date',           "ALTER TABLE inventory_items ADD COLUMN expiry_date DATE NULL"),
        ('batch_number',          "ALTER TABLE inventory_items ADD COLUMN batch_number VARCHAR(64) NULL"),
        ('supplier_id',           "ALTER TABLE inventory_items ADD COLUMN supplier_id INT NULL"),
        ('category_id',           "ALTER TABLE inventory_items ADD COLUMN category_id INT NULL"),
        ('unit',                  "ALTER TABLE inventory_items ADD COLUMN unit VARCHAR(20) NULL DEFAULT 'pcs'"),
        ('is_drug',               "ALTER TABLE inventory_items ADD COLUMN is_drug TINYINT(1) NOT NULL DEFAULT 0"),
        ('requires_prescription', "ALTER TABLE inventory_items ADD COLUMN requires_prescription TINYINT(1) NOT NULL DEFAULT 0"),
    ])

with app.app_context():
    print("[app.py] db.create_all()...", flush=True)
    db.create_all()
    migrate_schema()
    print("[app.py] seeding...", flush=True)
    seed_inventory(SystemConfig.get('business_type') or 'general')
    print("[app.py] startup complete", flush=True)

# OAuth Setup
oauth = OAuth(app)
# Construct the redirect URI that matches what's registered in Google Console
app_url = os.getenv("APP_URL", "http://localhost:5001").rstrip('/')
redirect_uri = f"{app_url}/auth/authorize"

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

app.logger.info(f"OAuth redirect_uri configured: {redirect_uri}")

# Register the endpoint blueprints
app.register_blueprint(customers_bp, url_prefix='/customers')
app.register_blueprint(orders_bp, url_prefix='/orders')
app.register_blueprint(inventory_bp, url_prefix='/inventory')
app.register_blueprint(users_bp, url_prefix='/users')
app.register_blueprint(reports_bp, url_prefix='/reports')
app.register_blueprint(pos_bp, url_prefix='/pos')
app.register_blueprint(payments_bp, url_prefix='/payments')
app.register_blueprint(pharmacy_bp, url_prefix='/pharmacy')
app.register_blueprint(create_auth_blueprint(oauth), url_prefix='/auth')


# ---------------------------------------------------------------------------
# Business-type feature map
# ---------------------------------------------------------------------------
# Each key is a business_type slug.  ``features`` is a set of capability
# names that drive nav visibility, dashboard cards and seed data.
BUSINESS_CONFIGS = {
    'pharmacy': {
        'label':       'Pharmacy',
        'icon':        'fa-prescription-bottle-medical',
        'color':       '#198754',
        'tagline':     'Prescriptions · Dispensing · Controlled drugs',
        'description': 'For licensed pharmacies. Handles prescription capture, '
                       'dispensing, batch/expiry tracking and controlled-drug ledger.',
        'features':    {'pos', 'shift', 'pharmacy', 'purchase_orders',
                        'inventory', 'reports', 'customers'},
    },
    'supermarket': {
        'label':       'Supermarket / Grocery',
        'icon':        'fa-cart-shopping',
        'color':       '#0d6efd',
        'tagline':     'FMCG · Perishables · Loyalty points',
        'description': 'For supermarkets and grocers. Fast checkout, expiry '
                       'tracking, loyalty programme and bulk purchase orders.',
        'features':    {'pos', 'shift', 'purchase_orders', 'inventory',
                        'reports', 'customers'},
    },
    'hardware': {
        'label':       'Hardware & Building',
        'icon':        'fa-hammer',
        'color':       '#fd7e14',
        'tagline':     'Tools · Materials · Trade accounts',
        'description': 'For hardware and building-material shops. Trade credit, '
                       'bulk items, purchase orders and stock management.',
        'features':    {'pos', 'shift', 'purchase_orders', 'inventory',
                        'reports', 'customers'},
    },
    'salon': {
        'label':       'Salon & Beauty',
        'icon':        'fa-scissors',
        'color':       '#d63384',
        'tagline':     'Services · Retail products · Client profiles',
        'description': 'For hair salons, barbershops and beauty studios. '
                       'Service-based POS, retail product sales and client loyalty.',
        'features':    {'pos', 'shift', 'inventory', 'reports', 'customers'},
    },
    'restaurant': {
        'label':       'Restaurant / Café',
        'icon':        'fa-utensils',
        'color':       '#dc3545',
        'tagline':     'Food · Beverages · Table service',
        'description': 'For restaurants, cafés and fast-food outlets. '
                       'Menu-based POS and daily sales reporting.',
        'features':    {'pos', 'shift', 'inventory', 'reports', 'customers'},
    },
    'general': {
        'label':       'General Retail',
        'icon':        'fa-store',
        'color':       '#6c757d',
        'tagline':     'Any product · Any customer',
        'description': 'A flexible setup for any retail business. '
                       'Full POS, inventory and purchase orders.',
        'features':    {'pos', 'shift', 'purchase_orders', 'inventory',
                        'reports', 'customers'},
    },
}


def _get_business_type():
    """Return the configured business_type slug, or None if onboarding not done."""
    try:
        return SystemConfig.get('business_type')
    except Exception:
        return None


# ---------------------------------------------------------------------------
# User context helper
# ---------------------------------------------------------------------------
def get_user_context():
    btype    = _get_business_type()
    features = BUSINESS_CONFIGS.get(btype or 'general',
                                    BUSINESS_CONFIGS['general'])['features']
    biz_name = ''
    try:
        biz_name = SystemConfig.get('business_name') or ''
    except Exception:
        pass
    base = {
        'business_type':  btype or 'general',
        'biz_configured': btype is not None,
        'biz_name':       biz_name,
        'features':       features,
        'BUSINESS_CONFIGS': BUSINESS_CONFIGS,
    }
    profile = session.get('profile')
    if profile:
        return {
            **base,
            'user_email': profile.get('email', ''),
            'user_name':  profile.get('name', ''),
            'user_role':  session.get('role', 'user'),
        }
    return {**base, 'user_email': '', 'user_name': '', 'user_role': 'guest'}


# ---------------------------------------------------------------------------
# Onboarding gate
# ---------------------------------------------------------------------------
_ONBOARDING_EXEMPT = frozenset({
    'static', 'onboarding', 'onboarding_save',
    'index', 'logout', 'login_page', 'register_page',
})
_API_PREFIXES = ('/pos/', '/payments/', '/pharmacy/', '/inventory/',
                 '/customers/', '/orders/', '/users/', '/reports/',
                 '/auth/', '/static/')


@app.before_request
def check_onboarding():
    """Send superuser/admin to /onboarding when business type is not yet set."""
    if request.endpoint in _ONBOARDING_EXEMPT or request.endpoint is None:
        return
    if any(request.path.startswith(p) for p in _API_PREFIXES):
        return
    if session.get('role') in ('superuser', 'admin'):
        try:
            if SystemConfig.get('business_type') is None:
                return redirect(url_for('onboarding'))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Onboarding route
# ---------------------------------------------------------------------------
@app.route('/onboarding', methods=['GET', 'POST'])
def onboarding():
    """First-run (and re-run) business-type selection.

    GET  — show the selector (redirect to dashboard if already configured
            and not superuser requesting a change).
    POST — save the selection and re-seed inventory if empty.
    """
    if request.method == 'POST':
        if 'profile' not in session:
            return redirect(url_for('index'))
        btype = request.form.get('business_type', '').strip()
        bname = request.form.get('business_name', '').strip()
        if btype not in BUSINESS_CONFIGS:
            ctx = get_user_context()
            ctx['error'] = 'Please select a valid business type.'
            return render_template('onboarding.html', **ctx)
        SystemConfig.set_value('business_type', btype)
        if bname:
            SystemConfig.set_value('business_name', bname)
        db.session.commit()
        # Seed type-appropriate inventory on first setup only
        from services.seed_service import seed_inventory
        seed_inventory(btype)
        app.logger.info(f"Business configured as '{btype}' by {session.get('profile', {}).get('email')}")
        return redirect(url_for('dashboard'))

    # GET — if already configured, only superuser may access for changes
    ctx = get_user_context()
    if _get_business_type() and session.get('role') not in ('superuser', 'admin'):
        return redirect(url_for('dashboard'))
    return render_template('onboarding.html', **ctx)


# --- Frontend Routes ---
@app.route('/')
def index():
    if 'profile' in session:
        return redirect(url_for('dashboard')) # Redirect to dashboard if logged in
    return render_template('landing.html') # Landing page gateway


# --- Top-level logout ---
# The auth blueprint also exposes /auth/logout; this convenience alias works
# whether the user signed in via Google OAuth or email/password and also
# clears any customer-side session keys.
@app.route('/logout')
def logout():
    user_email = (session.get('profile') or {}).get('email') or 'unknown'
    session.clear()
    app.logger.info(f"User {user_email} logged out.")
    return redirect(url_for('index'))


# /role-select kept as a redirect for any old bookmarks
@app.route('/role-select')
def role_select():
    if 'profile' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login_page'))


@app.route('/login')
def login_page():
    if 'profile' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')  # Business operator login


@app.route('/register')
def register_page():
    if 'profile' in session:
        return redirect(url_for('dashboard'))
    return render_template('register.html') # Registration form page (for operators)

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


# --- POS UI page routes (templates land in Phase 4) ---
def _safe_render(template, **ctx):
    """Render template; if missing, return a minimal placeholder.
    Lets us land Phase-3 routes before Phase-4 templates exist."""
    from jinja2 import TemplateNotFound
    try:
        return render_template(template, **ctx)
    except TemplateNotFound:
        return render_template('base.html',
                               page_body=f'<div class="container py-5">'
                                          f'<h2>{template}</h2>'
                                          f'<p class="text-muted">Template not yet implemented.</p>'
                                          f'</div>',
                               **ctx), 200


@app.route('/pos-ui')
@cashier_required
def pos_page():
    return _safe_render('pos.html', **get_user_context())


@app.route('/shift-ui')
@cashier_required
def shift_page():
    return _safe_render('shift.html', **get_user_context())


@app.route('/pharmacy-ui')
@pharmacist_required
def pharmacy_page():
    return _safe_render('pharmacy.html', **get_user_context())


@app.route('/purchase-orders-ui')
@stock_clerk_required
def purchase_orders_page():
    return _safe_render('purchase_orders.html', **get_user_context())


@app.route('/reports-dashboard')
@admin_required
def reports_dashboard_page():
    return _safe_render('reports_dashboard.html', **get_user_context())

@app.route('/orders/<int:order_id>/receipt')
@admin_required
def order_receipt_page(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        return render_template('404.html', message="Order not found"), 404
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
