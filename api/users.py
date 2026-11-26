"""Staff management within the current tenant.

Owners ('superuser') add, change and remove staff; admins approve or reject.
Every query here is scoped to the bound tenant by auth/tenancy.py. Nobody
can change their own account through this API, and platform admins cannot
be changed by a tenant.

Admins also unlock accounts locked by failed sign-ins and read the activity
log (the tenant's audit rows, newest first).
"""
import logging
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, session
from sqlalchemy.exc import IntegrityError

from auth.auth_middleware import admin_api_required, superuser_api_required
from models import ALL_ROLES, AuditLog, User, db
from services import audit_service
from services.phone import InvalidPhone
from verticals.features import feature_required

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


def _row(user, now=None):
    """to_dict plus the sign-in fields the users page shows."""
    now = now or datetime.utcnow()
    locked = user.locked_until is not None and user.locked_until > now
    return {
        **user.to_dict(),
        'last_login_at': user.last_login_at.isoformat() if user.last_login_at else None,
        'failed_logins': user.failed_logins or 0,
        'locked': locked,
        'locked_until': user.locked_until.isoformat() if locked else None,
    }


def _bad_role(role):
    return role not in (None, '') and role not in ALL_ROLES


def _role_error():
    return jsonify({'error': f'Role must be one of {", ".join(ALL_ROLES)}'}), 400


def _get(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        return None, (jsonify({'error': 'User not found'}), 404)
    return user, None


@users_bp.route('/pending_count', methods=['GET'])
@admin_api_required
def pending_count():
    return jsonify({'count': User.query.filter_by(status='pending').count()})


@users_bp.route('/roles', methods=['GET'])
@admin_api_required
def roles():
    """Role choices with a one-line description of what each can do."""
    from auth.permissions import role_choices
    return jsonify(role_choices())


@users_bp.route('/', methods=['GET'])
@admin_api_required
def list_users():
    now = datetime.utcnow()
    if session.get('role') == 'superuser':
        users = User.query.order_by(User.created_at.desc()).all()
    else:
        # Admins see who waits for approval and who is locked out.
        users = (User.query.filter((User.status == 'pending') | (User.locked_until > now))
                 .order_by(User.created_at.desc()).all())
    return jsonify([_row(u, now) for u in users])


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
    if _bad_role(data.get('role')):
        return _role_error()
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
    if _bad_role(data.get('role')):
        return _role_error()
    role = data.get('role') or 'user'
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


@users_bp.route('/<int:user_id>/unlock', methods=['POST'])
@admin_api_required
def unlock_user(user_id):
    """Clear a lock set by failed sign-ins, and the failure count."""
    user, err = _get(user_id)
    if err:
        return err
    before = {'failed_logins': user.failed_logins or 0,
              'locked_until': user.locked_until.isoformat() if user.locked_until else None}
    user.failed_logins = 0
    user.locked_until = None
    audit_service.log(session.get('user_id'), 'user.unlock', 'User', user.id, before,
                      {'email': user.email}, ip_address=request.remote_addr)
    db.session.commit()
    logger.info(f"User {user.id} unlocked by user {session.get('user_id')}")
    return jsonify({'message': f'{user.name or user.email} can sign in again',
                    'user': _row(user)})


# --- activity log -------------------------------------------------------------

ACTIVITY_PAGE_SIZE = 50

# Readable names for the common actions; others are spelled out from the key.
ACTION_LABELS = {
    'login': 'Signed in',
    'login.failed': 'Failed sign-in',
    'login.locked': 'Account locked',
    'logout': 'Signed out',
    'user.unlock': 'Account unlocked',
    'transaction.complete': 'Sale completed',
    'transaction.void': 'Sale voided',
    'transaction.refund': 'Sale refunded',
    'discount.line': 'Line discount',
    'discount.sale': 'Sale discount',
    'discount.override': 'Discount over the cap (manager code)',
    'payment.manual': 'M-Pesa code typed',
    'payment.bank': 'Bank payment recorded',
    'payment.verify': 'Payment verified',
    'payment.reject': 'Payment rejected',
    'payment.stk_push': 'M-Pesa prompt sent',
    'shift.open': 'Shift opened',
    'shift.close': 'Shift closed',
    'shift.cash_in': 'Cash put in the drawer',
    'shift.cash_out': 'Cash taken from the drawer',
    'inventory.stock_adjustment': 'Stock adjusted',
    'stock.adjust': 'Stock adjusted',
    'stock.writeoff': 'Stock written off',
    'po.create': 'Purchase order created',
    'po.receive': 'Purchase order received',
    'prescription.dispense': 'Prescription dispensed',
    'customer.quick_add': 'Customer added at the till',
}
# Filters offered on the page; a trailing * matches every action with that prefix.
ACTION_FILTERS = (
    ('login', 'Signed in'), ('login.failed', 'Failed sign-ins'), ('login.locked', 'Accounts locked'),
    ('logout', 'Signed out'), ('transaction.*', 'Sales, voids, refunds'),
    ('discount.*', 'Discounts'), ('payment.*', 'Payments'), ('shift.*', 'Shifts and cash'),
    ('stock.*', 'Stock'), ('inventory.*', 'Inventory'), ('user.*', 'Staff accounts'),
)
_SECRET_WORDS = ('password', 'otp', 'secret', 'token', 'hash', 'passkey')


def action_label(action):
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    return (action or '').replace('_', ' ').replace('.', ': ').capitalize()


def _clean(value):
    """Audit values as shown to admins, with any secret-looking key dropped."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()
                if not any(word in str(k).lower() for word in _SECRET_WORDS)}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _activity_query():
    """AuditLog query with ?user_id=&action=&from=&to= applied, or ValueError."""
    from api.staff import BadRange, nairobi_day_bounds, parse_range
    args = request.args
    q = AuditLog.query
    if args.get('user_id'):
        try:
            q = q.filter(AuditLog.user_id == int(args['user_id']))
        except ValueError:
            raise ValueError('user_id must be a number')
    action = (args.get('action') or '').strip()
    if action.endswith('*'):
        q = q.filter(AuditLog.action.startswith(action.rstrip('*'), autoescape=True))
    elif action:
        q = q.filter(AuditLog.action == action)
    if args.get('from') or args.get('to'):
        try:
            d_from, d_to = parse_range(args, default_days=366)
        except BadRange as e:
            raise ValueError(str(e))
        start, end = nairobi_day_bounds(d_from, d_to)
        q = q.filter(AuditLog.created_at >= start, AuditLog.created_at < end)
    return q


def _failed_logins_24h():
    since = datetime.utcnow() - timedelta(hours=24)
    rows = AuditLog.query.filter(AuditLog.action == 'login.failed',
                                 AuditLog.created_at >= since).all()
    by_user = {}
    for r in rows:
        details = r.new_value if isinstance(r.new_value, dict) else {}
        entry = by_user.setdefault(r.user_id, {'user_id': r.user_id, 'email': details.get('email'),
                                               'count': 0, 'last_at': None})
        entry['count'] += 1
        stamp = r.created_at.isoformat() if r.created_at else None
        if stamp and (entry['last_at'] is None or stamp > entry['last_at']):
            entry['last_at'] = stamp
    return {
        'count': len(rows),
        'accounts_locked_now': User.query.filter(User.locked_until > datetime.utcnow()).count(),
        'by_user': sorted(by_user.values(), key=lambda e: (-e['count'], e['email'] or '')),
    }


@users_bp.route('/activity', methods=['GET'])
@admin_api_required
@feature_required('activity')
def activity():
    """``GET /users/activity?user_id=&action=&from=&to=&page=``

    This business's audit rows, newest first, 50 a page. ``action`` is an
    exact action or a prefix ending in ``*`` (``discount.*``); dates are
    Nairobi days (YYYY-MM-DD).
    """
    try:
        q = _activity_query()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    total = q.count()
    rows = (q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset((page - 1) * ACTIVITY_PAGE_SIZE).limit(ACTIVITY_PAGE_SIZE).all())
    ids = {r.user_id for r in rows if r.user_id is not None}
    names = ({u.id: (u.name or u.email) for u in User.query.filter(User.id.in_(ids))}
             if ids else {})
    return jsonify({
        'rows': [{
            'id': r.id,
            'created_at': r.created_at.isoformat() if r.created_at else None,
            'user_id': r.user_id,
            'user_name': names.get(r.user_id),
            'action': r.action,
            'action_label': action_label(r.action),
            'entity_type': r.entity_type,
            'entity_id': r.entity_id,
            'details': _clean(r.new_value),
            'ip_address': r.ip_address,
        } for r in rows],
        'page': page,
        'per_page': ACTIVITY_PAGE_SIZE,
        'total': total,
        'pages': (total + ACTIVITY_PAGE_SIZE - 1) // ACTIVITY_PAGE_SIZE,
        'failed_logins_24h': _failed_logins_24h(),
    })
