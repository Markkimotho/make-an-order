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
from datetime import datetime, timedelta
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


def lock_campaign(campaign):
    return db.session.execute(select(Campaign).where(Campaign.id == campaign.id)
                              .with_for_update().execution_options(populate_existing=True)
                              ).scalar_one()


def place_campaign_order(campaign, member, cart, now=None):
    # Billing lock, then the campaign row: a close cannot slip between the
    # "is it open" check and the order (close takes the same locks).
    from services.billing.invoices import lock_tenant
    lock_tenant()
    campaign = lock_campaign(campaign)
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


def _send_code(order, code):
    """Text the code to the member's phones. The dedupe key carries the
    code's hash, so a rotated code is a new message."""
    member = db.session.get(Member, order.member_id) if order.member_id else None
    if member is None:
        return []
    body = (f"{masked(member)}: {order.order_number} is paid. Pickup code {code}. "
            "Show it when collecting.")
    hidden = body.replace(code, "******")
    return [sms_service.send(phone, body, "pickup_code",
                             dedupe_key=f"pickup:{order.id}:{order.pickup_code_hash[:12]}:{phone}",
                             stored_body=hidden)
            for phone in filter(None, dict.fromkeys([member.guardian_phone,
                                                     member.secondary_phone]))]


def _new_code(order):
    code = f"{secrets.randbelow(10 ** 6):06d}"
    order.pickup_code_hash = _code_hash(order.id, code)
    return code


def _locked_paid(order):
    """Lock order then invoice (billing, order, invoice: the order every
    path uses). Returns (order, paid?)."""
    from services.billing.invoices import lock_tenant
    lock_tenant()
    order = order_service.lock_order(order)
    invoice = order_service.invoice_for(order)
    paid = invoice is not None and billing.recompute(invoice).status == "paid"
    return order, paid


def issue_pickup_codes(order_id=None):
    """Send a code for every campaign order (or just ``order_id``) whose
    invoice is now fully paid and has none yet, and send a new code where
    every message carrying the current one failed. Run by the sweep and by
    the pay page's status poll. Commits per order. Returns how many codes
    went out."""
    issued = 0
    # New codes: campaign orders without one whose invoice is paid (in SQL).
    q = (Order.query.join(Invoice, Invoice.order_id == Order.id)
         .filter(Order.campaign_id.isnot(None), Order.picked_up_at.is_(None),
                 Order.pickup_code_hash.is_(None), Invoice.status == "paid",
                 Order.fulfillment_status.in_(("pending", "ready"))))
    # Resends: only orders with a recent failed pickup message.
    retry = _resend_candidates()
    if order_id is not None:
        q = q.filter(Order.id == order_id)
        retry &= {order_id}
    candidates = {o.id: o for o in q.all()}
    for oid in retry:
        order = db.session.get(Order, oid)
        if order is not None and order.pickup_code_hash is not None \
                and order.picked_up_at is None and order.fulfillment_status == "ready":
            candidates[oid] = order
    for _, order in sorted(candidates.items()):
        if order.pickup_code_hash is not None and not _code_undelivered(order):
            continue
        order, paid = _locked_paid(order)
        if not paid or order.picked_up_at is not None:
            db.session.commit()
            continue
        if order.pickup_code_hash is not None and not _code_undelivered(order):
            db.session.commit()
            continue
        code = _new_code(order)
        order.fulfillment_status = "ready"
        order.reserved_until = None
        db.session.commit()
        _send_code(order, code)
        issued += 1
    return issued


DELIVERY_FAILED = ("Failed", "Rejected", "AbsentSubscriber", "Expired")


def _undelivered(message):
    return message.status in ("failed", "skipped") or message.delivery_status in DELIVERY_FAILED


def _code_undelivered(order):
    """Every message carrying the order's current code failed: refused at
    sending, or reported undelivered by the network."""
    from models import SmsMessage
    rows = SmsMessage.query.filter(
        SmsMessage.dedupe_key.like(f"pickup:{order.id}:{order.pickup_code_hash[:12]}:%")).all()
    return bool(rows) and all(_undelivered(r) for r in rows)


def _resend_candidates(now=None):
    """Orders with a pickup message that failed in the last 3 days: the only
    ones that can need a new code (bounded by recent failures, not by how
    many orders are waiting)."""
    from models import SmsMessage
    now = now or datetime.utcnow()
    rows = (SmsMessage.query.filter(SmsMessage.kind == "pickup_code",
                                    SmsMessage.created_at >= now - timedelta(days=3))
            .filter(db.or_(SmsMessage.status.in_(("failed", "skipped")),
                           SmsMessage.delivery_status.in_(DELIVERY_FAILED))).all())
    ids = set()
    for row in rows:
        try:
            ids.add(int(row.dedupe_key.split(":")[1]))
        except (AttributeError, IndexError, ValueError):
            continue
    return ids


def rotate_pickup_code(order):
    """Staff resend: a new code replaces the old one (which stops working).
    Only for orders still holding their stock (pending or ready): a released
    order's goods may have gone to someone else."""
    order, paid = _locked_paid(order)
    if order.picked_up_at is not None:
        raise InstitutionError("already collected")
    if order.fulfillment_status not in ("pending", "ready"):
        raise InstitutionError(f"order is {order.fulfillment_status}; restock it first")
    if not paid:
        raise InstitutionError("not fully paid")
    code = _new_code(order)
    order.fulfillment_status = "ready"
    db.session.commit()
    return _send_code(order, code)


def collect(member_ref, code, user_id):
    """Hand over a paid order. Returns (order, None) or (order|None, reason)."""
    import re
    ref = re.sub(r"[^A-Z0-9]", "", (member_ref or "").upper())
    member = Member.query.filter_by(member_ref=ref).first()
    if member is None:
        return None, "no member with that number"
    code = (code or "").strip()
    from services.billing.invoices import lock_tenant
    lock_tenant()                     # billing, then order, then invoice: as every path does
    for order in (Order.query.filter(Order.member_id == member.id,
                                     Order.pickup_code_hash.isnot(None))
                  .order_by(Order.id.desc()).all()):
        if not hmac.compare_digest(order.pickup_code_hash, _code_hash(order.id, code)):
            continue
        order = db.session.execute(select(Order).where(Order.id == order.id).with_for_update()
                                   .execution_options(populate_existing=True)).scalar_one()
        # A rotation may have committed since the check above: decide on the
        # locked row, so an old code never works.
        if order.pickup_code_hash is None or not hmac.compare_digest(
                order.pickup_code_hash, _code_hash(order.id, code)):
            return None, "wrong code"
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

def close_campaign(campaign):
    """Close a campaign: unpaid and part-paid orders give their stock back;
    money paid stays as the member's credit. Fully paid orders are kept.
    Used for both a manual close and the scheduled one. Commits.

    The campaign is marked closed first, under the same locks orders are
    placed with, so no order can be added while it is being settled."""
    from services.billing.invoices import lock_tenant
    lock_tenant()
    campaign = lock_campaign(campaign)
    campaign.status = "closed"
    db.session.commit()
    released = 0
    for order in Order.query.filter_by(campaign_id=campaign.id, fulfillment_status="pending").all():
        order, paid = _locked_paid(order)
        if paid or order.fulfillment_status != "pending":
            db.session.commit()
            continue
        if order_service.release(order, reason="campaign_closed", force=True):
            released += 1
        db.session.commit()
    return released


def close_due_campaigns(now=None):
    """Close campaigns past closes_at (the sweep). Returns counts."""
    now = now or datetime.utcnow()
    counts = {"closed": 0, "released": 0}
    for campaign in Campaign.query.filter(Campaign.status == "open", Campaign.closes_at.isnot(None),
                                          Campaign.closes_at <= now).all():
        counts["released"] += close_campaign(campaign)
        counts["closed"] += 1
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
