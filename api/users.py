from flask import Blueprint, jsonify, request, session
from models import db, User, SUPERUSER_EMAIL
from auth.auth_middleware import admin_api_required, superuser_api_required
import logging

users_bp = Blueprint('users_mgmt', __name__)
logger = logging.getLogger(__name__)


@users_bp.route('/pending_count', methods=['GET'])
@admin_api_required
def pending_count():
    count = User.query.filter_by(status='pending').count()
    return jsonify({'count': count})


@users_bp.route('/', methods=['GET'])
@admin_api_required
def list_users():
    role = session.get('role')
    if role == 'superuser':
        users = User.query.order_by(User.created_at.desc()).all()
    else:
        # Admins only see pending users (for approval workflow)
        users = User.query.filter_by(status='pending').order_by(User.created_at.desc()).all()
    return jsonify([u.to_dict() for u in users])


@users_bp.route('/<int:user_id>', methods=['PUT'])
@superuser_api_required
def update_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    if user.email == SUPERUSER_EMAIL:
        return jsonify({'error': 'Cannot modify the primary superuser'}), 403
    data = request.get_json(silent=True) or {}
    if 'role' in data and data['role'] in ('superuser', 'admin', 'user'):
        user.role = data['role']
    if 'status' in data and data['status'] in ('pending', 'approved', 'rejected'):
        user.status = data['status']
    if 'phone_number' in data:
        user.phone_number = (data['phone_number'] or '').strip() or None
    db.session.commit()
    return jsonify({'message': 'User updated', 'user': user.to_dict()})


@users_bp.route('/<int:user_id>/approve', methods=['POST'])
@admin_api_required
def approve_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    if user.email == SUPERUSER_EMAIL:
        return jsonify({'error': 'Cannot modify the primary superuser'}), 403
    if user.status == 'approved':
        return jsonify({'message': 'User is already approved'}), 200
    user.status = 'approved'
    db.session.commit()
    logger.info(f"User {user.email} approved by {session.get('profile', {}).get('email')}")
    return jsonify({'message': f'{user.name or user.email} approved successfully', 'user': user.to_dict()})


@users_bp.route('/<int:user_id>/reject', methods=['POST'])
@admin_api_required
def reject_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    if user.email == SUPERUSER_EMAIL:
        return jsonify({'error': 'Cannot modify the primary superuser'}), 403
    user.status = 'rejected'
    db.session.commit()
    logger.info(f"User {user.email} rejected by {session.get('profile', {}).get('email')}")
    return jsonify({'message': f'{user.name or user.email} rejected', 'user': user.to_dict()})


@users_bp.route('/', methods=['POST'])
@superuser_api_required
def create_user():
    data  = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    name  = (data.get('name') or '').strip()
    phone = (data.get('phone_number') or '').strip() or None
    role  = data.get('role', 'user')
    if not email:
        return jsonify({'error': 'Email is required'}), 400
    if role not in ('admin', 'user'):
        role = 'user'
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'A user with this email already exists'}), 400
    user = User(email=email, name=name or None, phone_number=phone, role=role, status='approved')
    db.session.add(user)
    db.session.commit()
    return jsonify({'message': 'User created', 'user': user.to_dict()}), 201


@users_bp.route('/<int:user_id>', methods=['DELETE'])
@superuser_api_required
def delete_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    if user.email == SUPERUSER_EMAIL:
        return jsonify({'error': 'Cannot delete the primary superuser'}), 403
    email = user.email
    db.session.delete(user)
    db.session.commit()
    logger.info(f"User {email} deleted by {session.get('profile', {}).get('email')}")
    return jsonify({'message': f'User {email} deleted'})
