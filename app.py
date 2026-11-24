"""Application factory.

``create_app()`` builds a fully wired Flask app. Importing this module, or
calling the factory, performs no database, Redis or Africa's Talking I/O:
the schema is owned by Alembic (``flask db upgrade``), and a local MySQL
database is created on demand with ``flask create-db``.

``app = create_app()`` at the bottom is what gunicorn loads (``Procfile``).
"""
import logging
import os
from datetime import timedelta
from logging.handlers import RotatingFileHandler

import click
from authlib.integrations.flask_client import OAuth  # type: ignore
from dotenv import load_dotenv
from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   session, url_for)
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix

from api.customers import customers_bp
from api.inventory import inventory_bp
from api.orders import orders_bp
from api.payments import payments_bp
from api.pharmacy import pharmacy_bp
from api.pos import pos_bp
from api.reports import reports_bp
from api.users import users_bp
from auth.auth_middleware import (admin_required, cashier_required,
                                  login_required, pharmacist_required,
                                  stock_clerk_required)
from auth.auth_routes import create_auth_blueprint
from config import Config
from models import Customer, Order, SystemConfig, db

load_dotenv()

migrate = Migrate()


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


_ONBOARDING_EXEMPT = frozenset({
    'static', 'onboarding', 'onboarding_save',
    'index', 'logout', 'login_page', 'register_page',
})
_API_PREFIXES = ('/pos/', '/payments/', '/pharmacy/', '/inventory/',
                 '/customers/', '/orders/', '/users/', '/reports/',
                 '/auth/', '/static/')


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_app(config_override=None):
    """Build the app. ``config_override`` keys win over ``Config``."""
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config['SESSION_COOKIE_NAME'] = 'google-login-session'
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)
    app.config['PREFERRED_URL_SCHEME'] = (
        'https' if os.getenv("APP_URL", "").startswith("https") else 'http')
    if config_override:
        app.config.update(config_override)

    # Trust exactly the configured number of proxy hops (Heroku's router is 1),
    # so request.remote_addr is the real client, not the router.
    app.wsgi_app = ProxyFix(app.wsgi_app,
                            x_for=int(app.config['PROXY_FIX_X_FOR']),
                            x_proto=int(app.config['PROXY_FIX_X_PROTO']))

    _configure_logging(app)

    db.init_app(app)
    migrate.init_app(app, db)

    oauth = _register_oauth(app)
    app.register_blueprint(customers_bp, url_prefix='/customers')
    app.register_blueprint(orders_bp, url_prefix='/orders')
    app.register_blueprint(inventory_bp, url_prefix='/inventory')
    app.register_blueprint(users_bp, url_prefix='/users')
    app.register_blueprint(reports_bp, url_prefix='/reports')
    app.register_blueprint(pos_bp, url_prefix='/pos')
    app.register_blueprint(payments_bp, url_prefix='/payments')
    app.register_blueprint(pharmacy_bp, url_prefix='/pharmacy')
    app.register_blueprint(create_auth_blueprint(oauth), url_prefix='/auth')

    _register_core_routes(app)
    _register_cli(app)
    return app


def _configure_logging(app):
    if app.testing:
        return
    if app.debug:
        logging.basicConfig(level=logging.DEBUG)
        return
    if any(isinstance(h, RotatingFileHandler) for h in app.logger.handlers):
        return
    os.makedirs('logs', exist_ok=True)
    handler = RotatingFileHandler('logs/make-an-order.log', maxBytes=10240, backupCount=10)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
    handler.setLevel(logging.INFO)
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info('Make-An-Order startup')


def _register_oauth(app):
    """Register Google OAuth. Metadata is fetched lazily on first login."""
    oauth = OAuth(app)
    app_url = os.getenv("APP_URL", "http://localhost:5001").rstrip('/')
    oauth.register(
        name='google',
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        access_token_url='https://accounts.google.com/o/oauth2/token',
        authorize_url='https://accounts.google.com/o/oauth2/auth',
        api_base_url='https://www.googleapis.com/oauth2/v1/',
        userinfo_endpoint='https://openidconnect.googleapis.com/v1/userinfo',
        client_kwargs={'scope': 'email profile'},
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    )
    app.logger.info(f"OAuth redirect_uri configured: {app_url}/auth/authorize")
    return oauth


def _register_cli(app):
    @app.cli.command('create-db')
    def create_db_command():
        """Create the local MySQL database if missing (not for JawsDB)."""
        from services.database_service import create_database
        create_database()
        click.echo('Database checked/created. Next: flask db upgrade')


def _safe_render(template, **ctx):
    """Render template; if missing, return a minimal placeholder."""
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


def _register_core_routes(app):
    """Page routes, onboarding gate, error handlers and SMS webhooks.

    Endpoint names are the function names, unchanged from the pre-factory
    module, because templates resolve them with ``url_for``.
    """

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

    @app.route('/onboarding', methods=['GET', 'POST'])
    def onboarding():
        """First-run (and re-run) business-type selection."""
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
            from services.seed_service import seed_inventory
            seed_inventory(btype)
            app.logger.info(f"Business configured as '{btype}' by "
                            f"{session.get('profile', {}).get('email')}")
            return redirect(url_for('dashboard'))

        ctx = get_user_context()
        if _get_business_type() and session.get('role') not in ('superuser', 'admin'):
            return redirect(url_for('dashboard'))
        return render_template('onboarding.html', **ctx)

    @app.route('/')
    def index():
        if 'profile' in session:
            return redirect(url_for('dashboard'))
        return render_template('landing.html')

    @app.route('/logout')
    def logout():
        user_email = (session.get('profile') or {}).get('email') or 'unknown'
        session.clear()
        app.logger.info(f"User {user_email} logged out.")
        return redirect(url_for('index'))

    @app.route('/role-select')
    def role_select():
        if 'profile' in session:
            return redirect(url_for('dashboard'))
        return redirect(url_for('login_page'))

    @app.route('/login')
    def login_page():
        if 'profile' in session:
            return redirect(url_for('dashboard'))
        return render_template('index.html')

    @app.route('/register')
    def register_page():
        if 'profile' in session:
            return redirect(url_for('dashboard'))
        return render_template('register.html')

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

    @app.route('/orders/<int:order_id>/edit')
    @admin_required
    def order_edit_page(order_id):
        order = db.session.get(Order, order_id)
        if not order:
            return render_template('404.html', message="Order not found"), 404
        return render_template('order_edit.html', order=order, **get_user_context())

    @app.errorhandler(400)
    def bad_request_error(error):
        app.logger.error(f"Bad Request: {request.url} - {str(error)}")
        return jsonify({"error": "Bad Request", "message": str(error)}), 400

    @app.errorhandler(401)
    def unauthorized_error(error):
        app.logger.warning(f"Unauthorized Access: {request.url}")
        return jsonify({"error": "Unauthorized",
                        "message": "Authentication required or invalid credentials"}), 401

    @app.errorhandler(404)
    def not_found_error(error):
        app.logger.warning(f"Not Found: {request.url}")
        return render_template('404.html',
                               message="The page you are looking for does not exist."), 404

    @app.errorhandler(500)
    def internal_server_error(error):
        app.logger.exception(f"Internal Server Error: {request.url}")
        return jsonify({"error": "Internal Server Error",
                        "message": "An unexpected error occurred."}), 500

    @app.route('/incoming-messages', methods=['POST'])
    def incoming_messages():
        """Africa's Talking inbound SMS webhook."""
        data = request.get_json(force=True)
        app.logger.info(f'Incoming message: {data}')
        return Response(status=200)

    @app.route('/delivery-reports', methods=['POST'])
    def delivery_reports():
        """Africa's Talking delivery report webhook."""
        data = request.get_json(force=True)
        app.logger.info(f'Delivery report: {data}')
        return Response(status=200)


app = create_app()

if __name__ == '__main__':
    app.run(debug=True, port=5001)
