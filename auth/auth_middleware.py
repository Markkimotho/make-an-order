from functools import wraps
from flask import request, redirect, url_for, session

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # print(f"\nSession: {session}\n") # Removed print, use logger
        if 'profile' not in session:
            # For API calls, return 401 JSON. For UI, redirect.
            # This middleware is currently used on API and should distinguish.
            # A simple way for Flask APIs is to check request headers,
            # but for this setup, redirecting to login is the default for unauthorized.
            # If a strict API 401 is needed, modify to:
            # from flask import abort
            # if 'profile' not in session: abort(401)
            # For this UI-integrated app, redirect is fine for protected routes accessed via browser.
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function