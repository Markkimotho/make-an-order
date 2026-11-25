"""Distributor vertical: tier prices, the credit ceiling (including two
orders at once), containers, deni aging, reminders, eTIMS badge, Paybill by
account code, alternate phones, and the public duka link."""
import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope, unscoped
from models import (ContainerMovement, Customer, InventoryItem, Invoice, ItemVariant, Order,
                    PriceTier, SmsMessage, Tenant, TierPrice, db)
from services import billing, stock
from services.clock import today_nairobi
from services import orders as order_service
from tests.conftest import set_vertical, on_mysql
from verticals.distributor import service as distributor

D = Decimal


@pytest.fixture(autouse=True)
def _distributor_vertical(app):
    """acme runs as a distributor here: its routes 404 for other verticals."""
    set_vertical(app.acme_id, "distributor")


@pytest.fixture
def dist(app):
    unga = InventoryItem(name="Unga 2kg", price=D("200"), unit="pack")
    gas = InventoryItem(name="Gas 6kg refill", price=D("1000"), is_returnable_container=True)
    morphine = InventoryItem(name="Morphine", price=D("50"), is_controlled=True)
    tier = PriceTier(name="Wholesale")
    duka = Customer(name="Mama Mboga Duka", phone_number="0712000777", code="dk-01",
                    credit_limit=D("1000"), credit_term_days=14)
    db.session.add_all([unga, gas, morphine, tier, duka])
    db.session.flush()
    db.session.add(TierPrice(price_tier_id=tier.id, inventory_item_id=unga.id, price=D("180")))
    duka.price_tier_id = tier.id
    stock.receive(unga, 50, batch_number="U1")
    stock.receive(gas, 20, batch_number="G1")
    db.session.commit()
    return {"unga": unga.id, "gas": gas.id, "morphine": morphine.id, "duka": duka.id,
            "tier": tier.id, "token": duka.public_token}


def order(dist, qty, pay_now=False, item="unga"):
    return distributor.place_order(dist["duka"], [{"inventory_item_id": dist[item],
                                                   "quantity": qty}], pay_now=pay_now)


# --- prices and the ceiling -------------------------------------------------------

def test_orders_use_the_duka_tier_price_and_its_terms(app, dist):
    o, inv = order(dist, 3)
    db.session.commit()
    assert (inv.total, inv.kind) == (D("540.00"), "credit")
    # Terms count from the Nairobi date, which is a day ahead of UTC after 21:00 UTC.
    assert inv.due_date - today_nairobi(inv.issued_at) <= timedelta(days=14)
    assert db.session.get(Customer, dist["duka"]).account_ref == "DK01"


def test_credit_ceiling_refuses_more_deni_but_not_the_sale(app, dist, login_as):
    c = login_as("cashier")
    body = {"customer_id": dist["duka"], "items": [{"inventory_item_id": dist["unga"], "quantity": 3}]}
    assert c.post("/distributor/orders", json=body).status_code == 201          # 540 of 1000
    r = c.post("/distributor/orders", json=body)
    assert r.status_code == 409 and r.get_json()["can_pay_now"] is True
    assert r.get_json()["available"] == 460.0
    r = c.post("/distributor/orders", json={**body, "pay_now": True})
    assert r.status_code == 201 and r.get_json()["invoice"]["kind"] == "sale"
    assert billing.customer_exposure(dist["duka"]) == D("540.00")


@pytest.mark.skipif(not on_mysql(), reason="row locks need MySQL")
def test_two_orders_at_once_cannot_both_pass_the_ceiling(app, dist):
    results, barrier = [], threading.Barrier(2)

    def worker():
        with app.app_context(), tenant_scope(app.acme_id):
            barrier.wait()
            try:
                order(dist, 3)
                db.session.commit()
                results.append("ok")
            except distributor.CreditCeiling:
                db.session.rollback()
                results.append("ceiling")
            finally:
                db.session.remove()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ceiling", "ok"]


def test_variant_price_override_wins(app):
    shirt = InventoryItem(name="Shirt", price=D("800"), has_variants=True)
    duka = Customer(name="D", phone_number="0712000778", code="D2", credit_limit=D("5000"))
    db.session.add_all([shirt, duka])
    db.session.flush()
    big = ItemVariant(inventory_item_id=shirt.id, label="XL", price_override=D("950"))
    db.session.add(big)
    db.session.flush()
    stock.receive(shirt, 3, variant=big)
    _, inv = distributor.place_order(duka.id, [{"inventory_item_id": shirt.id, "quantity": 2,
                                                "variant_id": big.id}], pay_now=False)
    assert inv.total == D("1900.00")


# --- containers --------------------------------------------------------------------

def test_containers_are_issued_with_orders_and_returned(app, dist, login_as):
    order(dist, 3, item="gas", pay_now=True)
    db.session.commit()
    assert distributor.container_balance(dist["duka"], dist["gas"]) == 3
    c = login_as("cashier")
    r = c.post("/distributor/containers", json={"customer_id": dist["duka"],
                                                "inventory_item_id": dist["gas"], "delta": -2})
    assert r.status_code == 201 and r.get_json()["balance"] == 1
    r = c.post("/distributor/containers", json={"customer_id": dist["duka"],
                                                "inventory_item_id": dist["gas"], "delta": -5})
    assert r.status_code == 400 and "only holds 1" in r.get_json()["error"]
    r = c.post("/distributor/containers", json={"customer_id": dist["duka"],
                                                "inventory_item_id": dist["unga"], "delta": 1})
    assert r.status_code == 400                                    # not a container item


def test_cancelled_or_released_orders_take_their_containers_back(app, dist):
    cancelled, _ = order(dist, 2, item="gas", pay_now=True)
    released, _ = order(dist, 1, item="gas", pay_now=True)
    ids = (cancelled.id, released.id)
    db.session.commit()
    assert distributor.container_balance(dist["duka"], dist["gas"]) == 3
    order_service.cancel(db.session.get(Order, ids[0]))
    order_service.release(db.session.get(Order, ids[1]))
    db.session.commit()
    assert distributor.container_balance(dist["duka"], dist["gas"]) == 0
    assert [m.delta for m in ContainerMovement.query.filter_by(order_id=ids[0])
            .order_by(ContainerMovement.id)] == [2, -2]


# --- aging, reminders, eTIMS ---------------------------------------------------------

def test_aging_buckets_leave_out_pay_now_and_expired(app, dist):
    today = date(2026, 12, 31)
    for days_past in (-3, 0, 7, 8, 14, 15, 40):
        billing.create_invoice(total=D("100"), kind="credit", customer_id=dist["duka"],
                               due_date=today - timedelta(days=days_past))
    billing.create_invoice(total=D("100"), kind="sale", customer_id=dist["duka"])
    expired = billing.create_invoice(total=D("100"), kind="credit", customer_id=dist["duka"],
                                     due_date=today - timedelta(days=30))
    expired.status = "expired"
    db.session.commit()
    result = distributor.aging(today)
    assert {k: [r["days_past"] for r in v] for k, v in result["buckets"].items()} == \
        {"not_due": [-3], "0_7": [7, 0], "8_14": [14, 8], "15_plus": [40, 15]}
    assert result["totals"] == {"not_due": 100.0, "0_7": 200.0, "8_14": 200.0, "15_plus": 200.0}


def test_reminder_texts_the_pay_link_once_a_day(app, dist, login_as, sms_outbox, monkeypatch):
    monkeypatch.setitem(app.config, "PUBLIC_BASE_URL", "https://pay.example.co.ke")
    _, inv = order(dist, 2)
    db.session.commit()
    c = login_as("cashier")
    r = c.post(f"/distributor/invoices/{inv.id}/remind")
    assert r.status_code == 200, r.get_json()
    assert len(sms_outbox.sent) == 1
    text = sms_outbox.sent[0]["message"]
    assert inv.invoice_number in text and f"/o/acme/pay/{inv.pay_token}" in text
    assert c.post(f"/distributor/invoices/{inv.id}/remind").status_code == 200
    assert len(sms_outbox.sent) == 1 and SmsMessage.query.count() == 1
    billing.record_payment(method="cash", amount=D("360"), invoice=db.session.get(Invoice, inv.id))
    db.session.commit()
    assert c.post(f"/distributor/invoices/{inv.id}/remind").status_code == 400


def test_etims_badge_never_claims_fiscalisation_without_a_cuin(app, dist, login_as):
    _, inv = order(dist, 1)
    db.session.commit()
    assert distributor.etims_badge(inv) == {"status": "not_fiscalised", "label": "Not fiscalised"}
    r = login_as("admin").put(f"/distributor/invoices/{inv.id}/etims", json={"cuin": "KRA0012345"})
    assert r.get_json()["etims"] == {"status": "external", "label": "CUIN KRA0012345"}


# --- Paybill and phones ---------------------------------------------------------------

def test_paybill_by_account_code_pays_the_oldest_invoice_first(app, dist):
    _, first = order(dist, 1)
    _, second = order(dist, 1)
    db.session.commit()
    billing.record_payment(method="mpesa_c2b", amount=D("250"), receipt="SLT1234567",
                           bill_ref="dk 01")
    db.session.commit()
    assert db.session.get(Invoice, first.id).status == "paid"
    assert db.session.get(Invoice, second.id).amount_paid == D("70.00")


def test_alternate_phones(app, dist, login_as):
    other = Customer(name="Other", phone_number="0712000999", code="OT")
    db.session.add(other)
    db.session.commit()
    admin = login_as("admin")
    url = f"/distributor/customers/{dist['duka']}/phones"
    assert admin.post(url, json={"phone": "0733 000 111", "label": "wife"}).status_code == 201
    assert admin.post(url, json={"phone": "0712000999"}).status_code == 400      # another duka's
    assert admin.post(url, json={"phone": "+254733000111"}).status_code == 400   # already added
    assert distributor.customer_for_phone("0733000111").id == dist["duka"]
    profile = admin.get(f"/distributor/customers/{dist['duka']}").get_json()
    assert [p["phone"] for p in profile["phones"]] == ["254733000111"]
    assert profile["order_link"] == f"/o/acme/d/{dist['token']}"


def test_profile_update_normalises_the_account_code(app, dist, login_as):
    admin = login_as("admin")
    r = admin.put(f"/distributor/customers/{dist['duka']}",
                  json={"account_ref": "mm-22", "route_zone": "Kawangware", "gps_lat": -1.28,
                        "credit_term_days": 7})
    assert r.status_code == 200 and r.get_json()["account_ref"] == "MM22"
    assert r.get_json()["route_zone"] == "Kawangware"
    assert admin.put(f"/distributor/customers/{dist['duka']}",
                     json={"credit_term_days": 10}).status_code == 400
    assert login_as("cashier").put(f"/distributor/customers/{dist['duka']}",
                                   json={"route_zone": "x"}).status_code == 403


# --- the public duka link ----------------------------------------------------------------

def test_duka_link_shows_tier_prices_and_credit_left(app, dist, client):
    order(dist, 2)
    db.session.commit()
    html = client.get(f"/o/acme/d/{dist['token']}").get_data(as_text=True)
    assert "Mama Mboga Duka" in html and "180.00" in html and "Morphine" not in html
    assert "640.00" in html                                         # 1000 - 360 credit left
    assert "0712000777" not in html and "254712000777" not in html


def test_duka_orders_on_credit_or_pays_now_over_the_ceiling(app, dist, client):
    url = f"/o/acme/d/{dist['token']}/order"
    body = {"items": [{"inventory_item_id": dist["unga"], "quantity": 4}]}       # 720
    r = client.post(url, json=body)
    assert r.status_code == 201 and r.get_json()["on_credit"] is True
    r = client.post(url, json=body)
    assert r.status_code == 409 and r.get_json()["can_pay_now"] is True
    r = client.post(url, json={**body, "pay_now": True})
    assert r.status_code == 201 and r.get_json()["on_credit"] is False
    assert client.get(r.get_json()["pay_url"]).status_code == 200
    r = client.post(url, json={"items": [{"inventory_item_id": dist["morphine"], "quantity": 1}],
                               "pay_now": True})
    assert r.status_code == 400 and "controlled" in r.get_json()["error"]


def test_wrong_duka_links_are_404(app, dist, client, other_tenant):
    assert client.get("/o/acme/d/not-a-token").status_code == 404
    variant = dist["token"].swapcase()
    if variant != dist["token"]:                    # MySQL would match it case-insensitively
        assert client.get(f"/o/acme/d/{variant}").status_code == 404
    with unscoped():
        globex = db.session.get(Tenant, other_tenant).slug
    assert client.get(f"/o/{globex}/d/{dist['token']}").status_code == 404


@pytest.mark.skipif(not on_mysql(), reason="row locks need MySQL")
def test_two_returns_of_the_last_cylinder_at_once(app, dist):
    order(dist, 1, item="gas", pay_now=True)
    db.session.commit()
    results, barrier = [], threading.Barrier(2)

    def worker():
        with app.app_context(), tenant_scope(app.acme_id):
            barrier.wait()
            try:
                distributor.record_containers(dist["duka"], dist["gas"], -1)
                db.session.commit()
                results.append("ok")
            except order_service.OrderError:
                db.session.rollback()
                results.append("refused")
            finally:
                db.session.remove()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok", "refused"]
    assert distributor.container_balance(dist["duka"], dist["gas"]) == 0
