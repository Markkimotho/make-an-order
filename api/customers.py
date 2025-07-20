from flask import Blueprint, request, jsonify  # type: ignore
from models import db, Customer
from sqlalchemy.exc import IntegrityError
from auth.auth_middleware import login_required # Import login_required
import logging

customers_bp = Blueprint('customers', __name__)
logger = logging.getLogger(__name__)

@customers_bp.route('/register', methods=['POST'])
@login_required # Protect this route
def register_customer():
    """
    Function for registering a new customer on the route `/customers/register`
    """
    data = request.json
    name = data.get("name")
    phone_number = data.get("phone_number")
    code = data.get("code")

    if not name or not phone_number or not code:
        logger.warning("Attempt to register customer with missing data.")
        return jsonify({"error": "Missing customer details (name, phone_number, code are required)"}), 400

    try:
        new_customer = Customer(name=name, phone_number=phone_number, code=code)
        db.session.add(new_customer)
        db.session.commit()
        logger.info(f"Customer registered successfully: {new_customer.id}")
        return jsonify({"message": "Customer registered successfully", "customer_id": new_customer.id}), 201
    except IntegrityError:
        db.session.rollback()
        logger.error(f"Integrity error: Phone number '{phone_number}' or code '{code}' already exists.", exc_info=True)
        return jsonify({"error": "Phone number or code already exists"}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error registering customer: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500


@customers_bp.route('/view_customers', methods=['GET'])
@login_required # Protect this route
def view_customers():
    """
    Function for viewing all the customers on the route `/customers/view_customers`
    """
    try:
        customers = Customer.query.all()
        customer_list = [
            {
                "id": customer.id,
                "name": customer.name,
                "phone_number": customer.phone_number,
                "code": customer.code
            } for customer in customers
        ]
        logger.info(f"Retrieved {len(customer_list)} customers.")
        return jsonify(customer_list), 200
    except Exception as e:
        logger.error(f"Error viewing all customers: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500

@customers_bp.route('/view_customers/<int:id>', methods=['GET'])
@login_required # Protect this route
def view_customer(id):
    """
    Function for viewing a specific customer on the route `/customers/view_customers/<id>`
    """
    try:
        customer = db.session.get(Customer, id)
        if not customer:
            logger.warning(f"Customer with ID {id} not found.")
            return jsonify({"error": "Customer not found"}), 404
        customer_data = {
            "id": customer.id,
            "name": customer.name,
            "phone_number": customer.phone_number,
            "code": customer.code
        }
        logger.info(f"Retrieved customer with ID: {id}")
        return jsonify(customer_data), 200
    except Exception as e:
        logger.error(f"Error viewing customer with ID {id}: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500

@customers_bp.route('/update_customers/<int:id>', methods=['PUT'])
@login_required # Protect this route
def update_customer(id):
    """
    Function for updating a customer's details on the route `/customers/update_customers/<id>`
    """
    try:
        customer = db.session.get(Customer, id)
        if not customer:
            logger.warning(f"Attempt to update non-existent customer with ID: {id}")
            return jsonify({"error": "Customer not found"}), 404
        data = request.json

        # Check if any data is provided for update
        if not data:
            logger.warning(f"No update data provided for customer ID: {id}")
            return jsonify({"error": "No update data provided"}), 400

        customer.name = data.get("name", customer.name)
        customer.phone_number = data.get("phone_number", customer.phone_number)
        customer.code = data.get("code", customer.code)

        db.session.commit()
        logger.info(f"Customer with ID {id} updated successfully.")
        return jsonify({"message": "Customer updated successfully"}), 200
    except IntegrityError:
        db.session.rollback()
        # This catch is for unique constraint violations (phone_number, code)
        logger.error(f"Integrity error during update for customer ID {id}: Phone number or code already exists.", exc_info=True)
        return jsonify({"error": "Phone number or code already exists for another customer"}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating customer with ID {id}: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500

@customers_bp.route('/delete_customers/<int:id>', methods=['DELETE'])
@login_required # Protect this route
def delete_customer(id):
    """
    Function for deleting a customer on the route `/customers/delete_customers/<id>`
    """
    try:
        customer = db.session.get(Customer, id)
        if not customer:
            logger.warning(f"Attempt to delete non-existent customer with ID: {id}")
            return jsonify({"error": "Customer not found"}), 404
        db.session.delete(customer)
        db.session.commit()
        logger.info(f"Customer with ID {id} deleted successfully.")
        return jsonify({"message": "Customer deleted successfully"}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting customer with ID {id}: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500