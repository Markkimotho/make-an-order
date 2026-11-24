"""The SMS outbox: a row per message, dedupe, failures recorded, delivery
reports applied forwards only, across businesses."""
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from config import Config
from models import Customer, InventoryItem, SmsMessage, Tenant, User, db
from services import sms_service, stock
from services.sms_service import SendSMS


def rows():
    return SmsMessage.query.order_by(SmsMessage.id).all()


def test_a_send_is_a_row_and_goes_out_with_a_plus(app, sms_outbox):
    msg = sms_service.send("0712 345 678", "Hello", "test")
    assert (msg.status, msg.to_phone, msg.kind, msg.provider_message_id) == \
        ("sent", "254712345678", "test", "ATXid_000001")
    assert msg.sent_at is not None and msg.tenant_id == app.acme_id
    assert sms_outbox.sent == [{"message": "Hello", "to": ["+254712345678"],
                                "sender_id": Config.AT_SENDER_ID}]


def test_the_business_sender_id_is_used(app, sms_outbox):
    db.session.get(Tenant, app.acme_id).at_sender_id = "ACME"
    db.session.commit()
    sms_service.send("0712345678", "Hi", "test")
    assert sms_outbox.sent[0]["sender_id"] == "ACME"


def test_dedupe_key_sends_once(app, sms_outbox):
    first = sms_service.send("0712345678", "Due", "reminder", dedupe_key="overdue:7:d1")
    again = sms_service.send("0712345678", "Due", "reminder", dedupe_key="overdue:7:d1")
    assert again.id == first.id and len(sms_outbox.sent) == 1 and len(rows()) == 1
    sms_service.send("0712345678", "Due", "reminder", dedupe_key="overdue:7:d3")
    assert len(sms_outbox.sent) == 2


def test_same_dedupe_key_in_two_businesses_both_send(app, other_tenant, sms_outbox):
    sms_service.send("0712345678", "A", "reminder", dedupe_key="k1")
    with tenant_scope(other_tenant):
        sms_service.send("0712345678", "B", "reminder", dedupe_key="k1")
    assert len(sms_outbox.sent) == 2


@pytest.mark.parametrize("phone", ["", None, "12345", "+44 20 7946 0958"])
def test_a_bad_number_is_skipped_not_sent(app, sms_outbox, phone):
    msg = sms_service.send(phone, "Hi", "test")
    assert msg.status == "skipped" and msg.error == "not a Kenyan mobile number"
    assert sms_outbox.sent == []


def test_provider_outage_is_recorded_and_does_not_raise(app, sms_outbox):
    sms_outbox.fail = True
    msg = sms_service.send("0712345678", "Hi", "test")
    assert msg.status == "failed" and "outage" in msg.error


def test_provider_refusal_is_failed_with_its_reason(app, sms_outbox):
    sms_outbox.reply_code = 405
    msg = sms_service.send("0712345678", "Hi", "test")
    assert (msg.status, msg.error, msg.provider_message_id) == \
        ("failed", "InsufficientBalance", None)
    second = sms_service.send("0712345679", "Hi", "test")      # no unique-index clash
    assert second.status == "failed" and len(rows()) == 2


def test_send_refuses_to_commit_someone_elses_changes(app):
    db.session.add(Customer(name="Half-saved", phone_number="0712000777", code="HS"))
    with pytest.raises(sms_service.UnsavedChanges):
        sms_service.send("0712345678", "Hi", "test")


def test_one_time_codes_are_not_kept_in_the_outbox(app, sms_outbox):
    SendSMS.send_manager_otp("0712345678", "482913", "void")
    assert "482913" in sms_outbox.sent[0]["message"]
    stored = rows()[0].body
    assert "482913" not in stored and "******" in stored


def test_several_recipients_get_a_row_each(app, sms_outbox):
    assert SendSMS.send_low_stock_alert(["0712000001", "0712000002", "0712000001"],
                                        "Panadol", 2, 5) is True
    assert sorted(r.to_phone for r in rows()) == ["254712000001", "254712000002"]


# --- call sites ------------------------------------------------------------------

@pytest.fixture
def till(app, login_as, monkeypatch):
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    item = InventoryItem(name="Panadol", price=Decimal("50"), reorder_level=5)
    duka = Customer(name="Mama Duka", phone_number="0722000111", code="MD")
    db.session.add_all([item, duka])
    db.session.flush()
    stock.receive(item, 7)
    ids = {"item": item.id, "duka": duka.id}
    db.session.commit()
    client = login_as("admin")
    with tenant_scope(app.acme_id):
        admin = User.query.filter_by(role="admin").one()
        admin.phone_number = "0733000222"
        db.session.commit()
    client.post("/pos/shift/open", json={"opening_float": 0})
    return client, ids


def cash_sale(client, ids, qty):
    txn = client.post("/pos/transaction/new",
                      json={"customer_id": ids["duka"]}).get_json()["transaction"]["id"]
    client.post(f"/pos/transaction/{txn}/add_item",
                json={"inventory_item_id": ids["item"], "quantity": qty})
    return client.post(f"/pos/transaction/{txn}/complete",
                       json={"payment_method": "cash", "amount_tendered": 1000})


def test_checkout_texts_a_receipt_and_one_low_stock_alert_a_day(app, till, sms_outbox):
    client, ids = till
    assert cash_sale(client, ids, 3).status_code == 200          # 4 left: low
    assert cash_sale(client, ids, 1).status_code == 200          # 3 left: still low
    kinds = [(r.kind, r.to_phone) for r in rows()]
    assert kinds.count(("payment_receipt", "254722000111")) == 2
    assert kinds.count(("low_stock", "254733000222")) == 1


def test_checkout_survives_an_sms_outage(app, till, sms_outbox):
    client, ids = till
    sms_outbox.fail = True
    assert cash_sale(client, ids, 3).status_code == 200
    assert {r.status for r in rows()} == {"failed"}


# --- delivery reports ----------------------------------------------------------------

def report(client, message_id, status, **extra):
    return client.post("/delivery-reports", data={"id": message_id, "status": status,
                                                   "phoneNumber": "+254712345678", **extra})


def test_delivery_report_needs_no_login_and_finds_the_right_business(
        app, other_tenant, client, sms_outbox):
    sms_service.send("0712345678", "acme", "test")
    with tenant_scope(other_tenant):
        globex_msg = sms_service.send("0712345678", "globex", "test")
        globex_id = globex_msg.provider_message_id
    assert report(client, globex_id, "Success").status_code == 200
    with tenant_scope(other_tenant):
        assert SmsMessage.query.one().delivery_status == "Success"
    assert SmsMessage.query.one().delivery_status is None          # acme's untouched


def test_delivery_status_only_moves_forward(app, client, sms_outbox):
    mid = sms_service.send("0712345678", "x", "test").provider_message_id

    def status():
        db.session.expire_all()
        return SmsMessage.query.one().delivery_status

    report(client, mid, "Buffered")
    report(client, mid, "Sent")                                      # late: ignored
    assert status() == "Buffered"
    report(client, mid, "Failed", failureReason="AbsentSubscriber")
    assert status() == "Failed"
    assert SmsMessage.query.one().error == "AbsentSubscriber"
    report(client, mid, "Success")                                   # final stays final
    report(client, mid, "Submitted")
    assert status() == "Failed"


def test_delivery_report_for_an_unknown_message_is_ignored(app, client):
    assert report(client, "ATXid_nope", "Success").status_code == 200
    assert report(client, "", "").status_code == 200


def test_delivery_report_token_when_configured(app, client, sms_outbox, monkeypatch):
    monkeypatch.setattr(Config, "AT_DELIVERY_TOKEN", "s3cret")
    mid = sms_service.send("0712345678", "x", "test").provider_message_id
    assert client.post("/delivery-reports?token=wrong",
                       data={"id": mid, "status": "Success"}).status_code == 404
    assert client.post("/delivery-reports?token=s3cret",
                       data={"id": mid, "status": "Success"}).status_code == 200
    db.session.expire_all()
    assert SmsMessage.query.one().delivery_status == "Success"
