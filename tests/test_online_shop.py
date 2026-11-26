"""Online shop (/o/<slug>/shop), WhatsApp ordering, the staff Online orders
page and API, and the shop settings."""
import json
import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from auth.tenancy import tenant_scope, unscoped
from models import (Batch, Customer, InventoryItem, Invoice, ItemVariant, Order, SmsMessage,
                    Tenant, db)
from services import billing, stock
from services import online_shop
from tests.conftest import set_vertical

D = Decimal
ROOT = Path(__file__).resolve().parents[1]


def tenant_set(tenant_id, **fields):
    with unscoped():
        t = db.session.get(Tenant, tenant_id)
        for k, v in fields.items():
            setattr(t, k, v)
        db.session.commit()


@pytest.fixture
def goods(app):
    """acme as a general shop with a small catalogue."""
    set_vertical(app.acme_id, "shop")
    sugar = InventoryItem(name="Sugar 1kg", price=D("150"), category="Groceries", unit="pack",
                          icon="fas fa-cubes")
    soap = InventoryItem(name="Bar soap", price=D("80"), category="Household",
                         icon='"><script>alert(1)</script>')
    empty = InventoryItem(name="Out of stock rice", price=D("200"), category="Groceries")
    old = InventoryItem(name="Expired milk", price=D("60"), category="Groceries")
    shirt = InventoryItem(name="T-shirt", price=D("800"), category="Clothes", has_variants=True)
    db.session.add_all([sugar, soap, empty, old, shirt])
    db.session.flush()
    medium = ItemVariant(inventory_item_id=shirt.id, label="M", price_override=D("900"))
    large = ItemVariant(inventory_item_id=shirt.id, label="L")
    db.session.add_all([medium, large])
    db.session.flush()
    stock.receive(sugar, 20, batch_number="S1")
    stock.receive(soap, 3, batch_number="B1")
    stock.receive(old, 10, batch_number="M1", expiry_date=date.today() - timedelta(days=5))
    stock.receive(shirt, 4, variant=medium)
    db.session.commit()
    return {"sugar": sugar.id, "soap": soap.id, "empty": empty.id, "old": old.id,
            "shirt": shirt.id, "medium": medium.id, "large": large.id}


@pytest.fixture
def pharmacy_goods(app):
    """acme stays a pharmacy: OTC, prescription-only and controlled items."""
    panadol = InventoryItem(name="Panadol", price=D("50"), category="Pain", is_drug=True)
    amoxil = InventoryItem(name="Amoxil 500", price=D("300"), is_drug=True,
                           requires_prescription=True)
    morphine = InventoryItem(name="Morphine", price=D("90"), is_drug=True, is_controlled=True)
    db.session.add_all([panadol, amoxil, morphine])
    db.session.flush()
    stock.receive(panadol, 30, batch_number="P1")
    stock.receive(amoxil, 30, batch_number="A1")
    db.session.add(Batch(inventory_item_id=morphine.id, batch_number="MO1", quantity_on_hand=5))
    morphine.stock = 5
    db.session.commit()
    return {"panadol": panadol.id, "amoxil": amoxil.id, "morphine": morphine.id}


def catalogue_of(html):
    m = re.search(r'<script id="catalogue" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def checkout(items, **kw):
    return {"items": items, "name": "Achieng Otieno", "phone": "0712 345 678",
            "fulfilment": "pickup", **kw}


def post_order(client, body, ip="41.80.0.1", slug="acme"):
    return client.post(f"/o/{slug}/shop/order", json=body, headers={"X-Forwarded-For": ip})


def pay(invoice_id, receipt="QAB1234567"):
    inv = db.session.get(Invoice, invoice_id)
    billing.record_payment(method="mpesa_c2b", amount=inv.total, receipt=receipt, invoice=inv)
    db.session.commit()


def order_by_number(number):
    return Order.query.filter_by(order_number=number).one()


def place(client, goods, qty=2, ip="41.80.0.1", **kw):
    r = post_order(client, checkout([{"id": goods["sugar"], "quantity": qty}], **kw), ip=ip)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


# --- the shop page -----------------------------------------------------------------

def test_shop_is_404_for_unknown_slug_wrong_type_or_closed(app, client, goods):
    assert client.get("/o/acme/shop").status_code == 200
    assert client.get("/o/nobody/shop").status_code == 404
    assert client.get("/o/ACME/shop").status_code == 404
    tenant_set(app.acme_id, shop_open=False)
    assert client.get("/o/acme/shop").status_code == 404
    assert post_order(client, checkout([{"id": goods["sugar"], "quantity": 1}])).status_code == 404
    tenant_set(app.acme_id, shop_open=True)
    set_vertical(app.acme_id, "institution")               # no online_shop feature
    assert client.get("/o/acme/shop").status_code == 404
    assert post_order(client, checkout([{"id": goods["sugar"], "quantity": 1}]),
                      ip="41.80.0.9").status_code == 404


def test_catalogue_shows_only_sellable_stock_and_never_counts(app, client, goods):
    r = client.get("/o/acme/shop")
    assert r.headers["Cache-Control"] == "no-store"
    html = r.get_data(as_text=True)
    items = {p["name"]: p for p in catalogue_of(html)}
    assert set(items) == {"Sugar 1kg", "Bar soap", "T-shirt"}       # no empty, expired
    assert items["Sugar 1kg"]["stock"] == "in" and items["Bar soap"]["stock"] == "few"
    assert items["Sugar 1kg"]["icon"] == "fa-cubes"
    assert items["Bar soap"]["icon"] == "fa-box"                     # junk icon dropped
    assert "<script>alert" not in html
    shirt = items["T-shirt"]
    assert [s["label"] for s in shirt["sizes"]] == ["M"] and shirt["sizes"][0]["price"] == 900.0
    for p in items.values():
        assert p["stock"] in ("in", "few")
        assert not {"quantity", "count", "stock_count", "in_stock"} & set(p)
        for s in p.get("sizes", []):
            assert s["stock"] in ("in", "few")
    assert '"stock": 20' not in html and '"stock": 3' not in html
    assert "Groceries" in html and "Order on WhatsApp" not in html      # no number set


def test_pharmacy_shop_hides_prescription_and_controlled_items(app, client, pharmacy_goods):
    html = client.get("/o/acme/shop").get_data(as_text=True)
    names = {p["name"] for p in catalogue_of(html)}
    assert names == {"Panadol"}
    assert "Ask the pharmacist" in html
    for item in ("amoxil", "morphine"):
        r = post_order(client, checkout([{"id": pharmacy_goods[item], "quantity": 1}]),
                       ip=f"41.80.1.{len(item)}")
        assert r.status_code == 400 and "not sold online" in r.get_json()["error"]
    assert Order.query.count() == 0


def test_page_is_built_for_phones():
    html = (ROOT / "templates/public/shop.html").read_text()
    assert '<meta name="viewport" content="width=device-width, initial-scale=1' in html
    assert "overflow-x: hidden" in html
    for value in re.findall(r"(?<![-\w(])(?:min-)?width:\s*(\d+)px", html):
        assert int(value) <= 360, value
    assert not re.search(r'\swidth="(\d{4,}|[4-9]\d\d)"', html)
    assert "bootstrap@5.3.3" in html and "font-awesome/6" in html
    assert "localStorage" in html and "shop-cart:" in html
    assert "esc(p.name)" in html                                      # names escaped in JS


# --- placing an order ----------------------------------------------------------------

def test_order_is_a_pay_now_shop_order_that_holds_stock(app, client, goods):
    body = place(client, goods, qty=2)
    assert body["total"] == 300.0
    assert re.match(r"^http://localhost/o/acme/pay/[A-Za-z0-9_-]+$", body["pay_url"])
    assert body["status_url"].startswith("/o/acme/shop/order/")
    assert "whatsapp_url" not in body
    order = order_by_number(body["order"])
    inv = Invoice.query.filter_by(order_id=order.id).one()
    assert (order.channel, order.fulfillment_status, inv.kind, inv.status) == \
        ("shop", "pending", "sale", "open")
    assert order.reserved_until is not None and order.delivery_address is None
    assert inv.total == D("300.00") and inv.due_date is None
    assert db.session.get(InventoryItem, goods["sugar"]).stock == 18
    assert "online shop for pickup" in inv.notes


def test_more_than_in_stock_is_refused_without_counts(app, client, goods):
    r = post_order(client, checkout([{"id": goods["soap"], "quantity": 5}]))
    assert r.status_code == 409
    err = r.get_json()["error"]
    assert "Not enough Bar soap" in err and "3" not in err
    assert Order.query.count() == 0 and Customer.query.count() == 0
    assert db.session.get(InventoryItem, goods["soap"]).stock == 3


def test_prices_come_from_the_server(app, client, goods):
    items = [{"id": goods["sugar"], "quantity": 1, "price": 1, "unit_price": 1},
             {"id": goods["shirt"], "variant_id": goods["medium"], "quantity": 1, "price": 1}]
    r = post_order(client, checkout(items, total=2, amount=2))
    assert r.status_code == 201 and r.get_json()["total"] == 1050.0
    q = client.post("/o/acme/shop/quote", json={"items": items})
    assert q.status_code == 200 and q.get_json()["total"] == 1050.0
    assert [line["unit_price"] for line in q.get_json()["lines"]] == [150.0, 900.0]


def test_sizes_must_be_chosen_and_belong_to_the_item(app, client, goods):
    r = post_order(client, checkout([{"id": goods["shirt"], "quantity": 1}]))
    assert r.status_code == 400 and "Choose a size" in r.get_json()["error"]
    r = post_order(client, checkout([{"id": goods["sugar"], "variant_id": goods["medium"],
                                      "quantity": 1}]), ip="41.80.0.2")
    assert r.status_code == 400
    r = post_order(client, checkout([{"id": goods["shirt"], "variant_id": goods["large"],
                                      "quantity": 1}]), ip="41.80.0.3")
    assert r.status_code == 409                                     # L has none


@pytest.mark.parametrize("items, message", [
    ([], "cart is empty"), ("nope", "cart is empty"), ([{"id": "x", "quantity": 1}], "whole number"),
    ([{"id": 1, "quantity": 0}], "1 or more"), ([{"id": 999999, "quantity": 1}], "no longer sold"),
])
def test_bad_carts_are_refused(app, client, goods, items, message):
    r = post_order(client, checkout(items))
    assert r.status_code == 400 and message in r.get_json()["error"]


def test_customer_is_found_by_phone_in_any_format_and_keeps_their_name(app, client, goods):
    db.session.add(Customer(name="Jane Wanjiru", phone_number="0712345678", code="C1"))
    db.session.commit()
    for i, phone in enumerate(("+254 712 345 678", "712345678", "0712-345-678")):
        place(client, goods, qty=1, ip=f"41.80.2.{i}", phone=phone, name="Someone Else")
    assert Customer.query.count() == 1
    jane = Customer.query.one()
    assert jane.name == "Jane Wanjiru"
    assert {o.customer_id for o in Order.query.all()} == {jane.id}
    place(client, goods, qty=1, ip="41.80.2.9", phone="0722 000 111", name="New Person")
    new = Customer.query.filter_by(phone_number="254722000111").one()
    assert new.name == "New Person" and new.code.startswith("S")


def test_phone_and_name_are_checked(app, client, goods):
    r = post_order(client, checkout([{"id": goods["sugar"], "quantity": 1}], phone="12345"))
    assert r.status_code == 400 and "Kenyan mobile" in r.get_json()["error"]
    r = post_order(client, checkout([{"id": goods["sugar"], "quantity": 1}], name=" "), ip="41.80.3.1")
    assert r.status_code == 400 and "name" in r.get_json()["error"]


def test_delivery_needs_an_address_and_a_shop_that_delivers(app, client, goods):
    item = [{"id": goods["sugar"], "quantity": 1}]
    r = post_order(client, checkout(item, fulfilment="delivery"))
    assert r.status_code == 400 and "deliver" in r.get_json()["error"]
    body = place(client, goods, qty=1, ip="41.80.4.1", fulfilment="delivery",
                 address="Kilimani, near Yaya Centre")
    assert order_by_number(body["order"]).delivery_address == "Kilimani, near Yaya Centre"
    tenant_set(app.acme_id, shop_delivery=False)
    r = post_order(client, checkout(item, fulfilment="delivery", address="Kilimani"), ip="41.80.4.2")
    assert r.status_code == 400 and "does not deliver" in r.get_json()["error"]
    assert "Delivery</label>" not in client.get("/o/acme/shop").get_data(as_text=True)


def test_order_on_whatsapp_opens_a_chat_with_the_real_order(app, client, goods):
    r = post_order(client, checkout([{"id": goods["sugar"], "quantity": 1}], channel="whatsapp"))
    assert r.status_code == 400                                     # no number set
    tenant_set(app.acme_id, whatsapp_number="254700111222")
    html = client.get("/o/acme/shop").get_data(as_text=True)
    assert "Order on WhatsApp" in html and 'href="https://wa.me/254700111222?text=' in html
    body = place(client, goods, qty=2, ip="41.80.5.1", channel="whatsapp",
                 fulfilment="delivery", address="Umoja 2 & stage")
    order = order_by_number(body["order"])
    assert order.channel == "whatsapp"
    url = body["whatsapp_url"]
    assert url.startswith("https://wa.me/254700111222?text=")
    raw = url.split("?text=", 1)[1]
    assert not re.search(r"[\s&#]", raw)                            # fully url-encoded
    text = parse_qs(urlparse(url).query)["text"][0]
    assert text == unquote(raw)
    assert body["order"] in text and body["pay_url"] in text
    assert "Sugar 1kg x2: KES 300.00" in text and "Total: KES 300.00" in text
    assert "Umoja 2 & stage" in text and "/shop/order/" in text


def test_order_post_is_rate_limited(app, client, goods):
    codes = [post_order(client, checkout([]), ip="41.80.6.6").status_code for _ in range(6)]
    assert codes == [400] * 5 + [429]
    assert post_order(client, checkout([]), ip="41.80.6.7").status_code == 400


# --- the customer's order page ---------------------------------------------------------

def test_order_page_by_token_shows_items_and_status(app, client, goods, other_tenant):
    body = place(client, goods, qty=2)
    url = body["status_url"]
    token = url.rsplit("/", 1)[1]
    html = client.get(url).get_data(as_text=True)
    assert body["order"] in html and "Sugar 1kg" in html and "300.00" in html
    assert "Waiting for payment" in html and "Pay with M-Pesa" in html
    assert "0712" not in html and "254712345678" not in html
    order = order_by_number(body["order"])
    pay(Invoice.query.filter_by(order_id=order.id).one().id)
    html = client.get(url).get_data(as_text=True)
    assert "preparing your order" in html and "Pay with M-Pesa" not in html
    # pay page links back to the order
    assert url in client.get(f"/o/acme/pay/{token}").get_data(as_text=True)
    assert client.get("/o/acme/shop/order/not-a-token").status_code == 404
    with unscoped():
        globex = db.session.get(Tenant, other_tenant).slug
    assert client.get(f"/o/{globex}/shop/order/{token}").status_code == 404


def test_order_page_is_only_for_shop_orders(app, client, goods):
    cust = Customer(name="Credit", phone_number="0733000111", code="CR")
    db.session.add(cust)
    db.session.flush()
    inv = billing.create_invoice(total=D("100"), kind="credit", customer_id=cust.id)
    db.session.commit()
    assert client.get(f"/o/acme/shop/order/{inv.pay_token}").status_code == 404
    assert "Track your order" not in client.get(f"/o/acme/pay/{inv.pay_token}").get_data(as_text=True)


def test_order_page_tokens_are_kept_out_of_logs(app):
    from app import RedactCallbackTokens
    record = logging.LogRecord("x", logging.INFO, "", 0, "GET /o/acme/shop/order/SECRET123 200",
                               None, None)
    RedactCallbackTokens().filter(record)
    assert "SECRET123" not in record.getMessage()


# --- staff: Online orders ----------------------------------------------------------------

def test_cashier_sees_online_orders_but_stock_clerk_does_not(app, client, goods, login_as):
    place(client, goods)
    for role in ("cashier", "pharmacist", "admin"):
        c = login_as(role)
        assert c.get("/orders/online").status_code == 200, role
        assert c.get("/orders-ui").status_code == 200, role
        assert c.get("/orders/online/waiting").status_code == 200
    c = login_as("stock_clerk")
    assert c.get("/orders/online").status_code == 403
    assert c.put("/orders/1/fulfilment", json={"status": "ready"}).status_code == 403
    assert c.get("/orders-ui").status_code == 302


def test_unpaid_orders_cannot_be_marked_ready(app, client, goods, login_as):
    body = place(client, goods)
    order_id = order_by_number(body["order"]).id
    c = login_as("cashier")
    for status in ("ready", "collected"):
        r = c.put(f"/orders/{order_id}/fulfilment", json={"status": status})
        assert r.status_code == 409 and "not paid" in r.get_json()["error"]
    assert db.session.get(Order, order_id).fulfillment_status == "pending"
    row = c.get("/orders/online").get_json()["orders"][0]
    assert row["actions"] == ["cancelled"] and row["paid"] is False


def test_ready_texts_the_customer_once(app, client, goods, login_as, sms_outbox):
    body = place(client, goods)
    order_id = order_by_number(body["order"]).id
    pay(Invoice.query.filter_by(order_id=order_id).one().id)
    c = login_as("cashier")
    row = c.get("/orders/online").get_json()["orders"][0]
    assert row["paid"] and row["actions"] == ["ready", "collected", "cancelled"]
    assert c.get("/orders/online/waiting").get_json()["count"] == 1
    r = c.put(f"/orders/{order_id}/fulfilment", json={"status": "ready"})
    assert r.status_code == 200 and r.get_json()["order"]["sms"] == "sent"
    assert len(sms_outbox.sent) == 1
    assert "ready for pickup" in sms_outbox.sent[0]["message"]
    assert body["order"] in sms_outbox.sent[0]["message"]
    assert sms_outbox.sent[0]["to"] == ["+254712345678"]
    online_shop.notify(db.session.get(Order, order_id))                    # again: deduped
    assert len(sms_outbox.sent) == 1
    assert SmsMessage.query.filter_by(kind="order_ready").count() == 1
    assert c.put(f"/orders/{order_id}/fulfilment", json={"status": "ready"}).status_code == 409
    assert c.get("/orders/online/waiting").get_json()["count"] == 0
    r = c.put(f"/orders/{order_id}/fulfilment", json={"status": "collected"})
    assert r.status_code == 200
    done = db.session.get(Order, order_id)
    assert done.fulfillment_status == "collected" and done.picked_up_at is not None
    assert "Collected" in client.get(body["status_url"]).get_data(as_text=True)


def test_delivery_order_goes_out_for_delivery(app, client, goods, login_as, sms_outbox):
    body = place(client, goods, fulfilment="delivery", address="Rongai, Tuskys stage")
    order_id = order_by_number(body["order"]).id
    pay(Invoice.query.filter_by(order_id=order_id).one().id)
    c = login_as("pharmacist")
    assert c.put(f"/orders/{order_id}/fulfilment", json={"status": "dispatched"}).status_code == 200
    assert "out for delivery" in sms_outbox.sent[-1]["message"]
    assert "Out for delivery" in client.get(body["status_url"]).get_data(as_text=True)
    assert c.put(f"/orders/{order_id}/fulfilment", json={"status": "delivered"}).status_code == 200
    assert "Delivered" in client.get(body["status_url"]).get_data(as_text=True)


def test_pickup_orders_cannot_be_dispatched(app, client, goods, login_as):
    body = place(client, goods)
    order_id = order_by_number(body["order"]).id
    pay(Invoice.query.filter_by(order_id=order_id).one().id)
    r = login_as("cashier").put(f"/orders/{order_id}/fulfilment", json={"status": "dispatched"})
    assert r.status_code == 409


def test_cancel_returns_the_stock(app, client, goods, login_as):
    body = place(client, goods, qty=5)
    order_id = order_by_number(body["order"]).id
    assert db.session.get(InventoryItem, goods["sugar"]).stock == 15
    c = login_as("cashier")
    r = c.put(f"/orders/{order_id}/fulfilment", json={"status": "cancelled"})
    assert r.status_code == 200
    assert db.session.get(InventoryItem, goods["sugar"]).stock == 20
    order = db.session.get(Order, order_id)
    assert order.fulfillment_status == "cancelled"
    assert Invoice.query.filter_by(order_id=order.id).one().status == "void"
    assert c.put(f"/orders/{order.id}/fulfilment", json={"status": "cancelled"}).status_code == 409
    assert "Cancelled" in client.get(body["status_url"]).get_data(as_text=True)


def test_list_filters(app, client, goods, login_as):
    tenant_set(app.acme_id, whatsapp_number="254700111222")
    shop_order = place(client, goods, qty=1, ip="41.80.7.1")
    wa_order = place(client, goods, qty=1, ip="41.80.7.2", channel="whatsapp")
    pay(Invoice.query.filter_by(order_id=order_by_number(wa_order["order"]).id).one().id)
    c = login_as("cashier")

    def numbers(**q):
        r = c.get("/orders/online", query_string=q)
        assert r.status_code == 200, r.get_json()
        return [o["order_number"] for o in r.get_json()["orders"]]

    assert numbers() == [wa_order["order"], shop_order["order"]]          # newest first
    assert numbers(channel="whatsapp") == [wa_order["order"]]
    assert numbers(channel="shop") == [shop_order["order"]]
    assert numbers(channel="staff") == []
    assert numbers(paid="paid") == [wa_order["order"]]
    assert numbers(paid="unpaid") == [shop_order["order"]]
    assert numbers(status="open") == [wa_order["order"], shop_order["order"]]
    assert numbers(status="cancelled") == []
    row = c.get("/orders/online").get_json()["orders"][0]
    assert row["customer_phone"] == "254712345678" and row["channel"] == "whatsapp"
    assert row["items"] == [{"name": "Sugar 1kg", "quantity": 1, "subtotal": 150.0}]
    assert row["fulfilment"] == "pickup" and row["age_minutes"] is not None
    for bad in ({"channel": "fax"}, {"paid": "maybe"}, {"status": "lost"}, {"page": "x"}):
        assert c.get("/orders/online", query_string=bad).status_code == 400
    assert c.put("/orders/999999/fulfilment", json={"status": "ready"}).status_code == 404
    assert c.put("/orders/1/fulfilment", json={"status": "eaten"}).status_code == 400


def test_nav_badge_and_page_markup(app, client, goods, login_as):
    c = login_as("cashier")
    html = c.get("/dashboard").get_data(as_text=True)
    assert "Online orders" in html and 'id="navOrdersBadge"' in html
    assert "/orders/online/waiting" in html
    page = c.get("/orders-ui").get_data(as_text=True)
    assert "setInterval" in page and "20000" in page and "/o/acme/shop" in page


def test_staff_cannot_touch_another_business_order(app, client, goods, login_as, other_tenant):
    body = place(client, goods)
    order_id = order_by_number(body["order"]).id
    c = login_as("admin", tenant_id=other_tenant)
    assert c.put(f"/orders/{order_id}/fulfilment", json={"status": "cancelled"}).status_code == 404
    assert c.get("/orders/online").get_json()["orders"] == []


# --- settings ----------------------------------------------------------------------------

def settings_form(**kw):
    return {"daraja_env": "sandbox", "shop_settings": "1", "shop_open": "on",
            "shop_delivery": "on", **kw}


def test_settings_save_whatsapp_and_switches(app, login_as):
    set_vertical(app.acme_id, "shop")
    c = login_as("superuser")
    html = c.get("/settings/").get_data(as_text=True)
    assert "http://localhost/o/acme/shop" in html and "<svg" in html
    assert "https://wa.me/?text=" in html and "Share on WhatsApp" in html
    r = c.post("/settings/", data=settings_form(whatsapp_number="0712 000 111"))
    assert r.status_code == 302
    with unscoped():
        t = db.session.get(Tenant, app.acme_id)
        assert (t.whatsapp_number, t.shop_open, t.shop_delivery) == ("254712000111", True, True)
    r = c.post("/settings/", data=settings_form(whatsapp_number="12345"))
    assert r.status_code == 400 and "WhatsApp number" in r.get_data(as_text=True)
    with unscoped():
        assert db.session.get(Tenant, app.acme_id).whatsapp_number == "254712000111"
    data = settings_form(whatsapp_number="")
    del data["shop_open"], data["shop_delivery"]
    assert c.post("/settings/", data=data).status_code == 302
    with unscoped():
        t = db.session.get(Tenant, app.acme_id)
        assert (t.whatsapp_number, t.shop_open, t.shop_delivery) == (None, False, False)
    # A form without the shop section leaves the shop alone.
    tenant_set(app.acme_id, shop_open=True)
    assert c.post("/settings/", data={"daraja_env": "sandbox"}).status_code == 302
    with unscoped():
        assert db.session.get(Tenant, app.acme_id).shop_open is True


def test_shop_settings_are_superuser_only(app, login_as):
    for role in ("admin", "cashier"):
        c = login_as(role)
        assert c.post("/settings/", data=settings_form(shop_open="")).status_code == 302
        assert c.get("/settings/").status_code == 302
    with unscoped():
        assert db.session.get(Tenant, app.acme_id).shop_open is True


def test_institution_settings_have_no_shop_section(app, login_as):
    set_vertical(app.acme_id, "institution")
    html = login_as("superuser").get("/settings/").get_data(as_text=True)
    assert "Your shop link" not in html and 'name="whatsapp_number"' not in html


def test_qr_code_is_an_inline_svg():
    svg = online_shop.qr_svg("https://example.com/o/acme/shop")
    assert svg.startswith("<svg") and "<script" not in svg


def test_unpaid_hold_is_released_by_the_sweep(app, client, goods):
    from datetime import datetime
    from services import orders as order_service
    body = place(client, goods, qty=4)
    assert db.session.get(InventoryItem, goods["sugar"]).stock == 16
    counts = order_service.release_expired(now=datetime.utcnow() + timedelta(minutes=20))
    assert counts["released"] == 1
    assert db.session.get(InventoryItem, goods["sugar"]).stock == 20
    html = client.get(body["status_url"]).get_data(as_text=True)
    assert "Not paid in time" in html and "Pay with M-Pesa" not in html


# --- review fixes ------------------------------------------------------------------

def test_a_stranger_ordering_on_your_number_never_gets_your_money(app, client, goods, login_as):
    """Codex P1: a shop order placed with someone else's phone joined that
    customer's invoices, so their refund credit paid for it."""
    mine = place(client, goods, qty=2)                       # the real customer, paid
    mine_id = order_by_number(mine["order"]).id
    pay(Invoice.query.filter_by(order_id=mine_id).one().id)
    theirs = place(client, goods, qty=2, ip="41.80.0.2",     # a stranger, same phone
                   fulfilment="delivery", address="Somewhere else")
    theirs_invoice_id = Invoice.query.filter_by(
        order_id=order_by_number(theirs["order"]).id).one().id
    c = login_as("cashier")
    assert c.put(f"/orders/{mine_id}/fulfilment",
                 json={"status": "cancelled"}).status_code == 200
    db.session.expire_all()
    assert db.session.get(Invoice, theirs_invoice_id).status == "open"
    from models import Payment
    payment = Payment.query.one()
    assert payment.unallocated_amount == D("300")            # still the customer's credit


def test_paybill_money_without_a_named_invoice_skips_shop_orders(app, client, goods):
    body = place(client, goods)
    invoice = Invoice.query.filter_by(order_id=order_by_number(body["order"]).id).one()
    customer_id = invoice.customer_id
    billing.record_payment(method="mpesa_c2b", amount=D("300"), receipt="QPB0000001",
                           customer_id=customer_id)
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(Invoice, invoice.id).status == "open"
    pay(invoice.id, receipt="QPB0000002")                    # its own payment still pays it
    assert db.session.get(Invoice, invoice.id).status == "paid"


def test_agrovet_shop_leaves_out_the_vet_antibiotic(app, client):
    from services.seed_service import seed_inventory
    set_vertical(app.acme_id, "agrovet")
    seed_inventory("agrovet")
    db.session.commit()
    oxy = InventoryItem.query.filter(InventoryItem.name.like("Oxytetracycline%")).one()
    assert oxy.is_drug and oxy.requires_prescription
    tenant_set(app.acme_id, shop_open=True)
    names = {i["name"] for i in catalogue_of(client.get("/o/acme/shop").get_data(as_text=True))}
    assert "Oxytetracycline 20% 100ml" not in names
    assert "Albendazole Dewormer 1L" in names
    r = post_order(client, checkout([{"id": oxy.id, "quantity": 1}]))
    assert r.status_code in (400, 404, 409) and Order.query.count() == 0
