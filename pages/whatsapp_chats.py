"""The staff "WhatsApp chats" page: customers who asked to talk to a person,
answered from the app. Data and actions: api/whatsapp_chats.py."""
from flask import render_template

from auth.auth_middleware import cashier_required
from verticals.features import feature_required


@cashier_required
@feature_required('online_shop')
def whatsapp_chats_page():
    from app import get_user_context
    return render_template('whatsapp_chats.html', **get_user_context())


def register(app):
    app.add_url_rule('/whatsapp-ui', 'whatsapp_chats_page', whatsapp_chats_page)
