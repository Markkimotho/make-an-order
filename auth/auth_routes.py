"""Login, business signup and logout.

Login looks the user up across all tenants (emails are globally unique),
then the session carries that user's tenant. Google login only signs in
existing users; a new business starts at /register, which creates the
tenant and its owner (role 'superuser') together.

Login security: MAX_FAILED_LOGINS wrong passwords in a row lock the account
for LOCK_MINUTES (users.locked_until); a locked account is refused even with
the right password until the lock passes or an admin unlocks it. The
password form is also rate limited per IP. Every sign-in, failure, lock and
sign-out is written to the audit log with the client IP; the password never
is. Messages never say whether an email exists, except the lock message once
an account is actually locked.
"""
import logging
import math
import os
import time
from datetime import datetime, timedelta

from flask import Blueprint, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from auth.tenancy import current_tenant_id, unscoped
from config import Config
from public.limits import limiter
from services.phone import InvalidPhone, normalize_ke_phone

logger = logging.getLogger(__name__)

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
LOGIN_RATE_LIMIT = "10 per minute"
INVALID_LOGIN = "Invalid email or password."

_dummy_hash = None


def _utcnow():
    """Naive UTC, as the app stores times. Tests monkeypatch this."""
    return datetime.utcnow()


def _spend_hash_time(password):
    """Check against a throwaway hash so an unknown email takes as long as a
    wrong password (no timing hint that the email exists)."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = generate_password_hash("not-a-password")
    check_password_hash(_dummy_hash, password)


def locked_message(locked_until, now=None):
    minutes = max(1, math.ceil((locked_until - (now or _utcnow())).total_seconds() / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return (f"Too many failed sign-in attempts. This account is locked for "
            f"{minutes} more {unit}. Try again later or ask your manager to unlock it.")


def _audit(action, user=None, details=None):
    """Queue an audit row for a sign-in event (inside unscoped(); the caller
    commits). The row belongs to the user's business, or to none when no
    user matched. ``details`` must never hold a password."""
    from models import AuditLog, db
    db.session.add(AuditLog(
        tenant_id=user.tenant_id if user is not None else None,
        user_id=user.id if user is not None else None,
        action=action,
        entity_type='User' if user is not None else None,
        entity_id=user.id if user is not None else None,
        new_value=details,
        ip_address=request.remote_addr,
    ))


def record_logout(reason='signed_out'):
    """Audit the current session's sign-out. Needs a tenant bound (the
    request's, or the caller's tenant_scope); platform admins with no
    business chosen are not logged. Never blocks the sign-out."""
    from models import db
    from services import audit_service
    user_id = session.get('user_id')
    if not user_id or current_tenant_id() is None:
        return
    try:
        audit_service.log(user_id, 'logout', 'User', user_id, None, {'reason': reason},
                          ip_address=request.remote_addr)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.warning("logout audit not written for user %s", user_id, exc_info=True)


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
    session['last_seen'] = int(time.time())
    session.permanent = True
    logger.info(f"Session set: {user_fields['email']} role={user_fields['role']} "
                f"tenant={user_fields['tenant_id']}")


def _login_user(email, method='password'):
    """Find an approved user by email in any tenant and start their session.

    Returns None on success, else the message to show. Removes the
    platform-admin flag if the email is no longer in PLATFORM_ADMIN_EMAILS.
    A locked account is refused whatever the sign-in method. Success clears
    the failure count and records last_login_at and a 'login' audit row.
    """
    from models import User, db
    with unscoped():
        user = User.query.filter_by(email=email).with_for_update().first()
        if user is None:
            return 'not_found'
        now = _utcnow()
        if user.locked_until is not None and user.locked_until > now:
            _audit('login.failed', user, {'email': email, 'reason': 'locked', 'method': method})
            message = locked_message(user.locked_until, now)
            db.session.commit()
            return message
        if user.status in ('pending', 'rejected'):
            _audit('login.failed', user, {'email': email, 'reason': user.status,
                                          'method': method})
            db.session.commit()
        if user.status == 'pending':
            return "Your account is awaiting approval by your business owner."
        if user.status == 'rejected':
            return "Your account access has been denied. Contact your business owner."
        # Platform-admin rights need both the database flag (set only by
        # `flask create-platform-admin` / `grant-platform-admin`) and the
        # email in PLATFORM_ADMIN_EMAILS. Login can only remove the flag,
        # never grant it: anyone can sign up with any email address.
        if user.is_platform_admin and email not in _platform_admin_emails():
            user.is_platform_admin = False
            db.session.commit()
        if user.tenant_id is None and not user.is_platform_admin:
            return "This account is not linked to a business."
        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = now
        _audit('login', user, {'method': method})
        fields = {'id': user.id, 'email': user.email, 'name': user.name,
                  'role': user.role, 'tenant_id': user.tenant_id}
        db.session.commit()
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
        problem = _login_user(email, method='google')
        if problem == 'not_found':
            return redirect(url_for('register_page',
                                    message="No account for that Google address. "
                                            "Set up your business, or ask your owner to add you."))
        if problem:
            return redirect(url_for('login_page', message=problem))
        return redirect(url_for('dashboard'))

    # ── Local email/password login ──────────────────────────────────────

    @auth_bp.route('/login/local', methods=['POST'])
    @limiter.limit(LOGIN_RATE_LIMIT)
    def login_local():
        from models import User, db
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        typed = email[:255]
        with unscoped():
            # The row lock makes concurrent failures queue (MySQL), and the
            # increment is one SQL expression, so two failures can never
            # both read 4 and write 5.
            user = User.query.filter_by(email=email).with_for_update().first()
            now = _utcnow()
            if user is None:
                _spend_hash_time(password)
                _audit('login.failed', None, {'email': typed, 'reason': 'unknown_email'})
                db.session.commit()
                return redirect(url_for('login_page', message=INVALID_LOGIN))
            if user.locked_until is not None and user.locked_until > now:
                _audit('login.failed', user, {'email': typed, 'reason': 'locked'})
                message = locked_message(user.locked_until, now)
                db.session.commit()
                return redirect(url_for('login_page', message=message))
            if not user.check_password(password):
                if user.locked_until is not None:
                    # An old lock that has run out: count from zero again.
                    user.locked_until = None
                    user.failed_logins = 0
                    db.session.flush()
                user.failed_logins = User.failed_logins + 1
                db.session.flush()
                count = user.failed_logins          # re-read after the UPDATE
                _audit('login.failed', user, {'email': typed, 'reason': 'bad_password',
                                              'failed_logins': count})
                message = INVALID_LOGIN
                if count >= MAX_FAILED_LOGINS:
                    user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                    _audit('login.locked', user, {'email': typed, 'failed_logins': count,
                                                  'minutes': LOCK_MINUTES})
                    message = locked_message(user.locked_until, now)
                    logger.warning("Account %s locked after %d failed sign-ins", user.id, count)
                db.session.commit()
                return redirect(url_for('login_page', message=message))
        problem = _login_user(email, method='password')
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

        slug = None
        with unscoped():
            if User.query.filter_by(email=email).first() is not None:
                return fail("An account with this email already exists.")
            # Two signups with the same business name can race for one slug;
            # the loser retries with the next free one.
            for _ in range(5):
                taken = {s for (s,) in db.session.query(Tenant.slug).all()}
                tenant = Tenant(name=business_name, slug=slugify(business_name, taken),
                                vertical=vertical)
                owner = User(email=email, name=name, phone_number=phone,
                             role='superuser', status='approved')
                owner.set_password(password)
                try:
                    db.session.add(tenant)
                    db.session.flush()
                    owner.tenant_id = tenant.id
                    db.session.add(owner)
                    db.session.commit()
                    slug = tenant.slug
                    break
                except IntegrityError:
                    db.session.rollback()
                    if User.query.filter_by(email=email).first() is not None:
                        return fail("An account with this email already exists.")
            if slug is None:
                return fail("Could not create the business right now. Please try again.")
        logger.info(f"New business signup: {slug} ({vertical}) owner {email}")
        problem = _login_user(email, method='signup')
        if problem:
            return redirect(url_for('login_page', message=problem))
        return redirect(url_for('dashboard'))

    # ── Logout ──────────────────────────────────────────────────────────

    @auth_bp.route('/logout')
    def logout():
        identifier = (session.get('profile') or {}).get('email') or 'unknown'
        record_logout()
        session.clear()
        logger.info(f"User {identifier} logged out.")
        return redirect(url_for('index'))

    return auth_bp
