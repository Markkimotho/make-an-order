from flask import Blueprint, request, jsonify, session  # type: ignore
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
    data = request.json
    customer_id = data.get("customer_id")
    item = data.get("item")
    amount = data.get("amount")

    if not customer_id or not item or not amount:
        return jsonify({"error": "Missing order details"}), 400

    try:
        customer = db.session.get(Customer, customer_id)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404

        new_order = Order(customer_id=customer_id, item=item, amount=amount)
        db.session.add(new_order)
        db.session.commit()

        order_details = f"Item: {item}, Amount: {amount}"
        customer_name = customer.name
        phone_number = customer.phone_number
        SendSMS.send_order_confirmation(phone_number, customer_name, order_details)

        # Include the order ID in the response
        return jsonify({"message": "Order placed successfully!", "id": new_order.id, "SMS": SendSMS.send_order_confirmation(phone_number, customer_name, order_details)}), 201
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Error placing order"}), 500

@orders_bp.route('/view_orders/<int:customer_id>', methods=['GET'])
# @login_required
def view_orders(customer_id):
    """
    Endpoint for viewing all orders placed by a customer using `/orders/view_orders/<int:customer_id>`
    """
    orders = Order.query.filter_by(customer_id=customer_id).all()
    if orders:
        order_list = [
            {
                "id": order.id,
                "item": order.item,
                "amount": float(order.amount),
                "time": str(order.time)
            } for order in orders
        ]
        return jsonify(order_list), 200
    return jsonify({"message": "No orders found for this customer."}), 404

@orders_bp.route('/update_orders/<int:id>', methods=['PUT'])
def update_order(id):
    """
    Endpoint for updating order details on the route `/orders/update_orders/<id>`
    """
    order = db.session.get(Order, id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    data = request.json
    order.item = data.get("item", order.item)
    order.amount = data.get("amount", order.amount)

    try:
        db.session.commit()
        return jsonify({"message": "Order updated successfully"}), 200
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Failed to update order"}), 500

@orders_bp.route('/delete_orders/<int:id>', methods=['DELETE'])
def delete_order(id):
    """
    Endpoint for deleting an order on the route `/orders/delete_orders/<id>`
    """
    order = db.session.get(Order, id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    db.session.delete(order)
    db.session.commit()
    return jsonify({"message": "Order deleted successfully"}), 200
