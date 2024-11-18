# api/customer.py
from flask import Blueprint, request, jsonify, session # type: ignore
from models import db, Order, Customer
from services.sms_service import SendSMS
from sqlalchemy.exc import IntegrityError
from auth.auth_middleware import login_required


orders_bp = Blueprint('orders', __name__)

@orders_bp.route('/place_order', methods=['POST'])
# @login_required
def place_order():
    """
    Endpoint for creating a new order for a customer on the route `/orders/place_order`
    """
    print(f"Session data:{session}")
    data = request.json
    customer_id = data.get("customer_id")
    item = data.get("item")
    amount = data.get("amount")

    if not customer_id or not item or not amount:
        return jsonify({"error": "Missing order details"}), 400

    try:
        # Retrieve the customer to get their details including phone number for the SMS
        customer = db.session.get(Customer, customer_id)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404
        
        # Create the new order
        new_order = Order(customer_id=customer_id, item=item, amount=amount)
        db.session.add(new_order)
        db.session.commit()

        # Prepare the order details for SMS
        order_details = f"Item: {item}, Amount: {amount}"
        customer_name = customer.name 
        phone_number = customer.phone_number 

        # Send SMS notification
        SendSMS.send_order_confirmation(phone_number, customer_name, order_details)

        return jsonify({"message": "Order placed successfully!"}), 201
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Error placing order"}), 500

@orders_bp.route('/view_orders/<int:customer_id>', methods=['GET'])
# @login_required
def view_orders(customer_id):
    """
    Endpoint for viewing an order placed by a customer using the `customer_id` on the route `/orders/view_orders/<int:customer_id>`
    """
    orders = Order.query.filter_by(customer_id=customer_id).all()
    if orders:
        order_list = [
            {
                "id": order.id, 
                "item": order.item, 
                "amount": float(order.amount), 
                "time": str(order.time)
            } for order in orders]
        return jsonify(order_list), 200 
    else:
        return jsonify(order_list) if orders else jsonify({"message": "No orders found for this customer."}), 404
