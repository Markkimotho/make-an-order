"""Platform operator pages: list tenants and choose one to act in.

URL prefix: ``/platform``. Platform admins are users with
``is_platform_admin`` (set at login from PLATFORM_ADMIN_EMAILS).
"""
import logging

from flask import Blueprint, abort, redirect, render_template, session, url_for

from auth.auth_middleware import platform_admin_required
from auth.tenancy import unscoped
from models import Tenant, db

platform_bp = Blueprint('platform', __name__)
logger = logging.getLogger(__name__)


@platform_bp.route('/tenants', methods=['GET'])
@platform_admin_required
def tenants():
    from app import get_user_context
    with unscoped():
        rows = [t.to_dict() for t in Tenant.query.order_by(Tenant.created_at.desc()).all()]
    return render_template('platform_tenants.html', tenants=rows,
                           acting_tenant_id=session.get('tenant_id'), **get_user_context())


@platform_bp.route('/tenants/<slug>/act', methods=['POST'])
@platform_admin_required
def act_as(slug):
    with unscoped():
        tenant = db.session.query(Tenant.id).filter_by(slug=slug).first()
    if tenant is None:
        abort(404)
    session['tenant_id'] = tenant.id
    session['role'] = 'superuser'
    logger.info(f"Platform admin {session.get('profile', {}).get('email')} acting as {slug}")
    return redirect(url_for('dashboard'))
