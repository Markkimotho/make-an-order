# auth/auth_routes.py
from flask import Blueprint, redirect, url_for, session, jsonify
from auth.auth_middleware import login_required

def create_auth_blueprint(oauth):
    auth_bp = Blueprint('auth', __name__)

    google_client = oauth.create_client('google')

    @auth_bp.route('/')
    @login_required
    def hello_world():
        email = session['profile']['email']
        return jsonify({"message": f'Hello, you are logged in as {email}!'}), 200

    @auth_bp.route('/login')
    def login():
        
        print(f"\nSession before Login: {session}\n")
        
        redirect_uri = url_for('auth.authorize', _external=True)
        return google_client.authorize_redirect(redirect_uri)

    @auth_bp.route('/authorize')
    def authorize():
        token = google_client.authorize_access_token()
        user_info = google_client.get('userinfo').json()

        # Store user info and access token in the session
        session['profile'] = user_info
        session['access_token'] = token['access_token']
        session.permanent = True

        print(f"\nSession after OAuth login : {session}\n")
        print(f"\nAccess Token: {session['access_token']}\n")

        return jsonify({"message": "You have logged in successfully!"}), 200
    
    @auth_bp.route('/logout')
    def logout():
        session.clear()
        print(f"\nSession after Logout: {session}\n")
        return jsonify({"message": "You have logged out successfully!"}), 200

    return auth_bp
