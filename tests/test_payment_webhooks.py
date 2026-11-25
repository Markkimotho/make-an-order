"""Card (Flutterwave) webhook and verify routes, and transaction refs.

The webhook arrives with no session, so no tenant is bound; it finds the
owning business by the transaction ref. M-Pesa webhooks are per-business
URLs, tested in test_daraja_webhooks.py.
"""
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import TXN_OPEN, Payment, Transaction, db
from services import billing


def stk_callback(checkout_id, success=True):
    body = {"Body": {"stkCallback": {"CheckoutRequestID": checkout_id,
                                     "ResultCode": 0 if success else 1032,
                                     "ResultDesc": "ok" if success else "Cancelled by user"}}}
    if success:
        body["Body"]["stkCallback"]["CallbackMetadata"] = {"Item": [
            {"Name": "Amount", "Value": 100},
            {"Name": "MpesaReceiptNumber", "Value": "SJK82910PL"},
        ]}
    return body


@pytest.fixture
def open_txns(app, other_tenant):
    """One open transaction per tenant; same transaction_ref in both."""
    ids = {}
    for name, tid, cri in (("acme", app.acme_id, "ws_CO_ACME"),
                           ("globex", other_tenant, "ws_CO_GLOBEX")):
        with tenant_scope(tid):
            txn = Transaction(transaction_ref="TXN-1", total=Decimal("100"), status=TXN_OPEN,
                              checkout_request_id=cri)
            db.session.add(txn)
            db.session.commit()
            ids[name] = (tid, txn.id)
    return ids


def status_of(tid, txn_id):
    with tenant_scope(tid):
        return db.session.get(Transaction, txn_id).status


@pytest.fixture
def cards_on(app, monkeypatch):
    monkeypatch.setitem(app.config, "CARD_PAYMENTS_ENABLED", True)


def test_card_routes_are_off_by_default(client, login_as):
    assert client.post("/payments/card/webhook", json={}).status_code == 404
    c = login_as("cashier")
    assert c.post("/payments/card/initiate", json={"transaction_id": 1}).status_code == 404
    assert c.get("/payments/card/verify/123").status_code == 404


def test_card_webhook_with_ambiguous_reference_changes_nothing(client, open_txns, monkeypatch,
                                                               cards_on):
    """transaction_ref is unique per tenant only; a webhook naming a ref two
    tenants share must not complete either."""
    from services import flutterwave_service
    monkeypatch.setattr(flutterwave_service, "validate_webhook_signature", lambda raw, sig: True)
    monkeypatch.setattr(flutterwave_service, "verify_transaction",
                        lambda flw_id: {"status": "successful", "amount": 100, "flw_ref": "F1"})
    r = client.post("/payments/card/webhook", json={"data": {"tx_ref": "TXN-1", "id": 9}})
    assert r.status_code == 200 and r.get_json() == {"status": "unknown_txn"}
    assert status_of(*open_txns["acme"]) == TXN_OPEN
    assert status_of(*open_txns["globex"]) == TXN_OPEN


def test_card_webhook_with_unique_reference_completes_it(client, open_txns, monkeypatch,
                                                       cards_on):
    from services import flutterwave_service
    tid, txn_id = open_txns["globex"]
    with tenant_scope(tid):
        db.session.get(Transaction, txn_id).transaction_ref = "TXN-GLOBEX"
        db.session.commit()
    monkeypatch.setattr(flutterwave_service, "validate_webhook_signature", lambda raw, sig: True)
    monkeypatch.setattr(flutterwave_service, "verify_transaction",
                        lambda flw_id: {"status": "successful", "amount": 100, "flw_ref": "F1",
                                        "tx_ref": "TXN-GLOBEX"})
    r = client.post("/payments/card/webhook", json={"data": {"tx_ref": "TXN-GLOBEX", "id": 9}})
    assert r.status_code == 200
    # The card payment pays the sale's invoice; the till then completes the sale.
    with tenant_scope(tid):
        invoice = billing.open_invoice_for_transaction(txn_id)
        assert (invoice.status, invoice.total) == ("paid", Decimal("100.00"))
        assert Payment.query.one().mpesa_receipt == "FLWF1"
    # A retried webhook is recorded once.
    client.post("/payments/card/webhook", json={"data": {"tx_ref": "TXN-GLOBEX", "id": 9}})
    with tenant_scope(tid):
        assert Payment.query.count() == 1


def test_card_webhook_rejects_provider_record_for_another_ref(client, open_txns, monkeypatch,
                                                              cards_on):
    """A signed event pairing our ref with someone else's successful payment."""
    from services import flutterwave_service
    tid, txn_id = open_txns["globex"]
    with tenant_scope(tid):
        db.session.get(Transaction, txn_id).transaction_ref = "TXN-GLOBEX"
        db.session.commit()
    monkeypatch.setattr(flutterwave_service, "validate_webhook_signature", lambda raw, sig: True)
    monkeypatch.setattr(flutterwave_service, "verify_transaction",
                        lambda flw_id: {"status": "successful", "amount": 100, "flw_ref": "F9",
                                        "tx_ref": "TXN-SOMEONE-ELSE"})
    r = client.post("/payments/card/webhook", json={"data": {"tx_ref": "TXN-GLOBEX", "id": 9}})
    assert r.get_json() == {"status": "mismatch"}
    assert status_of(tid, txn_id) == TXN_OPEN


def test_transaction_refs_are_unique_across_businesses(app, other_tenant, login_as):
    refs = []
    for tid in (app.acme_id, other_tenant):
        client = login_as("admin", tenant_id=tid)
        client.post("/pos/shift/open", json={"opening_float": 0})
        r = client.post("/pos/transaction/new", json={})
        assert r.status_code in (200, 201), r.get_json()
        refs.append(r.get_json()["transaction"]["transaction_ref"])
    assert refs[0] != refs[1]
    assert refs[0].startswith(f"TXN-{app.acme_id}-") and refs[1].startswith(f"TXN-{other_tenant}-")


def test_card_verify_refuses_another_tenants_reference(login_as, open_txns, monkeypatch,
                                                      cards_on):
    from services import flutterwave_service
    asked = []
    monkeypatch.setattr(flutterwave_service, "verify_transaction",
                        lambda ref: asked.append(ref) or {"status": "successful"})
    tid, txn_id = open_txns["globex"]
    with tenant_scope(tid):
        db.session.get(Transaction, txn_id).card_ref = "FLW-GLOBEX"
        db.session.commit()
    client = login_as("cashier")
    assert client.get("/payments/card/verify/FLW-GLOBEX").status_code == 404
    assert asked == []
