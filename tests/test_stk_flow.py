"""STK push end to end: till -> Daraja -> callback/sweep -> paid invoice."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from models import Invoice, Payment, PaymentIntent, Transaction, db
from services import billing
from services.billing import stk
from tests import daraja_fakes

D = Decimal


@pytest.fixture
def till(app, login_as, monkeypatch):
    from config import Config
    from models import InventoryItem
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    fake = daraja_fakes.install(monkeypatch)
    slug, token = daraja_fakes.configure(app, monkeypatch, app.acme_id)
    item = InventoryItem(name="Sugar 1kg", price=D("100"), stock=40)
    db.session.add(item)
    db.session.commit()
    item_id = item.id
    client = login_as("cashier")
    client.post("/pos/shift/open", json={"opening_float": 0})
    txn = client.post("/pos/transaction/new", json={}).get_json()["transaction"]["id"]
    client.post(f"/pos/transaction/{txn}/add_item", json={"inventory_item_id": item_id})
    return {"client": client, "fake": fake, "txn": txn,
            "stk_url": f"/api/payments/daraja/stk/{slug}/{token}",
            "c2b_url": f"/api/payments/daraja/c2b/{slug}/{token}"}


def push(till, phone="0712345678"):
    return till["client"].post("/payments/mpesa/stk_push",
                               json={"transaction_id": till["txn"], "phone_number": phone})


def intent():
    return PaymentIntent.query.order_by(PaymentIntent.id.desc()).first()


def age(seconds):
    i = intent()
    i.created_at = datetime.utcnow() - timedelta(seconds=seconds)
    db.session.commit()


def invoice_status():
    return Invoice.query.one().status


# --- push ------------------------------------------------------------------------

def test_push_creates_invoice_and_pending_intent(till):
    r = push(till)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["intent"]["status"] == "pending"
    assert body["intent"]["checkout_request_id"] == "ws_CO_1"
    sent = till["fake"].posts("/processrequest")[0]
    assert (sent["Amount"], sent["AccountReference"]) == (100, body["invoice"]["invoice_number"])


def test_one_prompt_per_phone_at_a_time(till):
    assert push(till).status_code == 201
    r = push(till)
    assert r.status_code == 409 and "already sent" in r.get_json()["error"]
    assert len(till["fake"].posts("/processrequest")) == 1


def test_busy_phone(till):
    till["fake"].answer("/processrequest", 500, {"errorCode": "500.001.1001",
                                                "errorMessage": "Unable to lock subscriber"})
    r = push(till)
    assert r.status_code == 409 and "Wait a minute" in r.get_json()["error"]
    assert intent().status == "failed"
    assert invoice_status() == "open"


def test_daraja_not_set_up(app, login_as, till, monkeypatch):
    from auth.tenancy import unscoped
    from models import Tenant
    with unscoped():
        db.session.get(Tenant, app.acme_id).daraja_consumer_key = None
        db.session.commit()
    r = push(till)
    assert r.status_code == 400 and "not set up" in r.get_json()["error"]
    assert PaymentIntent.query.count() == 0


def test_bad_phone(till):
    assert push(till, phone="+25756098389").status_code == 400


# --- callback --------------------------------------------------------------------------

def callback(till, **kw):
    cri = intent().checkout_request_id
    return till["client"].post(till["stk_url"], json=daraja_fakes.stk_callback(cri, **kw))


def test_callback_success_verified_then_sale_completes(till):
    push(till)
    till["fake"].answer("/query", 200, {"ResultCode": "0", "ResultDesc": "ok"})
    r = callback(till)
    assert r.status_code == 200 and r.get_json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
    assert intent().status == "completed"
    assert invoice_status() == "paid"
    p = Payment.query.one()
    assert (p.method, p.mpesa_receipt, p.amount) == ("mpesa_stk", "SJK0000001", D("100.00"))
    done = till["client"].post(f"/pos/transaction/{till['txn']}/complete",
                               json={"payment_method": "mpesa"})
    assert done.status_code == 200, done.get_json()
    assert db.session.get(Transaction, till["txn"]).mpesa_ref == "SJK0000001"


def test_cancelled_on_phone(till):
    push(till)
    callback(till, code=1032)
    assert intent().status == "failed"
    assert Payment.query.count() == 0 and invoice_status() == "open"


def test_duplicate_callback_counts_once(till):
    push(till)
    till["fake"].default("/query", 200, {"ResultCode": "0"})
    callback(till)
    callback(till)
    assert Payment.query.count() == 1


def test_verification_unavailable_then_sweep_finishes(till):
    import requests
    push(till)
    till["fake"].answer("/query", 200, requests.Timeout("slow"))
    callback(till)
    assert intent().status == "callback_received"
    assert Payment.query.count() == 0
    age(120)
    till["fake"].answer("/query", 200, {"ResultCode": "0"})
    stk.sweep_tenant()
    assert intent().status == "completed"
    assert Payment.query.one().mpesa_receipt == "SJK0000001"


def test_callback_says_paid_but_query_disagrees_is_held(till):
    push(till)
    till["fake"].answer("/query", 200, {"ResultCode": "1032", "ResultDesc": "cancelled"})
    callback(till)
    assert intent().status == "callback_received"
    assert Payment.query.count() == 0


# --- missed callbacks: the sweep -------------------------------------------------------------

def test_sweep_finds_payment_whose_callback_never_came(till):
    push(till)
    age(100)
    till["fake"].answer("/query", 200, {"ResultCode": "0"})
    counts = stk.sweep_tenant()
    assert counts["completed"] == 1
    assert invoice_status() == "paid"
    p = Payment.query.one()
    assert p.mpesa_receipt is None
    # The receipt arrives later through the Paybill confirmation: same payment.
    inv = Invoice.query.one()
    till["client"].post(till["c2b_url"],
                        json=daraja_fakes.c2b("SJK0000009", 100, inv.invoice_number))
    assert Payment.query.count() == 1
    assert Payment.query.one().mpesa_receipt == "SJK0000009"


def test_late_callback_attaches_receipt(till):
    push(till)
    age(100)
    till["fake"].default("/query", 200, {"ResultCode": "0"})
    stk.sweep_tenant()
    callback(till, receipt="SJK0000010")
    assert Payment.query.count() == 1
    assert Payment.query.one().mpesa_receipt == "SJK0000010"


def test_processing_keeps_waiting_then_times_out(till):
    push(till)
    age(100)
    busy = {"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"}
    till["fake"].default("/query", 500, busy)
    stk.sweep_tenant()
    assert intent().status == "pending" and intent().last_queried_at is not None
    age(11 * 60)
    stk.sweep_tenant()
    assert intent().status == "timeout"
    assert invoice_status() == "open"


def test_sweep_leaves_young_intents_alone(till):
    push(till)
    stk.sweep_tenant()
    assert till["fake"].posts("/query") == []


def test_polling_asks_daraja_after_30_seconds(till):
    r = push(till)
    intent_id = r.get_json()["intent"]["id"]
    poll = till["client"].get(f"/payments/intents/{intent_id}")
    assert poll.get_json()["intent"]["status"] == "pending"
    assert till["fake"].posts("/query") == []
    age(40)
    till["fake"].answer("/query", 200, {"ResultCode": "0"})
    poll = till["client"].get(f"/payments/intents/{intent_id}")
    assert poll.get_json()["intent"]["status"] == "completed"
    assert poll.get_json()["invoice"]["status"] == "paid"


def test_paybill_and_stk_for_one_payment_in_either_order(till):
    push(till)
    inv = Invoice.query.one()
    till["client"].post(till["c2b_url"],
                        json=daraja_fakes.c2b("SJK0000001", 100, inv.invoice_number))
    till["fake"].default("/query", 200, {"ResultCode": "0"})
    callback(till, receipt="SJK0000001")
    assert Payment.query.count() == 1
    assert intent().status == "completed" and invoice_status() == "paid"


def test_sweep_command(app, till):
    push(till)
    age(100)
    till["fake"].answer("/query", 200, {"ResultCode": "0"})
    result = app.test_cli_runner().invoke(args=["payments-sweep"])
    assert result.exit_code == 0, result.output
    assert "completed': 1" in result.output


# --- typed codes, review, assign, reverse ----------------------------------------------------

def test_typed_code_then_daraja_confirms(till):
    r = till["client"].post("/payments/manual", json={"transaction_id": till["txn"],
                                                      "receipt": "sjk0000020", "amount": 100})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["invoice"]["status"] == "paid"
    review = till["client"].get("/payments/review")
    assert review.status_code in (401, 403)              # cashier cannot see the queue
    inv = Invoice.query.one()
    till["client"].post(till["c2b_url"],
                        json=daraja_fakes.c2b("SJK0000020", 100, inv.invoice_number))
    p = Payment.query.one()
    assert p.verified and p.method == "mpesa_manual"
    dup = till["client"].post("/payments/manual", json={"transaction_id": till["txn"],
                                                        "receipt": "SJK0000020", "amount": 100})
    assert dup.status_code in (400, 409)


def test_typed_codes_can_be_switched_off(app, till):
    from auth.tenancy import unscoped
    from models import Tenant
    with unscoped():
        db.session.get(Tenant, app.acme_id).manual_mpesa_enabled = False
        db.session.commit()
    r = till["client"].post("/payments/manual", json={"transaction_id": till["txn"],
                                                      "receipt": "SJK0000021", "amount": 100})
    assert r.status_code == 403


def test_owner_assigns_unmatched_payment_and_reverses_one(till, login_as):
    inv = billing.create_invoice(total=300, kind="credit")
    db.session.commit()
    inv_id = inv.id
    till["client"].post(till["c2b_url"], json=daraja_fakes.c2b("SJK0000030", 300, "TYPO"))
    owner = login_as("superuser")
    queue = owner.get("/payments/review").get_json()
    assert [p["mpesa_receipt"] for p in queue["unmatched"]] == ["SJK0000030"]
    pid = queue["unmatched"][0]["id"]
    r = owner.post(f"/payments/{pid}/assign", json={"invoice_id": inv_id})
    assert r.status_code == 200 and r.get_json()["invoice"]["status"] == "paid"
    r = owner.post(f"/payments/{pid}/reverse", json={"reason": "Safaricom reversal"})
    assert r.get_json()["payment"]["status"] == "reversed"
    assert db.session.get(Invoice, inv_id).status == "open"
    cashier = login_as("cashier")
    assert cashier.post(f"/payments/{pid}/assign", json={"invoice_id": inv_id}).status_code == 403
