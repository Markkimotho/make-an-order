"""Login, business signup and logout.

Login looks the user up across all tenants (emails are globally unique),
then the session carries that user's tenant. Google login only signs in
existing users; a new business starts at /register, which creates the
tenant and its owner (role 'superuser') together.
"""
import logging
import os

from flask import Blueprint, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError

from auth.tenancy import unscoped
from config import Config
from services.phone import InvalidPhone, normalize_ke_phone

logger = logging.getLogger(__name__)


def _platform_admin_emails():
    raw = Config.PLATFORM_ADMIN_EMAILS or ''
    return {e.strip().lower() for e in raw.split(',') if e.strip()}


def _session_for(user_fields):
    session.clear()
    session['profile'] = {'email': user_fields['email'],
                          'name': user_fields['name'] or user_fields['email']}
    session['role'] = user_fields['role']
    session['user_id'] = user_fields['id']
    session['tenant_id'] = user_fields['tenant_id']
    session.permanent = True
    logger.info(f"Session set: {user_fields['email']} role={user_fields['role']} "
                f"tenant={user_fields['tenant_id']}")


def _login_user(email):
    """Find an approved user by email in any tenant and start their session.

    Returns None on success, else the message to show. Refreshes the
    platform-admin flag from PLATFORM_ADMIN_EMAILS on every login.
    """
    from models import User, db
    with unscoped():
        user = User.query.filter_by(email=email).first()
        if user is None:
            return 'not_found'
        if user.status == 'pending':
            return "Your account is awaiting approval by your business owner."
        if user.status == 'rejected':
            return "Your account access has been denied. Contact your business owner."
        should_be_admin = email in _platform_admin_emails()
        if bool(user.is_platform_admin) != should_be_admin:
            user.is_platform_admin = should_be_admin
            db.session.commit()
        if user.tenant_id is None and not user.is_platform_admin:
            return "This account is not linked to a business."
        fields = {'id': user.id, 'email': user.email, 'name': user.name,
                  'role': user.role, 'tenant_id': user.tenant_id}
    _session_for(fields)
    return None


def create_auth_blueprint(oauth):
    auth_bp = Blueprint('auth', __name__)
    google_client = oauth.google

    # ── Google OAuth ────────────────────────────────────────────────────

    @auth_bp.route('/login')
    def login():
        if 'profile' in session:
            return redirect(url_for('dashboard'))
        try:
            app_url = os.getenv("APP_URL", "http://127.0.0.1:5001").rstrip('/')
            return google_client.authorize_redirect(f"{app_url}/auth/authorize",
                                                    prompt='select_account')
        except Exception as e:
            logger.error(f"Failed to initiate Google login: {type(e).__name__}: {e}", exc_info=True)
            return redirect(url_for('login_page', message="Could not connect to Google."))

    @auth_bp.route('/authorize')
    def authorize():
        try:
            google_client.authorize_access_token()
            user_info = google_client.get('userinfo').json()
        except Exception as e:
            logger.error(f"OAuth authorization failed: {type(e).__name__}: {e}", exc_info=True)
            return redirect(url_for('login_page', message="Google sign-in failed."))
        email = (user_info.get('email') or '').strip().lower()
        problem = _login_user(email)
        if problem == 'not_found':
            return redirect(url_for('register_page',
                                    message="No account for that Google address. "
                                            "Set up your business, or ask your owner to add you."))
        if problem:
            return redirect(url_for('login_page', message=problem))
        return redirect(url_for('dashboard'))

    # ── Local email/password login ──────────────────────────────────────

    @auth_bp.route('/login/local', methods=['POST'])
    def login_local():
        from models import User
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        with unscoped():
            user = User.query.filter_by(email=email).first()
            ok = user is not None and user.check_password(password)
        if not ok:
            return redirect(url_for('login_page', message="Invalid email or password."))
        problem = _login_user(email)
        if problem:
            return redirect(url_for('login_page', message=problem))
        return redirect(url_for('dashboard'))

    # ── Business signup ─────────────────────────────────────────────────

    @auth_bp.route('/register', methods=['GET', 'POST'])
    def register():
        from app import BUSINESS_CONFIGS
        from migrations.helpers import slugify
        from models import Tenant, User, db

        if 'profile' in session:
            return redirect(url_for('dashboard'))
        if request.method == 'GET':
            return redirect(url_for('register_page'))

        form = request.form
        business_name = form.get('business_name', '').strip()
        vertical = form.get('vertical', '').strip()
        name = form.get('name', '').strip()
        email = form.get('email', '').strip().lower()
        password = form.get('password', '')
        phone_raw = form.get('phone_number', '').strip()

        def fail(message):
            return render_template('register.html', error=message, form_data=form,
                                   BUSINESS_CONFIGS=BUSINESS_CONFIGS), 400

        if not all((business_name, vertical, name, email, password, phone_raw)):
            return fail("All fields are required.")
        if vertical not in BUSINESS_CONFIGS:
            return fail("Choose a type of business.")
        if len(password) < 8:
            return fail("Password must be at least 8 characters.")
        try:
            phone = normalize_ke_phone(phone_raw)
        except InvalidPhone:
            return fail("Enter a Kenyan mobile number, for example 0712 345 678.")

        with unscoped():
            if User.query.filter_by(email=email).first() is not None:
                return fail("An account with this email already exists.")
            taken = {slug for (slug,) in db.session.query(Tenant.slug).all()}
            tenant = Tenant(name=business_name, slug=slugify(business_name, taken),
                            vertical=vertical)
            db.session.add(tenant)
            db.session.flush()
            owner = User(tenant_id=tenant.id, email=email, name=name, phone_number=phone,
                         role='superuser', status='approved')
            owner.set_password(password)
            db.session.add(owner)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return fail("That business or email was just registered. Try signing in.")
            slug = tenant.slug
        logger.info(f"New business signup: {slug} ({vertical}) owner {email}")
        problem = _login_user(email)
        if problem:
            return redirect(url_for('login_page', message=problem))
        return redirect(url_for('dashboard'))

    # ── Logout ──────────────────────────────────────────────────────────

    @auth_bp.route('/logout')
    def logout():
        identifier = (session.get('profile') or {}).get('email') or 'unknown'
        session.clear()
        logger.info(f"User {identifier} logged out.")
        return redirect(url_for('index'))

    return auth_bp
