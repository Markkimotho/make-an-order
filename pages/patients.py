"""Pharmacy pages: the patient list and one patient's medication history.

The data comes from /pharmacy/patients and /pharmacy/patients/<id>/medications
(api/pharmacy.py); these views only render the pages.
"""
from flask import render_template

from auth.auth_middleware import pharmacist_required
from models import Customer, db
from verticals.features import feature_required


def _ctx():
    from app import get_user_context
    return get_user_context()


@pharmacist_required
@feature_required('patients')
def patients_page():
    return render_template('patients.html', **_ctx())


@pharmacist_required
@feature_required('patients')
def patient_page(customer_id):
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return render_template('404.html', message="Patient not found", **_ctx()), 404
    return render_template('patient.html', patient=customer, **_ctx())


def register(app):
    app.add_url_rule('/patients-ui', 'patients_page', patients_page)
    app.add_url_rule('/patients-ui/<int:customer_id>', 'patient_page', patient_page)
