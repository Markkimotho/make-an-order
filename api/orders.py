from flask import Blueprint, request, jsonify, session, Response  # type: ignore
from models import db, Order, OrderItem, Customer, InventoryItem, StockMovement
from services import orders as order_service
from services import audit_service, online_shop, stock
from services.sms_service import SendSMS
from sqlalchemy.exc import IntegrityError
from auth.auth_middleware import login_required, admin_api_required, cashier_api_required
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
            customer.phone_number, customer.name, f"Item: {item}, Amount: {amount}",
            order_id=new_order.id,
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

        # Goods already handed over come back through a return at the
        # counter, not by flipping the order's status.
        if status in FINAL_STATUSES and order.fulfillment_status == 'collected':
            return jsonify({'error': 'This order was collected; return the goods at the '
                                     'counter instead'}), 409
        # Cancelling/refunding puts the stock back (and voids the invoice of
        # an order placed through services/orders.py).
        if status in FINAL_STATUSES and order.status not in FINAL_STATUSES:
            reason = 'refund' if status == 'refunded' else 'cancel'
            _cancel(order, reason)

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


def _cancel(order: Order, reason: str = 'cancel'):
    """Put an order's stock back and void its invoice.

    Orders with lines go through services/orders.cancel (exact batches, or
    on hold for lines from before batches). Single-line legacy orders have
    only a summary string to go on.
    """
    uid = session.get('user_id')
    if order.order_items:
        if order.fulfillment_status not in ('cancelled', 'collected'):
            order_service.cancel(order, user_id=uid, reason=reason)
    else:
        # Legacy order: parse the item summary string
        for segment in order.item.split(', '):
            item_name, qty = _parse_order_label(segment.strip())
            inv = InventoryItem.query.filter_by(name=item_name).first()
            if inv and qty > 0:
                stock.give_back(inv, qty, reason=reason, user_id=uid,
                                note=f"legacy order #{order.id}")
                logger.info(f"Restored {qty}× '{item_name}' from legacy order {order.id}.")


@orders_bp.route('/delete_orders/<int:id>', methods=['DELETE'])
@admin_api_required
def delete_order(id):
    """Delete an order and restore its inventory stock."""
    try:
        order = db.session.get(Order, id)
        if not order:
            return jsonify({"error": "Order not found"}), 404
        if order.status not in FINAL_STATUSES:
            _cancel(order, 'cancel')
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


# ---------------------------------------------------------------------------
# Online orders (shop, WhatsApp, public links): list and fulfilment.
# Counter staff run these, so cashiers and pharmacists may too.
# ---------------------------------------------------------------------------
ONLINE_STATUSES = {'open', 'pending', 'ready', 'dispatched', 'collected', 'released',
                   'restock_needed', 'cancelled'}


@orders_bp.route('/online', methods=['GET'])
@cashier_api_required
def online_orders():
    """Remote orders newest first. Filters: status (or 'open'), channel,
    paid=paid|unpaid, customer_id, page, per_page."""
    status = request.args.get('status') or None
    channel = request.args.get('channel') or None
    paid = request.args.get('paid') or None
    if status and status not in ONLINE_STATUSES:
        return jsonify({'error': 'unknown status'}), 400
    if channel and channel not in online_shop.REMOTE_CHANNELS:
        return jsonify({'error': 'unknown channel'}), 400
    if paid and paid not in ('paid', 'unpaid'):
        return jsonify({'error': 'paid must be paid or unpaid'}), 400
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(max(1, int(request.args.get('per_page', 50))), 200)
        customer_id = int(request.args['customer_id']) if request.args.get('customer_id') else None
    except ValueError:
        return jsonify({'error': 'page, per_page and customer_id must be numbers'}), 400
    orders, total = online_shop.list_orders(status=status, channel=channel, paid=paid,
                                            customer_id=customer_id, page=page,
                                            per_page=per_page)
    now = datetime.utcnow()
    return jsonify({'orders': [online_shop.order_row(o, now, session.get('role')) for o in orders],
                    'waiting': online_shop.waiting_summary(),
                    'pagination': {'page': page, 'per_page': per_page, 'total': total,
                                   'pages': (total + per_page - 1) // per_page}}), 200


@orders_bp.route('/online/waiting', methods=['GET'])
@cashier_api_required
def online_waiting():
    """Paid orders nobody has started on: the nav badge and the new-order sound."""
    return jsonify(online_shop.waiting_summary()), 200


@orders_bp.route('/<int:id>/fulfilment', methods=['PUT'])
@cashier_api_required
def order_fulfilment(id):
    """{status: ready|dispatched|collected|delivered|cancelled}. Ready (pickup)
    and dispatched text the customer once."""
    status = str((request.get_json(silent=True) or {}).get('status') or '').lower()
    if status not in online_shop.FULFILMENT_ACTIONS:
        return jsonify({'error': 'status must be one of '
                                 + ', '.join(online_shop.FULFILMENT_ACTIONS)}), 400
    order = db.session.get(Order, id)
    if order is None:
        return jsonify({'error': 'Order not found'}), 404
    before = order.fulfillment_status
    try:
        order = online_shop.set_fulfilment(order, status, user_id=session.get('user_id'),
                                           role=session.get('role'))
    except online_shop.FulfilmentForbidden as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 403
    except online_shop.FulfilmentError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 409
    audit_service.log(session.get('user_id'), 'order.fulfilment', 'Order', order.id,
                      {'fulfillment_status': before},
                      {'fulfillment_status': order.fulfillment_status})
    db.session.commit()
    sms = online_shop.notify(order)
    row = online_shop.order_row(order, role=session.get('role'))
    row['sms'] = sms.status if sms is not None else None
    return jsonify({'order': row}), 200
