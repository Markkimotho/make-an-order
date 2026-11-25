"""Login, tenant and role decorators.

Every decorator here first runs the tenant check: the request must have a
bound tenant (set by app.bind_request_tenant from the session). A platform
admin with no tenant chosen is sent to /platform/tenants. Role checks then
apply within that tenant; 'superuser' is the tenant owner.
"""
from functools import wraps

from flask import g, jsonify, redirect, session, url_for

# Role groupings — keep in sync with models.core constants.
ADMIN_ROLES     = ('admin', 'superuser')
POS_ROLES       = ('cashier', 'pharmacist', 'admin', 'superuser')
PHARMACY_ROLES  = ('pharmacist', 'admin', 'superuser')
STOCK_ROLES     = ('stock_clerk', 'pharmacist', 'admin', 'superuser')
# Money screens: reports, reconciliation, expenses, payment checks.
FINANCE_ROLES   = ('accountant', 'admin', 'superuser')


def _role() -> str:
    return session.get('role') or 'user'


def _ui_gate():
    """None if the request may proceed, else the redirect to send."""
    if 'profile' not in session:
        return redirect(url_for('auth.login'))
    if g.get('current_tenant') is None:
        if g.get('is_platform_admin'):
            return redirect(url_for('platform.tenants'))
        session.clear()
        return redirect(url_for('auth.login'))
    return None


def _api_gate():
    """None if the API request may proceed, else the JSON error response."""
    if 'profile' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if g.get('current_tenant') is None:
        if g.get('is_platform_admin'):
            return jsonify({'error': 'Choose a tenant first'}), 403
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _ui(allowed_roles=None):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            blocked = _ui_gate()
            if blocked is not None:
                return blocked
            if allowed_roles is not None and _role() not in allowed_roles:
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator


def _api(allowed_roles=None):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            blocked = _api_gate()
            if blocked is not None:
                return blocked
            if allowed_roles is not None and _role() not in allowed_roles:
                return jsonify({'error': 'Forbidden',
                                'required_roles': list(allowed_roles)}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


tenant_required      = _ui()
tenant_api_required  = _api()
login_required       = tenant_required

admin_required       = _ui(ADMIN_ROLES)
superuser_required   = _ui(('superuser',))
admin_api_required   = _api(ADMIN_ROLES)
superuser_api_required = _api(('superuser',))

cashier_required,     cashier_api_required     = _ui(POS_ROLES),      _api(POS_ROLES)
pos_required,         pos_api_required         = cashier_required,   cashier_api_required
pharmacist_required,  pharmacist_api_required  = _ui(PHARMACY_ROLES), _api(PHARMACY_ROLES)
stock_clerk_required, stock_clerk_api_required = _ui(STOCK_ROLES),    _api(STOCK_ROLES)
finance_required,     finance_api_required     = _ui(FINANCE_ROLES),  _api(FINANCE_ROLES)


def roles_required(*allowed_roles):
    """Generic API decorator: ``@roles_required('cashier', 'admin')``."""
    return _api(allowed_roles)


def platform_admin_required(f):
    """Platform operators only. Works with or without a tenant chosen."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'profile' not in session:
            return redirect(url_for('auth.login'))
        if not g.get('is_platform_admin'):
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated
