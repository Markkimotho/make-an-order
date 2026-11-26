"""Runs the WhatsApp bot for one business: webhooks in, replies out.

One inbound message, in one transaction:
1. Lock the business row, then the customer's chat row (always in that
   order, as place_order and the M-Pesa code lock the business row too).
   Messages from one customer are handled one at a time.
2. Insert the message under a unique (business, Meta message id) index. A
   message seen before (Meta retries webhooks) stops here.
3. A chat handed to a person stays quiet (staff answer from the WhatsApp
   chats page) until the customer types MENU or staff hand it back.
4. Otherwise ``bot.step`` decides the answer. An action it asks for (place
   the order, list orders, hand over) runs inside the same transaction
   through the online shop's own code, so prices, stock holds and the
   prescription rules are those of the web shop.
5. Commit: the message, the order, the emptied cart and the awaited order
   number land together or not at all. A second tap on "Yes" finds no cart.
6. Only then, with no lock held: the M-Pesa prompt, and the replies.

If anything fails before the commit, the webhook answers 500 and Meta sends
the message again; after the commit, a retry is a duplicate and is dropped.

Notices (payment received, order ready) go through a small outbox: a
WhatsAppMessage row with a unique ``dedupe_key`` is queued first, then
claimed and sent. Two callbacks or sweeps at once cannot both send it, and
one that failed is retried by the payments sweep.

Meta delivers business messages only within 24 hours of the customer's last
message (by Meta's own timestamp). ``send`` checks that for every message;
outside it nothing goes to Meta, and order updates fall back to SMS
(services/online_shop.notify).
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from models import Customer, Invoice, Order, Tenant, WhatsAppChat, WhatsAppMessage, db
from models.types import SecretsKeyMissing
from services import billing
from services import online_shop as shop
from services.billing.invoices import lock_tenant
from services.phone import InvalidPhone, normalize_ke_phone
from services.whatsapp import bot
from services.whatsapp.client import (OUTSIDE_WINDOW, WhatsAppClient, WhatsAppError,
                                      WhatsAppNotConfigured, readable)

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)            # Meta's customer service window
CART_KEEP = timedelta(days=3)           # a cart left longer starts empty
HANDOFF_LAPSE = timedelta(hours=24)     # an unanswered handover goes back to the bot
RECENT_ORDERS = 5
MAX_ATTEMPTS = 4                        # sends of one queued notice
OUTSIDE = "outside WhatsApp's 24-hour window"


class WindowClosed(RuntimeError):
    """The customer last wrote more than 24 hours ago."""


class WebhookFailed(RuntimeError):
    """A message could not be handled; Meta should deliver it again."""


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


def in_window(chat, now=None):
    now = now or _now()
    return chat.last_inbound_at is not None and now - chat.last_inbound_at <= WINDOW


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

# Meta message types a person can open; stickers are left as "other".
MEDIA_KINDS = {"image": "photo", "document": "document", "audio": "voice note",
               "video": "video"}


def media_of(message):
    """(media id, mime type, file name) of a photo, document, voice note or
    video, else (None, None, None)."""
    kind = message.get("type")
    if kind not in MEDIA_KINDS:
        return None, None, None
    media = message.get(kind) or {}
    media_id = str(media.get("id") or "")[:128] or None
    name = str(media.get("filename") or "")[:255] or None
    return media_id, str(media.get("mime_type") or "")[:100] or None, name


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
    if kind in MEDIA_KINDS:
        media = message.get(kind) or {}
        return bot.Event("media", what=MEDIA_KINDS[kind],
                         text=str(media.get("caption") or "")[:1000])
    if kind == "location":
        loc = message.get("location") or {}
        words = ", ".join(str(loc[k]) for k in ("name", "address") if loc.get(k))
        if not words and loc.get("latitude") is not None:
            words = (f"Pin: https://maps.google.com/?q={loc.get('latitude')},"
                     f"{loc.get('longitude')}")
        return bot.Event("location", address=words)
    return bot.Event("other")


def sent_at(message, now):
    """When the customer sent it, by Meta's timestamp (never in the future).
    A message delivered late must not reopen the 24-hour window."""
    try:
        when = datetime.utcfromtimestamp(int(message.get("timestamp")))
    except (TypeError, ValueError, OverflowError, OSError):
        return now
    return min(when, now)


def _inbound_text(message, event):
    if event.kind == "text":
        return event.text
    if event.kind == "choice":
        return f"[tapped] {event.text or event.choice}"
    if event.kind == "location":
        return f"[location] {event.address}"
    if event.kind == "media":
        return f"[{event.what}]" + (f" {event.text}" if event.text else "")
    return f"[{message.get('type') or 'message'}]"


def handle_webhook(tenant, body):
    """Everything in one webhook delivery. Returns counts, for the log.
    Raises WebhookFailed when a message could not be handled, after trying
    the rest: the caller answers 500 so Meta delivers it again."""
    counts = {"messages": 0, "duplicates": 0, "statuses": 0, "ignored": 0, "failed": 0}
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
                    counts["failed"] += 1
                    continue
                counts["messages" if done else "duplicates"] += 1
    if counts["failed"]:
        raise WebhookFailed(str(counts))
    return counts


STATUS_ORDER = ("queued", "sending", "sent", "delivered", "read")


def record_status(status):
    """sent / delivered / read / failed for one of our messages."""
    msg = WhatsAppMessage.query.filter_by(wa_message_id=str(status.get("id") or "")).first()
    if msg is None or msg.direction != "out":
        return
    new = status.get("status")
    if new == "failed":
        errors = status.get("errors") or [{}]
        msg.status = "failed"
        msg.error = str(errors[0].get("title") or errors[0].get("message") or "failed")[:255]
    elif new in STATUS_ORDER and (msg.status not in STATUS_ORDER
                                  or STATUS_ORDER.index(new) > STATUS_ORDER.index(msg.status)):
        msg.status = new                       # statuses can arrive out of order
    db.session.commit()


# --- one inbound message --------------------------------------------------------------

def _lock_chat(chat_id):
    return db.session.execute(
        select(WhatsAppChat).where(WhatsAppChat.id == chat_id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one()


def _chat_for(wa_id, profile_name):
    """The chat row for this customer, locked; created on first contact.
    Call after lock_tenant(): holding the business lock, check-then-insert
    cannot race (the unique index is the last guard)."""
    chat = db.session.execute(
        select(WhatsAppChat).where(WhatsAppChat.wa_id == wa_id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if chat is None:
        chat = WhatsAppChat(wa_id=wa_id, state={}, mode="bot", needs_reply=False,
                            profile_name=(profile_name or "")[:120] or None)
        db.session.add(chat)
        db.session.flush()
    elif profile_name and chat.profile_name != profile_name[:120]:
        chat.profile_name = profile_name[:120]
    return chat


def _fresh_state(chat, now):
    """A chat back after a day starts at the menu; a cart older than three
    days is dropped. Orders still waiting for payment are kept."""
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
    when = sent_at(message, now)

    lock_tenant()
    if WhatsAppMessage.query.filter_by(wa_message_id=wa_message_id).first() is not None:
        db.session.rollback()                   # Meta's retry of a message we have
        return False
    chat = _chat_for(wa_id, profile_name)
    media_id, media_type, media_name = media_of(message)
    db.session.add(WhatsAppMessage(chat_id=chat.id, direction="in",
                                   wa_message_id=wa_message_id, kind=event.kind,
                                   body=_inbound_text(message, event)[:4000],
                                   status="received", created_at=now, media_id=media_id,
                                   media_type=media_type, media_name=media_name))
    db.session.flush()
    state = _fresh_state(chat, when)
    if chat.last_inbound_at is None or when > chat.last_inbound_at:
        chat.last_inbound_at = when

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
    prompt = None
    if reply.action:
        reply, prompt = _run(chat, state, reply, ctx, now)
    chat.state = reply.state
    chat_id = chat.id
    db.session.commit()

    messages = reply.messages
    if prompt is not None:
        messages = _send_prompt(prompt, ctx)
    send(tenant, chat_id, messages, reply_to=wa_message_id)
    return True


def _run(chat, state, reply, ctx, now):
    """The bot's action, inside the message's transaction. Returns (reply,
    prompt) where prompt is the M-Pesa push to send after the commit."""
    kind = reply.action[0]
    if kind == "orders":
        return bot.my_orders(reply.state, ctx, recent_orders(ctx.phone)), None
    if kind == "handoff":
        chat.mode, chat.needs_reply, chat.handoff_at = "person", True, now
        what = reply.action[1] if len(reply.action) > 1 else None
        return bot.handed_off(reply.state, ctx, what), None
    if kind == "checkout":
        details = reply.action[1]
        try:
            with db.session.begin_nested():
                order, invoice, _ = shop.place_order({**details, "note": None},
                                                     db.session.get(Tenant, chat.tenant_id),
                                                     channel="whatsapp")
        except shop.ShopError as e:
            return bot.order_failed(reply.state, ctx, str(e)), None
        number, total, link = order.order_number, invoice.total, billing.pay_url(invoice)
        logger.info("whatsapp order %s placed", number)
        placed = bot.order_placed(reply.state, ctx, number, total, link)
        prompt = {"invoice_id": invoice.id, "pay_phone": details["pay_phone"],
                  "number": number, "total": total, "link": link, "state": reply.state}
        return placed, prompt
    raise ValueError(f"unknown action {kind}")


def _send_prompt(prompt, ctx):
    """The M-Pesa push for an order already committed. Returns the messages
    telling the customer what happened."""
    from services.billing import stk
    from services.daraja import DarajaBusy, DarajaError, DarajaNotConfigured, DarajaUnavailable
    problem = None
    try:
        stk.start_stk(db.session.get(Invoice, prompt["invoice_id"]), prompt["pay_phone"])
    except InvalidPhone:
        problem = "That M-Pesa number is not valid."
    except stk.PromptPending:
        problem = "A prompt is already open on that phone. Finish or cancel it first."
    except DarajaBusy:
        problem = "That phone already has an M-Pesa prompt open."
    except DarajaUnavailable:
        problem = "M-Pesa did not answer. If the prompt comes and you pay, it will count."
    except DarajaNotConfigured:
        problem = "M-Pesa prompts are not set up for this shop."
    except (DarajaError, billing.BillingError) as e:
        logger.warning("whatsapp STK for %s failed: %s", prompt["number"], e)
        problem = "The M-Pesa prompt could not be sent."
    except Exception:                         # the order stands; the pay link still works
        logger.exception("whatsapp STK for %s failed", prompt["number"])
        problem = "The M-Pesa prompt could not be sent."
    if problem:
        db.session.rollback()
    return bot.order_placed(prompt["state"], ctx, prompt["number"], prompt["total"],
                            prompt["link"], problem).messages


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


# --- sending ---------------------------------------------------------------------------

def _client(tenant):
    try:
        return WhatsAppClient(tenant), None
    except (WhatsAppNotConfigured, SecretsKeyMissing) as e:
        return None, str(e)


def _deliver(client, chat, row, message):
    """Send one logged row. Sets its status; the caller commits."""
    row.attempts = (row.attempts or 0) + 1
    if not in_window(chat):
        row.status, row.error, row.attempts = "failed", OUTSIDE, MAX_ATTEMPTS
        return
    try:
        row.wa_message_id = client.send(chat.wa_id, message)
        row.status, row.error = "sent", None
        chat.last_outbound_at = _now()
    except WhatsAppError as e:
        detail = f"{e} (code {e.code})" if e.code else str(e)
        row.status, row.error = "failed", detail[:255]
        if e.code == OUTSIDE_WINDOW:
            row.attempts = MAX_ATTEMPTS
        logger.warning("whatsapp send to chat %s failed: %s", chat.id, detail)


def send(tenant, chat_id, messages, reply_to=None, sent_by=None):
    """Send and log each message. A failure is logged on the row, never raised:
    the customer's message is already handled."""
    if not messages:
        return []
    chat = db.session.get(WhatsAppChat, chat_id)
    client, setup_error = _client(tenant)
    if client is not None and reply_to:
        client.mark_read(reply_to)
    rows = []
    for message in messages:
        row = WhatsAppMessage(chat_id=chat_id, direction="out", kind=message["type"],
                              body=readable(message)[:4000], status="queued",
                              sent_by=sent_by, attempts=0, created_at=_now())
        if client is None:
            row.status, row.error, row.attempts = "failed", setup_error[:255], 1
        else:
            _deliver(client, chat, row, message)
        db.session.add(row)
        db.session.commit()
        rows.append(row)
    return rows


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
    row = send(tenant, chat.id, [bot.say(body[:4000])], sent_by=user_id)[0]
    if row.status == "failed" and row.error and f"(code {OUTSIDE_WINDOW})" in row.error:
        raise WindowClosed("WhatsApp says the customer's 24-hour window is closed. Call or "
                           "text them instead.")
    return row


def hand_back(chat):
    chat.mode, chat.needs_reply = "bot", False
    db.session.commit()


# --- the notice outbox -----------------------------------------------------------------

def queue_notice(chat_id, body, key, now=None):
    """Queue a text notice once per ``key``. Returns the row, or None when the
    key was queued before. Call under lock_tenant(); the caller commits."""
    key = key[:80]
    if WhatsAppMessage.query.filter_by(dedupe_key=key).first() is not None:
        return None
    row = WhatsAppMessage(chat_id=chat_id, direction="out", kind="text", body=body[:4000],
                          status="queued", dedupe_key=key, attempts=0,
                          created_at=now or _now())
    db.session.add(row)
    db.session.flush()
    return row


def deliver_notices(tenant, now=None, ids=None):
    """Send queued notices (and retry failed ones). Returns how many went out.
    A row left in "sending" by a crash mid-send is not retried: sending it
    twice is worse than a missed "payment received" (the customer can type
    ORDERS)."""
    now = now or _now()
    q = WhatsAppMessage.query.filter(WhatsAppMessage.dedupe_key.isnot(None),
                                     WhatsAppMessage.direction == "out",
                                     WhatsAppMessage.status.in_(("queued", "failed")),
                                     WhatsAppMessage.attempts < MAX_ATTEMPTS)
    if ids is not None:
        q = q.filter(WhatsAppMessage.id.in_(ids))
    pending = [(r.id, r.status, r.attempts, r.created_at) for r in q.order_by(WhatsAppMessage.id)]
    if not pending:
        return 0
    client, setup_error = _client(tenant)
    sent = 0
    for row_id, status, attempts, created in pending:
        # Claim it under a row lock: a second sender waits, then sees it
        # changed and skips it.
        row = db.session.execute(
            select(WhatsAppMessage).where(WhatsAppMessage.id == row_id).with_for_update()
            .execution_options(populate_existing=True)).scalar_one()
        if row.status != status or row.attempts != attempts:
            db.session.commit()
            continue
        row.status = "sending"
        db.session.commit()
        chat = db.session.get(WhatsAppChat, row.chat_id)
        if client is None:
            row.status, row.error = "failed", setup_error[:255]
            row.attempts = (row.attempts or 0) + 1
        else:
            _deliver(client, chat, row, bot.say(row.body))
        db.session.commit()
        sent += row.status == "sent"
    return sent


def _chat_for_order(order):
    if order.channel != "whatsapp" or order.customer_id is None:
        return None
    customer = db.session.get(Customer, order.customer_id)
    if customer is None:
        return None
    chat = WhatsAppChat.query.filter_by(wa_id=customer.phone_number).first()
    return chat if chat is not None and in_window(chat) else None


def notify_order(order, body):
    """Tell a WhatsApp customer about their order on WhatsApp, once per order
    and status. Returns True when it went (or had gone) out; False means the
    caller should send an SMS instead."""
    tenant = db.session.get(Tenant, order.tenant_id)
    if tenant is None or not connected(tenant):
        return False
    chat = _chat_for_order(order)
    if chat is None:
        return False
    lock_tenant()
    row = queue_notice(chat.id, body, f"order:{order.id}:{order.fulfillment_status}")
    db.session.commit()
    if row is None:
        return True                             # told already
    deliver_notices(tenant, ids=[row.id])
    row = db.session.get(WhatsAppMessage, row.id)
    if row.status == "sent":
        return True
    row.attempts = MAX_ATTEMPTS                 # the SMS goes instead: no WhatsApp retry
    db.session.commit()
    return False


def payment_notices(now=None):
    """Queue "payment received" (or "not paid in time") for chats waiting on
    an order, then send everything queued. Run after M-Pesa callbacks and in
    the payments sweep, in the business's scope. Returns messages sent."""
    now = now or _now()
    tenant = db.session.get(Tenant, _tenant_id())
    if tenant is None or not connected(tenant):
        return 0
    waiting = [c.id for c in WhatsAppChat.query.all() if (c.state or {}).get("awaiting")]
    for chat_id in waiting:
        lock_tenant()
        chat = _lock_chat(chat_id)              # never overwrite a newer cart
        state = dict(chat.state or {})
        keep = []
        for number in state.get("awaiting") or []:
            order = Order.query.filter_by(order_number=number).first()
            invoice = Invoice.query.filter_by(order_id=order.id).first() if order else None
            if order is None or invoice is None:
                continue
            if invoice.status == "paid":
                fulfilment = "delivery" if order.delivery_address else "pickup"
                queue_notice(chat.id, bot.paid_notice(number, fulfilment)["body"],
                             f"paid:{order.id}", now)
            elif order.fulfillment_status in ("released", "cancelled"):
                queue_notice(chat.id, f"Order {number} was not paid in time, so we released "
                                      "the items. Type SHOP to order again.",
                             f"released:{order.id}", now)
            else:
                keep.append(number)
        if keep != list(state.get("awaiting") or []):
            chat.state = {**state, "awaiting": keep}
        db.session.commit()
    return deliver_notices(tenant, now)


def _tenant_id():
    from auth.tenancy import current_tenant_id
    return current_tenant_id()


def media_for(tenant, message_id):
    """(bytes, mime type, file name) for a file a customer sent. Raises
    LookupError when there is none, WhatsAppError when Meta will not give it."""
    msg = db.session.get(WhatsAppMessage, message_id)
    if msg is None or not msg.media_id:
        raise LookupError("No file")
    client, error = _client(tenant)
    if client is None:
        raise WhatsAppNotConfigured(error)
    data, mime = client.media(msg.media_id)
    return data, mime or msg.media_type or "application/octet-stream", msg.media_name
