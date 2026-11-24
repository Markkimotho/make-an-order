"""Per-business Daraja client, with Safaricom's HTTP API stubbed."""
import base64

import pytest
from cryptography.fernet import Fernet

from auth.tenancy import unscoped
from models import Tenant, db
from services.daraja import (DarajaBusy, DarajaClient, DarajaNotConfigured, DarajaUnavailable,
                             callback_url, stk_amount, tokens)


class FakeDaraja:
    """Records calls; answers from a queue per path suffix."""

    def __init__(self):
        self.calls = []
        self.token_fetches = 0
        self.answers = {}

    def answer(self, path_end, status, body):
        self.answers.setdefault(path_end, []).append((status, body))

    def _reply(self, url):
        for path_end, queue in self.answers.items():
            if url.endswith(path_end) and queue:
                status, body = queue.pop(0)
                if isinstance(body, Exception):
                    raise body
                return Resp(status, body)
        raise AssertionError(f"unexpected call {url}")

    def get(self, url, auth=None, timeout=None):
        self.token_fetches += 1
        self.calls.append(("GET", url, auth, None))
        return Resp(200, {"access_token": f"tok{self.token_fetches}", "expires_in": "3599"})

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, headers, json))
        return self._reply(url)


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture
def daraja(monkeypatch):
    fake = FakeDaraja()
    import services.daraja.client as client_module
    monkeypatch.setattr(client_module.requests, "get", fake.get)
    monkeypatch.setattr(client_module.requests, "post", fake.post)
    monkeypatch.setattr(tokens, "_get_redis", lambda: None)
    tokens._memory.clear()
    return fake


@pytest.fixture
def shop(app, monkeypatch):
    monkeypatch.setitem(app.config, "TENANT_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr("config.Config.APP_URL", "https://app.example.co.ke")

    def make(slug, kind="paybill", **extra):
        with unscoped():
            t = Tenant(name=slug, slug=slug, vertical="distributor", daraja_shortcode="600123",
                       daraja_shortcode_type=kind, daraja_consumer_key=f"ck-{slug}",
                       daraja_consumer_secret=f"cs-{slug}", daraja_passkey="pk",
                       daraja_till_number="5551234" if kind == "till" else None, **extra)
            db.session.add(t)
            db.session.commit()
            db.session.refresh(t)
            for name in ("daraja_consumer_key", "daraja_consumer_secret", "daraja_passkey"):
                getattr(t, name)          # load deferred secrets while attached
            db.session.expunge(t)
            return t
    return make


def test_paybill_push_payload(daraja, shop):
    daraja.answer("/processrequest", 200, {"ResponseCode": "0", "CheckoutRequestID": "ws_1"})
    t = shop("duka-mosi")
    out = DarajaClient(t).stk_push("0712 345 678", 100.50, "INV000001", "Order")
    assert out["CheckoutRequestID"] == "ws_1"
    payload = daraja.calls[-1][3]
    assert payload["TransactionType"] == "CustomerPayBillOnline"
    assert (payload["BusinessShortCode"], payload["PartyB"]) == ("600123", "600123")
    assert payload["Amount"] == 101                      # rounded up, never under-collect
    assert payload["PhoneNumber"] == payload["PartyA"] == "254712345678"
    assert payload["AccountReference"] == "INV000001"
    assert payload["CallBackURL"] == (
        f"https://app.example.co.ke/api/payments/daraja/stk/duka-mosi/{t.callback_token}")
    raw = base64.b64decode(payload["Password"]).decode()
    assert raw == f"600123pk{payload['Timestamp']}"


def test_till_push_uses_buy_goods(daraja, shop):
    daraja.answer("/processrequest", 200, {"ResponseCode": "0", "CheckoutRequestID": "ws_2"})
    DarajaClient(shop("kiosk", kind="till")).stk_push("0712345678", 50, "INV000002")
    payload = daraja.calls[-1][3]
    assert payload["TransactionType"] == "CustomerBuyGoodsOnline"
    assert (payload["BusinessShortCode"], payload["PartyB"]) == ("600123", "5551234")


def test_busy_push(daraja, shop):
    daraja.answer("/processrequest", 500, {"errorCode": "500.001.1001",
                                          "errorMessage": "Unable to lock subscriber"})
    with pytest.raises(DarajaBusy):
        DarajaClient(shop("a")).stk_push("0712345678", 10, "INV000001")


@pytest.mark.parametrize("status,body,state", [
    (200, {"ResultCode": "0", "ResultDesc": "ok"}, "paid"),
    (200, {"ResultCode": "1032", "ResultDesc": "Request cancelled by user"}, "failed"),
    (200, {"ResultCode": "2001", "ResultDesc": "Wrong PIN"}, "failed"),
    (500, {"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"},
     "processing"),
])
def test_query_states(daraja, shop, status, body, state):
    daraja.answer("/query", status, body)
    assert DarajaClient(shop("a")).query("ws_1").state == state


def test_query_outage(daraja, shop):
    import requests
    daraja.answer("/query", 200, requests.Timeout("slow"))
    with pytest.raises(DarajaUnavailable):
        DarajaClient(shop("a")).query("ws_1")


def test_token_cached_once_per_business(daraja, shop):
    for _ in range(3):
        daraja.answer("/query", 200, {"ResultCode": "0"})
    a, b = shop("a"), shop("b")
    DarajaClient(a).query("x")
    DarajaClient(a).query("y")
    DarajaClient(b).query("z")
    gets = [c for c in daraja.calls if c[0] == "GET"]
    assert [c[2] for c in gets] == [("ck-a", "cs-a"), ("ck-b", "cs-b")]
    bearer = [c[2]["Authorization"] for c in daraja.calls if c[0] == "POST"]
    assert bearer == ["Bearer tok1", "Bearer tok1", "Bearer tok2"]


def test_token_cache_uses_redis_when_available(daraja, shop, monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.data, self.ttls = {}, {}

        def get(self, k):
            return self.data.get(k)

        def setex(self, k, ttl, v):
            self.data[k], self.ttls[k] = v, ttl

    store = FakeRedis()
    monkeypatch.setattr(tokens, "_get_redis", lambda: store)
    daraja.answer("/query", 200, {"ResultCode": "0"})
    daraja.answer("/query", 200, {"ResultCode": "0"})
    t = shop("a")
    DarajaClient(t).query("x")
    DarajaClient(t).query("y")
    assert daraja.token_fetches == 1
    assert store.ttls[f"daraja:token:{t.id}"] == 3599 - 120


def test_not_configured(shop, app):
    with unscoped():
        bare = Tenant(name="x", slug="x", vertical="pharmacy")
        db.session.add(bare)
        db.session.commit()
        db.session.refresh(bare)
        for name in ("daraja_consumer_key", "daraja_consumer_secret", "daraja_passkey"):
            getattr(bare, name)
        db.session.expunge(bare)
    with pytest.raises(DarajaNotConfigured):
        DarajaClient(bare)


def test_register_c2b(daraja, shop):
    daraja.answer("/registerurl", 200, {"ResponseCode": "0",
                                        "ResponseDescription": "Success"})
    t = shop("duka-mosi")
    DarajaClient(t).register_c2b()
    payload = daraja.calls[-1][3]
    assert payload["ResponseType"] == "Completed"
    assert payload["ConfirmationURL"].endswith(f"/c2b/duka-mosi/{t.callback_token}")
    assert payload["ValidationURL"].endswith(f"/c2b/duka-mosi/{t.callback_token}/validate")


def test_callback_urls_avoid_words_daraja_refuses(shop):
    t = shop("mpesa-safaricom-shop")      # even a slug with those words is the business's choice
    for kind in ("stk", "c2b"):
        path = callback_url(t, kind).split("/api/", 1)[1].split("/" + t.slug)[0]
        assert "mpesa" not in path.lower() and "safaricom" not in path.lower()


@pytest.mark.parametrize("balance,amount", [("100.50", 101), ("100", 100), ("0.20", 1),
                                            ("99.01", 100)])
def test_stk_amount_rounds_up(balance, amount):
    assert stk_amount(balance) == amount
