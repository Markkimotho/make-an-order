"""Public Daraja webhooks: per-business URLs, exact replies, no strays."""
import json
import logging
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import Customer, Invoice, Payment, db
from services import billing
from tests import daraja_fakes

STK_OK = {"ResultCode": 0, "ResultDesc": "Accepted"}
C2B_OK = {"ResultCode": 0, "ResultDesc": "Confirmation received successfully"}


@pytest.fixture
def shop(app, monkeypatch, other_tenant):
    fake = daraja_fakes.install(monkeypatch)
    slug, token = daraja_fakes.configure(app, monkeypatch, app.acme_id)
    other_slug, other_token = daraja_fakes.configure(app, monkeypatch, other_tenant)
    duka = Customer(name="Duka", phone_number="0712000444", code="D1")
    db.session.add(duka)
    db.session.flush()
    invoice = billing.create_invoice(total=500, kind="credit", customer_id=duka.id)
    db.session.commit()
    return {"fake": fake, "slug": slug, "token": token, "other_slug": other_slug,
            "other_token": other_token, "invoice": invoice.invoice_number,
            "invoice_id": invoice.id, "other": other_tenant, "acme": app.acme_id}


def c2b_url(slug, token):
    return f"/api/payments/daraja/c2b/{slug}/{token}"


def payments_in(tenant_id):
    with tenant_scope(tenant_id):
        return [(p.mpesa_receipt, p.amount, p.match_status) for p in Payment.query.all()]


def test_c2b_confirmation_records_and_matches(client, shop):
    r = client.post(c2b_url(shop["slug"], shop["token"]),
                    json=daraja_fakes.c2b("SJK0000001", 500, shop["invoice"].lower(), 90500))
    assert r.status_code == 200
    assert json.loads(r.get_data()) == C2B_OK
    assert list(json.loads(r.get_data())) == ["ResultCode", "ResultDesc"]
    assert payments_in(shop["acme"]) == [("SJK0000001", Decimal("500.00"), "matched")]
    with tenant_scope(shop["acme"]):
        p = Payment.query.one()
        assert (p.payer_name, p.org_account_balance) == ("WANJIKU KAMAU", Decimal("90500.00"))
        assert db.session.get(Invoice, shop["invoice_id"]).status == "paid"


def test_c2b_retry_counts_once(client, shop):
    for _ in range(3):
        r = client.post(c2b_url(shop["slug"], shop["token"]),
                        json=daraja_fakes.c2b("SJK0000001", 500, shop["invoice"]))
        assert r.get_json() == C2B_OK
    assert len(payments_in(shop["acme"])) == 1


def test_unknown_account_is_accepted_and_queued(client, shop):
    r = client.post(c2b_url(shop["slug"], shop["token"]),
                    json=daraja_fakes.c2b("SJK0000002", 250, "ADM 4021"))
    assert r.get_json() == C2B_OK
    assert payments_in(shop["acme"]) == [("SJK0000002", Decimal("250.00"), "unmatched")]


def test_validation_never_rejects(client, shop):
    r = client.post(c2b_url(shop["slug"], shop["token"]) + "/validate",
                    json=daraja_fakes.c2b("SJK0000003", 1, "WHATEVER"))
    assert r.status_code == 200 and r.get_json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
    assert payments_in(shop["acme"]) == []


@pytest.mark.parametrize("which", ["wrong_token", "other_tenants_token", "unknown_slug"])
def test_bad_url_records_nothing(client, shop, which):
    slug, token = {
        "wrong_token": (shop["slug"], "not-the-token"),
        "other_tenants_token": (shop["slug"], shop["other_token"]),
        "unknown_slug": ("no-such-shop", shop["token"]),
    }[which]
    for url in (c2b_url(slug, token), c2b_url(slug, token) + "/validate",
                f"/api/payments/daraja/stk/{slug}/{token}"):
        r = client.post(url, json=daraja_fakes.c2b("SJK0000004", 500, shop["invoice"]))
        assert r.status_code == 404
    assert payments_in(shop["acme"]) == [] and payments_in(shop["other"]) == []


def test_payment_lands_in_the_business_whose_url_was_called(client, shop):
    """Both businesses have an INV000001; the URL decides whose it is."""
    with tenant_scope(shop["other"]):
        other_invoice = billing.create_invoice(total=500)
        db.session.commit()
        assert other_invoice.invoice_number == shop["invoice"]
    client.post(c2b_url(shop["other_slug"], shop["other_token"]),
                json=daraja_fakes.c2b("SJK0000005", 500, shop["invoice"]))
    assert payments_in(shop["acme"]) == []
    assert payments_in(shop["other"]) == [("SJK0000005", Decimal("500.00"), "matched")]


def test_c2b_without_trans_id_is_ignored(client, shop):
    body = daraja_fakes.c2b("", 500, shop["invoice"])
    r = client.post(c2b_url(shop["slug"], shop["token"]), json=body)
    assert r.get_json() == C2B_OK
    assert payments_in(shop["acme"]) == []


def test_ip_allowlist(client, shop, app, monkeypatch):
    monkeypatch.setitem(app.config, "DARAJA_ALLOWED_IPS", "196.201.214.200, 196.201.214.206")
    url = c2b_url(shop["slug"], shop["token"])
    body = daraja_fakes.c2b("SJK0000006", 500, shop["invoice"])
    assert client.post(url, json=body,
                       headers={"X-Forwarded-For": "6.6.6.6"}).status_code == 404
    assert client.post(url, json=body,
                       headers={"X-Forwarded-For": "196.201.214.200"}).status_code == 200
    assert len(payments_in(shop["acme"])) == 1


def test_callback_token_is_redacted_from_logs(caplog):
    from app import RedactCallbackTokens
    record = logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                               '"POST /api/payments/daraja/c2b/acme/s3cr3tT0ken HTTP/1.1" 200',
                               (), None)
    RedactCallbackTokens().filter(record)
    assert "s3cr3tT0ken" not in record.getMessage()
    assert "/api/payments/daraja/c2b/acme/[token]" in record.getMessage()


def test_webhook_routes_never_contain_refused_words(app):
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith("/api/payments/daraja"):
            assert "mpesa" not in rule.rule.lower() and "safaricom" not in rule.rule.lower()
