"""Institution blueprint. URL prefix: ``/institution``.

* ``GET|POST /institution/members``                 list (``?q=``) / add
* ``PUT  /institution/members/<id>``                edit
* ``GET|POST /institution/campaigns``               list / create (with items)
* ``PUT  /institution/campaigns/<id>``              edit, open, close
* ``GET  /institution/campaigns/<id>/report``       paid vs pending, sizes, packing list
* ``POST /institution/pickup``                      ``{"member_ref", "code"}``: hand over
* ``POST /institution/orders/<id>/pickup-code``     new code (old one stops working), resent
"""
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request, session
from sqlalchemy.exc import IntegrityError

from auth.auth_middleware import admin_api_required, cashier_api_required
from models import CAMPAIGN_STATUSES, Campaign, CampaignItem, InventoryItem, Member, Order, db
from services import audit_service
from services.phone import InvalidPhone
from verticals.institution import service as institution

logger = logging.getLogger(__name__)
institution_bp = Blueprint("institution", __name__)


def _error(message, status=400):
    return jsonify({"error": message}), status


def _when(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        raise ValueError(f"not a date and time: {value}")


MEMBER_FIELDS = ("member_ref", "name", "group_label", "guardian_phone", "secondary_phone")


@institution_bp.route("/members", methods=["GET", "POST"])
@cashier_api_required
def members():
    if request.method == "GET":
        q = Member.query
        term = (request.args.get("q") or "").strip()
        if term:
            like = f"%{term}%"
            q = q.filter(db.or_(Member.name.ilike(like), Member.member_ref.ilike(like),
                                Member.group_label.ilike(like)))
        return jsonify([m.to_dict() for m in q.order_by(Member.group_label, Member.name)
                        .limit(500)]), 200
    if session.get("role") not in ("admin", "superuser"):
        return _error("admins only", 403)
    body = request.get_json(silent=True) or {}
    if not (body.get("member_ref") and body.get("name")):
        return _error("member_ref and name are required")
    try:
        member = Member(**{k: body.get(k) for k in MEMBER_FIELDS if body.get(k) not in (None, "")})
        db.session.add(member)
        db.session.commit()
    except (InvalidPhone, ValueError) as e:
        db.session.rollback()
        return _error(str(e))
    except IntegrityError:
        db.session.rollback()
        return _error("a member with that number exists")
    return jsonify(member.to_dict()), 201


@institution_bp.route("/members/<int:member_id>", methods=["PUT"])
@admin_api_required
def edit_member(member_id):
    member = db.session.get(Member, member_id)
    if member is None:
        return _error("member not found", 404)
    body = request.get_json(silent=True) or {}
    try:
        for field in MEMBER_FIELDS:
            if field in body:
                setattr(member, field, body[field] or None)
        db.session.commit()
    except (InvalidPhone, ValueError) as e:
        db.session.rollback()
        return _error(str(e))
    except IntegrityError:
        db.session.rollback()
        return _error("a member with that number exists")
    return jsonify(member.to_dict()), 200


def _apply_campaign(campaign, body):
    if "name" in body:
        campaign.name = (body.get("name") or "").strip() or campaign.name
    if "slug" in body or campaign.slug is None:
        slug = re.sub(r"[^a-z0-9-]", "", (body.get("slug") or campaign.name or "").lower()
                      .replace(" ", "-"))[:64]
        if not slug:
            raise ValueError("slug is required")
        campaign.slug = slug
    for field in ("description", "id_label"):
        if field in body:
            setattr(campaign, field, body.get(field) or None if field == "description"
                    else (body.get(field) or "Admission No"))
    if "opens_at" in body:
        campaign.opens_at = _when(body.get("opens_at"))
    if "closes_at" in body:
        campaign.closes_at = _when(body.get("closes_at"))
    if "allow_installments" in body:
        campaign.allow_installments = bool(body.get("allow_installments"))
    if "min_installment" in body:
        value = body.get("min_installment")
        try:
            campaign.min_installment = Decimal(str(value)) if value not in (None, "") else None
        except InvalidOperation:
            raise ValueError("min_installment must be a number")
    if "status" in body:
        if body["status"] not in CAMPAIGN_STATUSES:
            raise ValueError(f"status must be one of {CAMPAIGN_STATUSES}")
        if body["status"] == "open" and not (campaign.items or body.get("items")):
            raise ValueError("add items before opening the campaign")
        campaign.status = body["status"]
    if "items" in body:
        existing = {ci.inventory_item_id: ci for ci in campaign.items}
        keep = set()
        for entry in body.get("items") or []:
            item = db.session.get(InventoryItem, entry.get("inventory_item_id"))
            if item is None:
                raise ValueError("item not found")
            if item.is_controlled:
                raise ValueError(f"{item.name} is a controlled drug")
            try:
                price = Decimal(str(entry.get("price", item.price)))
            except InvalidOperation:
                raise ValueError("price must be a number")
            if price <= 0:
                raise ValueError("prices must be above zero")
            row = existing.get(item.id)
            if row is None:
                row = CampaignItem(inventory_item_id=item.id, price=price)
                campaign.items.append(row)
            else:
                row.price = price
            keep.add(item.id)
        for item_id, row in existing.items():
            if item_id not in keep:
                campaign.items.remove(row)
    if campaign.closes_at and campaign.opens_at and campaign.closes_at <= campaign.opens_at:
        raise ValueError("closes_at must be after opens_at")


@institution_bp.route("/campaigns", methods=["GET", "POST"])
@cashier_api_required
def campaigns():
    if request.method == "GET":
        return jsonify([c.to_dict() for c in Campaign.query.order_by(Campaign.created_at.desc())]), 200
    if session.get("role") not in ("admin", "superuser"):
        return _error("admins only", 403)
    campaign = Campaign(name=((request.get_json(silent=True) or {}).get("name") or "").strip())
    if not campaign.name:
        return _error("name is required")
    try:
        _apply_campaign(campaign, request.get_json(silent=True) or {})
        db.session.add(campaign)
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        return _error(str(e))
    except IntegrityError:
        db.session.rollback()
        return _error("a campaign with that link name exists")
    audit_service.log(session.get("user_id"), "campaign.create", "Campaign", campaign.id, None,
                      campaign.to_dict())
    db.session.commit()
    return jsonify(campaign.to_dict()), 201


@institution_bp.route("/campaigns/<int:campaign_id>", methods=["PUT"])
@admin_api_required
def edit_campaign(campaign_id):
    campaign = db.session.get(Campaign, campaign_id)
    if campaign is None:
        return _error("campaign not found", 404)
    old = campaign.to_dict()
    body = request.get_json(silent=True) or {}
    closing = body.get("status") == "closed" and campaign.status != "closed"
    if closing:
        body = {k: v for k, v in body.items() if k != "status"}
    try:
        _apply_campaign(campaign, body)
        db.session.flush()
    except ValueError as e:
        db.session.rollback()
        return _error(str(e))
    except IntegrityError:
        db.session.rollback()
        return _error("a campaign with that link name exists")
    audit_service.log(session.get("user_id"), "campaign.edit", "Campaign", campaign.id, old,
                      campaign.to_dict())
    db.session.commit()
    if closing:
        # The same settlement as the scheduled close: unpaid and part-paid
        # orders give stock back, money stays as the member's credit.
        released = institution.close_campaign(campaign)
        audit_service.log(session.get("user_id"), "campaign.close", "Campaign", campaign.id,
                          None, {"released_orders": released})
        db.session.commit()
    return jsonify(campaign.to_dict()), 200


@institution_bp.route("/campaigns/<int:campaign_id>/report", methods=["GET"])
@cashier_api_required
def campaign_report(campaign_id):
    campaign = db.session.get(Campaign, campaign_id)
    if campaign is None:
        return _error("campaign not found", 404)
    return jsonify(institution.campaign_report(campaign)), 200


@institution_bp.route("/pickup", methods=["POST"])
@cashier_api_required
def pickup():
    body = request.get_json(silent=True) or {}
    order, problem = institution.collect(body.get("member_ref"), body.get("code"),
                                         session.get("user_id"))
    if problem is not None:
        db.session.rollback()
        status = 409 if order is not None else 404
        return jsonify({"error": problem, "order": order.to_dict() if order else None}), status
    audit_service.log(session.get("user_id"), "order.collected", "Order", order.id, None,
                      {"order_number": order.order_number})
    db.session.commit()
    return jsonify({"order": order.to_dict()}), 200


@institution_bp.route("/orders/<int:order_id>/pickup-code", methods=["POST"])
@cashier_api_required
def resend_pickup_code(order_id):
    order = db.session.get(Order, order_id)
    if order is None or order.campaign_id is None:
        return _error("order not found", 404)
    try:
        messages = institution.rotate_pickup_code(order)
    except institution.InstitutionError as e:
        db.session.rollback()
        return _error(str(e), 409)
    audit_service.log(session.get("user_id"), "order.pickup_code_rotated", "Order", order_id, None,
                      {"sent": [m.status for m in messages]})
    db.session.commit()
    return jsonify({"sent": [m.to_dict() for m in messages]}), 200
