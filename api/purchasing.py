"""Pricing table, low-stock purchase order drafts and purchase order PDFs.

Routes go on the inventory blueprint (imported at the end of
api/inventory.py), so they live under /inventory like the rest of stock and
purchasing.

  GET  /inventory/pricing                                  admin
  POST /inventory/pricing/apply                            admin
  PUT  /inventory/pricing/defaults                         admin or owner
  POST /inventory/purchase_orders/draft_from_low_stock     stock roles
  GET  /inventory/purchase_orders/<id>.pdf                 stock roles
"""
from decimal import Decimal

from flask import Response, jsonify, request, session

from api.inventory import _next_po_number, _po_or_404, inventory_bp
from auth.auth_middleware import admin_api_required, stock_clerk_api_required
from auth.tenancy import current_tenant_id
from models import (PO_DRAFT, Batch, InventoryItem, PurchaseOrder, PurchaseOrderItem,
                    Supplier, Tenant, User, db)
from services import audit_service, pricing, purchase_order_pdf
from services.clock import now_nairobi
from verticals.features import feature_required


def _tenant():
    return db.session.get(Tenant, current_tenant_id())


def _defaults(tenant):
    return {'default_markup_pct': float(tenant.default_markup_pct),
            'price_rounding': tenant.price_rounding}


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

@inventory_bp.route('/pricing', methods=['GET'])
@admin_api_required
@feature_required('pricing')
def pricing_table():
    """Every item with cost, price, markup used, margin and suggested price.
    ``?flagged=1`` keeps only items priced below cost or under 10% margin."""
    tenant = _tenant()
    items = InventoryItem.query.order_by(InventoryItem.name).all()
    rows = [pricing.row(i, tenant) for i in items]
    if request.args.get('flagged') in ('1', 'true'):
        rows = [r for r in rows if r['flagged']]
    return jsonify({
        'defaults': _defaults(tenant),
        'low_margin_pct': float(pricing.LOW_MARGIN_PCT),
        'counts': {'items': len(items),
                   'below_cost': sum(r['below_cost'] for r in rows),
                   'low_margin': sum(r['low_margin'] for r in rows),
                   'no_cost': sum(r['cost_price'] is None for r in rows)},
        'items': rows,
    }), 200


@inventory_bp.route('/pricing/apply', methods=['POST'])
@admin_api_required
@feature_required('pricing')
def apply_suggested_prices():
    """Set prices to the suggested ones. Body: ``{"item_ids": [..]}`` or
    ``{"all_auto": true}`` (every item in auto mode). Items without a cost
    are skipped and listed; an unknown id refuses the whole request."""
    data = request.get_json(silent=True) or {}
    tenant = _tenant()
    if data.get('all_auto') is True:
        items = InventoryItem.query.filter_by(price_mode='auto').order_by(InventoryItem.id).all()
    else:
        ids = data.get('item_ids')
        if not isinstance(ids, list) or not ids:
            return jsonify({'error': 'Send item_ids (a list) or all_auto: true'}), 400
        items = []
        for raw in ids:
            try:
                item = db.session.get(InventoryItem, int(raw))
            except (TypeError, ValueError):
                item = None
            if item is None:
                return jsonify({'error': f'Item {raw} not found'}), 404
            items.append(item)

    uid = session.get('user_id')
    changed, skipped = [], []
    for item in items:
        suggested = pricing.suggested_price(item, tenant)
        if suggested is None:
            skipped.append(item.id)
            continue
        if pricing.set_price(item, suggested, uid, 'pricing.apply',
                             {'cost_price': str(item.cost_price),
                              'markup_pct': str(pricing.effective_markup(item, tenant)),
                              'rounding': tenant.price_rounding}):
            changed.append(item.id)
    db.session.commit()
    return jsonify({'message': f'{len(changed)} price(s) updated',
                    'updated': changed, 'skipped_no_cost': skipped,
                    'items': [pricing.row(i, tenant) for i in items]}), 200


@inventory_bp.route('/pricing/defaults', methods=['PUT'])
@admin_api_required
@feature_required('pricing')
def update_pricing_defaults():
    """Body: ``{"default_markup_pct": 30, "price_rounding": 5}`` (either)."""
    data = request.get_json(silent=True) or {}
    if 'default_markup_pct' not in data and 'price_rounding' not in data:
        return jsonify({'error': 'Send default_markup_pct and/or price_rounding'}), 400
    markup = rounding = None
    if 'default_markup_pct' in data:
        try:
            markup = pricing.parse_markup(data.get('default_markup_pct'))
        except ValueError:
            markup = None
        if markup is None:
            return jsonify({'error': 'default_markup_pct must be between 0 and 1000'}), 400
    if 'price_rounding' in data:
        try:
            rounding = pricing.parse_rounding(data.get('price_rounding'))
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
    tenant = _tenant()
    before = _defaults(tenant)
    if markup is not None:
        tenant.default_markup_pct = markup
    if rounding is not None:
        tenant.price_rounding = rounding
    audit_service.log(session.get('user_id'), 'tenant.pricing_defaults', 'Tenant', tenant.id,
                      before, _defaults(tenant))
    db.session.commit()
    return jsonify({'message': 'Pricing defaults saved', 'defaults': _defaults(tenant)}), 200


# ---------------------------------------------------------------------------
# Purchase orders: draft from low stock, PDF
# ---------------------------------------------------------------------------

def reorder_quantity(item):
    """The item's reorder quantity, else enough to reach twice its reorder
    level (at least one)."""
    if item.reorder_quantity:
        return int(item.reorder_quantity)
    return max(2 * int(item.reorder_level or 0) - int(item.stock or 0), 1)


def last_unit_cost(item):
    """Unit cost of the latest batch received with a cost, else the item's
    cost price, else 0."""
    batch = (Batch.query.filter(Batch.inventory_item_id == item.id, Batch.unit_cost.isnot(None))
             .order_by(Batch.received_at.desc(), Batch.id.desc()).first())
    if batch is not None:
        return Decimal(batch.unit_cost)
    if item.cost_price is not None:
        return Decimal(item.cost_price)
    return Decimal('0')


@inventory_bp.route('/purchase_orders/draft_from_low_stock', methods=['POST'])
@stock_clerk_api_required
@feature_required('purchasing')
def draft_po_from_low_stock():
    """One draft PO with a line for every item at or below its reorder
    level. ``{"supplier_id": 3}`` keeps it to that supplier's items; with no
    supplier every low item goes in (including items with no supplier).
    Items sold in sizes (variants) are left out: their stock is per size."""
    data = request.get_json(silent=True) or {}
    supplier_id = data.get('supplier_id')
    q = InventoryItem.query.filter(InventoryItem.stock <= InventoryItem.reorder_level,
                                   InventoryItem.has_variants.is_(False))
    supplier = None
    if supplier_id not in (None, ''):
        try:
            supplier = db.session.get(Supplier, int(supplier_id))
        except (TypeError, ValueError):
            supplier = None
        if supplier is None:
            return jsonify({'error': 'Unknown supplier'}), 400
        q = q.filter(InventoryItem.supplier_id == supplier.id)
    items = q.order_by(InventoryItem.name, InventoryItem.id).all()
    if not items:
        who = f' from {supplier.name}' if supplier else ''
        return jsonify({'error': f'Nothing{who} is at or below its reorder level'}), 400

    po = PurchaseOrder(po_number=_next_po_number(), supplier_id=supplier.id if supplier else None,
                       ordered_by=session.get('user_id'), status=PO_DRAFT,
                       notes='Drafted from low stock')
    db.session.add(po)
    db.session.flush()
    for item in items:
        db.session.add(PurchaseOrderItem(po_id=po.id, inventory_item_id=item.id,
                                         quantity_ordered=reorder_quantity(item),
                                         quantity_received=0, unit_cost=last_unit_cost(item)))
    db.session.flush()
    audit_service.log(session.get('user_id'), 'po.create', 'PurchaseOrder', po.id, None,
                      {**po.to_dict(), 'source': 'low_stock'})
    db.session.commit()
    return jsonify({'message': f'Draft {po.po_number} with {len(items)} line(s)',
                    'purchase_order': po.to_dict()}), 201


@inventory_bp.route('/purchase_orders/<int:id>.pdf', methods=['GET'])
@stock_clerk_api_required
@feature_required('purchasing')
def purchase_order_pdf_view(id):
    po, err = _po_or_404(id)
    if err:
        return err
    preparer = db.session.get(User, po.ordered_by) if po.ordered_by else None
    if preparer is None:
        preparer = db.session.get(User, session.get('user_id'))
    pdf = purchase_order_pdf.build(_tenant(), po, preparer, now_nairobi())
    return Response(pdf, mimetype='application/pdf', headers={
        'Content-Disposition': f'inline; filename="{po.po_number}.pdf"'})
