"""Institution rules: finding a member safely, campaign orders, pickup codes,
closing campaigns, and the campaign dashboard. Runs in the bound business.

Member lookup on a public page needs the number and the first name; a
wrong pair answers exactly like an unknown number, and only "Brian O.,
Form 1 East" is shown back. Admission numbers are often sequential, so the
number alone must not reveal anyone.

Pickup: a 6-digit code goes by SMS (guardian and second phone) only when
the order's invoice is paid in full, never on an instalment. Only its HMAC
is stored. Collecting locks the order row; a second scan sees who
collected it and when.
"""
import hashlib
import hmac
import logging
import secrets
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select

from models import (Campaign, CampaignItem, InventoryItem, Invoice, ItemVariant, Member, Order,
                    OrderItem, User, db)
from services import billing, sms_service
from services import orders as order_service
from services.billing.invoices import money
from services.billing.matching import C2B_MATCHERS, Match

logger = logging.getLogger(__name__)

MEMBER_TOKEN_AGE = 30 * 60          # a looked-up member is good for 30 minutes on the page


class InstitutionError(order_service.OrderError):
    pass


# --- members --------------------------------------------------------------------

def masked(member):
    """'Brian Otieno', 'Form 1 East' -> 'Brian O., Form 1 East'."""
    parts = (member.name or "").split()
    name = parts[0] + (f" {parts[-1][0]}." if len(parts) > 1 else "") if parts else "Member"
    return f"{name}, {member.group_label}" if member.group_label else name


def find_member(member_ref, first_name):
    """The member with this number whose first name matches, else None."""
    import re
    ref = re.sub(r"[^A-Z0-9]", "", (member_ref or "").upper())
    given = (first_name or "").strip().casefold()
    if not ref or not given:
        return None
    member = Member.query.filter_by(member_ref=ref).first()
    if member is None or member.member_ref != ref:
        return None
    first = (member.name or "").split()[0].casefold() if member.name else ""
    return member if hmac.compare_digest(first, given) else None


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="campaign-member")


def member_token(member, campaign):
    return _serializer().dumps({"m": member.id, "c": campaign.id})


def member_from_token(token, campaign):
    try:
        data = _serializer().loads(token or "", max_age=MEMBER_TOKEN_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if data.get("c") != campaign.id:
        return None
    return db.session.get(Member, data.get("m"))


# --- campaign orders -----------------------------------------------------------------

def campaign_lines(campaign, cart):
    """``[{"campaign_item_id", "quantity", "variant_id"?}]`` at campaign prices."""
    by_id = {ci.id: ci for ci in campaign.items}
    lines = []
    for entry in cart or []:
        ci = by_id.get(int(entry.get("campaign_item_id") or 0))
        try:
            qty = int(entry.get("quantity", 0))
        except (TypeError, ValueError):
            raise InstitutionError("quantities must be whole numbers")
        if ci is None:
            raise InstitutionError("that item is not in this campaign")
        if qty <= 0:
            continue
        item = db.session.get(InventoryItem, ci.inventory_item_id)
        variant = None
        if item.has_variants:
            variant = db.session.get(ItemVariant, int(entry.get("variant_id") or 0))
            if variant is None or variant.inventory_item_id != item.id:
                raise InstitutionError(f"choose a size for {item.name}")
        lines.append(order_service.Line(item=item, quantity=qty, unit_price=money(ci.price),
                                        variant=variant))
    if not lines:
        raise InstitutionError("choose at least one item")
    return lines


def place_campaign_order(campaign, member, cart, now=None):
    if not campaign.is_open(now):
        raise InstitutionError("this campaign is not open")
    return order_service.create_order(
        campaign_lines(campaign, cart), member_id=member.id, campaign_id=campaign.id,
        channel="public_link", pay_now=True, now=now,
        notes=f"{campaign.name}: {member.member_ref}")


def check_instalment(invoice, amount):
    """Validate a part payment for a campaign invoice. Returns the amount."""
    order = db.session.get(Order, invoice.order_id) if invoice.order_id else None
    campaign = db.session.get(Campaign, order.campaign_id) if order and order.campaign_id else None
    if campaign is None or not campaign.allow_installments:
        raise InstitutionError("this invoice is paid in full")
    amount = money(amount)
    balance = money(invoice.balance)
    minimum = money(campaign.min_installment or 0)
    if amount <= 0 or amount > balance:
        raise InstitutionError(f"pay between 1 and {balance:,.2f}")
    if amount < balance and amount < minimum:
        raise InstitutionError(f"the smallest instalment is KES {minimum:,.2f}")
    return amount


def instalment_terms(invoice):
    """(allowed, minimum) for the pay page."""
    order = db.session.get(Order, invoice.order_id) if invoice.order_id else None
    campaign = db.session.get(Campaign, order.campaign_id) if order and order.campaign_id else None
    if campaign is None or not campaign.allow_installments:
        return False, None
    return True, float(money(campaign.min_installment or 0))


# --- pickup codes ---------------------------------------------------------------------

def _code_hash(order_id, code):
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, f"pickup:{order_id}:{code}".encode(), hashlib.sha256).hexdigest()


def issue_pickup_codes(order_id=None):
    """Send a code for every campaign order (or just ``order_id``) whose
    invoice is now fully paid and has none yet. Run by the sweep and by the
    pay page's status poll. Commits per order. Returns how many were issued."""
    issued = 0
    q = Order.query.filter(Order.campaign_id.isnot(None), Order.pickup_code_hash.is_(None),
                           Order.picked_up_at.is_(None),
                           Order.fulfillment_status.in_(("pending", "ready")))
    if order_id is not None:
        q = q.filter(Order.id == order_id)
    candidates = q.order_by(Order.id).all()
    for order in candidates:
        invoice = order_service.invoice_for(order)
        if invoice is None or billing.recompute(invoice).status != "paid":
            db.session.commit()
            continue
        order = order_service.lock_order(order)
        if order.pickup_code_hash is not None:
            db.session.commit()
            continue
        code = f"{secrets.randbelow(10 ** 6):06d}"
        order.pickup_code_hash = _code_hash(order.id, code)
        order.fulfillment_status = "ready"
        order.reserved_until = None
        member = db.session.get(Member, order.member_id) if order.member_id else None
        db.session.commit()
        issued += 1
        if member is None:
            continue
        body = (f"{masked(member)}: {order.order_number} is paid. Pickup code {code}. "
                "Show it when collecting.")
        hidden = body.replace(code, "******")
        for phone in filter(None, dict.fromkeys([member.guardian_phone, member.secondary_phone])):
            sms_service.send(phone, body, "pickup_code",
                             dedupe_key=f"pickup:{order.id}:{phone}", stored_body=hidden)
    return issued


def collect(member_ref, code, user_id):
    """Hand over a paid order. Returns (order, None) or (order|None, reason)."""
    import re
    ref = re.sub(r"[^A-Z0-9]", "", (member_ref or "").upper())
    member = Member.query.filter_by(member_ref=ref).first()
    if member is None:
        return None, "no member with that number"
    code = (code or "").strip()
    for order in (Order.query.filter(Order.member_id == member.id,
                                     Order.pickup_code_hash.isnot(None))
                  .order_by(Order.id.desc()).all()):
        if not hmac.compare_digest(order.pickup_code_hash, _code_hash(order.id, code)):
            continue
        order = db.session.execute(select(Order).where(Order.id == order.id).with_for_update()
                                   .execution_options(populate_existing=True)).scalar_one()
        if order.picked_up_at is not None:
            by = db.session.get(User, order.picked_up_by) if order.picked_up_by else None
            return order, (f"already collected {order.picked_up_at:%Y-%m-%d %H:%M} UTC"
                           + (f" by {by.name or by.email}" if by else ""))
        invoice = order_service.invoice_for(order)
        if invoice is None or billing.recompute(invoice).status != "paid":
            return order, "not fully paid"
        order.picked_up_at = datetime.utcnow()
        order.picked_up_by = user_id
        order.fulfillment_status = "collected"
        db.session.flush()
        return order, None
    return None, "wrong code"


# --- closing ---------------------------------------------------------------------------

def close_due_campaigns(now=None):
    """Close campaigns past closes_at: unpaid and part-paid orders give their
    stock back; money paid stays as the member's credit. Commits."""
    now = now or datetime.utcnow()
    counts = {"closed": 0, "released": 0}
    for campaign in Campaign.query.filter(Campaign.status == "open", Campaign.closes_at.isnot(None),
                                          Campaign.closes_at <= now).all():
        for order in Order.query.filter_by(campaign_id=campaign.id, fulfillment_status="pending"):
            invoice = order_service.invoice_for(order)
            if invoice is not None and billing.recompute(invoice).status == "paid":
                continue
            if order_service.release(order, reason="campaign_closed", force=True):
                counts["released"] += 1
        campaign.status = "closed"
        counts["closed"] += 1
        db.session.commit()
    return counts


# --- dashboard -----------------------------------------------------------------------------

def campaign_report(campaign):
    orders = Order.query.filter_by(campaign_id=campaign.id).order_by(Order.id).all()
    totals = defaultdict(Decimal)
    counts = Counter()
    sizes = Counter()
    packing = []
    for order in orders:
        invoice = order_service.invoice_for(order)
        status = invoice.status if invoice is not None else "none"
        counts[status] += 1
        if invoice is not None:
            totals["billed"] += money(invoice.total) if status not in ("void", "expired") else 0
            totals["paid"] += money(invoice.amount_paid)
        if order.fulfillment_status == "collected":
            counts["collected"] += 1
        if status in ("paid", "partial"):
            for oi in OrderItem.query.filter_by(order_id=order.id):
                sizes[oi.item_name] += oi.quantity
        if status == "paid" and order.fulfillment_status != "collected":
            member = db.session.get(Member, order.member_id) if order.member_id else None
            packing.append({"order_number": order.order_number,
                            "member_ref": member.member_ref if member else None,
                            "member": member.name if member else None,
                            "group": member.group_label if member else None,
                            "items": [f"{oi.item_name} ×{oi.quantity}" for oi in
                                      OrderItem.query.filter_by(order_id=order.id)]})
    packing.sort(key=lambda r: (r["group"] or "", r["member"] or ""))
    return {"campaign": campaign.to_dict(),
            "orders": dict(counts), "billed": float(totals["billed"]),
            "paid": float(totals["paid"]),
            "outstanding": float(max(totals["billed"] - totals["paid"], Decimal("0"))),
            "sizes": dict(sorted(sizes.items())), "packing_list": packing}


# --- Paybill: a parent pays with the admission number -------------------------------------

def _match_member_ref(ref):
    member = Member.query.filter_by(member_ref=ref).first()
    return Match(member_id=member.id) if member is not None else None


if _match_member_ref not in C2B_MATCHERS:
    C2B_MATCHERS.append(_match_member_ref)
