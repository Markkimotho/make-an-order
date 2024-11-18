from flask import Blueprint, request, jsonify  # type: ignore
from models import db, Customer
from sqlalchemy.exc import IntegrityError

customers_bp = Blueprint('customers', __name__)

@customers_bp.route('/register', methods=['POST'])
def register_customer():
    """
    Function for registering a new customer on the route `/customers/register`
    """
    data = request.json
    name = data.get("name")
    phone_number = data.get("phone_number")
    code = data.get("code")

    try:
        new_customer = Customer(name=name, phone_number=phone_number, code=code)
        db.session.add(new_customer)
        db.session.commit()
        return jsonify({"message": "Customer registered successfully", "customer_id": new_customer.id}), 201
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Phone number or code already exists"}), 400

@customers_bp.route('/view_customers', methods=['GET'])
def view_customers():
    """
    Function for viewing all the customers on the route `/customers/view_customers`
    """
    customers = Customer.query.all()
    customer_list = [
        {
            "id": customer.id, 
            "name": customer.name, 
            "phone_number": customer.phone_number, 
            "code": customer.code
        } for customer in customers
    ]
    return jsonify(customer_list), 200

@customers_bp.route('/view_customers/<int:id>', methods=['GET'])
def view_customer(id):
    """
    Function for viewing a specific customer on the route `/customers/view_customers/<id>`
    """
    customer = db.session.get(Customer, id)
    if not customer:
        return jsonify({"error": "Customer not found"}), 404
    customer_data = {
        "id": customer.id,
        "name": customer.name,
        "phone_number": customer.phone_number,
        "code": customer.code
    }
    return jsonify(customer_data), 200

@customers_bp.route('/update_customers/<int:id>', methods=['PUT'])
def update_customer(id):
    """
    Function for updating a customer's details on the route `/customers/update_customers/<id>`
    """
    customer = db.session.get(Customer, id)
    if not customer:
        return jsonify({"error": "Customer not found"}), 404
    data = request.json
    customer.name = data.get("name", customer.name)
    customer.phone_number = data.get("phone_number", customer.phone_number)
    customer.code = data.get("code", customer.code)

    try:
        db.session.commit()
        return jsonify({"message": "Customer updated successfully"}), 200
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Failed to update customer"}), 500

@customers_bp.route('/delete_customers/<int:id>', methods=['DELETE'])
def delete_customer(id):
    """
    Function for deleting a customer on the route `/customers/delete_customers/<id>`
    """
    customer = db.session.get(Customer, id)
    if not customer:
        return jsonify({"error": "Customer not found"}), 404
    db.session.delete(customer)
    db.session.commit()
    return jsonify({"message": "Customer deleted successfully"}), 200
