from flask import Blueprint, request, jsonify # type: ignore
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
        return jsonify({"message": "Customer registered successfully"}), 201
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
