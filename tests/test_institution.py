"""Institution vertical: safe member lookup, campaign orders, instalments,
Paybill by admission number, holds and closing, pickup codes, report."""
import threading
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import (Campaign, CampaignItem, InventoryItem, Invoice, ItemVariant, Member, Order,
                    Payment, SmsMessage, db)
from services import billing, stock
from services import orders as order_service
from tests import daraja_fakes
from tests.conftest import on_mysql
from verticals.institution import service as institution

D = Decimal


@pytest.fixture
def school(app, monkeypatch):
    fake = daraja_fakes.install(monkeypatch)
    daraja_fakes.configure(app, monkeypatch, app.acme_id)
    shirt = InventoryItem(name="School shirt", price=D("900"), has_variants=True)
    tie = InventoryItem(name="Tie", price=D("300"))
    db.session.add_all([shirt, tie])
    db.session.flush()
    s28 = ItemVariant(inventory_item_id=shirt.id, label="Size 28")
    s32 = ItemVariant(inventory_item_id=shirt.id, label="Size 32")
    db.session.add_all([s28, s32])
    db.session.flush()
    stock.receive(shirt, 10, variant=s28)
    stock.receive(shirt, 10, variant=s32)
    stock.receive(tie, 20, batch_number="T1")
    brian = Member(member_ref="f1/023", name="Brian Otieno", group_label="Form 1 East",
                   guardian_phone="0712000101", secondary_phone="0712000102")
    camp = Campaign(name="Form 1 uniform", slug="f1-uniform", status="open",
                    closes_at=datetime.utcnow() + timedelta(days=14),
                    allow_installments=True, min_installment=D("500"))
    db.session.add_all([brian, camp])
    db.session.flush()
    ci_shirt = CampaignItem(campaign_id=camp.id, inventory_item_id=shirt.id, price=D("850"))
    ci_tie = CampaignItem(campaign_id=camp.id, inventory_item_id=tie.id, price=D("250"))
    db.session.add_all([ci_shirt, ci_tie])
    db.session.commit()
    return {"fake": fake, "camp": camp.id, "member": brian.id, "shirt": ci_shirt.id,
            "tie": ci_tie.id, "s28": s28.id, "url": "/o/acme/c/f1-uniform"}


def lookup(client, school, ref="F1/023", first="brian"):
    return client.post(school["url"] + "/lookup", json={"member_ref": ref, "first_name": first})


def place(client, school, items=None):
    token = lookup(client, school).get_json()["member_token"]
    items = items or [{"campaign_item_id": school["shirt"], "quantity": 1, "variant_id": school["s28"]},
                      {"campaign_item_id": school["tie"], "quantity": 1}]
    return client.post(school["url"] + "/order", json={"member_token": token, "items": items})


def pay(invoice_id, amount, receipt):
    billing.record_payment(method="mpesa_c2b", amount=D(amount), receipt=receipt,
                           invoice=db.session.get(Invoice, invoice_id))
    db.session.commit()


# --- lookup -----------------------------------------------------------------------

def test_lookup_needs_number_and_first_name_and_shows_only_a_masked_name(app, school, client):
    r = lookup(client, school)
    assert r.status_code == 200
    assert r.get_json()["member"] == "Brian O., Form 1 East"
    assert "Otieno" not in r.get_data(as_text=True)
    wrong_name, unknown = lookup(client, school, first="Kevin"), lookup(client, school, ref="F1/024")
    assert wrong_name.status_code == unknown.status_code == 404
    assert wrong_name.get_json() == unknown.get_json()


def test_lookups_are_rate_limited(app, school, client):
    codes = [lookup(client, school, ref=f"F1/{n:03d}").status_code for n in range(11)]
    assert codes[-1] == 429


def test_member_token_is_bound_to_its_campaign(app, school, client):
    other = Campaign(name="Trip", slug="trip", status="open")
    db.session.add(other)
    db.session.commit()
    token = lookup(client, school).get_json()["member_token"]
    r = client.post("/o/acme/c/trip/order", json={"member_token": token, "items": []})
    assert r.status_code == 400 and "again" in r.get_json()["error"]
    r = client.post(school["url"] + "/order", json={"member_token": token + "x", "items": []})
    assert r.status_code == 400


# --- ordering and paying ------------------------------------------------------------

def test_campaign_order_uses_campaign_prices_and_holds_stock(app, school, client):
    r = place(client, school)
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["total"] == 1100.0
    order = Order.query.one()
    invoice = Invoice.query.one()
    assert (order.member_id, order.campaign_id, order.customer_id) == \
        (school["member"], school["camp"], None)
    assert invoice.member_id == school["member"] and order.reserved_until is not None
    assert db.session.get(ItemVariant, school["s28"]).stock == 9
    r = place(client, school, [{"campaign_item_id": school["shirt"], "quantity": 1}])
    assert r.status_code == 400 and "size" in r.get_json()["error"]


def test_draft_and_closed_campaigns(app, school, client):
    camp = db.session.get(Campaign, school["camp"])
    camp.status = "draft"
    db.session.commit()
    assert client.get(school["url"]).status_code == 404
    camp = db.session.get(Campaign, school["camp"])
    camp.status = "closed"
    db.session.commit()
    assert "not taking orders" in client.get(school["url"]).get_data(as_text=True)
    assert place(client, school).status_code == 400


def test_instalments_on_the_pay_page(app, school, client):
    pay_url = place(client, school).get_json()["pay_url"]
    assert "Pay in instalments" in client.get(pay_url).get_data(as_text=True)
    r = client.post(pay_url + "/stk", json={"phone": "0712345678", "amount": 200})
    assert r.status_code == 400 and "500" in r.get_json()["error"]
    r = client.post(pay_url + "/stk", json={"phone": "0712345678", "amount": 5000})
    assert r.status_code == 400
    r = client.post(pay_url + "/stk", json={"phone": "0712345678", "amount": 600})
    assert r.status_code == 201, r.get_json()
    assert school["fake"].posts("/processrequest")[0]["Amount"] == 600


def test_amounts_only_where_the_campaign_allows_them(app, client, monkeypatch):
    daraja_fakes.install(monkeypatch)
    invoice = billing.create_invoice(total=D("1000"), kind="sale")
    db.session.commit()
    r = client.post(f"/o/acme/pay/{invoice.pay_token}/stk", json={"phone": "0712345678",
                                                                  "amount": 500})
    assert r.status_code == 400 and "full" in r.get_json()["error"]


def test_paybill_by_admission_number_pays_the_members_oldest_invoice(app, school, client):
    place(client, school)
    billing.record_payment(method="mpesa_c2b", amount=D("700"), receipt="SLX0000001",
                           bill_ref="f1 023")
    db.session.commit()
    invoice = Invoice.query.one()
    assert (invoice.status, invoice.amount_paid) == ("partial", D("700.00"))
    assert Payment.query.one().member_id == school["member"]


def test_part_paid_order_is_kept_then_released_at_close_with_credit(app, school, client):
    place(client, school)
    order, invoice = Order.query.one(), Invoice.query.one()
    pay(invoice.id, "500", "SLX0000002")
    order_service.release_expired(datetime.utcnow() + timedelta(hours=1))
    assert db.session.get(Order, order.id).fulfillment_status == "pending"
    assert db.session.get(ItemVariant, school["s28"]).stock == 9
    camp = db.session.get(Campaign, school["camp"])
    camp.closes_at = datetime.utcnow() - timedelta(minutes=1)
    db.session.commit()
    assert institution.close_due_campaigns() == {"closed": 1, "released": 1}
    assert db.session.get(Order, order.id).fulfillment_status == "released"
    assert db.session.get(ItemVariant, school["s28"]).stock == 10
    assert db.session.get(Invoice, invoice.id).status == "void"
    assert Payment.query.one().unallocated_amount == D("500.00")       # the member's credit


# --- pickup ------------------------------------------------------------------------------

def paid_with_code(school, client, sms_outbox):
    """Order, pay in two parts, issue the code. Returns the code."""
    place(client, school)
    invoice = Invoice.query.one()
    pay(invoice.id, "500", "SLX0000003")
    assert institution.issue_pickup_codes() == 0 and sms_outbox.sent == []
    pay(invoice.id, "600", "SLX0000004")
    assert institution.issue_pickup_codes() == 1
    assert sorted(m["to"][0] for m in sms_outbox.sent) == ["+254712000101", "+254712000102"]
    code = sms_outbox.sent[0]["message"].split("Pickup code ")[1][:6]
    assert all(code not in row.body for row in SmsMessage.query)            # stored masked
    assert institution.issue_pickup_codes() == 0                          # once
    order = Order.query.one()
    assert order.fulfillment_status == "ready" and order.pickup_code_hash != code
    return code


def test_pickup_code_only_when_fully_paid(app, school, client, sms_outbox):
    assert len(paid_with_code(school, client, sms_outbox)) == 6


def test_collecting_checks_the_code_and_cannot_happen_twice(app, school, client, sms_outbox,
                                                           login_as):
    code = paid_with_code(school, client, sms_outbox)
    desk = login_as("cashier")
    r = desk.post("/institution/pickup", json={"member_ref": "F1023", "code": "000000"
                                               if code != "000000" else "111111"})
    assert r.status_code == 404
    r = desk.post("/institution/pickup", json={"member_ref": "f1/023", "code": code})
    assert r.status_code == 200 and r.get_json()["order"]["fulfillment_status"] == "collected"
    again = desk.post("/institution/pickup", json={"member_ref": "F1023", "code": code})
    assert again.status_code == 409 and "already collected" in again.get_json()["error"]


def test_reversed_payment_blocks_collection(app, school, client, sms_outbox, login_as):
    code = paid_with_code(school, client, sms_outbox)
    for payment in Payment.query.all():
        billing.reverse_payment(payment)
    db.session.commit()
    r = login_as("cashier").post("/institution/pickup", json={"member_ref": "F1023", "code": code})
    assert r.status_code == 409 and "not fully paid" in r.get_json()["error"]


def test_status_poll_sends_the_code_as_soon_as_it_is_paid(app, school, client, sms_outbox):
    pay_url = place(client, school).get_json()["pay_url"]
    intent = client.post(pay_url + "/stk", json={"phone": "0712345678"}).get_json()["intent"]
    invoice = Invoice.query.one()
    pay(invoice.id, "1100", "SLX0000005")
    assert client.get(f"{pay_url}/status?intent={intent['id']}").status_code == 200
    assert any("Pickup code" in m["message"] for m in sms_outbox.sent)


@pytest.mark.skipif(not on_mysql(), reason="row locks need MySQL")
def test_two_scans_at_once_hand_over_once(app, school, client, sms_outbox, login_as):
    code = paid_with_code(school, client, sms_outbox)
    results, barrier = [], threading.Barrier(2)

    def worker():
        with app.app_context(), tenant_scope(app.acme_id):
            barrier.wait()
            order, problem = institution.collect("F1023", code, None)
            db.session.commit()
            results.append(problem)
            db.session.remove()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results, key=str)[0] is None
    assert results.count(None) == 1


# --- report and staff API -------------------------------------------------------------------

def test_report_counts_sizes_and_packing_list(app, school, client, login_as):
    place(client, school)
    pay(Invoice.query.one().id, "1100", "SLX0000006")
    report = login_as("cashier").get(f"/institution/campaigns/{school['camp']}/report").get_json()
    assert report["orders"]["paid"] == 1 and report["paid"] == 1100.0
    assert report["sizes"] == {"School shirt (Size 28)": 1, "Tie": 1}
    assert [p["member_ref"] for p in report["packing_list"]] == ["F1023"]


def test_staff_create_campaigns_and_members(app, school, login_as):
    admin = login_as("admin")
    controlled = InventoryItem(name="Morphine", price=D("50"), is_controlled=True)
    db.session.add(controlled)
    db.session.flush()
    controlled_id = controlled.id
    tie_id = InventoryItem.query.filter_by(name="Tie").one().id
    db.session.commit()
    r = admin.post("/institution/campaigns", json={"name": "Trip 2027", "status": "open"})
    assert r.status_code == 400 and "items" in r.get_json()["error"]
    r = admin.post("/institution/campaigns", json={
        "name": "Trip 2027", "items": [{"inventory_item_id": controlled_id, "price": 50}]})
    assert r.status_code == 400 and "controlled" in r.get_json()["error"]
    r = admin.post("/institution/campaigns", json={
        "name": "Trip 2027", "status": "open", "items": [{"inventory_item_id": tie_id, "price": 99}]})
    assert r.status_code == 201 and r.get_json()["slug"] == "trip-2027"
    r = admin.post("/institution/members", json={"member_ref": "F1-023", "name": "Dup"})
    assert r.status_code == 400 and "exists" in r.get_json()["error"]
    r = admin.post("/institution/members", json={"member_ref": "F2/001", "name": "Amina Yusuf",
                                                  "guardian_phone": "0712 000 200"})
    assert r.status_code == 201 and r.get_json()["guardian_phone"] == "254712000200"
    assert login_as("cashier", email="c2@x.co").post(
        "/institution/members", json={"member_ref": "X1", "name": "Y"}).status_code == 403


def test_institution_landing_panel(app, login_as):
    from tests.conftest import create_tenant
    school_id = create_tenant("Moi Girls", "moi-girls", "institution")
    page = login_as("admin", email="head@moi.ac.ke", tenant_id=school_id).get("/dashboard")
    html = page.get_data(as_text=True)
    assert "Campaigns" in html and "Hand over" in html and "Pharmacy today" not in html


# --- review fixes (Codex, step 5) ----------------------------------------------------

def test_reallocating_a_members_payment_moves_its_owner(app, school, client):
    place(client, school)
    amina = Member(member_ref="F1/030", name="Amina Yusuf")
    db.session.add(amina)
    db.session.flush()
    other = billing.create_invoice(total=D("100"), kind="sale", member_id=amina.id)
    db.session.commit()
    payment = billing.record_payment(method="mpesa_c2b", amount=D("200"), receipt="SLX0000010",
                                     bill_ref="F1023")
    db.session.commit()
    billing.reallocate(payment, db.session.get(Invoice, other.id))
    db.session.commit()
    payment = db.session.get(Payment, payment.id)
    assert payment.member_id == amina.id
    brian_invoice = Invoice.query.filter_by(member_id=school["member"]).one()
    assert brian_invoice.amount_paid == 0                    # leftover is Amina's, not Brian's
    assert payment.unallocated_amount == D("100.00")


def test_closing_a_campaign_by_hand_settles_it(app, school, client, login_as):
    place(client, school)
    order_id, invoice_id = Order.query.one().id, Invoice.query.one().id
    pay(invoice_id, "500", "SLX0000011")
    order_service.release_expired(datetime.utcnow() + timedelta(hours=1))
    r = login_as("admin").put(f"/institution/campaigns/{school['camp']}", json={"status": "closed"})
    assert r.status_code == 200 and r.get_json()["status"] == "closed"
    assert db.session.get(Order, order_id).fulfillment_status == "released"
    assert db.session.get(ItemVariant, school["s28"]).stock == 10


def test_failed_pickup_sms_gets_a_new_code(app, school, client, sms_outbox, login_as):
    place(client, school)
    sms_outbox.fail = True
    pay(Invoice.query.one().id, "1100", "SLX0000012")
    assert institution.issue_pickup_codes() == 1
    first_hash = Order.query.one().pickup_code_hash
    assert {m.status for m in SmsMessage.query} == {"failed"}
    sms_outbox.fail = False
    assert institution.issue_pickup_codes() == 1                  # retried with a new code
    assert Order.query.one().pickup_code_hash != first_hash
    code = sms_outbox.sent[-1]["message"].split("Pickup code ")[1][:6]
    desk = login_as("cashier")
    r = desk.post(f"/institution/orders/{Order.query.one().id}/pickup-code")
    assert r.status_code == 200                                    # staff resend rotates again
    newer = sms_outbox.sent[-1]["message"].split("Pickup code ")[1][:6]
    if code != newer:                                              # the old code stops working
        assert desk.post("/institution/pickup",
                         json={"member_ref": "F1023", "code": code}).status_code == 404
    assert desk.post("/institution/pickup", json={"member_ref": "F1023", "code": newer}).status_code == 200


def test_downgrade_refuses_before_dropping_member_data(app, school):
    from flask_migrate import downgrade
    from sqlalchemy import inspect
    with pytest.raises(SystemExit):              # flask_migrate reports the RuntimeError, exits 1
        downgrade(revision="0009_distributor")
    assert "members" in inspect(db.engine).get_table_names()
