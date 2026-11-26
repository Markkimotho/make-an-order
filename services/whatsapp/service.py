"""Runs the WhatsApp bot for one business: webhooks in, replies out.

One inbound message, in order:
1. Seen before (Meta retries webhooks)? Ignore it. The message row is
   inserted under a unique (tenant, Meta message id) index, so two
   deliveries of the same message can never both get through.
2. Lock the customer's chat row. Messages from one customer are handled one
   at a time, so a double tap cannot place two orders.
3. A chat handed to a person stays quiet (staff answer from the WhatsApp
   chats page) until the customer types MENU or staff hand it back.
4. Otherwise ``bot.step`` decides the answer; an action it asks for (place
   the order, list orders, hand over) runs here through the online shop's
   own code, so prices, stock holds and the prescription rules are the
   same as on the web shop.
5. Commit, then send. No database lock is held while Meta is called.

Business-started messages are only allowed within 24 hours of the
customer's last message (Meta's service window). Outside it nothing is sent
on WhatsApp; order updates fall back to SMS (services/online_shop.notify).
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models import Customer, Invoice, Order, Tenant, WhatsAppChat, WhatsAppMessage, db
from models.types import SecretsKeyMissing
from services import billing
from services import online_shop as shop
from services.phone import InvalidPhone, normalize_ke_phone
from services.whatsapp import bot
from services.whatsapp.client import (OUTSIDE_WINDOW, WhatsAppClient, WhatsAppError,
                                      WhatsAppNotConfigured, readable)

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)            # Meta's customer service window
CART_KEEP = timedelta(days=3)           # a cart left longer starts empty
HANDOFF_LAPSE = timedelta(hours=24)     # an unanswered handover goes back to the bot
RECENT_ORDERS = 5


class WindowClosed(RuntimeError):
    """The customer last wrote more than 24 hours ago."""


def _now():
    return datetime.utcnow()


def local_phone(wa_id):
    """2547XXXXXXXX -> 0712 345 678; '' for a number outside Kenya."""
    try:
        phone = normalize_ke_phone(wa_id)
    except InvalidPhone:
        return ""
    return f"0{phone[3:6]} {phone[6:9]} {phone[9:]}"


def connected(tenant):
    """The business can chat: number, token and the bot switched on."""
    try:
        return bool(tenant.wa_bot_enabled and tenant.wa_phone_number_id
                    and tenant.wa_access_token)
    except SecretsKeyMissing:
        return False


def _mpesa_online(tenant):
    from services.daraja import DarajaClient, DarajaError
    try:
        DarajaClient(tenant)
    except (DarajaError, SecretsKeyMissing):
        return False
    return True


def _ctx(tenant, chat):
    from verticals.features import features_for
    phone = local_phone(chat.wa_id)
    known = ""
    if phone:
        customer = Customer.query.filter_by(
            phone_number=normalize_ke_phone(chat.wa_id)).first()
        known = customer.name if customer is not None else ""
    selling = "online_shop" in features_for(tenant.vertical)
    return bot.Ctx(business=tenant.name,
                   items=shop.catalogue() if selling and tenant.shop_open else [],
                   shop_open=bool(selling and tenant.shop_open),
                   delivery=bool(tenant.shop_delivery),
                   mpesa_online=_mpesa_online(tenant),
                   phone=phone, known_name=known,
                   profile_name=chat.profile_name or "",
                   is_pharmacy="pharmacy" in features_for(tenant.vertical))


# --- webhook payloads -----------------------------------------------------------------

def parse_event(message):
    """Meta's message object -> bot.Event."""
    kind = message.get("type")
    if kind == "text":
        return bot.Event("text", text=(message.get("text") or {}).get("body") or "")
    if kind == "interactive":
        inter = message.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return bot.Event("choice", choice=str(reply.get("id") or "")[:200],
                         text=str(reply.get("title") or ""))
    if kind == "button":                       # a template's quick reply
        button = message.get("button") or {}
        return bot.Event("choice", choice=str(button.get("payload") or "")[:200],
                         text=str(button.get("text") or ""))
    if kind == "location":
        loc = message.get("location") or {}
        words = ", ".join(str(loc[k]) for k in ("name", "address") if loc.get(k))
        if not words and loc.get("latitude") is not None:
            words = (f"Pin: https://maps.google.com/?q={loc.get('latitude')},"
                     f"{loc.get('longitude')}")
        return bot.Event("location", address=words)
    return bot.Event("other")


def _inbound_text(message, event):
    if event.kind == "text":
        return event.text
    if event.kind == "choice":
        return f"[tapped] {event.text or event.choice}"
    if event.kind == "location":
        return f"[location] {event.address}"
    return f"[{message.get('type') or 'message'}]"


def handle_webhook(tenant, body):
    """Everything in one webhook delivery. Returns counts, for the log."""
    counts = {"messages": 0, "duplicates": 0, "statuses": 0, "ignored": 0}
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            meta = value.get("metadata") or {}
            if str(meta.get("phone_number_id") or "") != (tenant.wa_phone_number_id or ""):
                counts["ignored"] += 1          # another number on the same app
                continue
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                     for c in value.get("contacts") or []}
            for status in value.get("statuses") or []:
                record_status(status)
                counts["statuses"] += 1
            for message in value.get("messages") or []:
                try:
                    done = handle_message(tenant, message, names.get(message.get("from")))
                except Exception:               # one bad message never blocks the rest
                    db.session.rollback()
                    logger.exception("whatsapp message %s failed", message.get("id"))
                    counts["failed"] = counts.get("failed", 0) + 1
                    continue
                counts["messages" if done else "duplicates"] += 1
    return counts


def record_status(status):
    """sent / delivered / read / failed for one of our messages."""
    msg = WhatsAppMessage.query.filter_by(wa_message_id=str(status.get("id") or "")).first()
    if msg is None or msg.direction != "out":
        return
    new = status.get("status")
    order = ("queued", "sent", "delivered", "read")
    if new == "failed":
        errors = status.get("errors") or [{}]
        msg.status = "failed"
        msg.error = str(errors[0].get("title") or errors[0].get("message") or "failed")[:255]
    elif new in order and (msg.status not in order or order.index(new) > order.index(msg.status)):
        msg.status = new                       # statuses can arrive out of order
    db.session.commit()


# --- one inbound message --------------------------------------------------------------

def _chat_for(wa_id, profile_name):
    """The chat row for this customer, locked. Created on first contact."""
    for _ in range(2):
        chat = db.session.execute(
            select(WhatsAppChat).where(WhatsAppChat.wa_id == wa_id).with_for_update()
            .execution_options(populate_existing=True)).scalar_one_or_none()
        if chat is not None:
            if profile_name and chat.profile_name != profile_name:
                chat.profile_name = profile_name[:120]
            return chat
        try:
            with db.session.begin_nested():
                db.session.add(WhatsAppChat(wa_id=wa_id, state={}, mode="bot",
                                            needs_reply=False,
                                            profile_name=(profile_name or "")[:120] or None))
        except IntegrityError:                  # first contact, twice at once
            pass
    raise RuntimeError("could not open the chat")


def _fresh_state(chat, now):
    """A chat back after a day starts at the menu; a cart older than three
    days is dropped."""
    state = dict(chat.state or {})
    last = chat.last_inbound_at
    if last is None or now - last <= WINDOW:
        return state
    keep = {"name": state.get("name"), "awaiting": state.get("awaiting")}
    if now - last <= CART_KEEP:
        keep["cart"] = state.get("cart")
    return {k: v for k, v in keep.items() if v}


def handle_message(tenant, message, profile_name=None, now=None):
    """Handle one customer message. Returns False for a duplicate."""
    now = now or _now()
    wa_id = "".join(ch for ch in str(message.get("from") or "") if ch.isdigit())[:20]
    wa_message_id = str(message.get("id") or "")[:128]
    if not wa_id or not wa_message_id:
        return False
    event = parse_event(message)

    chat = _chat_for(wa_id, profile_name)
    try:
        with db.session.begin_nested():
            db.session.add(WhatsAppMessage(chat_id=chat.id, direction="in",
                                           wa_message_id=wa_message_id, kind=event.kind,
                                           body=_inbound_text(message, event)[:4000],
                                           status="received", created_at=now))
    except IntegrityError:
        db.session.rollback()
        return False
    state = _fresh_state(chat, now)
    chat.last_inbound_at = now

    if chat.mode == "person" and chat.handoff_at and now - chat.handoff_at > HANDOFF_LAPSE:
        chat.mode, chat.needs_reply = "bot", False
    if chat.mode == "person":
        typed = " ".join(event.text.lower().split()) if event.kind == "text" else ""
        if typed not in bot.GREETINGS:
            chat.needs_reply = True             # staff answer from the chats page
            chat.state = state
            db.session.commit()
            return True
        chat.mode, chat.needs_reply = "bot", False

    if not connected(tenant):
        chat.state = state
        db.session.commit()
        return True

    ctx = _ctx(tenant, chat)
    reply = bot.step(state, event, ctx)
    chat_id = chat.id
    if reply.action:
        chat.state = reply.state
        db.session.commit()                     # the action commits its own work
        reply = _run(tenant, chat_id, reply, ctx, now)
        chat = db.session.get(WhatsAppChat, chat_id)
    chat.state = reply.state
    db.session.commit()
    send(tenant, chat_id, reply.messages, reply_to=wa_message_id)
    return True


def _run(tenant, chat_id, reply, ctx, now):
    kind = reply.action[0]
    state = reply.state
    if kind == "orders":
        return bot.my_orders(state, ctx, recent_orders(ctx.phone))
    if kind == "handoff":
        chat = db.session.get(WhatsAppChat, chat_id)
        chat.mode, chat.needs_reply, chat.handoff_at = "person", True, now
        db.session.commit()
        return bot.handed_off(state, ctx)
    if kind == "checkout":
        return _checkout(tenant, state, ctx, reply.action[1])
    raise ValueError(f"unknown action {kind}")


def recent_orders(phone):
    if not phone:
        return []
    customer = Customer.query.filter_by(phone_number=normalize_ke_phone(phone)).first()
    if customer is None:
        return []
    rows = []
    for order in (Order.query.filter_by(customer_id=customer.id)
                  .order_by(Order.id.desc()).limit(RECENT_ORDERS)):
        invoice = Invoice.query.filter_by(order_id=order.id).first()
        status = shop.public_status(order, invoice)
        unpaid = (invoice is not None and invoice.status in ("open", "partial")
                  and not status["final"])
        rows.append({"number": order.order_number or f"#{order.id}",
                     "total": order.amount, "status": status["text"],
                     "pay_url": billing.pay_url(invoice) if unpaid else None})
    return rows


def _checkout(tenant, state, ctx, details):
    from services.billing import stk
    from services.daraja import DarajaBusy, DarajaError, DarajaNotConfigured, DarajaUnavailable
    body = {**details, "note": None}
    try:
        order, invoice, _ = shop.place_order(body, tenant, channel="whatsapp")
    except shop.ShopError as e:
        db.session.rollback()
        return bot.order_failed(state, ctx, str(e))
    db.session.commit()
    number, total, pay_link = order.order_number, invoice.total, billing.pay_url(invoice)
    logger.info("whatsapp order %s placed", number)
    problem = None
    try:
        stk.start_stk(invoice, details["pay_phone"])
    except InvalidPhone:
        db.session.rollback()
        problem = "That M-Pesa number is not valid."
    except stk.PromptPending:
        db.session.rollback()
        problem = "A prompt is already open on that phone. Finish or cancel it first."
    except DarajaBusy:
        problem = "That phone already has an M-Pesa prompt open."
    except DarajaUnavailable:
        problem = "M-Pesa did not answer. If the prompt comes and you pay, it will count."
    except DarajaNotConfigured:
        db.session.rollback()
        problem = "M-Pesa prompts are not set up for this shop."
    except (DarajaError, billing.BillingError) as e:
        db.session.rollback()
        logger.warning("whatsapp STK for %s failed: %s", number, e)
        problem = "The M-Pesa prompt could not be sent."
    return bot.order_placed(state, ctx, number, total, pay_link, problem)


# --- sending ---------------------------------------------------------------------------

def send(tenant, chat_id, messages, reply_to=None, sent_by=None):
    """Send and log each message. A failure is logged on the row, never raised:
    the customer's message is already handled."""
    if not messages:
        return []
    chat = db.session.get(WhatsAppChat, chat_id)
    try:
        client = WhatsAppClient(tenant)
    except (WhatsAppNotConfigured, SecretsKeyMissing) as e:
        client, setup_error = None, str(e)
    if client is not None and reply_to:
        client.mark_read(reply_to)
    rows = []
    for message in messages:
        row = WhatsAppMessage(chat_id=chat_id, direction="out", kind=message["type"],
                              body=readable(message)[:4000], status="queued",
                              sent_by=sent_by, created_at=_now())
        if client is None:
            row.status, row.error = "failed", setup_error[:255]
        else:
            try:
                row.wa_message_id = client.send(chat.wa_id, message)
                row.status = "sent"
                chat.last_outbound_at = row.created_at
            except WhatsAppError as e:
                detail = f"{e} (code {e.code})" if e.code else str(e)
                row.status, row.error = "failed", detail[:255]
                logger.warning("whatsapp send to chat %s failed: %s", chat_id, e)
        db.session.add(row)
        db.session.commit()
        rows.append(row)
    return rows


def in_window(chat, now=None):
    now = now or _now()
    return chat.last_inbound_at is not None and now - chat.last_inbound_at <= WINDOW


def staff_reply(tenant, chat, body, user_id):
    """A staff member answers a customer from the chats page."""
    body = (body or "").strip()
    if not body:
        raise ValueError("Type a message")
    if not in_window(chat):
        raise WindowClosed("The customer last wrote more than 24 hours ago, so WhatsApp "
                           "will not deliver a free message. Call or text them instead.")
    chat.needs_reply = False
    db.session.commit()
    rows = send(tenant, chat.id, [bot.say(body[:4000])], sent_by=user_id)
    row = rows[0]
    if row.status == "failed" and row.error and f"(code {OUTSIDE_WINDOW})" in row.error:
        raise WindowClosed("WhatsApp says the customer's 24-hour window is closed. Call or "
                           "text them instead.")
    return row


def hand_back(chat):
    chat.mode, chat.needs_reply = "bot", False
    db.session.commit()


# --- order updates ---------------------------------------------------------------------

def _chat_for_order(order):
    if order.channel != "whatsapp" or order.customer_id is None:
        return None
    customer = db.session.get(Customer, order.customer_id)
    if customer is None:
        return None
    chat = WhatsAppChat.query.filter_by(wa_id=customer.phone_number).first()
    return chat if chat is not None and in_window(chat) else None


def notify_order(order, body):
    """Tell a WhatsApp customer about their order on WhatsApp. Returns True
    when sent; False means the caller should text an SMS instead."""
    tenant = db.session.get(Tenant, order.tenant_id)
    if tenant is None or not connected(tenant):
        return False
    chat = _chat_for_order(order)
    if chat is None:
        return False
    rows = send(tenant, chat.id, [bot.say(body)])
    return bool(rows) and rows[0].status == "sent"


def payment_notices(now=None):
    """Tell each chat waiting on a payment that it came in (or that the hold
    ran out). Run after M-Pesa callbacks and in the payments sweep, in the
    business's scope. Returns how many messages were sent."""
    now = now or _now()
    tenant = db.session.get(Tenant, _tenant_id())
    if tenant is None or not connected(tenant):
        return 0
    sent = 0
    for chat in WhatsAppChat.query.all():
        waiting = list((chat.state or {}).get("awaiting") or [])
        if not waiting:
            continue
        keep, notes = [], []
        for number in waiting:
            order = Order.query.filter_by(order_number=number).first()
            invoice = Invoice.query.filter_by(order_id=order.id).first() if order else None
            if order is None or invoice is None:
                continue
            if invoice.status == "paid":
                notes.append(bot.paid_notice(number, "delivery" if order.delivery_address
                                             else "pickup"))
            elif order.fulfillment_status in ("released", "cancelled"):
                notes.append(bot.say(f"Order {number} was not paid in time, so we released "
                                      "the items. Type SHOP to order again."))
            else:
                keep.append(number)
        if not notes:
            continue
        chat_id = chat.id
        chat.state = {**(chat.state or {}), "awaiting": keep}
        db.session.commit()
        if in_window(db.session.get(WhatsAppChat, chat_id), now):
            rows = send(tenant, chat_id, notes)
            sent += sum(1 for r in rows if r.status == "sent")
    return sent


def _tenant_id():
    from auth.tenancy import current_tenant_id
    return current_tenant_id()
