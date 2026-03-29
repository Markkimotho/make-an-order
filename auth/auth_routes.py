from flask import Blueprint, redirect, url_for, session, request, render_template
from auth.auth_middleware import login_required
import logging

logger = logging.getLogger(__name__)


def create_auth_blueprint(oauth):
    auth_bp = Blueprint('auth', __name__)

    google_client = oauth.create_client('google')

    # ── Google OAuth ────────────────────────────────────────────────────

    @auth_bp.route('/login')
    def login():
        if 'profile' in session:
            return redirect(url_for('dashboard'))
        redirect_uri = url_for('auth.authorize', _external=True)
        return google_client.authorize_redirect(redirect_uri, prompt='select_account')

    @auth_bp.route('/authorize')
    def authorize():
        try:
            token     = google_client.authorize_access_token()
            user_info = google_client.get('userinfo').json()
            email     = user_info.get('email', '')
            name      = user_info.get('name', '')
            _sync_oauth_user(email, name)
            return redirect(url_for('dashboard'))
        except Exception as e:
            logger.error(f"OAuth authorization failed: {e}", exc_info=True)
            return redirect(url_for('index', message="Login failed. Please try again."))

    # ── Local email/password login ──────────────────────────────────────

    @auth_bp.route('/login/local', methods=['POST'])
    def login_local():
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        from models import User
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            return redirect(url_for('index', message="Invalid email or password."))
        if user.status == 'pending':
            return redirect(url_for('index', message="Your account is awaiting approval by an administrator."))
        if user.status == 'rejected':
            return redirect(url_for('index', message="Your account access has been denied. Contact an administrator."))
        _populate_session(user)
        logger.info(f"Local login: {email} ({user.role})")
        return redirect(url_for('dashboard'))

    # ── Registration ────────────────────────────────────────────────────

    @auth_bp.route('/register', methods=['GET', 'POST'])
    def register():
        if 'profile' in session:
            return redirect(url_for('dashboard'))
        if request.method == 'POST':
            email    = request.form.get('email', '').strip().lower()
            name     = request.form.get('name', '').strip()
            password = request.form.get('password', '')
            phone    = request.form.get('phone_number', '').strip()
            from models import db, User
            if not email or not password or not name or not phone:
                return render_template('register.html', error="All fields are required.")
            if len(password) < 8:
                return render_template('register.html', error="Password must be at least 8 characters.", form_data=request.form)
            if User.query.filter_by(email=email).first():
                return render_template('register.html', error="An account with this email already exists.", form_data=request.form)
            user = User(email=email, name=name, phone_number=phone, role='user', status='pending')
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            logger.info(f"New registration: {email}, awaiting approval.")
            return render_template('register.html', success=True)
        return render_template('register.html')

    # ── Logout ──────────────────────────────────────────────────────────

    @auth_bp.route('/logout')
    def logout():
        user_email = session.get('profile', {}).get('email', 'unknown')
        session.clear()
        logger.info(f"User {user_email} logged out.")
        return redirect(url_for('index'))

    # ── Shared helpers ──────────────────────────────────────────────────

    def _sync_oauth_user(email, name):
        """Look up or create User for Google OAuth login, always approve."""
        from models import db, User, SUPERUSER_EMAIL
        user = User.query.filter_by(email=email).first()
        if not user:
            role = 'superuser' if email == SUPERUSER_EMAIL else 'user'
            user = User(email=email, name=name or None, role=role, status='approved')
            db.session.add(user)
            db.session.commit()
        else:
            changed = False
            if email == SUPERUSER_EMAIL and user.role != 'superuser':
                user.role = 'superuser'; changed = True
            if user.status != 'approved':
                user.status = 'approved'; changed = True
            if name and user.name != name:
                user.name = name; changed = True
            if changed:
                db.session.commit()
        _populate_session(user)

    def _populate_session(user):
        session['profile'] = {'email': user.email, 'name': user.name or user.email}
        session['role']    = user.role
        session['user_id'] = user.id
        session.permanent  = True
        logger.info(f"Session set: {user.email} role={user.role} status={user.status}")

    return auth_bp
