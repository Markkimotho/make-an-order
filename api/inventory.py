from flask import Blueprint, jsonify, request, session, Response
from models import (
    db, InventoryItem, Order, OrderItem, StockMovement, User, Customer,
    Category, Supplier, PurchaseOrder, PurchaseOrderItem,
    PO_DRAFT, PO_SENT, PO_PARTIAL, PO_RECEIVED, PO_CANCELLED, PO_STATUSES,
)
from services.sms_service import SendSMS
from services import audit_service
from auth.auth_middleware import (
    admin_api_required, superuser_api_required, login_required,
    stock_clerk_api_required,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from datetime import date, datetime, timedelta
from decimal import Decimal
import logging
import csv
import io

inventory_bp = Blueprint('inventory', __name__)
logger = logging.getLogger(__name__)

LOW_STOCK_THRESHOLD = 5


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


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
            barcode=(data.get('barcode') or '').strip() or None,
            cost_price=data.get('cost_price'),
            reorder_level=int(data.get('reorder_level', 5) or 5),
            expiry_date=_parse_date(data.get('expiry_date')),
            batch_number=(data.get('batch_number') or '').strip() or None,
            supplier_id=data.get('supplier_id'),
            category_id=data.get('category_id'),
            unit=(data.get('unit') or 'pcs').strip() or 'pcs',
            is_drug=bool(data.get('is_drug', False)),
            requires_prescription=bool(data.get('requires_prescription', False)),
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
        return jsonify({'error': 'SKU/barcode must be unique' if (sku or data.get('barcode'))
                        else 'Item with this name already exists'}), 400
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
    if 'sku' in data:
        item.sku = (data.get('sku') or '').strip() or None
    if 'barcode' in data:
        item.barcode = (data.get('barcode') or '').strip() or None
    if 'cost_price' in data:
        item.cost_price = data.get('cost_price')
    if 'reorder_level' in data:
        item.reorder_level = int(data.get('reorder_level') or 0)
    if 'expiry_date' in data:
        item.expiry_date = _parse_date(data.get('expiry_date'))
    if 'batch_number' in data:
        item.batch_number = (data.get('batch_number') or '').strip() or None
    if 'supplier_id' in data:
        item.supplier_id = data.get('supplier_id')
    if 'category_id' in data:
        item.category_id = data.get('category_id')
    if 'unit' in data:
        item.unit = (data.get('unit') or 'pcs').strip() or 'pcs'
    if 'is_drug' in data:
        item.is_drug = bool(data.get('is_drug'))
    if 'requires_prescription' in data:
        item.requires_prescription = bool(data.get('requires_prescription'))
    # Stock is intentionally NOT updated here — use /restock or /stock_adjustment

    try:
        db.session.commit()
        return jsonify({'message': f'"{item.name}" updated', 'item': item.to_dict()}), 200
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'SKU/barcode must be unique'}), 400
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
    customer_id = data.get('customer_id')

    if not customer_id:
        return jsonify({'error': 'Please select a customer to place an order'}), 400

    # Verify customer exists
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return jsonify({'error': 'Customer not found'}), 404

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
        label    = item.name if qty == 1 else f"{item.name} ×{qty}"
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


# ===========================================================================
# POS extensions: stock adjustments, expiry, item movement, suppliers, POs
# ===========================================================================

@inventory_bp.route('/stock_adjustment', methods=['POST'])
@admin_api_required
def stock_adjustment():
    """Manually adjust an item's stock with an audit trail.

    Body: ``{"inventory_item_id": 1, "delta": -3, "reason": "shrinkage", "note": "..."}``.
    A negative delta deducts; a positive delta adds. Writes a StockMovement
    and an AuditLog entry.
    """
    data = request.get_json(silent=True) or {}
    iid = data.get('inventory_item_id')
    if not iid:
        return jsonify({'error': 'inventory_item_id is required'}), 400
    item = db.session.get(InventoryItem, iid)
    if not item:
        return jsonify({'error': 'Item not found'}), 404
    try:
        delta = int(data.get('delta', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'delta must be an integer'}), 400
    if delta == 0:
        return jsonify({'error': 'delta must be non-zero'}), 400
    reason = (data.get('reason') or 'adjustment').strip() or 'adjustment'
    note   = (data.get('note') or '').strip() or None
    new_stock = int(item.stock or 0) + delta
    if new_stock < 0:
        return jsonify({'error': 'adjustment would make stock negative'}), 400

    old = item.stock
    item.stock = new_stock
    db.session.add(StockMovement(
        item_id=item.id, item_name=item.name, delta=delta,
        reason=reason, user_id=session.get('user_id'), note=note,
    ))
    audit_service.log(session.get('user_id'), 'inventory.stock_adjustment',
                      'InventoryItem', item.id,
                      {'stock': old}, {'stock': new_stock, 'reason': reason})
    db.session.commit()
    return jsonify({'message': 'Stock adjusted', 'item': item.to_dict()}), 200


@inventory_bp.route('/low_stock_reorder', methods=['GET'])
@login_required
def low_stock_reorder():
    """Items where ``stock <= reorder_level`` (POS reorder helper, flat list)."""
    rows = (InventoryItem.query
            .filter(InventoryItem.stock <= InventoryItem.reorder_level)
            .order_by(InventoryItem.stock.asc())
            .all())
    return jsonify([i.to_dict() for i in rows]), 200


@inventory_bp.route('/expiring', methods=['GET'])
@login_required
def expiring_items():
    """Items with ``expiry_date`` within the next ``?days=`` (default 30) days."""
    try:
        days = int(request.args.get('days', 30))
    except ValueError:
        return jsonify({'error': 'days must be an integer'}), 400
    cutoff = date.today() + timedelta(days=max(days, 0))
    rows = (InventoryItem.query
            .filter(InventoryItem.expiry_date.isnot(None),
                    InventoryItem.expiry_date <= cutoff)
            .order_by(InventoryItem.expiry_date.asc())
            .all())
    return jsonify([i.to_dict() for i in rows]), 200


@inventory_bp.route('/items/<int:id>/movement', methods=['GET'])
@admin_api_required
def item_movement(id):
    """Stock-movement history for one item, newest first."""
    item = db.session.get(InventoryItem, id)
    if not item:
        return jsonify({'error': 'Item not found'}), 404
    rows = (StockMovement.query.filter_by(item_id=id)
            .order_by(StockMovement.created_at.desc())
            .limit(500).all())
    return jsonify({
        'item': item.to_dict(),
        'movements': [m.to_dict() for m in rows],
    }), 200


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------

@inventory_bp.route('/suppliers', methods=['GET'])
@login_required
def list_suppliers():
    rows = Supplier.query.order_by(Supplier.name).all()
    return jsonify([s.to_dict() for s in rows]), 200


SUPPLIER_FIELDS = ('name', 'contact_name', 'phone', 'email', 'address', 'payment_terms')


def _supplier_field(data, field):
    """Trimmed value or None. 'contact_person' is accepted for contact_name
    (what the purchase-orders page has always sent)."""
    raw = data.get(field)
    if raw is None and field == 'contact_name':
        raw = data.get('contact_person')
    return (raw or '').strip() or None


@inventory_bp.route('/suppliers', methods=['POST'])
@admin_api_required
def create_supplier():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    s = Supplier(
        name=name,
        contact_name=_supplier_field(data, 'contact_name'),
        phone=_supplier_field(data, 'phone'),
        email=_supplier_field(data, 'email'),
        address=_supplier_field(data, 'address'),
        payment_terms=_supplier_field(data, 'payment_terms'),
    )
    db.session.add(s)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'Supplier name must be unique'}), 400
    return jsonify({'message': 'Supplier created', 'supplier': s.to_dict()}), 201


@inventory_bp.route('/suppliers/<int:id>', methods=['PUT'])
@admin_api_required
def update_supplier(id):
    s = db.session.get(Supplier, id)
    if not s:
        return jsonify({'error': 'Supplier not found'}), 404
    data = request.get_json(silent=True) or {}
    if 'name' in data and not (data.get('name') or '').strip():
        return jsonify({'error': 'name cannot be empty'}), 400
    for f in SUPPLIER_FIELDS:
        if f in data or (f == 'contact_name' and 'contact_person' in data):
            setattr(s, f, _supplier_field(data, f))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'Supplier name must be unique'}), 400
    return jsonify({'message': 'Supplier updated', 'supplier': s.to_dict()}), 200


@inventory_bp.route('/suppliers/<int:id>', methods=['DELETE'])
@superuser_api_required
def delete_supplier(id):
    s = db.session.get(Supplier, id)
    if not s:
        return jsonify({'error': 'Supplier not found'}), 404
    db.session.delete(s)
    db.session.commit()
    return jsonify({'message': 'Supplier deleted'}), 200


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------

def _po_or_404(po_id):
    po = db.session.get(PurchaseOrder, po_id)
    if po is None:
        return None, (jsonify({'error': 'Purchase order not found'}), 404)
    return po, None


def _next_po_number():
    today = datetime.utcnow().strftime('%Y%m%d')
    prefix = f'PO-{today}-'
    last = (PurchaseOrder.query
            .filter(PurchaseOrder.po_number.like(f'{prefix}%'))
            .order_by(PurchaseOrder.id.desc()).first())
    seq = 1
    if last and last.po_number.startswith(prefix):
        try:
            seq = int(last.po_number.rsplit('-', 1)[1]) + 1
        except (ValueError, IndexError):
            seq = 1
    return f'{prefix}{seq:04d}'


@inventory_bp.route('/purchase_orders', methods=['GET'])
@stock_clerk_api_required
def list_purchase_orders():
    status = (request.args.get('status') or '').strip().lower()
    q = PurchaseOrder.query
    if status:
        if status not in PO_STATUSES:
            return jsonify({'error': f'status must be one of {list(PO_STATUSES)}'}), 400
        q = q.filter_by(status=status)
    rows = q.order_by(PurchaseOrder.created_at.desc()).limit(200).all()
    return jsonify([po.to_dict() for po in rows]), 200


@inventory_bp.route('/purchase_orders/<int:id>', methods=['GET'])
@stock_clerk_api_required
def get_purchase_order(id):
    po, err = _po_or_404(id)
    if err: return err
    return jsonify(po.to_dict()), 200


@inventory_bp.route('/purchase_orders', methods=['POST'])
@stock_clerk_api_required
def create_purchase_order():
    """Create a draft PO.

    Body::

        { "supplier_id": 1, "expected_delivery_date": "2025-01-15",
          "notes": "...",
          "items": [{"inventory_item_id": 5, "quantity_ordered": 50,
                     "unit_cost": 25.00}, ...] }
    """
    data = request.get_json(silent=True) or {}
    supplier_id = data.get('supplier_id')
    if not supplier_id or db.session.get(Supplier, supplier_id) is None:
        return jsonify({'error': 'valid supplier_id is required'}), 400
    items = data.get('items') or []
    if not items:
        return jsonify({'error': 'items list is required'}), 400

    po = PurchaseOrder(
        po_number=_next_po_number(),
        supplier_id=supplier_id,
        created_by=session.get('user_id'),
        status=PO_DRAFT,
        expected_delivery_date=_parse_date(data.get('expected_delivery_date')),
        notes=(data.get('notes') or '').strip() or None,
        total=Decimal('0'),
    )
    db.session.add(po)
    db.session.flush()

    total = Decimal('0')
    for entry in items:
        iid = entry.get('inventory_item_id')
        if not iid or db.session.get(InventoryItem, iid) is None:
            return jsonify({'error': f'inventory item {iid} not found'}), 404
        try:
            qty  = int(entry.get('quantity_ordered', 0))
            cost = Decimal(str(entry.get('unit_cost', 0)))
        except Exception:
            return jsonify({'error': 'quantity_ordered/unit_cost invalid'}), 400
        if qty <= 0 or cost < 0:
            return jsonify({'error': 'quantity_ordered must be > 0, unit_cost >= 0'}), 400
        line_total = (Decimal(qty) * cost)
        db.session.add(PurchaseOrderItem(
            purchase_order_id=po.id,
            inventory_item_id=iid,
            quantity_ordered=qty,
            quantity_received=0,
            unit_cost=cost,
            line_total=line_total,
        ))
        total += line_total
    po.total = total

    audit_service.log(session.get('user_id'), 'po.create',
                      'PurchaseOrder', po.id, None, po.to_dict())
    db.session.commit()
    return jsonify({'message': 'Purchase order created', 'purchase_order': po.to_dict()}), 201


@inventory_bp.route('/purchase_orders/<int:id>/status', methods=['PUT'])
@stock_clerk_api_required
def set_po_status(id):
    """Move a PO between draft/sent/cancelled. Receiving is a separate endpoint."""
    po, err = _po_or_404(id)
    if err: return err
    data = request.get_json(silent=True) or {}
    new_status = (data.get('status') or '').strip().lower()
    if new_status not in (PO_DRAFT, PO_SENT, PO_CANCELLED):
        return jsonify({'error': f'status must be one of '
                                  f'{[PO_DRAFT, PO_SENT, PO_CANCELLED]}'}), 400
    if po.status in (PO_RECEIVED,):
        return jsonify({'error': 'cannot change a fully-received PO'}), 400
    old = po.status
    po.status = new_status
    audit_service.log(session.get('user_id'), 'po.status',
                      'PurchaseOrder', po.id, {'status': old}, {'status': new_status})
    db.session.commit()
    return jsonify({'message': 'Status updated', 'purchase_order': po.to_dict()}), 200


@inventory_bp.route('/purchase_orders/<int:id>/receive', methods=['POST'])
@stock_clerk_api_required
def receive_purchase_order(id):
    """Receive (some or all) of a PO. Increments stock, writes
    StockMovement('restock'), updates batch_number / expiry_date on the
    InventoryItem when this is its first received batch.

    Body::

        { "items": [{"po_item_id": 7, "quantity_received": 50,
                     "batch_number": "B-001", "expiry_date": "2026-12-31"}] }
    """
    po, err = _po_or_404(id)
    if err: return err
    if po.status in (PO_RECEIVED, PO_CANCELLED):
        return jsonify({'error': f'PO is {po.status}'}), 400

    data = request.get_json(silent=True) or {}
    payload_items = data.get('items') or []
    if not payload_items:
        return jsonify({'error': 'items list is required'}), 400

    po_items_by_id = {pi.id: pi for pi in po.items}
    for entry in payload_items:
        pid = entry.get('po_item_id')
        po_item = po_items_by_id.get(pid)
        if not po_item:
            return jsonify({'error': f'po_item {pid} not on this PO'}), 404
        try:
            qty = int(entry.get('quantity_received', 0))
        except (TypeError, ValueError):
            return jsonify({'error': 'quantity_received must be an integer'}), 400
        if qty <= 0:
            continue
        new_received = (po_item.quantity_received or 0) + qty
        if new_received > po_item.quantity_ordered:
            return jsonify({'error': f'cannot receive more than ordered for line {pid}'}), 400
        po_item.quantity_received = new_received

        item = db.session.get(InventoryItem, po_item.inventory_item_id)
        if not item:
            continue
        item.stock = int(item.stock or 0) + qty
        # Capture batch / expiry on first receipt of this item if blank.
        batch  = (entry.get('batch_number') or '').strip() or None
        expiry = _parse_date(entry.get('expiry_date'))
        if batch and not item.batch_number:
            item.batch_number = batch
        elif batch:
            item.batch_number = batch  # always update to most recent batch
        if expiry:
            item.expiry_date = expiry
        if not item.supplier_id:
            item.supplier_id = po.supplier_id

        db.session.add(StockMovement(
            item_id=item.id, item_name=item.name, delta=qty,
            reason='restock', user_id=session.get('user_id'),
            note=f'PO {po.po_number}',
        ))

    # Update PO status based on received vs ordered totals.
    fully = all((pi.quantity_received or 0) >= pi.quantity_ordered for pi in po.items)
    partly = any((pi.quantity_received or 0) > 0 for pi in po.items)
    po.status = PO_RECEIVED if fully else (PO_PARTIAL if partly else po.status)
    if fully:
        po.received_date = date.today()

    audit_service.log(session.get('user_id'), 'po.receive',
                      'PurchaseOrder', po.id, None,
                      {'items': payload_items, 'status': po.status})
    db.session.commit()
    return jsonify({'message': 'PO received', 'purchase_order': po.to_dict()}), 200
