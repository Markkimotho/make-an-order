"""Sign out sessions left idle longer than the business allows.

Every request of a signed-in user (pages and API polling alike) stamps
``session['last_seen']``. When the gap since the last request is longer than
the tenant's ``session_idle_minutes``, the session is cleared and the user
is sent to the login page; an API path answers 401 JSON instead.
"""
import time

from flask import jsonify, redirect, session, url_for

from auth.tenancy import tenant_scope

# Platform admins with no business chosen have no tenant setting.
DEFAULT_IDLE_MINUTES = 30


def _now():
    """Seconds since the epoch. Tests move ``session['last_seen']`` instead."""
    return int(time.time())


def idle_message(minutes):
    return f"You were signed out after {minutes} minutes of inactivity"


def enforce_idle_timeout(tenant, wants_json):
    """None if the session may go on (and stamps it), else the response."""
    minutes = tenant.session_idle_minutes if tenant is not None else DEFAULT_IDLE_MINUTES
    now = _now()
    last = session.get('last_seen')
    if (minutes and minutes > 0 and isinstance(last, (int, float))
            and now - last > minutes * 60):
        if tenant is not None:
            from auth.auth_routes import record_logout
            with tenant_scope(tenant.id):
                record_logout(reason='idle')
        session.clear()
        message = idle_message(minutes)
        if wants_json:
            return jsonify({'error': 'Session expired', 'message': message}), 401
        return redirect(url_for('login_page', message=message))
    session['last_seen'] = now
    return None
