"""Public pay link /o/<slug>/pay/<pay_token>: no login, found only by its
token within its own business, pushes STK, polls, rate-limited."""
import logging
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope, unscoped
from models import Customer, Invoice, PaymentIntent, Tenant, db
from services import billing
from tests import daraja_fakes
from tests.conftest import make_app

D = Decimal


@pytest.fixture
def link(app, monkeypatch):
    """Acme with Daraja set up and an open KES 1,500.50 credit invoice."""
    fake = daraja_fakes.install(monkeypatch)
    slug, token = daraja_fakes.configure(app, monkeypatch, app.acme_id)
    cust = Customer(name="Wanjiku Kamau", phone_number="0722000999", code="WK")
    db.session.add(cust)
    db.session.flush()
    invoice = billing.create_invoice(total=D("1500.50"), kind="credit", customer_id=cust.id)
    db.session.commit()
    return {"fake": fake, "slug": slug, "token": invoice.pay_token, "id": invoice.id,
            "number": invoice.invoice_number,
            "url": f"/o/{slug}/pay/{invoice.pay_token}",
            "stk_url": f"/api/payments/daraja/stk/{slug}/{token}"}


def push(client, link, phone="0712345678", ip="196.201.214.1"):
    return client.post(link["url"] + "/stk", json={"phone": phone},
                       headers={"X-Forwarded-For": ip})


# --- the page -------------------------------------------------------------------

def test_page_opens_without_login_and_shows_only_the_invoice(client, link):
    r = client.get(link["url"])
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Acme Pharmacy" in html and link["number"] in html and "1,500.50" in html
    assert "Pay with M-Pesa" in html
    assert "Wanjiku" not in html and "0722000999" not in html and "254722000999" not in html
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "noindex" in r.headers["X-Robots-Tag"]


def test_wrong_links_are_404(app, client, link, other_tenant):
    assert client.get(f"/o/{link['slug']}/pay/not-a-token").status_code == 404
    assert client.get(f"/o/nobody/pay/{link['token']}").status_code == 404
    with unscoped():
        globex = db.session.get(Tenant, other_tenant).slug
    assert client.get(f"/o/{globex}/pay/{link['token']}").status_code == 404   # A's token, B's slug
    assert client.post(f"/o/{globex}/pay/{link['token']}/stk",
                       json={"phone": "0712345678"}).status_code == 404


def test_a_lapsed_business_has_no_public_pages(app, client, link):
    with unscoped():
        db.session.get(Tenant, app.acme_id).subscription_status = "cancelled"
        db.session.commit()
    assert client.get(link["url"]).status_code == 404


def test_a_void_invoice_is_404(app, client, link):
    billing.void_invoice(db.session.get(Invoice, link["id"]))
    db.session.commit()
    assert client.get(link["url"]).status_code == 404


# --- paying ---------------------------------------------------------------------

def test_push_from_the_page_pays_the_balance_rounded_up(client, link):
    r = push(client, link)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["intent"]["status"] == "pending" and body["invoice"]["balance"] == 1500.5
    sent = link["fake"].posts("/processrequest")[0]
    assert (sent["Amount"], sent["AccountReference"], sent["PhoneNumber"]) == \
        (1501, link["number"], "254712345678")
    assert "phone" not in body["intent"]                 # nothing about the payer echoed
    assert PaymentIntent.query.one().initiated_by is None


def test_status_follows_the_payment_to_paid(client, link):
    intent_id = push(client, link).get_json()["intent"]["id"]
    status = client.get(f"{link['url']}/status?intent={intent_id}").get_json()
    assert status["intent"]["status"] == "pending" and not status["intent"]["done"]
    link["fake"].default("/query", 200, {"ResponseCode": "0", "ResultCode": "0",
                                         "ResultDesc": "The service request is processed successfully."})
    cri = PaymentIntent.query.one().checkout_request_id
    assert client.post(link["stk_url"], json=daraja_fakes.stk_callback(
        cri, receipt="SKP1234567", amount=1501)).status_code == 200
    status = client.get(f"{link['url']}/status?intent={intent_id}").get_json()
    assert status["intent"]["status"] == "completed" and status["intent"]["done"]
    assert status["invoice"]["status"] == "paid" and status["invoice"]["balance"] == 0
    html = client.get(link["url"]).get_data(as_text=True)
    assert "Paid in full" in html
    r = push(client, link, phone="0712345670")
    assert r.status_code == 409 and "already paid" in r.get_json()["error"]


def test_status_only_for_this_invoices_intents(app, client, link):
    other = billing.create_invoice(total=D("50"), kind="sale")
    db.session.commit()
    other_url = f"/o/{link['slug']}/pay/{other.pay_token}"
    other_intent = client.post(other_url + "/stk", json={"phone": "0712345671"}).get_json()
    intent_id = other_intent["intent"]["id"]
    assert client.get(f"{link['url']}/status?intent={intent_id}").status_code == 404
    assert client.get(f"{link['url']}/status?intent=abc").status_code == 404
    assert client.get(f"{link['url']}/status").status_code == 404


def test_polling_asks_daraja_at_most_every_15_seconds(client, link):
    intent_id = push(client, link).get_json()["intent"]["id"]
    intent = db.session.get(PaymentIntent, intent_id)
    intent.created_at = datetime.utcnow() - timedelta(seconds=45)
    db.session.commit()
    link["fake"].default("/query", 500, {"errorCode": "500.001.1001",
                                         "errorMessage": "The transaction is being processed"})
    for _ in range(4):
        assert client.get(f"{link['url']}/status?intent={intent_id}").status_code == 200
    assert len(link["fake"].posts("/query")) == 1


def test_errors_a_customer_can_act_on(app, client, link):
    r = push(client, link, phone="12345")
    assert r.status_code == 400 and "Kenyan mobile number" in r.get_json()["error"]
    assert push(client, link, phone="").status_code == 400
    assert push(client, link).status_code == 201
    r = push(client, link)                                    # same phone within 90 s
    assert r.status_code == 409 and "already sent a prompt" in r.get_json()["error"]


def test_business_without_daraja_says_pay_at_the_counter(app, client, link):
    with unscoped():
        db.session.get(Tenant, app.acme_id).daraja_consumer_key = None
        db.session.commit()
    r = push(client, link)
    assert r.status_code == 400 and "Pay at the counter" in r.get_json()["error"]


# --- rate limits ------------------------------------------------------------------

def test_five_pushes_a_minute_per_ip(client, link):
    codes = [push(client, link, phone="12345", ip="41.90.1.1").status_code for _ in range(6)]
    assert codes == [400] * 5 + [429]
    assert push(client, link, phone="12345", ip="41.90.1.2").status_code == 400   # other IP


def test_three_prompts_per_invoice_per_10_minutes_from_any_ip(client, link):
    codes = [push(client, link, phone=f"071234560{i}", ip=f"41.90.2.{i}").status_code
             for i in range(4)]
    assert codes == [201, 201, 201, 429]
    assert len(link["fake"].posts("/processrequest")) == 3


def test_refused_requests_do_not_use_up_the_invoice_limit(client, link):
    for i in range(3):
        assert push(client, link, phone="12345", ip=f"41.90.3.{i}").status_code == 400
    assert push(client, link, ip="41.90.3.9").status_code == 201


def test_redis_down_still_serves_the_page(app, database_url, link):
    down = make_app(database_url, RATELIMIT_STORAGE_URI="redis://127.0.0.1:1/0")
    with down.app_context(), tenant_scope(app.acme_id):
        c = down.test_client()
        assert c.get(link["url"]).status_code == 200
        assert c.post(link["url"] + "/stk", json={"phone": "12345"}).status_code == 400


# --- links and logs -------------------------------------------------------------------

def test_pay_url(app, link, monkeypatch):
    invoice = db.session.get(Invoice, link["id"])
    monkeypatch.setitem(app.config, "PUBLIC_BASE_URL", "https://pay.example.co.ke/")
    assert billing.pay_url(invoice) == f"https://pay.example.co.ke{link['url']}"
    monkeypatch.setitem(app.config, "PUBLIC_BASE_URL", None)

    # pytest-flask pushes a request context into every test; a fresh thread
    # has none, like the SMS jobs run from the CLI.
    import threading
    raised = []

    def job():
        with app.app_context(), tenant_scope(app.acme_id):
            try:
                billing.pay_url(db.session.get(Invoice, link["id"]))
            except billing.BillingError as e:
                raised.append(str(e))
            finally:
                db.session.remove()

    worker = threading.Thread(target=job)
    worker.start()
    worker.join()
    assert raised and "PUBLIC_BASE_URL" in raised[0]
    with app.test_request_context(base_url="https://shop.local"):
        assert billing.pay_url(invoice) == f"https://shop.local{link['url']}"


def test_pay_tokens_are_kept_out_of_logs(link):
    from app import RedactCallbackTokens
    record = logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                               '"GET %s/status?intent=4 HTTP/1.1" 200', (link["url"],), None)
    RedactCallbackTokens().filter(record)
    assert link["token"] not in record.getMessage()
    assert f"/o/{link['slug']}/pay/[token]/status" in record.getMessage()


def test_case_variants_of_the_link_are_404(client, link):
    """MySQL compares case-insensitively; a variant would otherwise be a
    second identity for the same invoice."""
    assert client.get(f"/o/{link['slug'].upper()}/pay/{link['token']}").status_code == 404
    assert client.get(f"/o/{link['slug']}/pay/{link['token'].swapcase()}").status_code == 404


def test_the_invoice_cap_counts_prompts_in_the_database(client, link):
    """Counted from payment_intents under the invoice lock, so it holds for
    requests racing in parallel, not just one after another."""
    for i in range(3):
        db.session.add(PaymentIntent(invoice_id=link["id"], phone=f"25471234560{i}",
                                     amount=D("1501"), status="failed"))
    db.session.commit()
    r = push(client, link, ip="41.90.9.9")
    assert r.status_code == 429 and "Too many" in r.get_json()["error"]
    assert link["fake"].posts("/processrequest") == []
    old = PaymentIntent.query.all()
    for intent in old:
        intent.created_at = datetime.utcnow() - timedelta(minutes=11)
    db.session.commit()
    assert push(client, link, ip="41.90.9.9").status_code == 201


@pytest.mark.skipif(not __import__("tests.conftest", fromlist=["on_mysql"]).on_mysql(),
                    reason="row locks need MySQL")
def test_parallel_pushes_send_at_most_three_prompts(app, link):
    import threading
    results, barrier = [], threading.Barrier(5)

    def worker(i):
        with app.app_context():
            c = app.test_client()
            barrier.wait()
            results.append(push(c, link, phone=f"07123456{i:02d}", ip=f"41.90.8.{i}").status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 201, 201, 429, 429]
    assert len(link["fake"].posts("/processrequest")) == 3
