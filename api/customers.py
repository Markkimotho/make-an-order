from flask import Blueprint, request, jsonify, session  # type: ignore
from models import (
    db, Customer, Order, Transaction, LoyaltyTransaction,
    PAY_CASH, PAY_MPESA, PAY_CARD,
)
from sqlalchemy.exc import IntegrityError
from auth.auth_middleware import (
    admin_api_required, superuser_api_required, cashier_api_required, login_required,
)
from services import audit_service, loyalty_service
from services.phone import InvalidPhone
from decimal import Decimal
import logging

customers_bp = Blueprint('customers', __name__)
logger = logging.getLogger(__name__)


@customers_bp.route('/register', methods=['POST'])
@admin_api_required
def register_customer():
    data         = request.json or {}
    name         = data.get("name", "").strip()
    phone_number = data.get("phone_number", "").strip()
    code         = data.get("code", "").strip()
    email        = data.get("email", "").strip() or None

    if not name or not phone_number or not code:
        return jsonify({"error": "name, phone_number, and code are required"}), 400

    try:
        customer = Customer(name=name, phone_number=phone_number, code=code, email=email)
        db.session.add(customer)
        db.session.commit()
        logger.info(f"Customer {customer.id} registered.")
        return jsonify({"message": "Customer registered successfully", "customer_id": customer.id}), 201
    except InvalidPhone:
        db.session.rollback()
        return jsonify({"error": "Invalid Kenyan phone number"}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Phone number or code already exists"}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error registering customer: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500


@customers_bp.route('/view_customers', methods=['GET'])
@admin_api_required
def view_customers():
    """Return customers with pagination. Query params: page (1-based), per_page (default 50)."""
    try:
        page     = max(1, int(request.args.get('page', 1)))
        per_page = min(int(request.args.get('per_page', 50)), 200)

        q = Customer.query.order_by(Customer.name)
        total = q.count()
        customers = q.offset((page - 1) * per_page).limit(per_page).all()
        logger.info(f"Retrieved {len(customers)} customers page {page}.")

        return jsonify({
            'customers': [c.to_dict() for c in customers],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'pages': (total + per_page - 1) // per_page,
            }
        }), 200
    except Exception as e:
        logger.error(f"Error viewing customers: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500


@customers_bp.route('/view_customers/<int:id>', methods=['GET'])
@admin_api_required
def view_customer_detail(id):
    """Get a specific customer with their order history and stats."""
    try:
        customer = db.session.get(Customer, id)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404

        orders = Order.query.filter_by(customer_id=id).order_by(Order.time.desc()).all()
        total_spent = sum(float(o.amount) for o in orders if o.status not in ('cancelled', 'refunded'))

        return jsonify({
            'customer': customer.to_dict(),
            'orders': [o.to_dict() for o in orders],
            'stats': {
                'total_orders': len(orders),
                'total_spent': round(total_spent, 2),
                'completed_orders': len([o for o in orders if o.status == 'completed']),
                'pending_orders': len([o for o in orders if o.status == 'pending']),
            }
        }), 200
    except Exception as e:
        logger.error(f"Error fetching customer {id}: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500


@customers_bp.route('/update_customers/<int:id>', methods=['PUT'])
@superuser_api_required
def update_customer(id):
    try:
        customer = db.session.get(Customer, id)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404
        data = request.json
        if not data:
            return jsonify({"error": "No update data provided"}), 400

        customer.name         = data.get("name",         customer.name)
        customer.email        = data.get("email",        customer.email)
        customer.phone_number = data.get("phone_number", customer.phone_number)
        customer.code         = data.get("code",         customer.code)

        db.session.commit()
        logger.info(f"Customer {id} updated.")
        return jsonify({"message": "Customer updated successfully"}), 200
    except InvalidPhone:
        db.session.rollback()
        return jsonify({"error": "Invalid Kenyan phone number"}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Phone number or code already exists for another customer"}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating customer {id}: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500


@customers_bp.route('/delete_customers/<int:id>', methods=['DELETE'])
@superuser_api_required
def delete_customer(id):
    try:
        customer = db.session.get(Customer, id)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404
        db.session.delete(customer)
        db.session.commit()
        logger.info(f"Customer {id} deleted.")
        return jsonify({"message": "Customer deleted successfully"}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting customer {id}: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500


# ===========================================================================
# Loyalty
# ===========================================================================

@customers_bp.route('/<int:id>/loyalty', methods=['GET'])
@login_required
def customer_loyalty(id):
    """Loyalty balance + recent ledger entries."""
    customer = db.session.get(Customer, id)
    if not customer:
        return jsonify({'error': 'Customer not found'}), 404
    history = (LoyaltyTransaction.query.filter_by(customer_id=id)
               .order_by(LoyaltyTransaction.created_at.desc())
               .limit(50).all())
    return jsonify({
        'customer_id': customer.id,
        'balance': int(customer.loyalty_points or 0),
        'history': [h.to_dict() for h in history],
    }), 200


@customers_bp.route('/<int:id>/loyalty/redeem', methods=['POST'])
@cashier_api_required
def customer_loyalty_redeem(id):
    """Redeem loyalty points as a discount on an open transaction.

    Body: ``{"points": 50, "transaction_id": 12}``.
    Each point = KES 1 of discount (this matches the simple ratio used by
    :func:`services.loyalty_service.award_points` in reverse — adjust if
    business rule changes).
    """
    customer = db.session.get(Customer, id)
    if not customer:
        return jsonify({'error': 'Customer not found'}), 404
    body = request.get_json(silent=True) or {}
    try:
        points = int(body.get('points', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'points must be an integer'}), 400
    txn_id = body.get('transaction_id')
    if points <= 0 or not txn_id:
        return jsonify({'error': 'points (>0) and transaction_id are required'}), 400

    txn = db.session.get(Transaction, txn_id)
    if not txn:
        return jsonify({'error': 'Transaction not found'}), 404
    if txn.customer_id != customer.id:
        return jsonify({'error': 'Transaction does not belong to this customer'}), 400

    try:
        loyalty_service.redeem_points(customer.id, txn.id, points)
    except loyalty_service.LoyaltyError as e:
        return jsonify({'error': str(e)}), 400

    # Apply as transaction-level discount (1 point = 1 KES).
    from api.pos import _recalc_totals  # local import to avoid cycle
    existing = Decimal(str(txn.discount_amount or 0))
    _recalc_totals(txn, discount_override=existing + Decimal(points))

    audit_service.log(session.get('user_id'), 'loyalty.redeem',
                      'Customer', customer.id, None,
                      {'points': points, 'transaction_id': txn.id})
    db.session.commit()
    return jsonify({'message': 'Points redeemed',
                    'balance': int(customer.loyalty_points or 0),
                    'transaction': txn.to_dict()}), 200


# ===========================================================================
# Credit
# ===========================================================================

@customers_bp.route('/<int:id>/credit', methods=['GET'])
@login_required
def customer_credit(id):
    """Customer credit summary: limit, balance, recent credit-paid transactions."""
    customer = db.session.get(Customer, id)
    if not customer:
        return jsonify({'error': 'Customer not found'}), 404
    recent = (Transaction.query
              .filter(Transaction.customer_id == id,
                      Transaction.payment_method == 'credit')
              .order_by(Transaction.created_at.desc()).limit(50).all())
    return jsonify({
        'customer_id': customer.id,
        'credit_limit':   float(customer.credit_limit or 0),
        'credit_balance': float(customer.credit_balance or 0),
        'available':      float((customer.credit_limit or 0) - (customer.credit_balance or 0)),
        'recent_credit_sales': [t.to_dict() for t in recent],
    }), 200


@customers_bp.route('/<int:id>/credit/payment', methods=['POST'])
@cashier_api_required
def customer_credit_payment(id):
    """Record a payment against a customer's credit balance.

    Body: ``{"amount": 500.00, "payment_method": "cash"|"mpesa"|"card",
             "mpesa_ref": "..."}``.
    """
    customer = db.session.get(Customer, id)
    if not customer:
        return jsonify({'error': 'Customer not found'}), 404
    body = request.get_json(silent=True) or {}
    try:
        amount = Decimal(str(body.get('amount', 0)))
    except Exception:
        return jsonify({'error': 'amount must be numeric'}), 400
    if amount <= 0:
        return jsonify({'error': 'amount must be > 0'}), 400
    method = (body.get('payment_method') or '').lower()
    if method not in (PAY_CASH, PAY_MPESA, PAY_CARD):
        return jsonify({'error': 'payment_method must be cash, mpesa, or card'}), 400

    current = Decimal(str(customer.credit_balance or 0))
    if amount > current:
        return jsonify({'error': f'amount exceeds outstanding balance ({current})'}), 400
    customer.credit_balance = (current - amount).quantize(Decimal('0.01'))

    audit_service.log(session.get('user_id'), 'credit.payment',
                      'Customer', customer.id,
                      {'credit_balance': float(current)},
                      {'amount': float(amount), 'method': method,
                       'mpesa_ref': body.get('mpesa_ref'),
                       'credit_balance': float(customer.credit_balance)})
    db.session.commit()
    return jsonify({'message': 'Payment recorded',
                    'credit_balance': float(customer.credit_balance)}), 200
