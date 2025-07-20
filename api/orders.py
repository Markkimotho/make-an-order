from flask import Blueprint, request, jsonify, session  # type: ignore
from models import db, Order, Customer
from services.sms_service import SendSMS
from sqlalchemy.exc import IntegrityError
from auth.auth_middleware import login_required
import logging

orders_bp = Blueprint('orders', __name__)
logger = logging.getLogger(__name__)

@orders_bp.route('/place_order', methods=['POST'])
@login_required # Protect this route
def place_order():
    """
    Endpoint for creating a new order for a customer on the route `/orders/place_order`
    """
    data = request.json
    customer_id = data.get("customer_id")
    item = data.get("item")
    amount = data.get("amount")

    if not customer_id or not item or not amount:
        logger.warning("Attempt to place order with missing details.")
        return jsonify({"error": "Missing order details (customer_id, item, amount are required)"}), 400

    try:
        customer = db.session.get(Customer, customer_id)
        if not customer:
            logger.warning(f"Customer with ID {customer_id} not found for order placement.")
            return jsonify({"error": "Customer not found"}), 404

        new_order = Order(customer_id=customer_id, item=item, amount=amount)
        db.session.add(new_order)
        db.session.commit()
        logger.info(f"Order placed successfully for customer ID {customer_id}, Order ID: {new_order.id}")

        order_details = f"Item: {item}, Amount: {amount}"
        customer_name = customer.name
        phone_number = customer.phone_number

        # Call SMS service once and capture its status
        sms_sent_status = SendSMS.send_order_confirmation(phone_number, customer_name, order_details)
        sms_status_message = "SMS sent successfully" if sms_sent_status else "SMS sending failed"
        if not sms_sent_status:
            logger.error(f"Failed to send SMS for order {new_order.id} to {phone_number}.")

        return jsonify(
            {
                "message": "Order placed successfully!",
                "id": new_order.id,
                "sms_status": sms_status_message # Return status, not the call itself
            }), 201

    except IntegrityError:
        db.session.rollback()
        logger.error("Integrity error during order placement.", exc_info=True)
        return jsonify({"error": "Error placing order due to data conflict"}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error placing order: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500

@orders_bp.route('/view_orders', methods=['GET'])
@orders_bp.route('/view_orders/<int:customer_id>', methods=['GET'])
@login_required # Protect this route
def view_orders(customer_id=None):
    """
    Endpoint for viewing all orders or orders placed by a specific customer.
    If customer_id is provided, filters orders by that customer.
    """
    try:
        if customer_id:
            orders = Order.query.filter_by(customer_id=customer_id).all()
            if not orders:
                logger.info(f"No orders found for customer with ID {customer_id}.")
                return jsonify({"message": f"No orders found for customer with ID {customer_id}."}), 404
            logger.info(f"Retrieved {len(orders)} orders for customer ID {customer_id}.")
        else:
            orders = Order.query.all()
            logger.info(f"Retrieved {len(orders)} total orders.")

        order_list = [
            {
                "id": order.id,
                "customer_id": order.customer_id,
                "item": order.item,
                "amount": float(order.amount),
                "time": order.time.isoformat() # ISO format for easier JS parsing
            } for order in orders
        ]
        return jsonify(order_list), 200
    except Exception as e:
        logger.error(f"Error viewing orders (customer_id: {customer_id}): {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500


@orders_bp.route('/update_orders/<int:id>', methods=['PUT'])
@login_required # Protect this route
def update_order(id):
    """
    Endpoint for updating order details on the route `/orders/update_orders/<id>`
    """
    try:
        order = db.session.get(Order, id)
        if not order:
            logger.warning(f"Attempt to update non-existent order with ID: {id}")
            return jsonify({"error": "Order not found"}), 404
        data = request.json

        # Check if any data is provided for update
        if not data:
            logger.warning(f"No update data provided for order ID: {id}")
            return jsonify({"error": "No update data provided"}), 400

        order.item = data.get("item", order.item)
        order.amount = data.get("amount", order.amount)

        db.session.commit()
        logger.info(f"Order with ID {id} updated successfully.")
        return jsonify({"message": "Order updated successfully"}), 200
    except IntegrityError:
        db.session.rollback()
        logger.error(f"Integrity error during order update for ID {id}.", exc_info=True)
        return jsonify({"error": "Failed to update order due to data conflict"}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating order with ID {id}: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500

@orders_bp.route('/delete_orders/<int:id>', methods=['DELETE'])
@login_required # Protect this route
def delete_order(id):
    """
    Endpoint for deleting an order on the route `/orders/delete_orders/<id>`
    """
    try:
        order = db.session.get(Order, id)
        if not order:
            logger.warning(f"Attempt to delete non-existent order with ID: {id}")
            return jsonify({"error": "Order not found"}), 404
        db.session.delete(order)
        db.session.commit()
        logger.info(f"Order with ID {id} deleted successfully.")
        return jsonify({"message": "Order deleted successfully"}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting order with ID {id}: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500