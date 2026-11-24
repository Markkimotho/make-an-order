"""Payment webhooks arrive with no session, so no tenant is bound.

Review finding: after tenancy, the M-Pesa and Flutterwave callbacks raised
TenantNotBound (a 500 in production) and payments were never recorded. The
test suite had hidden it because requests inherited the test's tenant.
"""
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import TXN_COMPLETED, TXN_OPEN, Transaction, db


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


def test_stk_callback_without_session_marks_the_right_tenants_txn(client, open_txns):
    r = client.post("/payments/mpesa/callback", json=stk_callback("ws_CO_GLOBEX"))
    assert r.status_code == 200
    assert r.get_json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
    assert status_of(*open_txns["globex"]) == TXN_COMPLETED
    assert status_of(*open_txns["acme"]) == TXN_OPEN


def test_stk_callback_failure_leaves_txn_open(client, open_txns):
    r = client.post("/payments/mpesa/callback", json=stk_callback("ws_CO_ACME", success=False))
    assert r.status_code == 200
    assert status_of(*open_txns["acme"]) == TXN_OPEN


def test_stk_callback_unknown_reference_is_acknowledged(client, open_txns):
    r = client.post("/payments/mpesa/callback", json=stk_callback("ws_CO_NOPE"))
    assert r.status_code == 200
    assert r.get_json()["ResultCode"] == 0


def test_card_webhook_with_ambiguous_reference_changes_nothing(client, open_txns, monkeypatch):
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


def test_card_webhook_with_unique_reference_completes_it(client, open_txns, monkeypatch):
    from services import flutterwave_service
    tid, txn_id = open_txns["globex"]
    with tenant_scope(tid):
        db.session.get(Transaction, txn_id).transaction_ref = "TXN-GLOBEX"
        db.session.commit()
    monkeypatch.setattr(flutterwave_service, "validate_webhook_signature", lambda raw, sig: True)
    monkeypatch.setattr(flutterwave_service, "verify_transaction",
                        lambda flw_id: {"status": "successful", "amount": 100, "flw_ref": "F1"})
    r = client.post("/payments/card/webhook", json={"data": {"tx_ref": "TXN-GLOBEX", "id": 9}})
    assert r.status_code == 200
    assert status_of(tid, txn_id) == TXN_COMPLETED


def test_status_lookup_refuses_another_tenants_checkout(login_as, open_txns, app, monkeypatch):
    """Review sweep finding: the status route asked the provider about any
    checkout id, so business B could read business A's payment status."""
    from services import mpesa_service
    asked = []
    monkeypatch.setattr(mpesa_service, "query_stk_status",
                        lambda cri: asked.append(cri) or {"ResultCode": "0"})
    client = login_as("cashier")                       # acme
    assert client.get("/payments/mpesa/status/ws_CO_GLOBEX").status_code == 404
    assert asked == []
    assert client.get("/payments/mpesa/status/ws_CO_ACME").status_code == 200
    assert asked == ["ws_CO_ACME"]


def test_card_verify_refuses_another_tenants_reference(login_as, open_txns, monkeypatch):
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
