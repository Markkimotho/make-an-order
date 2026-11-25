"""Pricing page: markup per item, auto or manual mode, bulk "apply
suggested prices", the business's default markup and rounding, and items
priced below cost. Data comes from /inventory/pricing (api/purchasing.py)."""
from flask import render_template

from auth.auth_middleware import admin_required
from verticals.features import feature_required


@admin_required
@feature_required('pricing')
def pricing_page():
    from app import get_user_context
    return render_template('pricing.html', **get_user_context())


def register(app):
    app.add_url_rule('/purchasing-ui/pricing', endpoint='pricing_page', view_func=pricing_page)
