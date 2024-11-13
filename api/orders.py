from flask import Blueprint, request, jsonify # type: ignore
from models import db, Order
from sqlalchemy.exc import IntegrityError

orders_bp = Blueprint('orders', __name__)

@orders_bp.route('/place_order', methods=['POST'])
def place_order():
    """
    Function for creating a new order for a customer on the route `/orders/place_order`
    """
    data = request.json
    customer_id = data.get("customer_id")
    item = data.get("item")
    amount = data.get("amount")

    if not customer_id or not item or not amount:
        return jsonify({"error": "Missing order details"}), 400

    try:
        new_order = Order(customer_id=customer_id, item=item, amount=amount)
        db.session.add(new_order)
        db.session.commit()
        return jsonify({"message": "Order placed successfully!"}), 201
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Error placing order"}), 500

@orders_bp.route('/view_orders/<int:customer_id>', methods=['GET'])
def view_orders(customer_id):
    """
    Function for viewing an order placed by a customer using the `customer_id` on the route `/orders/view_orders/<int:customer_id>`
    """
    orders = Order.query.filter_by(customer_id=customer_id).all()
    order_list = [{"id": order.id, "item": order.item, "amount": float(order.amount), "time": str(order.time)} for order in orders]
    return jsonify(order_list) if orders else jsonify({"message": "No orders found for this customer."}), 404
