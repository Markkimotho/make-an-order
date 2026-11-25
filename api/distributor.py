"""Distributor blueprint. URL prefix: ``/distributor``.

* ``GET  /distributor/dashboard``                   deni aging, containers, restock needed
* ``POST /distributor/orders``                      order for a duka (credit or pay now)
* ``GET|POST /distributor/tiers``                   price tiers
* ``PUT  /distributor/tiers/<id>/prices``           tier prices ``{"prices": [{item_id, price}]}``
* ``PUT  /distributor/customers/<id>``              account code, tier, KYC, route, GPS
* ``GET  /distributor/customers/<id>``              profile, credit, containers, phones, order link
* ``POST /distributor/customers/<id>/phones``       alternate payer number
* ``DELETE /distributor/customers/<id>/phones/<pid>``
* ``POST /distributor/containers``                  containers returned (-) or issued (+)
* ``POST /distributor/invoices/<id>/remind``        SMS the balance with the pay link
* ``PUT  /distributor/invoices/<id>/etims``         record a CUIN raised on KRA's portal
"""
import logging
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request, session
from sqlalchemy.exc import IntegrityError

from auth.auth_middleware import admin_api_required, cashier_api_required
from models import (Customer, CustomerPhone, InventoryItem, Invoice, Order, PriceTier, TierPrice,
                    db)
from services import audit_service, billing, stock
from services import orders as order_service
from verticals.distributor import service as distributor

logger = logging.getLogger(__name__)
distributor_bp = Blueprint("distributor", __name__)


def _error(message, status=400, **extra):
    return jsonify({"error": message, **extra}), status


@distributor_bp.route("/dashboard", methods=["GET"])
@cashier_api_required
def dashboard():
    restock = Order.query.filter_by(fulfillment_status="restock_needed").order_by(Order.id).all()
    return jsonify({"aging": distributor.aging(),
                    "containers": distributor.container_balances(),
                    "restock_needed": [o.to_dict() for o in restock],
                    "review": {k: len(v) for k, v in billing.review_queue().items()}}), 200


@distributor_bp.route("/orders", methods=["POST"])
@cashier_api_required
def place_order():
    body = request.get_json(silent=True) or {}
    try:
        order, invoice = distributor.place_order(
            body.get("customer_id"), body.get("items"), pay_now=bool(body.get("pay_now")),
            channel="staff", user_id=session.get("user_id"),
            delivery_address=(body.get("delivery_address") or "").strip() or None)
    except distributor.CreditCeiling as e:
        db.session.rollback()
        return _error(str(e), 409, available=float(e.available), can_pay_now=True)
    except (order_service.OrderError, stock.StockError, billing.BillingError) as e:
        db.session.rollback()
        return _error(str(e))
    audit_service.log(session.get("user_id"), "distributor.order", "Order", order.id, None,
                      {"invoice": invoice.invoice_number, "total": str(invoice.total)})
    db.session.commit()
    return jsonify({"order": order.to_dict(), "invoice": invoice.to_dict()}), 201


@distributor_bp.route("/tiers", methods=["GET", "POST"])
@admin_api_required
def tiers():
    if request.method == "GET":
        return jsonify([t.to_dict() for t in PriceTier.query.order_by(PriceTier.name)]), 200
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not name:
        return _error("name is required")
    tier = PriceTier(name=name)
    db.session.add(tier)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _error("a tier with that name exists")
    return jsonify(tier.to_dict()), 201


@distributor_bp.route("/tiers/<int:tier_id>/prices", methods=["PUT"])
@admin_api_required
def tier_prices(tier_id):
    if db.session.get(PriceTier, tier_id) is None:
        return _error("tier not found", 404)
    rows = (request.get_json(silent=True) or {}).get("prices") or []
    for entry in rows:
        item = db.session.get(InventoryItem, entry.get("inventory_item_id") or entry.get("item_id"))
        if item is None:
            db.session.rollback()
            return _error("item not found", 404)
        try:
            price = Decimal(str(entry.get("price")))
        except (InvalidOperation, TypeError):
            db.session.rollback()
            return _error("price must be a number")
        if price < 0:
            db.session.rollback()
            return _error("price cannot be negative")
        row = TierPrice.query.filter_by(price_tier_id=tier_id, inventory_item_id=item.id).first()
        if row is None:
            db.session.add(TierPrice(price_tier_id=tier_id, inventory_item_id=item.id, price=price))
        else:
            row.price = price
    db.session.commit()
    return jsonify([r.to_dict() for r in TierPrice.query.filter_by(price_tier_id=tier_id)]), 200


PROFILE_FIELDS = ("account_ref", "national_id", "physical_address", "kra_pin", "route_zone",
                  "gps_lat", "gps_lng", "price_tier_id", "credit_limit", "credit_term_days")


@distributor_bp.route("/customers/<int:customer_id>", methods=["GET", "PUT"])
@cashier_api_required
def customer_profile(customer_id):
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return _error("customer not found", 404)
    if request.method == "PUT":
        if session.get("role") not in ("admin", "superuser"):
            return _error("admins only", 403)
        body = request.get_json(silent=True) or {}
        old = customer.to_dict()
        try:
            for field in PROFILE_FIELDS:
                if field not in body:
                    continue
                value = body[field]
                if field == "price_tier_id" and value and db.session.get(PriceTier, value) is None:
                    return _error("price tier not found", 404)
                if field == "credit_term_days" and int(value) not in (7, 14, 30):
                    return _error("credit_term_days must be 7, 14 or 30")
                setattr(customer, field, value if value not in ("",) else None)
            if body.get("new_order_link"):
                import secrets
                customer.public_token = secrets.token_urlsafe(16)[:22]
            db.session.flush()
        except (ValueError, TypeError) as e:
            db.session.rollback()
            return _error(str(e))
        except IntegrityError:
            db.session.rollback()
            return _error("that account code is taken")
        audit_service.log(session.get("user_id"), "customer.profile", "Customer", customer.id,
                          old, customer.to_dict())
        db.session.commit()
    out = customer.to_dict()
    out.update({"credit": distributor.credit_summary(customer),
                "containers": distributor.container_balances(customer.id),
                "phones": [p.to_dict() for p in CustomerPhone.query.filter_by(
                    customer_id=customer.id).order_by(CustomerPhone.id)],
                "order_link": f"/o/{_slug()}/d/{customer.public_token}"})
    return jsonify(out), 200


def _slug():
    from auth.tenancy import current_tenant_id
    from models import Tenant
    return db.session.get(Tenant, current_tenant_id()).slug


@distributor_bp.route("/customers/<int:customer_id>/phones", methods=["POST"])
@admin_api_required
def add_phone(customer_id):
    if db.session.get(Customer, customer_id) is None:
        return _error("customer not found", 404)
    body = request.get_json(silent=True) or {}
    try:
        row = distributor.add_phone(customer_id, body.get("phone"), body.get("label"))
    except order_service.OrderError as e:
        db.session.rollback()
        return _error(str(e))
    db.session.commit()
    return jsonify(row.to_dict()), 201


@distributor_bp.route("/customers/<int:customer_id>/phones/<int:phone_id>", methods=["DELETE"])
@admin_api_required
def remove_phone(customer_id, phone_id):
    row = db.session.get(CustomerPhone, phone_id)
    if row is None or row.customer_id != customer_id:
        return _error("not found", 404)
    db.session.delete(row)
    db.session.commit()
    return jsonify({"message": "removed"}), 200


@distributor_bp.route("/containers", methods=["POST"])
@cashier_api_required
def containers():
    body = request.get_json(silent=True) or {}
    try:
        row = distributor.record_containers(body.get("customer_id"), body.get("inventory_item_id"),
                                            body.get("delta", 0), body.get("note"),
                                            session.get("user_id"))
    except (order_service.OrderError, ValueError, TypeError) as e:
        db.session.rollback()
        return _error(str(e))
    db.session.commit()
    return jsonify({"movement": row.to_dict(),
                    "balance": distributor.container_balance(row.customer_id,
                                                             row.inventory_item_id)}), 201


@distributor_bp.route("/invoices/<int:invoice_id>/remind", methods=["POST"])
@cashier_api_required
def remind(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None:
        return _error("invoice not found", 404)
    try:
        message = distributor.remind(invoice)
    except (order_service.OrderError, billing.BillingError) as e:
        db.session.rollback()
        return _error(str(e))
    return jsonify({"sms": message.to_dict()}), 200


@distributor_bp.route("/invoices/<int:invoice_id>/etims", methods=["PUT"])
@admin_api_required
def etims(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None:
        return _error("invoice not found", 404)
    cuin = ((request.get_json(silent=True) or {}).get("cuin") or "").strip()
    if cuin and len(cuin) > 64:
        return _error("CUIN too long")
    old = invoice.external_etims_ref
    invoice.external_etims_ref = cuin or None
    audit_service.log(session.get("user_id"), "invoice.etims_ref", "Invoice", invoice.id,
                      {"external_etims_ref": old}, {"external_etims_ref": invoice.external_etims_ref})
    db.session.commit()
    return jsonify({"invoice": invoice.to_dict(), "etims": distributor.etims_badge(invoice)}), 200
