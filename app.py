"""Application factory.

``create_app()`` builds a fully wired Flask app. Importing this module, or
calling the factory, performs no database, Redis or Africa's Talking I/O:
the schema is owned by Alembic (``flask db upgrade``), and a local MySQL
database is created on demand with ``flask create-db``.

``app = create_app()`` at the bottom is what gunicorn loads (``Procfile``).
"""
import logging
import os
import re
from datetime import timedelta
from logging.handlers import RotatingFileHandler

import click
from authlib.integrations.flask_client import OAuth  # type: ignore
from dotenv import load_dotenv
from flask import (Flask, Response, g, jsonify, redirect, render_template, request,
                   session, url_for)
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix

from public import limiter, public_bp
from api.customers import customers_bp
from api.daraja import daraja_bp
from api.distributor import distributor_bp
from api.institution import institution_bp
from api.inventory import inventory_bp
from api.orders import orders_bp
from api.payments import payments_bp
from api.pharmacy import pharmacy_bp
from api.platform import platform_bp
from api.pos import pos_bp
from api.reports import reports_bp
from api.settings import settings_bp
from api.users import users_bp
from auth.auth_middleware import (admin_required, cashier_required,
                                  login_required, pharmacist_required,
                                  stock_clerk_required, superuser_required)
from auth.auth_routes import create_auth_blueprint
from auth.tenancy import CrossTenantWrite, bind_tenant, reset_tenant, unscoped
from config import Config
from models import VERTICALS, Campaign, Customer, Order, Tenant, User, db

load_dotenv()

migrate = Migrate()


# ---------------------------------------------------------------------------
# Verticals
# ---------------------------------------------------------------------------
# One entry per tenant vertical (models.VERTICALS). ``features`` drives nav
# visibility and dashboard cards; ``seed`` names the demo catalogue used by
# ``flask seed-demo``.
BUSINESS_CONFIGS = {
    'pharmacy': {
        'label':       'Pharmacy / Agro-vet',
        'icon':        'fa-prescription-bottle-medical',
        'color':       '#198754',
        'tagline':     'Prescriptions · Expiry · Controlled drugs · Refills',
        'description': 'For chemists and agro-vets. Prescription capture, batch and '
                       'expiry tracking, the controlled-drug register and refill reminders.',
        'features':    {'pos', 'shift', 'pharmacy', 'purchase_orders',
                        'inventory', 'reports', 'customers'},
        'seed':        'pharmacy',
    },
    'distributor': {
        'label':       'Distributor / Wholesaler',
        'icon':        'fa-truck',
        'color':       '#0d6efd',
        'tagline':     'Trade credit · Route orders · Collections',
        'description': 'For FMCG, gas and hardware distributors selling to dukas on '
                       'credit, with M-Pesa collections and credit limits.',
        'features':    {'pos', 'shift', 'purchase_orders', 'inventory',
                        'reports', 'customers'},
        'seed':        'supermarket',
    },
    'institution': {
        'label':       'School / Estate / Chama',
        'icon':        'fa-school',
        'color':       '#6f42c1',
        'tagline':     'Pre-orders · M-Pesa collection · Pickup',
        'description': 'For schools, gated estates and chamas collecting payments '
                       'for uniforms, bundles and levies against a student or house number.',
        'features':    {'inventory', 'reports', 'customers'},
        'seed':        'general',
    },
}
assert set(BUSINESS_CONFIGS) == set(VERTICALS)


def get_user_context():
    """Template context. Business name and vertical come from the bound tenant."""
    tenant   = g.get('current_tenant')
    vertical = tenant.vertical if tenant else None
    features = BUSINESS_CONFIGS[vertical]['features'] if vertical else set()
    base = {
        'business_type':  vertical or 'distributor',
        'biz_configured': tenant is not None,
        'biz_name':       tenant.name if tenant else '',
        'tenant':         tenant,
        'is_platform_admin': bool(g.get('is_platform_admin')),
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


_API_PREFIXES = ('/pos/', '/payments/', '/pharmacy/', '/inventory/',
                 '/customers/', '/orders/', '/users/', '/reports/')

# Endpoints that still work when the tenant's subscription has lapsed.
_SUBSCRIPTION_EXEMPT = frozenset({'static', 'logout', 'auth.logout', 'index'})


def _wants_json():
    return any(request.path.startswith(p) for p in _API_PREFIXES)


def bind_request_tenant():
    """Bind the logged-in user's tenant for this request (before_request).

    Every request starts with no tenant bound, whatever the surrounding
    code had bound, so a request can only ever act as its own session's
    tenant. The session is then re-checked against the database: a user who
    was deleted, disabled or moved to another tenant is logged out, the
    session's role is refreshed from the user's current role, and a tenant
    whose subscription lapsed gets 402. Platform admins may have no tenant
    and are exempt from the membership and subscription checks.
    """
    g.current_tenant = None
    g.is_platform_admin = False
    g.base_token = bind_tenant(None)
    g.tenant_token = None
    user_id = session.get('user_id')
    if not user_id:
        return None

    tenant_id = session.get('tenant_id')
    with unscoped():
        user = db.session.get(User, user_id)
        if user is None or user.status != 'approved':
            session.clear()
            return None
        is_platform_admin = bool(user.is_platform_admin)
        if not is_platform_admin and (tenant_id is None or user.tenant_id != tenant_id):
            session.clear()
            return None
        tenant = db.session.get(Tenant, tenant_id) if tenant_id else None
        if tenant_id and tenant is None:
            session.clear()
            return None
        # Platform admins act as owner of the tenant they chose.
        role = 'superuser' if is_platform_admin else user.role
        db.session.expunge_all()   # keep the loaded tenant usable after unscoped()

    if session.get('role') != role:
        session['role'] = role
    g.is_platform_admin = is_platform_admin
    if tenant is None:
        return None

    if (not is_platform_admin and not tenant.is_active_subscription()
            and request.endpoint not in _SUBSCRIPTION_EXEMPT):
        if _wants_json():
            return jsonify({'error': 'Subscription inactive'}), 402
        return render_template('trial_ended.html', tenant=tenant), 402

    g.current_tenant = tenant
    g.tenant_token = bind_tenant(tenant.id)
    return None


def unbind_request_tenant(exc=None):
    for name in ('tenant_token', 'base_token'):
        token = g.pop(name, None)
        if token is not None:
            reset_tenant(token)


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

    # MySQL defaults to REPEATABLE READ, where a transaction keeps reading the
    # snapshot taken at its first read, even after waiting for a row lock.
    # Billing locks a row and then reads (next invoice number, credit owed);
    # READ COMMITTED makes those reads see what the lock holder committed.
    if app.config['SQLALCHEMY_DATABASE_URI'].startswith('mysql'):
        options = dict(app.config.get('SQLALCHEMY_ENGINE_OPTIONS') or {})
        options.setdefault('isolation_level', 'READ COMMITTED')
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = options

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
    app.register_blueprint(platform_bp, url_prefix='/platform')
    app.register_blueprint(daraja_bp, url_prefix='/api/payments/daraja')
    app.register_blueprint(settings_bp, url_prefix='/settings')
    app.register_blueprint(distributor_bp, url_prefix='/distributor')
    app.register_blueprint(institution_bp, url_prefix='/institution')
    app.register_blueprint(public_bp, url_prefix='/o')
    limiter.init_app(app)
    app.register_blueprint(create_auth_blueprint(oauth), url_prefix='/auth')

    app.before_request(bind_request_tenant)
    app.teardown_request(unbind_request_tenant)

    _register_core_routes(app)
    _register_cli(app)
    return app


class RedactCallbackTokens(logging.Filter):
    """Webhook URLs carry each business's callback token, and pay links an
    invoice's pay token; keep both out of request logs (werkzeug, gunicorn
    access log, app logs)."""

    _pattern = re.compile(r"(/api/payments/daraja/(?:stk|c2b)/[^/\s]+/|/o/[^/\s]+/(?:pay|r|d)/)"
                          r"[^/\s?\"]+")

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = self._pattern.sub(r"\1[token]", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


def _configure_logging(app):
    token_filter = RedactCallbackTokens()
    for name in ("werkzeug", "gunicorn.access", "gunicorn.error", "api.daraja",
                 "services.billing.stk", "public.routes", "public.refill", "public.duka", app.logger.name):
        target = logging.getLogger(name)
        if not any(isinstance(f, RedactCallbackTokens) for f in target.filters):
            target.addFilter(token_filter)
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

    @app.cli.command('create-platform-admin')
    @click.argument('email')
    @click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option('--name', default=None)
    def create_platform_admin_command(email, password, name):
        """Create a platform operator who belongs to no business."""
        from auth.tenancy import tenant_scope
        email = email.strip().lower()
        if len(password) < 8:
            raise click.ClickException('Password must be at least 8 characters')
        with unscoped(), tenant_scope(None):
            if User.query.filter_by(email=email).first() is not None:
                raise click.ClickException(f'A user with email {email} already exists; '
                                           'use flask grant-platform-admin')
            user = User(email=email, name=name, role='superuser', status='approved',
                        is_platform_admin=True)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
        click.echo(f'Created platform admin {email}. It must also be listed in '
                   'PLATFORM_ADMIN_EMAILS, or the flag is removed at their next login.')

    @app.cli.command('grant-platform-admin')
    @click.argument('email')
    def grant_platform_admin_command(email):
        """Give an existing user platform-admin rights (also needs PLATFORM_ADMIN_EMAILS)."""
        email = email.strip().lower()
        with unscoped():
            user = User.query.filter_by(email=email).first()
            if user is None:
                raise click.ClickException(f'No user with email {email}')
            user.is_platform_admin = True
            db.session.commit()
        click.echo(f'{email} is a platform admin while listed in PLATFORM_ADMIN_EMAILS.')

    tenants_cli = click.Group('tenants', help='Business (tenant) administration.')

    @tenants_cli.command('import-env-credentials')
    @click.argument('slug')
    def import_env_credentials(slug):
        """Copy the old single-business MPESA_* settings into a business."""
        mapping = {
            'daraja_shortcode': Config.MPESA_BUSINESS_SHORT_CODE,
            'daraja_consumer_key': Config.MPESA_CONSUMER_KEY,
            'daraja_consumer_secret': Config.MPESA_CONSUMER_SECRET,
            'daraja_passkey': Config.MPESA_PASSKEY,
        }
        missing = [k for k, v in mapping.items() if not v]
        if missing:
            raise click.ClickException(f'MPESA_* settings missing for: {", ".join(missing)}')
        with unscoped():
            tenant = Tenant.query.filter_by(slug=slug).first()
            if tenant is None:
                raise click.ClickException(f'No tenant with slug {slug!r}')
            for field, value in mapping.items():
                setattr(tenant, field, value)
            tenant.daraja_shortcode_type = tenant.daraja_shortcode_type or 'paybill'
            tenant.daraja_env = Config.MPESA_ENV or 'sandbox'
            summary = f'type {tenant.daraja_shortcode_type}, {tenant.daraja_env}'
            db.session.commit()
        click.echo(f'Daraja credentials copied into {slug} ({summary}). '
                   'The MPESA_* settings can now be removed.')

    @tenants_cli.command('register-c2b')
    @click.argument('slug')
    def register_c2b(slug):
        """Register a business's Paybill confirmation URLs with Safaricom."""
        from services.daraja import DarajaClient, DarajaError, callback_url
        with unscoped():
            tenant = Tenant.query.filter_by(slug=slug).first()
            if tenant is None:
                raise click.ClickException(f'No tenant with slug {slug!r}')
            try:
                DarajaClient(tenant).register_c2b()
            except DarajaError as e:
                raise click.ClickException(f'Safaricom refused: {e}')
            url = callback_url(tenant, 'c2b').rsplit('/', 1)[0] + '/[token]'
        click.echo(f'Registered {url} for {slug} (ResponseType Completed).')

    @tenants_cli.command('import-csv')
    @click.argument('slug')
    @click.argument('kind', type=click.Choice(['items', 'customers', 'members', 'tier_prices']))
    @click.argument('path', type=click.Path(exists=True, dir_okay=False))
    @click.option('--strict', is_flag=True, help='Import nothing if any row is rejected.')
    def import_csv_command(slug, kind, path, strict):
        """Import items, customers, members or tier prices from a CSV file."""
        from auth.tenancy import tenant_scope
        from services.csv_import import ImportError_, import_csv
        with unscoped():
            tenant = Tenant.query.filter_by(slug=slug).first()
            if tenant is None:
                raise click.ClickException(f'No tenant with slug {slug!r}')
            tenant_id = tenant.id
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        with tenant_scope(tenant_id):
            try:
                report = import_csv(kind, text, strict=strict)
            except ImportError_ as e:
                db.session.rollback()
                raise click.ClickException(str(e))
        click.echo(report.as_text())
        if report.rejected:
            raise SystemExit(1)

    app.cli.add_command(tenants_cli)

    @app.cli.command('payments-sweep')
    def payments_sweep_command():
        """Ask Daraja about every silent STK push and release unpaid stock
        holds past their time (run every 10 minutes)."""
        from auth.tenancy import tenant_scope
        from models import PaymentIntent
        from services.billing.stk import WAITING, sweep_tenant
        from services.orders import release_expired
        from verticals.institution.service import close_due_campaigns, issue_pickup_codes
        with unscoped():
            tenant_ids = sorted(
                {tid for (tid,) in db.session.query(PaymentIntent.tenant_id)
                 .filter(PaymentIntent.status.in_(WAITING)).distinct()}
                | {tid for (tid,) in db.session.query(Order.tenant_id)
                   .filter(Order.fulfillment_status.in_(("pending", "released"))).distinct()}
                | {tid for (tid,) in db.session.query(Campaign.tenant_id)
                   .filter(Campaign.status == "open").distinct()})
        totals = {}
        for tenant_id in tenant_ids:
            try:
                with tenant_scope(tenant_id):
                    counts = sweep_tenant()
                    counts.update({f"orders_{k}": v for k, v in release_expired().items()})
                    counts.update({f"campaigns_{k}": v
                                   for k, v in close_due_campaigns().items()})
                    counts["pickup_codes"] = issue_pickup_codes()
            except Exception as e:                      # one business never stops the rest
                db.session.rollback()
                app.logger.error(f"payments sweep failed for tenant {tenant_id}: {e}")
                continue
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
        app.logger.info(f"payments sweep: {len(tenant_ids)} businesses, {totals}")
        click.echo(f"Swept {len(tenant_ids)} businesses: {totals}")

    @app.cli.command('seed-demo')
    @click.argument('slug')
    def seed_demo_command(slug):
        """Fill an empty tenant's inventory with the demo catalogue for its vertical."""
        from auth.tenancy import tenant_scope
        from services.seed_service import seed_inventory
        with unscoped():
            tenant = Tenant.query.filter_by(slug=slug).first()
            if tenant is None:
                raise click.ClickException(f'No tenant with slug {slug!r}')
            tenant_id, vertical = tenant.id, tenant.vertical
        with tenant_scope(tenant_id):
            seed_inventory(BUSINESS_CONFIGS[vertical]['seed'])
        click.echo(f'Seeded demo inventory for {slug} (skipped if it already had items).')


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

    @app.route('/onboarding', methods=['GET', 'POST'])
    @superuser_required
    def onboarding():
        """Owner's business settings: name and vertical."""
        tenant = g.current_tenant
        if request.method == 'POST':
            vertical = request.form.get('business_type', '').strip()
            name = request.form.get('business_name', '').strip()
            if vertical not in BUSINESS_CONFIGS:
                ctx = get_user_context()
                ctx['error'] = 'Please select a valid business type.'
                return render_template('onboarding.html', **ctx)
            row = db.session.get(Tenant, tenant.id)
            row.vertical = vertical
            if name:
                row.name = name
            db.session.commit()
            app.logger.info(f"Tenant {row.slug} settings updated by "
                            f"{session.get('profile', {}).get('email')}")
            return redirect(url_for('dashboard'))
        return render_template('onboarding.html', **get_user_context())

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
        return render_template('index.html', message=request.args.get('message'))

    @app.route('/register')
    def register_page():
        if 'profile' in session:
            return redirect(url_for('dashboard'))
        return render_template('register.html', BUSINESS_CONFIGS=BUSINESS_CONFIGS,
                               error=request.args.get('message'))

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

    @app.errorhandler(CrossTenantWrite)
    def cross_tenant_write(error):
        # A reference to another business's row: from this tenant's point of
        # view it does not exist. Logged, because a route let it get this far.
        app.logger.warning(f"Cross-tenant write refused: {request.method} {request.path}: {error}")
        db.session.rollback()
        if _wants_json():
            return jsonify({"error": "Not found"}), 404
        return render_template('404.html', message="Not found"), 404

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
        """Africa's Talking delivery reports (one URL for every business).

        Sent form-encoded: id, status, phoneNumber, failureReason, ...
        Always 200 so Africa's Talking does not retry; unknown ids are ignored.
        """
        from services import sms_service
        if not sms_service.delivery_token_ok(request.args.get('token')):
            return Response(status=404)
        data = request.form.to_dict() or (request.get_json(silent=True) or {})
        try:
            sms_service.record_delivery(data.get('id'), data.get('status'),
                                        data.get('failureReason'))
        except Exception:
            db.session.rollback()
            app.logger.error('delivery report not applied: id=%s status=%s',
                             data.get('id'), data.get('status'), exc_info=True)
        return Response(status=200)


app = create_app()

if __name__ == '__main__':
    app.run(debug=True, port=5001)
