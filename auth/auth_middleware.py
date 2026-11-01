from functools import wraps
from flask import redirect, url_for, session, jsonify

# Role groupings — keep in sync with models.py constants.
ADMIN_ROLES     = ('admin', 'superuser')
POS_ROLES       = ('cashier', 'pharmacist', 'admin', 'superuser')
PHARMACY_ROLES  = ('pharmacist', 'admin', 'superuser')
STOCK_ROLES     = ('stock_clerk', 'admin', 'superuser')


def _role() -> str:
    return session.get('role') or 'user'


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'profile' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    """UI routes — redirects non-admins to dashboard."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'profile' not in session:
            return redirect(url_for('auth.login'))
        if _role() not in ADMIN_ROLES:
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def superuser_required(f):
    """UI routes — redirects non-superusers to dashboard."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'profile' not in session:
            return redirect(url_for('auth.login'))
        if _role() != 'superuser':
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def admin_api_required(f):
    """API routes — returns JSON 403 if not admin/superuser."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'profile' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        if _role() not in ADMIN_ROLES:
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated

def superuser_api_required(f):
    """API routes — returns JSON 403 if not superuser."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'profile' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        if _role() != 'superuser':
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# POS role decorators
# ---------------------------------------------------------------------------

def _make_role_decorators(allowed_roles, label):
    """Return (ui_decorator, api_decorator) that gate on ``allowed_roles``."""
    def ui_decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'profile' not in session:
                return redirect(url_for('auth.login'))
            if _role() not in allowed_roles:
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        decorated.__doc__ = f"{label} (UI). Allowed roles: {allowed_roles}"
        return decorated

    def api_decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'profile' not in session:
                return jsonify({'error': 'Unauthorized'}), 401
            if _role() not in allowed_roles:
                return jsonify({'error': 'Forbidden',
                                'required_roles': list(allowed_roles)}), 403
            return f(*args, **kwargs)
        decorated.__doc__ = f"{label} (API). Allowed roles: {allowed_roles}"
        return decorated

    return ui_decorator, api_decorator


# Cashier or higher (covers cashier, pharmacist, admin, superuser)
cashier_required,     cashier_api_required     = _make_role_decorators(POS_ROLES,      'Cashier')
pos_required,         pos_api_required         = cashier_required, cashier_api_required  # alias

pharmacist_required,  pharmacist_api_required  = _make_role_decorators(PHARMACY_ROLES, 'Pharmacist')
stock_clerk_required, stock_clerk_api_required = _make_role_decorators(STOCK_ROLES,    'Stock clerk')


def roles_required(*allowed_roles):
    """Generic API decorator: ``@roles_required('cashier', 'admin')``."""
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'profile' not in session:
                return jsonify({'error': 'Unauthorized'}), 401
            if _role() not in allowed_roles:
                return jsonify({'error': 'Forbidden',
                                'required_roles': list(allowed_roles)}), 403
            return f(*args, **kwargs)
        return decorated
    return wrapper

