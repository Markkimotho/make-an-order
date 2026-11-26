from flask import Blueprint, g, jsonify, request, session, Response
from models import (
    db, Batch, InventoryItem, Order, OrderItem, StockMovement, User, Customer,
    Category, Supplier, PurchaseOrder, PurchaseOrderItem,
    PO_DRAFT, PO_SENT, PO_PARTIAL, PO_RECEIVED, PO_CANCELLED, PO_STATUSES,
)
from services import billing, controlled, pricing, stock
from services import orders as order_service
from services.clock import today_nairobi
from services.sms_service import SendSMS
from services import audit_service
from auth.auth_middleware import (
    admin_api_required, superuser_api_required, login_required,
    stock_clerk_api_required,
)
from verticals.features import feature_required
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select
from datetime import date, datetime, timedelta
from decimal import Decimal
import logging
import csv
import io

inventory_bp = Blueprint('inventory', __name__)
logger = logging.getLogger(__name__)


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

def _reference_error(data):
    """400 response if supplier_id/category_id name a row this business does
    not have (another business's id, or a made-up one); else None."""
    for key, model, label in (('supplier_id', Supplier, 'supplier'),
                              ('category_id', Category, 'category')):
        value = data.get(key)
        if value in (None, ''):
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            return jsonify({'error': f'{key} must be a number'}), 400
        if db.session.get(model, value) is None:
            return jsonify({'error': f'Unknown {label}'}), 400
    return None


@inventory_bp.route('/items', methods=['GET'])
@login_required
def get_items():
    category = request.args.get('category', '').strip()
    q = InventoryItem.query
    if category:
        q = q.filter_by(category=category)
    items = q.order_by(InventoryItem.category, InventoryItem.name).all()
    soonest = InventoryItem.soonest_batches(i.id for i in items)
    return jsonify([i.to_dict(soonest) for i in items])


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

def _receipt_register(data, item, supplier_name=None, reference=None):
    """Register details for stock arriving (used only for controlled drugs)."""
    supplier = supplier_name
    if not supplier and item.supplier_id:
        found = db.session.get(Supplier, item.supplier_id)
        supplier = found.name if found else None
    return controlled.Register.from_request(
        data, supplier_name=(data.get('supplier_name') or supplier),
        reference_doc_no=(data.get('reference_doc_no') or reference),
    ).with_pharmacist(db.session.get(User, session.get('user_id')))


def _witness_register(data):
    return controlled.Register.from_request(data).with_pharmacist(
        db.session.get(User, session.get('user_id')))


@inventory_bp.route('/items', methods=['POST'])
@admin_api_required
def create_item():
    data        = request.get_json(silent=True) or {}
    bad_ref = _reference_error(data)
    if bad_ref:
        return bad_ref
    name        = data.get('name', '').strip()
    category    = data.get('category', '').strip()
    price       = data.get('price')
    description = (data.get('description') or '').strip() or None
    initial_stock = max(0, int(data.get('stock', 0) or 0))
    icon        = (data.get('icon') or 'fa-box').strip()
    sku         = (data.get('sku') or '').strip() or None
    try:
        priced = pricing.pricing_fields(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    auto_price = (priced.get('price_mode') == 'auto'
                  and data.get('cost_price') not in (None, ''))
    if auto_price:
        price = 0       # set from cost and markup below

    if not name or price is None or not category:
        return jsonify({'error': 'name, price, and category are required'}), 400

    try:
        item = InventoryItem(
            name=name, price=price, category=category,
            description=description, icon=icon, sku=sku,
            barcode=(data.get('barcode') or '').strip() or None,
            cost_price=data.get('cost_price'),
            reorder_level=int(data.get('reorder_level', 5) or 5),
            supplier_id=data.get('supplier_id'),
            category_id=data.get('category_id'),
            unit=(data.get('unit') or 'pcs').strip() or 'pcs',
            is_drug=bool(data.get('is_drug', False)),
            requires_prescription=bool(data.get('requires_prescription', False)),
            is_controlled=bool(data.get('is_controlled', False)),
            drug_schedule=(data.get('drug_schedule') or '').strip() or None,
            **priced,
        )
        if auto_price:
            item.price = pricing.selling_price(
                data.get('cost_price'), pricing.effective_markup(item, g.current_tenant),
                g.current_tenant.price_rounding)
        db.session.add(item)
        db.session.flush()

        if initial_stock > 0:
            item = stock.receive(item, initial_stock,
                                 batch_number=(data.get('batch_number') or '').strip() or None,
                                 expiry_date=_parse_date(data.get('expiry_date')),
                                 unit_cost=data.get('cost_price'), reason='initial_stock',
                                 user_id=session.get('user_id'),
                                 register=_receipt_register(data, item))
        elif item.is_controlled:
            controlled.open_register(item, db.session.get(User, session.get('user_id')))

        db.session.commit()
        logger.info(f"Inventory item '{name}' created (id={item.id}, sku={sku}).")
        return jsonify({'message': f'"{name}" added to inventory', 'item': item.to_dict()}), 201
    except (stock.StockError, ValueError) as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
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
    bad_ref = _reference_error(data)
    if bad_ref:
        return bad_ref
    try:
        priced = pricing.pricing_fields(data)
        new_price = pricing.to_decimal(data['price'], 'price') if 'price' in data else None
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if new_price is not None and new_price < 0:
        return jsonify({'error': 'price cannot be negative'}), 400
    for field, value in priced.items():
        setattr(item, field, value)
    item.name        = data.get('name',        item.name)
    item.category    = data.get('category',    item.category)
    item.description = data.get('description', item.description) or None
    item.icon        = data.get('icon',        item.icon)
    if 'sku' in data:
        item.sku = (data.get('sku') or '').strip() or None
    if 'drug_schedule' in data:
        item.drug_schedule = (data.get('drug_schedule') or '').strip() or None
    if 'is_controlled' in data:
        wanted = bool(data.get('is_controlled'))
        if wanted and not item.is_controlled:
            item.is_controlled = True
            db.session.flush()
            # The register starts at the stock on hand.
            controlled.open_register(item, db.session.get(User, session.get('user_id')))
        elif not wanted and item.is_controlled:
            if controlled.ControlledDrugEntry.query.filter_by(inventory_item_id=item.id).first():
                db.session.rollback()
                return jsonify({'error': 'A drug with register entries stays controlled'}), 409
            item.is_controlled = False
    if 'barcode' in data:
        item.barcode = (data.get('barcode') or '').strip() or None
    if 'cost_price' in data:
        item.cost_price = data.get('cost_price')
    if 'reorder_level' in data:
        item.reorder_level = int(data.get('reorder_level') or 0)
    if 'expiry_date' in data or 'batch_number' in data:
        # Old edit form: these describe the item's only batch. With several
        # batches, each is edited on its own (PUT /inventory/batches/<id>).
        batches = Batch.query.filter_by(inventory_item_id=item.id).all()
        if len(batches) > 1:
            return jsonify({'error': 'This item has several batches; edit them one by one'}), 409
        if batches:
            if 'expiry_date' in data:
                batches[0].expiry_date = _parse_date(data.get('expiry_date'))
            number = (data.get('batch_number') or '').strip()
            if number:
                batches[0].batch_number = number
    if 'supplier_id' in data:
        item.supplier_id = data.get('supplier_id') or None
    if 'category_id' in data:
        item.category_id = data.get('category_id') or None
    if 'unit' in data:
        item.unit = (data.get('unit') or 'pcs').strip() or 'pcs'
    if 'is_drug' in data:
        item.is_drug = bool(data.get('is_drug'))
    if 'requires_prescription' in data:
        item.requires_prescription = bool(data.get('requires_prescription'))
    # Stock is intentionally NOT updated here — use /restock or /stock_adjustment

    # An 'auto' item's price follows its cost and markup; a manual one takes
    # the price sent. Either change is audited.
    uid = session.get('user_id')
    suggested = pricing.suggested_price(item, g.current_tenant)
    if item.price_mode == 'auto' and suggested is not None:
        pricing.set_price(item, suggested, uid, 'pricing.auto',
                          {'cost_price': str(item.cost_price), 'source': 'item_edit'})
    elif new_price is not None:
        pricing.set_price(item, new_price, uid, 'pricing.manual')

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
    if controlled.ControlledDrugEntry.query.filter_by(inventory_item_id=item.id).first():
        return jsonify({'error': f'"{name}" has controlled-drug register entries; the '
                                 'register must be kept, so it cannot be deleted'}), 409
    try:
        db.session.delete(item)
        db.session.commit()
        logger.info(f"Inventory item '{name}' (id={id}) deleted.")
        return jsonify({'message': f'"{name}" removed from inventory'}), 200
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': f'"{name}" has records that must be kept'}), 409
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
    unit_cost = None
    if data.get('unit_cost') not in (None, ''):
        try:
            unit_cost = pricing.to_decimal(data.get('unit_cost'), 'unit_cost')
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        if unit_cost < 0:
            return jsonify({'error': 'unit_cost cannot be negative'}), 400

    try:
        item = stock.receive(item, quantity,
                             batch_number=(data.get('batch_number') or '').strip() or None,
                             expiry_date=_parse_date(data.get('expiry_date')),
                             unit_cost=unit_cost,
                             reason='restock', user_id=session.get('user_id'), note=note,
                             register=_receipt_register(data, item))
        suggested = None
        if unit_cost is not None:
            suggested = pricing.reprice_on_receipt(item, unit_cost, session.get('user_id'))
        db.session.commit()
        logger.info(f"Restocked {quantity}× '{item.name}' → stock={item.stock}.")
        return jsonify({
            'message':   f'Restocked {quantity} units of "{item.name}"',
            'new_stock': item.stock,
            'price':     float(item.price),
            'suggested_price': float(suggested) if suggested is not None else None,
        }), 200
    except stock.StockError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error restocking item {id}: {e}", exc_info=True)
        return jsonify({'error': 'An internal server error occurred'}), 500


# ---------------------------------------------------------------------------
# Low-stock & audit log (admin+)
# ---------------------------------------------------------------------------

LOW_STOCK_RULE = 'stock <= reorder_level'


def _low_stock_items():
    """The one low-stock rule: stock at or below the item's reorder level.
    /inventory/low_stock, /inventory/low_stock_reorder, /reports/low_stock
    and the till's SMS alert all use it."""
    return (InventoryItem.query
            .filter(InventoryItem.stock <= InventoryItem.reorder_level)
            .order_by(InventoryItem.stock.asc(), InventoryItem.name.asc(), InventoryItem.id.asc())
            .all())


@inventory_bp.route('/low_stock', methods=['GET'])
@stock_clerk_api_required
def low_stock():
    items = _low_stock_items()
    soonest = InventoryItem.soonest_batches(i.id for i in items)
    return jsonify({
        'rule':  LOW_STOCK_RULE,
        'count': len(items),
        'items': [i.to_dict(soonest) for i in items],
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
        resolved.append((item, qty))

    if not resolved:
        return jsonify({'error': 'No valid items in cart'}), 400

    # One order, one invoice on the customer's credit terms, stock taken
    # (soonest expiry first) through services/orders.py: all or nothing.
    uid = session.get('user_id')
    try:
        order, invoice = order_service.create_order(
            [order_service.Line(item=item, quantity=qty, unit_price=item.price)
             for item, qty in resolved],
            customer_id=customer.id, channel='staff', pay_now=False,
            due_date=billing.credit_due_date(customer, today_nairobi()), user_id=uid)
    except order_service.CreditCeiling as e:
        db.session.rollback()
        return jsonify({'error': str(e), 'available': float(e.available)}), 409
    except (order_service.OrderError, stock.StockError, billing.BillingError) as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    summary_parts = [item.name if qty == 1 else f"{item.name} ×{qty}" for item, qty in resolved]
    total = invoice.total
    db.session.commit()

    # SMS confirmation
    try:
        if customer and customer.phone_number:
            SendSMS.send_order_confirmation(
                customer.phone_number, customer.name, ', '.join(summary_parts),
                order_id=order.id,
            )
    except Exception:
        logger.warning("SMS send failed after order commit.", exc_info=True)

    return jsonify({
        'message':  f"Order placed — {len(resolved)} item type(s), total {float(total):.2f}",
        'order_id': order.id,
        'order_number': order.order_number,
        'invoice': invoice.to_dict(),
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
    old = item.stock
    try:
        item = stock.adjust(item, delta, batch_number=data.get('batch_number'),
                            expiry_date=_parse_date(data.get('expiry_date')),
                            reason=reason, note=note, user_id=session.get('user_id'),
                            register=_witness_register(data))
    except stock.StockError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    audit_service.log(session.get('user_id'), 'inventory.stock_adjustment',
                      'InventoryItem', item.id,
                      {'stock': old}, {'stock': item.stock, 'reason': reason})
    db.session.commit()
    return jsonify({'message': 'Stock adjusted', 'item': item.to_dict()}), 200


@inventory_bp.route('/low_stock_reorder', methods=['GET'])
@login_required
def low_stock_reorder():
    """Items where ``stock <= reorder_level`` (POS reorder helper, flat list)."""
    rows = _low_stock_items()
    soonest = InventoryItem.soonest_batches(i.id for i in rows)
    return jsonify([i.to_dict(soonest) for i in rows]), 200


@inventory_bp.route('/expiring', methods=['GET'])
@login_required
def expiring_items():
    """Items with ``expiry_date`` within the next ``?days=`` (default 30) days."""
    try:
        days = int(request.args.get('days', 30))
    except ValueError:
        return jsonify({'error': 'days must be an integer'}), 400
    today = today_nairobi()
    cutoff = today + timedelta(days=max(days, 0))
    rows = (db.session.query(Batch, InventoryItem)
            .join(InventoryItem, InventoryItem.id == Batch.inventory_item_id)
            .filter(Batch.quantity_on_hand > 0, Batch.expiry_date.isnot(None),
                    Batch.expiry_date <= cutoff)
            .order_by(Batch.expiry_date.asc(), Batch.id.asc()).all())
    return jsonify([{**item.to_dict(), 'batch': batch.to_dict(),
                     'batch_number': batch.batch_number,
                     'expiry_date': batch.expiry_date.isoformat(),
                     'quantity_on_hand': batch.quantity_on_hand,
                     'days_to_expiry': (batch.expiry_date - today).days,
                     'expired': batch.expiry_date < today}
                    for batch, item in rows]), 200


@inventory_bp.route('/items/<int:id>/batches', methods=['GET'])
@login_required
def item_batches(id):
    item = db.session.get(InventoryItem, id)
    if not item:
        return jsonify({'error': 'Item not found'}), 404
    batches = (Batch.query.filter_by(inventory_item_id=id)
               .order_by(Batch.expiry_date.is_(None), Batch.expiry_date, Batch.id).all())
    return jsonify([b.to_dict() for b in batches]), 200


@inventory_bp.route('/batches/<int:id>', methods=['PUT'])
@admin_api_required
def update_batch(id):
    """Correct a batch's number or expiry, or release a quarantined batch
    (``"quarantined": false``) once its goods are checked. Quantities change
    only through adjustments."""
    batch = db.session.get(Batch, id)
    if not batch:
        return jsonify({'error': 'Batch not found'}), 404
    data = request.get_json(silent=True) or {}
    if 'batch_number' in data:
        number = (data.get('batch_number') or '').strip()
        if not number:
            return jsonify({'error': 'batch_number cannot be empty'}), 400
        batch.batch_number = number
    if 'expiry_date' in data:
        batch.expiry_date = _parse_date(data.get('expiry_date'))
    if data.get('quarantined') is False and batch.quarantined:
        if batch.recalled_at is not None:
            db.session.rollback()
            return jsonify({'error': 'A recalled batch is never released. '
                                     'Write its stock off instead.'}), 400
        if batch.batch_number.upper().startswith(stock.QUARANTINE_BATCH):
            return jsonify({'error': 'Give the checked goods their batch number to release them'}), 400
        batch.quarantined = False
        audit_service.log(session.get('user_id'), 'inventory.batch_release', 'Batch', batch.id,
                          None, batch.to_dict())
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'That batch number already exists for this item'}), 400
    return jsonify({'batch': batch.to_dict()}), 200


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
# Batches, write-offs and recalls (stock roles; texting buyers is admin only)
# ---------------------------------------------------------------------------

BATCH_STATUSES = ('all', 'active', 'expiring', 'expired', 'quarantined', 'recalled')
EXPIRING_DAYS = 90


def _batch_status(batch, today, expiring_days):
    """recalled, quarantined, empty, expired, expiring or active."""
    if batch.recalled_at is not None:
        return 'recalled'
    if batch.quarantined:
        return 'quarantined'
    if not batch.quantity_on_hand:
        return 'empty'
    if batch.expiry_date and batch.expiry_date < today:
        return 'expired'
    if batch.expiry_date and (batch.expiry_date - today).days <= expiring_days:
        return 'expiring'
    return 'active'


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def _batch_row(batch, item, today, expiring_days):
    unit_cost = batch.unit_cost if batch.unit_cost is not None else item.cost_price
    qty = int(batch.quantity_on_hand or 0)
    return {**batch.to_dict(),
            'item_id': item.id, 'item_name': item.name, 'unit': item.unit,
            'is_controlled': bool(item.is_controlled), 'has_variants': bool(item.has_variants),
            'days_to_expiry': (batch.expiry_date - today).days if batch.expiry_date else None,
            'unit_cost': float(_money(unit_cost)) if unit_cost is not None else None,
            'cost_known': unit_cost is not None,
            'value_at_cost': float(_money(unit_cost) * qty),
            'status': _batch_status(batch, today, expiring_days)}


@inventory_bp.route('/batches', methods=['GET'])
@stock_clerk_api_required
@feature_required('batches')
def list_batches():
    """Batches with stock (and every recalled batch), soonest expiry first.

    ``?status=all|active|expiring|expired|quarantined|recalled`` (active
    means sellable, so it includes expiring), ``?item_id=``, ``?days=``
    (expiring window, default 90), ``?include_empty=1``.
    """
    status = (request.args.get('status') or 'all').strip()
    if status not in BATCH_STATUSES:
        return jsonify({'error': 'status must be one of ' + ', '.join(BATCH_STATUSES)}), 400
    try:
        days = max(int(request.args.get('days', EXPIRING_DAYS)), 0)
        item_id = int(request.args['item_id']) if request.args.get('item_id') else None
    except ValueError:
        return jsonify({'error': 'days and item_id must be numbers'}), 400
    include_empty = request.args.get('include_empty') in ('1', 'true')
    today = today_nairobi()
    q = (db.session.query(Batch, InventoryItem)
         .join(InventoryItem, InventoryItem.id == Batch.inventory_item_id))
    if item_id is not None:
        q = q.filter(Batch.inventory_item_id == item_id)
    if not include_empty:
        q = q.filter((Batch.quantity_on_hand > 0) | Batch.recalled_at.isnot(None))
    rows = q.order_by(Batch.expiry_date.is_(None), Batch.expiry_date, Batch.id).all()
    wanted = {'all': None, 'active': {'active', 'expiring'}, 'expiring': {'expiring'},
              'expired': {'expired'}, 'quarantined': {'quarantined'},
              'recalled': {'recalled'}}[status]
    out = [_batch_row(b, i, today, days) for b, i in rows]
    if wanted is not None:
        out = [r for r in out if r['status'] in wanted]
    return jsonify({'today': today.isoformat(), 'status': status, 'expiring_days': days,
                    'count': len(out),
                    'quantity': sum(r['quantity_on_hand'] for r in out),
                    'value_at_cost': float(sum(_money(r['value_at_cost']) for r in out)),
                    'batches': out}), 200


def _batch_or_404(id):
    """(batch, None), or (None, 404 response) for a missing batch or one
    whose item is gone (SQLite without FK enforcement can leave those)."""
    batch = db.session.get(Batch, id)
    if batch is None or db.session.get(InventoryItem, batch.inventory_item_id) is None:
        return None, (jsonify({'error': 'Batch not found'}), 404)
    return batch, None


def _stock_error(e):
    body = {'error': str(e)}
    if isinstance(e, controlled.RegisterIncomplete):
        body['register_missing'] = e.missing
    return jsonify(body), 400


@inventory_bp.route('/batches/<int:id>/write_off', methods=['POST'])
@stock_clerk_api_required
def write_off_batch(id):
    """Write off units of one batch.

    Body: ``{"quantity": 3, "reason": "damaged", "note": "..."}``; reason is
    one of services/stock.WRITE_OFF_REASONS. Controlled drugs also need
    ``witness_name`` and ``witness_reg_no``.
    """
    batch, missing = _batch_or_404(id)
    if missing:
        return missing
    data = request.get_json(silent=True) or {}
    try:
        quantity = int(data.get('quantity') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'quantity must be a whole number'}), 400
    item = db.session.get(InventoryItem, batch.inventory_item_id)
    try:
        summary = stock.write_off(
            item, quantity, batch=batch, reason=(data.get('reason') or '').strip(),
            note=(data.get('note') or '').strip() or None, user_id=session.get('user_id'),
            register=_witness_register(data) if item.is_controlled else None)
    except stock.StockError as e:
        db.session.rollback()
        return _stock_error(e)
    db.session.commit()
    return jsonify({'message': 'Written off', 'write_off': summary,
                    'batch': db.session.get(Batch, id).to_dict()}), 200


@inventory_bp.route('/batches/<int:id>/recall', methods=['POST'])
@stock_clerk_api_required
@feature_required('recalls')
def recall_batch(id):
    """Recall a batch: ``{"reason": "...", "reference": "PPB/RC/..."}``. It is
    held for good; its stock leaves only by write-off."""
    batch, missing = _batch_or_404(id)
    if missing:
        return missing
    data = request.get_json(silent=True) or {}
    try:
        batch = stock.recall_batch(batch, reason=data.get('reason'),
                                   reference=data.get('reference'),
                                   user_id=session.get('user_id'))
    except stock.StockError as e:
        db.session.rollback()
        return _stock_error(e)
    db.session.commit()
    return jsonify({'message': 'Batch recalled', 'batch': batch.to_dict()}), 200


@inventory_bp.route('/batches/<int:id>/buyers', methods=['GET'])
@stock_clerk_api_required
@feature_required('recalls')
def batch_buyers(id):
    """Who has units of this batch: sales and orders, net of returns."""
    from services import recalls
    batch, missing = _batch_or_404(id)
    if missing:
        return missing
    item = db.session.get(InventoryItem, batch.inventory_item_id)
    return jsonify({'batch': _batch_row(batch, item, today_nairobi(), EXPIRING_DAYS),
                    **recalls.buyers_summary(batch)}), 200


@inventory_bp.route('/batches/<int:id>/recall/notify', methods=['POST'])
@admin_api_required
@feature_required('recalls')
def notify_recall(id):
    """Text each buyer with a phone, once per batch."""
    from services import recalls
    batch, missing = _batch_or_404(id)
    if missing:
        return missing
    try:
        counts = recalls.notify_recall(batch, user_id=session.get('user_id'))
    except recalls.RecallError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'message': 'Patients texted', **counts}), 200


def _nairobi_day_start_utc(day):
    """Midnight in Nairobi on ``day`` as the naive UTC datetime the app stores."""
    from datetime import time, timezone
    from services.clock import NAIROBI
    return (datetime.combine(day, time.min, tzinfo=NAIROBI)
            .astimezone(timezone.utc).replace(tzinfo=None))


def _write_off_rows(d_from, d_to, reason=None):
    from models import AuditLog
    from services.clock import now_nairobi
    rows = (AuditLog.query
            .filter(AuditLog.action == 'stock.write_off',
                    AuditLog.created_at >= _nairobi_day_start_utc(d_from),
                    AuditLog.created_at < _nairobi_day_start_utc(d_to + timedelta(days=1)))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(5000).all())
    users = {u.id: (u.name or u.email) for u in
             User.query.filter(User.id.in_({r.user_id for r in rows if r.user_id})).all()} \
        if any(r.user_id for r in rows) else {}
    out = []
    for r in rows:
        v = r.new_value or {}
        if reason and v.get('reason') != reason:
            continue
        when = now_nairobi(r.created_at) if r.created_at else None
        out.append({'id': r.id, 'date': when.strftime('%Y-%m-%d %H:%M') if when else None,
                    'month': when.strftime('%Y-%m') if when else None,
                    'item_id': v.get('item_id'), 'item_name': v.get('item_name'),
                    'batch_id': v.get('batch_id'), 'batch_number': v.get('batch_number'),
                    'expiry_date': v.get('expiry_date'),
                    'quantity': int(v.get('quantity') or 0), 'reason': v.get('reason'),
                    'unit_cost': float(_money(v.get('unit_cost'))),
                    'cost': float(_money(v.get('cost'))), 'note': v.get('note'),
                    'user_id': r.user_id, 'by': users.get(r.user_id)})
    return out


@inventory_bp.route('/write_offs', methods=['GET'])
@stock_clerk_api_required
def write_offs_report():
    """Write-offs between ``?from=`` and ``?to=`` (Nairobi dates, default
    this month), optional ``?reason=``, with totals by reason and by month
    (quantity and KES at cost). ``?format=csv`` downloads the rows."""
    today = today_nairobi()
    d_from = _parse_date(request.args.get('from')) or today.replace(day=1)
    d_to = _parse_date(request.args.get('to')) or today
    if d_from > d_to:
        return jsonify({'error': '"from" must not be after "to"'}), 400
    reason = (request.args.get('reason') or '').strip() or None
    if reason and reason not in stock.WRITE_OFF_REASONS:
        return jsonify({'error': 'reason must be one of ' + ', '.join(stock.WRITE_OFF_REASONS)}), 400
    rows = _write_off_rows(d_from, d_to, reason)

    if request.args.get('format') == 'csv':
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(['date', 'item', 'batch', 'expiry', 'quantity', 'reason',
                    'unit_cost', 'cost', 'note', 'by'])
        for r in rows:
            w.writerow([r['date'], r['item_name'], r['batch_number'], r['expiry_date'] or '',
                        r['quantity'], r['reason'], f"{r['unit_cost']:.2f}", f"{r['cost']:.2f}",
                        r['note'] or '', r['by'] or ''])
        name = f'write-offs-{d_from.isoformat()}-to-{d_to.isoformat()}.csv'
        return Response(out.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename={name}'})

    by_reason = {k: {'quantity': 0, 'cost': Decimal('0.00')} for k in stock.WRITE_OFF_REASONS}
    by_month = {}
    for r in rows:
        bucket = by_reason.setdefault(r['reason'], {'quantity': 0, 'cost': Decimal('0.00')})
        bucket['quantity'] += r['quantity']
        bucket['cost'] += _money(r['cost'])
        month = by_month.setdefault(r['month'], {})
        cell = month.setdefault(r['reason'], {'quantity': 0, 'cost': Decimal('0.00')})
        cell['quantity'] += r['quantity']
        cell['cost'] += _money(r['cost'])

    def plain(totals):
        return {k: {'quantity': v['quantity'], 'cost': float(v['cost'])} for k, v in totals.items()}

    return jsonify({
        'from': d_from.isoformat(), 'to': d_to.isoformat(), 'reason': reason,
        'reasons': list(stock.WRITE_OFF_REASONS),
        'count': len(rows),
        'total_quantity': sum(r['quantity'] for r in rows),
        'total_cost': float(sum((_money(r['cost']) for r in rows), Decimal('0.00'))),
        'by_reason': plain(by_reason),
        'by_month': {m: plain(v) for m, v in sorted(by_month.items(), reverse=True)},
        'write_offs': rows,
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


def _next_po_number(now=None):
    # The Nairobi date: between midnight and 03:00 the UTC date is yesterday.
    from services.clock import today_nairobi
    today = today_nairobi(now).strftime('%Y%m%d')
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
        ordered_by=session.get('user_id'),
        status=PO_DRAFT,
        expected_date=_parse_date(data.get('expected_delivery_date') or data.get('expected_date')),
        notes=(data.get('notes') or '').strip() or None,
    )
    db.session.add(po)
    db.session.flush()

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
        db.session.add(PurchaseOrderItem(
            po_id=po.id,
            inventory_item_id=iid,
            quantity_ordered=qty,
            quantity_received=0,
            unit_cost=cost,
            batch_number=(entry.get('batch_number') or '').strip() or None,
            expiry_date=_parse_date(entry.get('expiry_date')),
        ))
    db.session.flush()

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
    """Receive (some or all) of a PO. Each delivery goes into its own batch
    (the batch number given, else the PO line's, else ``PO-<po_number>``)
    at the line's unit cost, through services/stock.py.

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

    # Two receipts of one PO at once must not both read the same received
    # quantities: lock the PO, then its lines, before checking anything.
    po = db.session.execute(select(PurchaseOrder).where(PurchaseOrder.id == po.id)
                            .with_for_update()
                            .execution_options(populate_existing=True)).scalar_one()
    if po.status in (PO_RECEIVED, PO_CANCELLED):
        db.session.rollback()
        return jsonify({'error': f'PO is {po.status}'}), 400
    lines = db.session.execute(select(PurchaseOrderItem).where(PurchaseOrderItem.po_id == po.id)
                               .order_by(PurchaseOrderItem.id).with_for_update()
                               .execution_options(populate_existing=True)).scalars().all()
    po_items_by_id = {pi.id: pi for pi in lines}
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
        # Each delivery is its own batch, with its own expiry.
        if not item.supplier_id:
            item.supplier_id = po.supplier_id
        try:
            stock.receive(item, qty,
                          batch_number=(entry.get('batch_number') or '').strip()
                          or (po_item.batch_number or None) or f'PO-{po.po_number}',
                          expiry_date=_parse_date(entry.get('expiry_date')) or po_item.expiry_date,
                          unit_cost=po_item.unit_cost, reason='restock',
                          user_id=session.get('user_id'), note=f'PO {po.po_number}',
                          register=_receipt_register(
                              entry, item,
                              supplier_name=(db.session.get(Supplier, po.supplier_id).name
                                             if po.supplier_id else None),
                              reference=po.po_number))
            # The delivery's cost becomes the item's; 'auto' items re-price.
            pricing.reprice_on_receipt(item, po_item.unit_cost, session.get('user_id'))
        except stock.StockError as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400

    # Update PO status based on received vs ordered totals.
    fully = all((pi.quantity_received or 0) >= pi.quantity_ordered for pi in po.items)
    partly = any((pi.quantity_received or 0) > 0 for pi in po.items)
    po.status = PO_RECEIVED if fully else (PO_PARTIAL if partly else po.status)

    audit_service.log(session.get('user_id'), 'po.receive',
                      'PurchaseOrder', po.id, None,
                      {'items': payload_items, 'status': po.status})
    db.session.commit()
    return jsonify({'message': 'PO received', 'purchase_order': po.to_dict()}), 200


# Pricing, low-stock purchase order drafts and PO PDFs live in their own
# module; importing it adds its routes to this blueprint.
from api import purchasing as _purchasing  # noqa: E402,F401
