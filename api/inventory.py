from flask import Blueprint, jsonify, request, session, Response
from models import db, InventoryItem, Order, OrderItem, StockMovement, User, Customer
from services.sms_service import SendSMS
from auth.auth_middleware import admin_api_required, superuser_api_required, login_required
from sqlalchemy.exc import IntegrityError
from decimal import Decimal
import logging
import csv
import io

inventory_bp = Blueprint('inventory', __name__)
logger = logging.getLogger(__name__)

LOW_STOCK_THRESHOLD = 5


# ---------------------------------------------------------------------------
# Read-only (all logged-in users)
# ---------------------------------------------------------------------------

@inventory_bp.route('/items', methods=['GET'])
@login_required
def get_items():
    category = request.args.get('category', '').strip()
    q = InventoryItem.query
    if category:
        q = q.filter_by(category=category)
    items = q.order_by(InventoryItem.category, InventoryItem.name).all()
    return jsonify([i.to_dict() for i in items])


@inventory_bp.route('/categories', methods=['GET'])
@login_required
def get_categories():
    rows = (db.session.query(InventoryItem.category)
            .distinct()
            .order_by(InventoryItem.category)
            .all())
    return jsonify([r[0] for r in rows])


@inventory_bp.route('/items/by-sku/<sku>', methods=['GET'])
@login_required
def get_item_by_sku(sku):
    """Lookup item by SKU (for barcode scanner)."""
    try:
        item = InventoryItem.query.filter_by(sku=sku.strip()).first()
        if not item:
            return jsonify({'error': 'Item not found'}), 404
        return jsonify(item.to_dict()), 200
    except Exception as e:
        logger.error(f"Error looking up item by SKU {sku}: {e}", exc_info=True)
        return jsonify({'error': 'An internal server error occurred'}), 500


# ---------------------------------------------------------------------------

@inventory_bp.route('/items', methods=['POST'])
@admin_api_required
def create_item():
    data        = request.get_json(silent=True) or {}
    name        = data.get('name', '').strip()
    category    = data.get('category', '').strip()
    price       = data.get('price')
    description = (data.get('description') or '').strip() or None
    stock       = max(0, int(data.get('stock', 0)))
    icon        = (data.get('icon') or 'fa-box').strip()
    sku         = (data.get('sku') or '').strip() or None

    if not name or price is None or not category:
        return jsonify({'error': 'name, price, and category are required'}), 400

    try:
        item = InventoryItem(
            name=name, price=price, category=category,
            description=description, stock=stock, icon=icon, sku=sku,
        )
        db.session.add(item)
        db.session.flush()

        if stock > 0:
            db.session.add(StockMovement(
                item_id=item.id, item_name=name, delta=stock,
                reason='initial_stock', user_id=session.get('user_id'),
            ))

        db.session.commit()
        logger.info(f"Inventory item '{name}' created (id={item.id}, sku={sku}).")
        return jsonify({'message': f'"{name}" added to inventory', 'item': item.to_dict()}), 201
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'SKU must be unique' if sku else 'Item with this name already exists'}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating inventory item: {e}", exc_info=True)
        return jsonify({'error': 'An internal server error occurred'}), 500


@inventory_bp.route('/items/<int:id>', methods=['PUT'])
@admin_api_required
def update_item(id):
    item = db.session.get(InventoryItem, id)
    if not item:
        return jsonify({'error': 'Item not found'}), 404

    data = request.get_json(silent=True) or {}
    item.name        = data.get('name',        item.name)
    item.price       = data.get('price',       item.price)
    item.category    = data.get('category',    item.category)
    item.description = data.get('description', item.description) or None
    item.icon        = data.get('icon',        item.icon)
    item.sku         = (data.get('sku') or '').strip() or None
    # Stock is intentionally NOT updated here — use /restock

    try:
        db.session.commit()
        return jsonify({'message': f'"{item.name}" updated', 'item': item.to_dict()}), 200
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'SKU must be unique'}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating inventory item {id}: {e}", exc_info=True)
        return jsonify({'error': 'An internal server error occurred'}), 500


@inventory_bp.route('/items/<int:id>', methods=['DELETE'])
@superuser_api_required
def delete_item(id):
    item = db.session.get(InventoryItem, id)
    if not item:
        return jsonify({'error': 'Item not found'}), 404
    name = item.name
    try:
        db.session.delete(item)
        db.session.commit()
        logger.info(f"Inventory item '{name}' (id={id}) deleted.")
        return jsonify({'message': f'"{name}" removed from inventory'}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting inventory item {id}: {e}", exc_info=True)
        return jsonify({'error': 'An internal server error occurred'}), 500


@inventory_bp.route('/items/<int:id>/restock', methods=['POST'])
@admin_api_required
def restock_item(id):
    item = db.session.get(InventoryItem, id)
    if not item:
        return jsonify({'error': 'Item not found'}), 404

    data     = request.get_json(silent=True) or {}
    quantity = int(data.get('quantity', 0))
    note     = (data.get('note') or '').strip() or None

    if quantity <= 0:
        return jsonify({'error': 'Quantity must be a positive integer'}), 400

    try:
        item.stock += quantity
        db.session.add(StockMovement(
            item_id=item.id, item_name=item.name, delta=quantity,
            reason='restock', user_id=session.get('user_id'), note=note,
        ))
        db.session.commit()
        logger.info(f"Restocked {quantity}× '{item.name}' → stock={item.stock}.")
        return jsonify({
            'message':   f'Restocked {quantity} units of "{item.name}"',
            'new_stock': item.stock,
        }), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error restocking item {id}: {e}", exc_info=True)
        return jsonify({'error': 'An internal server error occurred'}), 500


# ---------------------------------------------------------------------------
# Low-stock & audit log (admin+)
# ---------------------------------------------------------------------------

@inventory_bp.route('/low_stock', methods=['GET'])
@admin_api_required
def low_stock():
    threshold = int(request.args.get('threshold', LOW_STOCK_THRESHOLD))
    items = (InventoryItem.query
             .filter(InventoryItem.stock < threshold)
             .order_by(InventoryItem.stock)
             .all())
    return jsonify({
        'threshold': threshold,
        'count':     len(items),
        'items':     [i.to_dict() for i in items],
    }), 200


@inventory_bp.route('/movements', methods=['GET'])
@admin_api_required
def stock_movements_log():
    item_id = request.args.get('item_id')
    limit   = min(int(request.args.get('limit', 50)), 200)
    q = StockMovement.query.order_by(StockMovement.created_at.desc())
    if item_id:
        q = q.filter_by(item_id=item_id)
    return jsonify([m.to_dict() for m in q.limit(limit).all()]), 200


# ---------------------------------------------------------------------------
# Place order (all logged-in users)
# ---------------------------------------------------------------------------

@inventory_bp.route('/place_order', methods=['POST'])
@login_required
def place_inventory_order():
    data = request.get_json(silent=True) or {}
    role = session.get('role', 'user')

    if role in ('admin', 'superuser'):
        customer_id = data.get('customer_id')
        if not customer_id:
            return jsonify({'error': 'No customer selected'}), 400
    else:
        user = db.session.get(User, session.get('user_id'))
        if not user:
            return jsonify({'error': 'Session expired. Please log in again.'}), 401
        if not user.phone_number:
            return jsonify({'error': 'A phone number is required to place orders. Contact an admin.'}), 400
        customer_id = _get_or_create_customer(user)
        if not customer_id:
            return jsonify({'error': 'Could not set up your customer account. Contact an admin.'}), 500

    cart = data.get('items', [])
    if not cart:
        return jsonify({'error': 'Cart is empty'}), 400

    # Validate all items upfront (all-or-nothing)
    resolved = []
    for entry in cart:
        item = db.session.get(InventoryItem, entry['id'])
        if not item:
            return jsonify({'error': f"Item ID {entry['id']} not found"}), 404
        qty = int(entry['quantity'])
        if qty <= 0:
            continue
        if item.stock < qty:
            return jsonify({'error': f'Only {item.stock} units of "{item.name}" available'}), 400
        resolved.append((item, qty))

    if not resolved:
        return jsonify({'error': 'No valid items in cart'}), 400

    # Build summary and total
    summary_parts = []
    total = Decimal('0')
    for item, qty in resolved:
        label    = item.name if qty == 1 else f"{item.name} \u00d7{qty}"
        subtotal = Decimal(str(item.price)) * qty
        summary_parts.append(label)
        total += subtotal

    # Create Order header
    order = Order(
        customer_id=customer_id,
        item=', '.join(summary_parts),
        amount=total,
        status='pending',
    )
    db.session.add(order)
    db.session.flush()

    # Create OrderItems, decrement stock, log movements
    uid = session.get('user_id')
    for item, qty in resolved:
        subtotal = Decimal(str(item.price)) * qty
        db.session.add(OrderItem(
            order_id=order.id, inventory_item_id=item.id,
            item_name=item.name, quantity=qty,
            unit_price=item.price, subtotal=subtotal,
        ))
        item.stock -= qty
        db.session.add(StockMovement(
            item_id=item.id, item_name=item.name, delta=-qty,
            reason='sale', user_id=uid,
        ))

    db.session.commit()

    # SMS confirmation
    try:
        customer = db.session.get(Customer, customer_id)
        if customer and customer.phone_number:
            SendSMS.send_order_confirmation(
                customer.phone_number, customer.name, ', '.join(summary_parts)
            )
    except Exception:
        logger.warning("SMS send failed after order commit.", exc_info=True)

    return jsonify({
        'message':  f"Order placed — {len(resolved)} item type(s), total {float(total):.2f}",
        'order_id': order.id,
        'items':    summary_parts,
    }), 201


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_customer(user):
    """Find or auto-create a Customer record for a normal user. Returns customer_id or None."""
    code     = f"USR-{user.id}"
    customer = Customer.query.filter_by(code=code).first()
    if customer:
        return customer.id
    existing = Customer.query.filter_by(phone_number=user.phone_number).first()
    if existing:
        return existing.id
    try:
        customer = Customer(name=user.name or user.email, phone_number=user.phone_number, code=code)
        db.session.add(customer)
        db.session.flush()
        return customer.id
    except IntegrityError:
        db.session.rollback()
        return None


# ---------------------------------------------------------------------------
# CSV export (admin+)
# ---------------------------------------------------------------------------

@inventory_bp.route('/export', methods=['GET'])
@admin_api_required
def export_inventory():
    """Download full inventory list as a CSV file."""
    category = request.args.get('category', '').strip()
    q = InventoryItem.query.order_by(InventoryItem.category, InventoryItem.name)
    if category:
        q = q.filter_by(category=category)

    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(['ID', 'Name', 'Category', 'Price', 'Stock', 'Description'])
    for item in q.all():
        w.writerow([
            item.id, item.name, item.category,
            f'{float(item.price):.2f}', item.stock,
            item.description or '',
        ])

    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=inventory.csv'},
    )
