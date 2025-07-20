from flask import Blueprint, redirect, url_for, session, jsonify
from auth.auth_middleware import login_required
import logging

logger = logging.getLogger(__name__)

def create_auth_blueprint(oauth):
    auth_bp = Blueprint('auth', __name__)

    google_client = oauth.create_client('google')

    @auth_bp.route('/')
    @login_required
    def hello_world():
        # This route is hit after successful authentication and redirects to dashboard.
        # The main logic flow now goes: / -> /login -> Google -> /authorize -> /dashboard
        user_email = session['profile']['email']
        logger.info(f"User {user_email} accessed protected root.")
        return redirect(url_for('dashboard')) # Redirect to the main dashboard UI


    @auth_bp.route('/login')
    def login():
        logger.info("Initiating Google OAuth login.")
        # print(f"\nSession before Login: {session}\n") # Removed print, use logger
        redirect_uri = url_for('auth.authorize', _external=True)
        return google_client.authorize_redirect(redirect_uri)

    @auth_bp.route('/authorize')
    def authorize():
        try:
            token = google_client.authorize_access_token()
            user_info = google_client.get('userinfo').json()

            # Store user info and access token in the session
            session['profile'] = user_info
            session['access_token'] = token['access_token']
            session.permanent = True

            logger.info(f"User {user_info.get('email')} logged in successfully via OAuth.")
            # print(f"\nSession after OAuth login : {session}\n") # Removed print, use logger
            # print(f"\nAccess Token: {session['access_token']}\n") # Removed print, use logger

            # Redirect to the main dashboard UI after successful login
            return redirect(url_for('dashboard'))
        except Exception as e:
            logger.error(f"OAuth authorization failed: {e}", exc_info=True)
            # Redirect back to login with an error message or a general error page
            return redirect(url_for('index', message="Login failed. Please try again."))


    @auth_bp.route('/logout')
    def logout():
        user_email = session['profile']['email'] if 'profile' in session else 'unknown'
        session.clear()
        logger.info(f"User {user_email} logged out successfully.")
        # print(f"\nSession after Logout: {session}\n") # Removed print, use logger
        return redirect(url_for('index', message="You have logged out successfully!")) # Redirect to index page with message

    return auth_bp