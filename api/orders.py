from flask import Blueprint, request, jsonify, session, Response  # type: ignore
from models import db, Order, OrderItem, Customer, InventoryItem, StockMovement
from services.sms_service import SendSMS
from sqlalchemy.exc import IntegrityError
from auth.auth_middleware import login_required, admin_api_required
from datetime import datetime, timedelta
import logging
import re
import csv
import io

orders_bp = Blueprint('orders', __name__)
logger = logging.getLogger(__name__)

VALID_STATUSES   = {'pending', 'confirmed', 'completed', 'cancelled', 'refunded'}
FINAL_STATUSES   = {'cancelled', 'refunded'}
VALID_PAYMENTS   = {None, 'cash', 'mpesa', 'card'}


# ---------------------------------------------------------------------------
# Legacy manual order entry (admin+) — kept for non-inventory orders
# ---------------------------------------------------------------------------
@orders_bp.route('/place_order', methods=['POST'])
@admin_api_required
def place_order():
    data = request.json or {}
    customer_id = data.get("customer_id")
    item        = data.get("item")
    amount      = data.get("amount")

    if not customer_id or not item or not amount:
        return jsonify({"error": "Missing order details (customer_id, item, amount are required)"}), 400

    try:
        customer = db.session.get(Customer, customer_id)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404

        new_order = Order(customer_id=customer_id, item=item, amount=amount, status='pending')
        db.session.add(new_order)
        db.session.commit()
        logger.info(f"Legacy order {new_order.id} placed for customer {customer_id}.")

        sms_ok = SendSMS.send_order_confirmation(
            customer.phone_number, customer.name, f"Item: {item}, Amount: {amount}"
        )
        return jsonify({
            "message":    "Order placed successfully!",
            "id":         new_order.id,
            "sms_status": "SMS sent successfully" if sms_ok else "SMS sending failed",
        }), 201
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Error placing order due to data conflict"}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error placing legacy order: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500


# ---------------------------------------------------------------------------
# View orders (admin+)
# ---------------------------------------------------------------------------
@orders_bp.route('/view_orders', methods=['GET'])
@orders_bp.route('/view_orders/<int:customer_id>', methods=['GET'])
@admin_api_required
def view_orders(customer_id=None):
    """Return orders with pagination. Query params: page (1-based), per_page (default 50)."""
    try:
        page     = max(1, int(request.args.get('page', 1)))
        per_page = min(int(request.args.get('per_page', 50)), 200)

        q = Order.query.order_by(Order.time.desc())
        if customer_id:
            q = q.filter_by(customer_id=customer_id)

        total = q.count()
        orders = q.offset((page - 1) * per_page).limit(per_page).all()
        logger.info(f"Retrieved {len(orders)} orders page {page} (customer_id={customer_id}).")

        result = []
        for o in orders:
            d = o.to_dict()
            d['customer_name'] = o.customer.name if o.customer else 'Unknown'
            result.append(d)

        return jsonify({
            'orders': result,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'pages': (total + per_page - 1) // per_page,
            }
        }), 200
    except Exception as e:
        logger.error(f"Error viewing orders: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500


# ---------------------------------------------------------------------------
# User's own orders
# ---------------------------------------------------------------------------
@orders_bp.route('/my_orders', methods=['GET'])
@login_required
def my_orders():
    """Returns all orders for the logged-in user's auto-provisioned customer record."""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify([]), 200
    try:
        customer = Customer.query.filter_by(code=f"USR-{user_id}").first()
        if not customer:
            return jsonify([]), 200
        orders = (Order.query
                  .filter_by(customer_id=customer.id)
                  .order_by(Order.time.desc())
                  .all())
        return jsonify([o.to_dict() for o in orders]), 200
    except Exception as e:
        logger.error(f"Error fetching my_orders: {e}", exc_info=True)
        return jsonify({'error': 'An internal server error occurred'}), 500


# ---------------------------------------------------------------------------
# Update order summary fields (item label / amount)
# ---------------------------------------------------------------------------
@orders_bp.route('/update_orders/<int:id>', methods=['PUT'])
@admin_api_required
def update_order(id):
    try:
        order = db.session.get(Order, id)
        if not order:
            return jsonify({"error": "Order not found"}), 404
        data = request.json
        if not data:
            return jsonify({"error": "No update data provided"}), 400
        order.item   = data.get("item",   order.item)
        order.amount = data.get("amount", order.amount)
        db.session.commit()
        return jsonify({"message": "Order updated successfully"}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating order {id}: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500


# ---------------------------------------------------------------------------
# Update order status (admin+) — cancelling/refunding restores stock
# ---------------------------------------------------------------------------
@orders_bp.route('/<int:id>/status', methods=['PUT'])
@admin_api_required
def update_order_status(id):
    data   = request.get_json(silent=True) or {}
    status = data.get('status', '').lower()
    if status not in VALID_STATUSES:
        return jsonify({'error': f'Invalid status. Valid: {", ".join(sorted(VALID_STATUSES))}'}), 400
    try:
        order = db.session.get(Order, id)
        if not order:
            return jsonify({'error': 'Order not found'}), 404

        # Restore stock when moving into a cancellation/refund state
        if status in FINAL_STATUSES and order.status not in FINAL_STATUSES:
            reason = 'refund' if status == 'refunded' else 'cancel'
            _restore_stock(order, reason=reason)

        order.status = status
        db.session.commit()
        logger.info(f"Order {id} status → {status}")
        return jsonify({'message': f'Order status updated to {status}', 'status': status}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating status for order {id}: {e}", exc_info=True)
        return jsonify({'error': 'An internal server error occurred'}), 500


# ---------------------------------------------------------------------------
# Update payment details (admin+)
# ---------------------------------------------------------------------------
@orders_bp.route('/<int:id>/payment', methods=['PUT'])
@admin_api_required
def update_order_payment(id):
    data           = request.get_json(silent=True) or {}
    payment_method = data.get('payment_method')
    if payment_method not in VALID_PAYMENTS:
        return jsonify({'error': f'Invalid payment method. Valid: cash, mpesa, card'}), 400
    try:
        order = db.session.get(Order, id)
        if not order:
            return jsonify({'error': 'Order not found'}), 404
        order.payment_method    = payment_method
        order.payment_reference = data.get('payment_reference', order.payment_reference)
        db.session.commit()
        return jsonify({'message': 'Payment details updated'}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating payment for order {id}: {e}", exc_info=True)
        return jsonify({'error': 'An internal server error occurred'}), 500


# ---------------------------------------------------------------------------
# Delete order — restores inventory stock
# ---------------------------------------------------------------------------
def _parse_order_label(label: str):
    """Parse 'ItemName ×N' -> (ItemName, N). Single-item labels return (label, 1)."""
    m = re.match(r'^(.+)\s+×(\d+)$', label)
    if m:
        return m.group(1), int(m.group(2))
    return label, 1


def _restore_stock(order: Order, reason: str = 'cancel'):
    """Restore inventory stock for an order and log StockMovements.

    Uses OrderItems (Phase 2+) when available; falls back to parsing the
    Order.item summary string for legacy (Phase 1) orders.
    """
    uid = session.get('user_id')
    if order.order_items:
        for oi in order.order_items:
            if oi.inventory_item_id:
                inv = db.session.get(InventoryItem, oi.inventory_item_id)
                if inv:
                    inv.stock += oi.quantity
                    db.session.add(StockMovement(
                        item_id=inv.id, item_name=oi.item_name,
                        delta=oi.quantity, reason=reason, user_id=uid,
                        note=f"order #{order.id}",
                    ))
                    logger.info(f"Restored {oi.quantity}× '{oi.item_name}' (order {order.id}).")
    else:
        # Legacy order: parse the item summary string
        for segment in order.item.split(', '):
            item_name, qty = _parse_order_label(segment.strip())
            inv = InventoryItem.query.filter_by(name=item_name).first()
            if inv:
                inv.stock += qty
                db.session.add(StockMovement(
                    item_id=inv.id, item_name=item_name,
                    delta=qty, reason=reason, user_id=uid,
                    note=f"legacy order #{order.id}",
                ))
                logger.info(f"Restored {qty}× '{item_name}' from legacy order {order.id}.")


@orders_bp.route('/delete_orders/<int:id>', methods=['DELETE'])
@admin_api_required
def delete_order(id):
    """Delete an order and restore its inventory stock."""
    try:
        order = db.session.get(Order, id)
        if not order:
            return jsonify({"error": "Order not found"}), 404
        _restore_stock(order)
        db.session.delete(order)
        db.session.commit()
        logger.info(f"Order {id} deleted.")
        return jsonify({"message": "Order deleted successfully"}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting order {id}: {e}", exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500


# ---------------------------------------------------------------------------
# CSV export (admin+)
# ---------------------------------------------------------------------------

def _period_start(period: str):
    """Return the start datetime for the requested period, or None for 'all'."""
    now = datetime.utcnow()
    return {
        'today': now.replace(hour=0, minute=0, second=0, microsecond=0),
        'week':  now - timedelta(days=7),
        'month': now - timedelta(days=30),
    }.get(period)


@orders_bp.route('/export', methods=['GET'])
@admin_api_required
def export_orders():
    """Download orders as a CSV file."""
    period      = request.args.get('period', 'all')
    status_f    = request.args.get('status', '')
    customer_id = request.args.get('customer_id', '')

    q = Order.query.order_by(Order.time.desc())
    since = _period_start(period)
    if since:
        q = q.filter(Order.time >= since)
    if status_f:
        q = q.filter(Order.status == status_f)
    if customer_id:
        q = q.filter(Order.customer_id == int(customer_id))

    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(['Order ID', 'Customer', 'Phone', 'Items', 'Total', 'Status',
                'Payment Method', 'Payment Reference', 'Date'])
    for o in q.all():
        w.writerow([
            o.id,
            o.customer.name if o.customer else '',
            o.customer.phone_number if o.customer else '',
            o.item,
            f'{float(o.amount):.2f}',
            o.status,
            o.payment_method or '',
            o.payment_reference or '',
            o.time.strftime('%Y-%m-%d %H:%M') if o.time else '',
        ])

    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=orders.csv'},
    )
