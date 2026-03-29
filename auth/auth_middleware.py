from functools import wraps
from flask import redirect, url_for, session, jsonify

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
        if session.get('role') not in ('admin', 'superuser'):
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def superuser_required(f):
    """UI routes — redirects non-superusers to dashboard."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'profile' not in session:
            return redirect(url_for('auth.login'))
        if session.get('role') != 'superuser':
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def admin_api_required(f):
    """API routes — returns JSON 403 if not admin/superuser."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'profile' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        if session.get('role') not in ('admin', 'superuser'):
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated

def superuser_api_required(f):
    """API routes — returns JSON 403 if not superuser."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'profile' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        if session.get('role') != 'superuser':
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated
