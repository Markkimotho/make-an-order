from flask import Blueprint, request, jsonify  # type: ignore
from models import db, Customer
from sqlalchemy.exc import IntegrityError
from auth.auth_middleware import admin_api_required, superuser_api_required
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
