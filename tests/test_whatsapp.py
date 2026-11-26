"""WhatsApp chatbot end to end: Meta's webhook, the signature, the bot placing
a real order with an M-Pesa prompt, the payment notice, handing a chat to a
person, staff replies, order updates and the settings page.

Meta and Daraja are fakes (no network): FakeMeta answers Graph API calls and
records every message the bot sends."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import requests

from auth.tenancy import tenant_scope, unscoped
from models import (Customer, InventoryItem, Invoice, Order, PaymentIntent, Tenant,
                    WhatsAppChat, WhatsAppMessage, db)
from services import stock
from services.whatsapp import service
from services.whatsapp.client import verify_signature
from tests import daraja_fakes
from tests.conftest import set_vertical

D = Decimal
SECRET = "app-secret-123"
NUMBER_ID = "109876543210"
CUSTOMER = "254712345678"


class FakeMeta:
    def __init__(self, fallback_post, fallback_get):
        self.sent, self.reads, self.gets = [], [], []
        self.fail = None                 # (status, error dict) for the next sends
        self._n = 0
        self._post, self._get = fallback_post, fallback_get

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        if not url.startswith("https://graph.facebook.com/"):
            return self._post(url, json=json, headers=headers, timeout=timeout)
        assert headers["Authorization"] == "Bearer meta-token"
        if json.get("status") == "read":
            self.reads.append(json["message_id"])
            return daraja_fakes.Resp(200, {"success": True})
        if self.fail:
            return daraja_fakes.Resp(*self.fail)
        self._n += 1
        self.sent.append(json)
        return daraja_fakes.Resp(200, {"messaging_product": "whatsapp",
                                       "messages": [{"id": f"wamid.out{self._n}"}]})

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        if not url.startswith("https://graph.facebook.com/"):
            return self._get(url, auth=kw.get("auth"), timeout=timeout)
        self.gets.append(url)
        return daraja_fakes.Resp(200, {"display_phone_number": "+254 700 111222",
                                       "verified_name": "Duka Bora"})

    def texts(self):
        """What the customer read, one string per message."""
        out = []
        for p in self.sent:
            if p["type"] == "text":
                out.append(p["text"]["body"])
            else:
                inter = p["interactive"]
                out.append(inter["body"]["text"])
        return out

    def ids(self, index=-1):
        inter = self.sent[index].get("interactive") or {}
        if inter.get("type") == "button":
            return [b["reply"]["id"] for b in inter["action"]["buttons"]]
        if inter.get("type") == "list":
            return [r["id"] for r in inter["action"]["sections"][0]["rows"]]
        return []


@pytest.fixture
def wa(app, client, monkeypatch):
    """acme as a connected shop: Daraja and WhatsApp set up, a small catalogue."""
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    daraja = daraja_fakes.install(monkeypatch)
    slug, token = daraja_fakes.configure(app, monkeypatch, app.acme_id)
    meta = FakeMeta(requests.post, requests.get)
    monkeypatch.setattr(requests, "post", meta.post)
    monkeypatch.setattr(requests, "get", meta.get)
    set_vertical(app.acme_id, "shop")
    with unscoped():
        t = db.session.get(Tenant, app.acme_id)
        t.wa_phone_number_id = NUMBER_ID
        t.wa_access_token = "meta-token"
        t.wa_app_secret = SECRET
        t.wa_verify_token = "verify-me"
        t.wa_bot_enabled = True
        t.shop_delivery = False
        db.session.commit()
    sugar = InventoryItem(name="Sugar 1kg", price=D("150"), category="Groceries")
    soap = InventoryItem(name="Bar soap", price=D("80"), category="Household")
    rx = InventoryItem(name="Amoxil 500", price=D("300"), category="Medicines", is_drug=True,
                       requires_prescription=True)
    db.session.add_all([sugar, soap, rx])
    db.session.flush()
    stock.receive(sugar, 20, batch_number="S1")
    stock.receive(soap, 10, batch_number="B1")
    stock.receive(rx, 10, batch_number="R1")
    db.session.commit()
    return {"meta": meta, "daraja": daraja, "client": client, "sugar": sugar.id,
            "soap": soap.id, "rx": rx.id, "stk_url": f"/api/payments/daraja/stk/{slug}/{token}"}


_n = [0]


def message(text=None, choice=None, title="", wa_id=CUSTOMER, kind=None, extra=None):
    _n[0] += 1
    msg = {"from": wa_id, "id": f"wamid.in{_n[0]}", "timestamp": "1764150000"}
    if text is not None:
        msg.update(type="text", text={"body": text})
    elif choice is not None:
        msg.update(type="interactive",
                   interactive={"type": "button_reply", "button_reply": {"id": choice,
                                                                        "title": title}})
    else:
        msg.update(type=kind or "image", **(extra or {}))
    return msg


def webhook(messages=(), statuses=(), number_id=NUMBER_ID, name="Achieng"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "254700111222", "phone_number_id": number_id},
            "contacts": [{"profile": {"name": name}, "wa_id": CUSTOMER}],
            "messages": list(messages), "statuses": list(statuses)}}]}]}


def deliver(client, body, secret=SECRET, slug="acme"):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(f"/webhooks/whatsapp/{slug}", data=raw,
                       headers={"Content-Type": "application/json",
                                "X-Hub-Signature-256": sig})


def say(wa, text=None, choice=None, **kw):
    r = deliver(wa["client"], webhook([message(text=text, choice=choice, **kw)]))
    assert r.status_code == 200
    return wa["meta"].texts()[-1] if wa["meta"].sent else None


# --- the webhook --------------------------------------------------------------------------

def test_meta_verification_needs_the_business_verify_token(wa):
    c = wa["client"]
    ok = c.get("/webhooks/whatsapp/acme?hub.mode=subscribe&hub.verify_token=verify-me"
               "&hub.challenge=12345")
    assert ok.status_code == 200 and ok.get_data(as_text=True) == "12345"
    assert c.get("/webhooks/whatsapp/acme?hub.mode=subscribe&hub.verify_token=nope"
                 "&hub.challenge=1").status_code == 403
    assert c.get("/webhooks/whatsapp/nobody?hub.mode=subscribe&hub.verify_token=verify-me"
                 "&hub.challenge=1").status_code == 404


def test_unsigned_or_wrongly_signed_posts_are_refused(wa):
    c = wa["client"]
    body = webhook([message(text="hi")])
    assert c.post("/webhooks/whatsapp/acme", json=body).status_code == 403
    assert deliver(c, body, secret="someone-else").status_code == 403
    assert WhatsAppMessage.query.count() == 0 and wa["meta"].sent == []


def test_signature_check():
    raw = b'{"a":1}'
    good = "sha256=" + hmac.new(b"s", raw, hashlib.sha256).hexdigest()
    assert verify_signature("s", raw, good)
    assert not verify_signature("s", raw + b" ", good)
    assert not verify_signature(None, raw, good) and not verify_signature("s", raw, None)
    assert not verify_signature("s", raw, good.replace("sha256=", "sha1="))


def test_hi_gets_the_menu_and_a_read_receipt(wa):
    body = say(wa, "Hi")
    assert "Welcome to" in body and wa["meta"].ids() == ["m:shop", "m:orders", "m:talk"]
    chat = WhatsAppChat.query.one()
    assert (chat.wa_id, chat.profile_name) == (CUSTOMER, "Achieng")
    assert wa["meta"].reads == ["wamid.in" + str(_n[0])]
    assert [m.direction for m in WhatsAppMessage.query.order_by(WhatsAppMessage.id)] == ["in", "out"]


def test_meta_retries_are_answered_once(wa):
    msg = message(text="hi")
    deliver(wa["client"], webhook([msg]))
    deliver(wa["client"], webhook([msg]))
    assert len(wa["meta"].sent) == 1
    assert WhatsAppMessage.query.filter_by(direction="in").count() == 1


def test_another_number_on_the_same_app_is_ignored(wa):
    deliver(wa["client"], webhook([message(text="hi")], number_id="555"))
    assert wa["meta"].sent == [] and WhatsAppChat.query.count() == 0


def test_bot_switched_off_records_but_does_not_answer(wa, app):
    with unscoped():
        db.session.get(Tenant, app.acme_id).wa_bot_enabled = False
        db.session.commit()
    say(wa, "hi")
    assert wa["meta"].sent == [] and WhatsAppMessage.query.count() == 1


def test_prescription_medicines_are_never_offered(wa):
    say(wa, "hi")
    say(wa, "amoxil")
    assert "Nothing matches" in wa["meta"].texts()[-1]
    say(wa, choice=f"i:{wa['rx']}")
    assert "not available" in wa["meta"].texts()[-1]
    assert not (WhatsAppChat.query.one().state.get("cart"))


# --- a whole order ------------------------------------------------------------------------

def order_two_sugars(wa):
    say(wa, "hi")
    say(wa, choice="m:shop")
    assert wa["meta"].ids() == ["c:Groceries", "c:Household"]
    say(wa, choice="c:Groceries")
    say(wa, choice=f"i:{wa['sugar']}")
    say(wa, choice="q:2")
    say(wa, choice="k:checkout")
    say(wa, choice="n:profile")             # "Use Achieng"
    assert "Pay with M-Pesa from 0712 345 678" in wa["meta"].texts()[-1]
    return say(wa, choice="y:self")


def test_order_on_whatsapp_places_a_held_order_and_sends_the_mpesa_prompt(wa):
    reply = order_two_sugars(wa)
    order = Order.query.one()
    assert order.channel == "whatsapp" and order.fulfillment_status == "pending"
    assert order.reserved_until is not None
    invoice = Invoice.query.filter_by(order_id=order.id).one()
    assert invoice.total == D("300") and invoice.status == "open"
    assert db.session.get(InventoryItem, wa["sugar"]).stock == 18       # held
    assert Customer.query.one().name == "Achieng"
    push = wa["daraja"].posts("/processrequest")[-1]
    assert push["PhoneNumber"] == "254712345678" and push["Amount"] == 300
    assert f"Order {order.order_number} is placed" in reply and "/o/acme/pay/" in reply
    chat = WhatsAppChat.query.one()
    assert "cart" not in chat.state and chat.state["awaiting"] == [order.order_number]


def test_payment_callback_tells_the_customer(wa):
    order_two_sugars(wa)
    number = Order.query.one().order_number
    cri = PaymentIntent.query.one().checkout_request_id
    wa["daraja"].answer("/query", 200, {"ResultCode": "0", "ResultDesc": "ok"})
    r = wa["client"].post(wa["stk_url"],
                          json=daraja_fakes.stk_callback(cri, amount=300))
    assert r.status_code == 200
    assert Invoice.query.one().status == "paid"
    assert wa["meta"].texts()[-1].startswith(f"Payment received for order {number}")
    assert WhatsAppChat.query.one().state["awaiting"] == []
    sent = len(wa["meta"].sent)
    with tenant_scope(wa_tenant(wa)):
        assert service.payment_notices() == 0                     # told once
    assert len(wa["meta"].sent) == sent


def wa_tenant(wa):
    return WhatsAppChat.query.one().tenant_id


def test_out_of_stock_at_checkout_keeps_the_cart(wa):
    say(wa, "hi")
    say(wa, choice=f"i:{wa['soap']}")
    say(wa, "10")
    say(wa, choice="k:checkout")
    say(wa, "Achieng")
    stock_item = db.session.get(InventoryItem, wa["soap"])
    stock.adjust(stock_item, -5, reason="damaged")                 # someone sold 5 meanwhile
    db.session.commit()
    reply = say(wa, choice="y:self")
    assert "could not be placed" in reply and "Not enough Bar soap" in reply
    assert Order.query.count() == 0
    assert WhatsAppChat.query.one().state["cart"][0]["qty"] == 10


def test_my_orders(wa):
    order_two_sugars(wa)
    reply = say(wa, "orders")
    number = Order.query.one().order_number
    assert f"{number}: KES 300, Waiting for payment" in reply and "Pay: " in reply


# --- a person takes over ---------------------------------------------------------------

def test_talk_to_us_hands_over_and_the_bot_goes_quiet(wa, login_as):
    say(wa, "hi")
    reply = say(wa, choice="m:talk")
    assert "will reply here soon" in reply
    sent = len(wa["meta"].sent)
    say(wa, "Do you have Amoxil? I have a prescription")
    assert len(wa["meta"].sent) == sent                         # the bot stays quiet
    chat = WhatsAppChat.query.one()
    assert chat.mode == "person" and chat.needs_reply
    chat_id = chat.id

    staff = login_as("cashier")
    listed = staff.get("/whatsapp/chats").get_json()
    assert listed["waiting"] == 1 and listed["chats"][0]["phone"] == "0712 345 678"
    assert staff.get("/whatsapp/chats/waiting").get_json() == {"count": 1}
    thread = staff.get(f"/whatsapp/chats/{chat_id}").get_json()
    assert thread["messages"][-1]["body"].startswith("Do you have Amoxil")
    r = staff.post(f"/whatsapp/chats/{chat_id}/reply",
                   json={"body": "Yes, bring it to the counter. We open at 8."})
    assert r.status_code == 201, r.get_json()
    assert wa["meta"].texts()[-1] == "Yes, bring it to the counter. We open at 8."
    assert db.session.get(WhatsAppChat, chat_id).needs_reply is False
    assert WhatsAppMessage.query.filter(WhatsAppMessage.sent_by.isnot(None)).count() == 1

    assert staff.post(f"/whatsapp/chats/{chat_id}/hand-back").status_code == 200
    assert "Welcome" in say(wa, "hi")


def test_menu_takes_the_customer_back_to_the_bot(wa):
    say(wa, "hi")
    say(wa, choice="m:talk")
    assert "Welcome" in say(wa, "menu")
    assert WhatsAppChat.query.one().mode == "bot"


def test_staff_cannot_reply_after_24_hours(wa, login_as):
    say(wa, "hi")
    say(wa, choice="m:talk")
    chat = WhatsAppChat.query.one()
    chat.last_inbound_at = datetime.utcnow() - timedelta(hours=25)
    db.session.commit()
    staff = login_as("cashier")
    assert staff.get(f"/whatsapp/chats/{chat.id}").get_json()["chat"]["can_reply"] is False
    r = staff.post(f"/whatsapp/chats/{chat.id}/reply", json={"body": "hello?"})
    assert r.status_code == 409 and "24 hours" in r.get_json()["error"]


def test_a_failed_send_is_logged_not_lost(wa, login_as):
    say(wa, "hi")
    say(wa, choice="m:talk")
    wa["meta"].fail = (400, {"error": {"message": "Re-engagement message", "code": 131047}})
    chat = WhatsAppChat.query.one()
    r = login_as("cashier").post(f"/whatsapp/chats/{chat.id}/reply", json={"body": "hi"})
    assert r.status_code == 409
    assert "24-hour window" in r.get_json()["error"]
    row = WhatsAppMessage.query.filter(WhatsAppMessage.sent_by.isnot(None)).one()
    assert row.status == "failed" and "Re-engagement" in row.error and "131047" in row.error


def test_stock_clerk_cannot_read_chats(wa, login_as):
    say(wa, "hi")
    assert login_as("stock_clerk").get("/whatsapp/chats").status_code == 403


def test_institutions_have_no_chats_page(wa, app, login_as):
    set_vertical(app.acme_id, "institution")
    c = login_as("admin")
    assert c.get("/whatsapp/chats").status_code == 404
    assert c.get("/whatsapp-ui").status_code == 404


def test_chats_page_renders(wa, login_as):
    html = login_as("cashier").get("/whatsapp-ui").get_data(as_text=True)
    assert 'id="compose"' in html and "WhatsApp chats" in html


# --- delivery statuses and order updates -----------------------------------------------

def test_delivery_statuses_update_our_messages_in_order(wa):
    say(wa, "hi")
    out = WhatsAppMessage.query.filter_by(direction="out").one()
    for status in ("delivered", "sent", "read"):          # "sent" arrives late
        deliver(wa["client"], webhook(statuses=[{"id": out.wa_message_id, "status": status,
                                                 "recipient_id": CUSTOMER}]))
    assert db.session.get(WhatsAppMessage, out.id).status == "read"


def test_order_ready_goes_on_whatsapp_inside_the_window_else_sms(wa, login_as, sms_outbox):
    order_two_sugars(wa)
    order = Order.query.one()
    inv = Invoice.query.one()
    from services import billing
    billing.record_payment(method="mpesa_c2b", amount=inv.total, receipt="QWA0000001",
                           invoice=inv)
    db.session.commit()
    cashier = login_as("cashier")
    r = cashier.put(f"/orders/{order.id}/fulfilment", json={"status": "ready"})
    assert r.status_code == 200, r.get_json()
    assert wa["meta"].texts()[-1].startswith(f"Your order {order.order_number} is ready")
    assert sms_outbox.sent == []

    # A day later the window is shut: the next update goes by SMS.
    chat = WhatsAppChat.query.one()
    chat.last_inbound_at = datetime.utcnow() - timedelta(hours=30)
    db.session.commit()
    o2 = Order.query.one()
    o2.fulfillment_status = "pending"
    o2.delivery_address = "Yaya Centre"
    db.session.commit()
    r = cashier.put(f"/orders/{order.id}/fulfilment", json={"status": "dispatched"})
    assert r.status_code == 200
    assert any("out for delivery" in s["message"] for s in sms_outbox.sent)


# --- settings -----------------------------------------------------------------------------

def test_settings_connect_check_and_disconnect(app, login_as, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setitem(app.config, "TENANT_SECRETS_KEY", Fernet.generate_key().decode())
    meta = FakeMeta(requests.post, requests.get)
    monkeypatch.setattr(requests, "post", meta.post)
    monkeypatch.setattr(requests, "get", meta.get)
    set_vertical(app.acme_id, "shop")
    owner = login_as("superuser")

    r = owner.post("/settings/whatsapp", data={"wa_phone_number_id": "0712 345 678",
                                               "wa_bot_enabled": "on"})
    assert r.status_code == 400 and "digits only" in r.get_data(as_text=True)
    r = owner.post("/settings/whatsapp", data={"wa_phone_number_id": NUMBER_ID,
                                               "wa_bot_enabled": "on"})
    assert r.status_code == 400 and "before switching the bot on" in r.get_data(as_text=True)
    r = owner.post("/settings/whatsapp", data={"wa_phone_number_id": NUMBER_ID,
                                               "wa_access_token": "meta-token",
                                               "wa_app_secret": SECRET, "wa_bot_enabled": "on"})
    assert r.status_code == 302
    with unscoped():
        t = db.session.get(Tenant, app.acme_id)
        assert t.wa_bot_enabled and t.wa_access_token == "meta-token" and t.wa_verify_token
        verify = t.wa_verify_token
    page = owner.get("/settings/").get_data(as_text=True)
    assert "/webhooks/whatsapp/acme" in page and verify in page
    assert "meta-token" not in page and SECRET not in page          # write-only

    # A blank token keeps the stored one.
    owner.post("/settings/whatsapp", data={"wa_phone_number_id": NUMBER_ID,
                                           "wa_bot_enabled": "on"})
    with unscoped():
        assert db.session.get(Tenant, app.acme_id).wa_access_token == "meta-token"

    owner.post("/settings/whatsapp/check")
    with unscoped():
        assert db.session.get(Tenant, app.acme_id).wa_display_phone == "+254 700 111222"
    assert meta.gets and NUMBER_ID in meta.gets[0]

    owner.post("/settings/whatsapp/disconnect")
    with unscoped():
        t = db.session.get(Tenant, app.acme_id)
        assert not t.wa_bot_enabled and t.wa_phone_number_id is None and t.wa_access_token is None


def test_one_number_belongs_to_one_business(app, other_tenant, login_as, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setitem(app.config, "TENANT_SECRETS_KEY", Fernet.generate_key().decode())
    set_vertical(app.acme_id, "shop")
    with unscoped():
        db.session.get(Tenant, other_tenant).wa_phone_number_id = NUMBER_ID
        db.session.commit()
    r = login_as("superuser").post("/settings/whatsapp", data={"wa_phone_number_id": NUMBER_ID})
    assert r.status_code == 400 and "already connected" in r.get_data(as_text=True)


def test_only_the_owner_changes_whatsapp_settings(app, login_as):
    set_vertical(app.acme_id, "shop")
    r = login_as("admin").post("/settings/whatsapp", data={"wa_phone_number_id": NUMBER_ID})
    assert r.status_code in (302, 403)
    with unscoped():
        assert db.session.get(Tenant, app.acme_id).wa_phone_number_id is None


def test_tapping_yes_twice_places_one_order(wa):
    order_two_sugars(wa)
    say(wa, choice="y:self")                      # the same old button again
    assert Order.query.count() == 1
    assert len(wa["daraja"].posts("/processrequest")) == 1


def test_a_customer_back_after_a_day_starts_at_the_menu_with_the_cart(wa):
    say(wa, "hi")
    say(wa, choice=f"i:{wa['sugar']}")
    say(wa, choice="q:2")
    say(wa, choice="k:checkout")                  # waiting for a name
    chat = WhatsAppChat.query.one()
    chat.last_inbound_at = datetime.utcnow() - timedelta(hours=30)
    db.session.commit()
    reply = say(wa, "Achieng")                    # not taken as a name any more
    state = WhatsAppChat.query.one().state
    assert "name" not in state and state["cart"][0]["qty"] == 2
    assert "Welcome" in reply and "2 items in your cart" in reply


def test_an_unanswered_handover_goes_back_to_the_bot_after_a_day(wa):
    say(wa, "hi")
    say(wa, choice="m:talk")
    chat = WhatsAppChat.query.one()
    chat.handoff_at = datetime.utcnow() - timedelta(hours=25)
    db.session.commit()
    assert "Welcome" in say(wa, "hello")
    assert WhatsAppChat.query.one().mode == "bot"


def test_payments_sweep_sends_the_notice(wa, app):
    order_two_sugars(wa)
    inv = Invoice.query.one()
    from services import billing
    billing.record_payment(method="mpesa_c2b", amount=inv.total, receipt="QWA0000002",
                           invoice=inv)
    db.session.commit()
    result = app.test_cli_runner().invoke(args=["payments-sweep"])
    assert result.exit_code == 0, result.output
    assert "whatsapp_notices': 1" in result.output
    assert wa["meta"].texts()[-1].startswith("Payment received")
