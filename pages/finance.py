"""Money pages: expenses and cashier reconciliation.

The daily report keeps its route in app.py (``reports_dashboard_page``);
its numbers and these pages' come from /reports/* (api/finance_reports.py).
"""
from flask import render_template

from auth.auth_middleware import finance_required
from verticals.features import feature_required


@finance_required
@feature_required('expenses')
def expenses_page():
    from app import get_user_context
    return render_template('expenses.html', **get_user_context())


@finance_required
@feature_required('finance')
def reconciliation_page():
    from app import get_user_context
    return render_template('reconciliation.html', **get_user_context())


def register(app):
    app.add_url_rule('/money-ui/expenses', endpoint='expenses_page', view_func=expenses_page)
    app.add_url_rule('/money-ui/reconciliation', endpoint='reconciliation_page',
                     view_func=reconciliation_page)
