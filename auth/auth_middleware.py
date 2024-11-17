from functools import wraps
from flask import request, redirect, url_for, session

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        print(f"\nSession: {session}\n")
        if 'profile' not in session:
            return redirect(url_for('auth.login'))  # redirect to login if not authenticated
        return f(*args, **kwargs)
    return decorated_function
