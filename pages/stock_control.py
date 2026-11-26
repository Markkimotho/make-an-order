"""Stock pages: batches and expiry (with low stock), recalls, write-offs.

The data comes from /inventory/batches, /inventory/low_stock,
/inventory/batches/<id>/buyers and /inventory/write_offs (api/inventory.py).
"""
from flask import render_template

from auth.auth_middleware import stock_clerk_required
from verticals.features import feature_required


def _context():
    from app import get_user_context        # app imports this module
    from services.stock import WRITE_OFF_REASONS
    return {**get_user_context(), "write_off_reasons": WRITE_OFF_REASONS}


@stock_clerk_required
@feature_required("batches")
def batches_page():
    return render_template("batches.html", **_context())


@stock_clerk_required
@feature_required("recalls")
def recalls_page():
    return render_template("recalls.html", **_context())


@stock_clerk_required
@feature_required("inventory")
def writeoffs_page():
    return render_template("writeoffs.html", **_context())


def register(app):
    app.add_url_rule("/stock-ui/batches", "batches_page", batches_page)
    app.add_url_rule("/stock-ui/recalls", "recalls_page", recalls_page)
    app.add_url_rule("/stock-ui/write-offs", "writeoffs_page", writeoffs_page)
