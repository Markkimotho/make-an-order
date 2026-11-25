"""Public campaign page: /o/<slug>/c/<campaign_slug>.

Shareable to a parents' WhatsApp group. A parent enters the admission (or
house) number and the child's first name; the page shows back only "Brian
O., Form 1 East" so a mistyped number is caught, then takes the order and
goes to the invoice's pay page (full amount or an instalment, if the
campaign allows). A wrong number and a wrong name get the same answer, and
lookups are rate-limited: numbers are often sequential.
"""
import logging

from flask import abort, g, jsonify, render_template, request

from models import Campaign, InventoryItem, ItemVariant, db
from public.limits import limiter
from public.routes import _error, public_bp, public_tenant
from services import stock
from services import orders as order_service
from verticals.institution import service as institution

logger = logging.getLogger(__name__)
NOT_FOUND = "We could not find that number with that first name. Check both and try again."


def _campaign(campaign_slug):
    campaign = Campaign.query.filter_by(slug=campaign_slug).first()
    if campaign is None or campaign.slug != campaign_slug or campaign.status == "draft":
        abort(404)
    return campaign


def _items(campaign):
    out = []
    for ci in campaign.items:
        item = db.session.get(InventoryItem, ci.inventory_item_id)
        if item is None:
            continue
        sizes = ([{"id": v.id, "label": v.label, "in_stock": int(v.stock or 0)}
                  for v in ItemVariant.query.filter_by(inventory_item_id=item.id)
                  .order_by(ItemVariant.id)] if item.has_variants else [])
        out.append({"campaign_item_id": ci.id, "name": item.name, "price": float(ci.price),
                    "sizes": sizes, "in_stock": int(item.stock or 0)})
    return out


@public_bp.route("/<slug>/c/<campaign_slug>", methods=["GET"])
@limiter.limit("60 per minute")
@public_tenant
def campaign_page(slug, campaign_slug):
    campaign = _campaign(campaign_slug)
    return render_template("public/campaign.html", business=g.public_business_name, slug=slug,
                           campaign=campaign, is_open=campaign.is_open(), items=_items(campaign))


@public_bp.route("/<slug>/c/<campaign_slug>/lookup", methods=["POST"])
@limiter.limit("10 per minute")
@public_tenant
def campaign_lookup(slug, campaign_slug):
    campaign = _campaign(campaign_slug)
    body = request.get_json(silent=True) or {}
    member = institution.find_member(body.get("member_ref"), body.get("first_name"))
    if member is None:
        return _error(NOT_FOUND, 404)
    return jsonify({"member": institution.masked(member),
                    "member_token": institution.member_token(member, campaign)}), 200


@public_bp.route("/<slug>/c/<campaign_slug>/order", methods=["POST"])
@limiter.limit("5 per minute")
@public_tenant
def campaign_order(slug, campaign_slug):
    campaign = _campaign(campaign_slug)
    body = request.get_json(silent=True) or {}
    member = institution.member_from_token(body.get("member_token"), campaign)
    if member is None:
        return _error("Look the number up again; the page timed out.", 400)
    try:
        order, invoice = institution.place_campaign_order(campaign, member, body.get("items"))
    except stock.InsufficientStock as e:
        db.session.rollback()
        return _error(f"Only {e.sellable} of {e.item_name} left", 409)
    except (order_service.OrderError, stock.StockError) as e:
        db.session.rollback()
        return _error(str(e), 400)
    db.session.commit()
    logger.info("campaign %s order %s", campaign.slug, order.order_number)
    return jsonify({"order": order.order_number, "total": float(invoice.total),
                    "pay_url": f"/o/{slug}/pay/{invoice.pay_token}"}), 201
