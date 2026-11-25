"""Staff management within the current tenant.

Owners ('superuser') add, change and remove staff; admins approve or reject.
Every query here is scoped to the bound tenant by auth/tenancy.py. Nobody
can change their own account through this API, and platform admins cannot
be changed by a tenant.
"""
import logging

from flask import Blueprint, jsonify, request, session
from sqlalchemy.exc import IntegrityError

from auth.auth_middleware import admin_api_required, superuser_api_required
from models import ALL_ROLES, User, db
from services.phone import InvalidPhone

users_bp = Blueprint('users_mgmt', __name__)
logger = logging.getLogger(__name__)

STAFF_ROLES = tuple(r for r in ALL_ROLES)   # superuser = tenant owner
STATUSES = ('pending', 'approved', 'rejected')


def _protected(user):
    """An error response if ``user`` must not be changed by this request."""
    if user.id == session.get('user_id'):
        return jsonify({'error': 'You cannot change your own account here'}), 403
    if user.is_platform_admin:
        return jsonify({'error': 'Platform administrators cannot be changed'}), 403
    return None


def _get(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        return None, (jsonify({'error': 'User not found'}), 404)
    return user, None


@users_bp.route('/pending_count', methods=['GET'])
@admin_api_required
def pending_count():
    return jsonify({'count': User.query.filter_by(status='pending').count()})


@users_bp.route('/', methods=['GET'])
@admin_api_required
def list_users():
    if session.get('role') == 'superuser':
        users = User.query.order_by(User.created_at.desc()).all()
    else:
        # Admins only see pending users (approval workflow).
        users = User.query.filter_by(status='pending').order_by(User.created_at.desc()).all()
    return jsonify([u.to_dict() for u in users])


@users_bp.route('/<int:user_id>', methods=['PUT'])
@superuser_api_required
def update_user(user_id):
    user, err = _get(user_id)
    if err:
        return err
    blocked = _protected(user)
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    if data.get('role') in STAFF_ROLES:
        user.role = data['role']
    if data.get('status') in STATUSES:
        user.status = data['status']
    if 'phone_number' in data:
        try:
            user.phone_number = data['phone_number']
        except InvalidPhone:
            return jsonify({'error': 'Invalid Kenyan phone number'}), 400
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'Another staff member already uses that phone number'}), 400
    return jsonify({'message': 'User updated', 'user': user.to_dict()})


def _set_status(user_id, status):
    user, err = _get(user_id)
    if err:
        return err
    blocked = _protected(user)
    if blocked:
        return blocked
    if user.status == status:
        return jsonify({'message': f'User is already {status}', 'user': user.to_dict()}), 200
    user.status = status
    db.session.commit()
    logger.info(f"User {user.email} {status} by {session.get('profile', {}).get('email')}")
    return jsonify({'message': f'{user.name or user.email} {status}', 'user': user.to_dict()})


@users_bp.route('/<int:user_id>/approve', methods=['POST'])
@admin_api_required
def approve_user(user_id):
    return _set_status(user_id, 'approved')


@users_bp.route('/<int:user_id>/reject', methods=['POST'])
@admin_api_required
def reject_user(user_id):
    return _set_status(user_id, 'rejected')


@users_bp.route('/', methods=['POST'])
@superuser_api_required
def create_user():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    name = (data.get('name') or '').strip()
    role = data.get('role') if data.get('role') in STAFF_ROLES else 'user'
    if not email:
        return jsonify({'error': 'Email is required'}), 400
    try:
        user = User(email=email, name=name or None, phone_number=data.get('phone_number'),
                    role=role, status='approved')
    except InvalidPhone:
        return jsonify({'error': 'Invalid Kenyan phone number'}), 400
    if data.get('password'):
        if len(data['password']) < 8:
            return jsonify({'error': 'Password must be at least 8 characters'}), 400
        user.set_password(data['password'])
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        # Emails are unique across all businesses, phones within one.
        db.session.rollback()
        return jsonify({'error': 'A user with this email or phone number already exists'}), 400
    return jsonify({'message': 'User created', 'user': user.to_dict()}), 201


@users_bp.route('/<int:user_id>', methods=['DELETE'])
@superuser_api_required
def delete_user(user_id):
    user, err = _get(user_id)
    if err:
        return err
    blocked = _protected(user)
    if blocked:
        return blocked
    email = user.email
    db.session.delete(user)
    db.session.commit()
    logger.info(f"User {email} deleted by {session.get('profile', {}).get('email')}")
    return jsonify({'message': f'User {email} deleted'})
