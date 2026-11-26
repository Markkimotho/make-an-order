"""The staff "Online orders" page: orders from the online shop, WhatsApp
and public links, newest first, with ready / dispatched / collected /
cancel actions.

Data and actions come from /orders/online and /orders/<id>/fulfilment
(api/orders.py). Cashiers and pharmacists run the counter, so they see it
as well as admins.
"""
from flask import render_template

from auth.auth_middleware import cashier_required
from verticals.features import feature_required


@cashier_required
@feature_required('orders')
def orders_page():
    from app import get_user_context
    return render_template('orders.html', **get_user_context())


def register(app):
    app.add_url_rule('/orders-ui', 'orders_page', orders_page)
