"""Staff pages: roles and permissions, the activity log, performance.

Data comes from the JSON APIs (/users/activity, /staff/performance); the
permission matrix is rendered server-side from auth/permissions.py.
"""
from flask import render_template

from auth.auth_middleware import admin_required, finance_required
from verticals.features import feature_required


@admin_required
@feature_required('staff')
def permissions_page():
    from app import get_user_context
    from auth.permissions import matrix
    return render_template('staff_permissions.html', matrix=matrix(), **get_user_context())


@admin_required
@feature_required('activity')
def activity_page():
    from app import get_user_context
    from api.users import ACTION_FILTERS
    from models import User
    staff = User.query.order_by(User.name, User.email).all()
    return render_template('staff_activity.html', action_filters=ACTION_FILTERS,
                           staff=[{'id': u.id, 'name': u.name or u.email} for u in staff],
                           **get_user_context())


@finance_required
@feature_required('staff')
def performance_page():
    from app import get_user_context
    return render_template('staff_performance.html', **get_user_context())


def register(app):
    app.add_url_rule('/staff-ui/permissions', 'permissions_page', permissions_page)
    app.add_url_rule('/staff-ui/activity', 'activity_page', activity_page)
    app.add_url_rule('/staff-ui/performance', 'performance_page', performance_page)
