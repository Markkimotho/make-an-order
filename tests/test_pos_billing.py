"""POS checkout, void, refund and customer credit, all on invoices."""
import threading
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import (Customer, InventoryItem, Invoice, Payment, Transaction, TransactionItem,
                    db)
from services import billing
from tests.conftest import on_mysql

D = Decimal


@pytest.fixture
def till(app, login_as, monkeypatch):
    """A cashier with an open shift, an item at KES 100 (tax off)."""
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    item = InventoryItem(name="Unga 2kg", price=D("100"), stock=50)
    duka = Customer(name="Duka Mosi", phone_number="0712000333", code="DUKA",
                    credit_limit=D("500"))
    db.session.add_all([item, duka])
    db.session.commit()
    ids = {"item": item.id, "duka": duka.id}   # read before any request empties the session
    client = login_as("cashier")
    assert client.post("/pos/shift/open", json={"opening_float": 0}).status_code in (200, 201)
    return {"client": client, "app": app, **ids}


def sale(till, qty=1, customer=None):
    c = till["client"]
    r = c.post("/pos/transaction/new", json={"customer_id": customer} if customer else {})
    txn = r.get_json()["transaction"]["id"]
    r = c.post(f"/pos/transaction/{txn}/add_item",
               json={"inventory_item_id": till["item"], "quantity": qty})
    assert r.status_code in (200, 201), r.get_json()
    return txn


def complete(till, txn, method, **extra):
    return till["client"].post(f"/pos/transaction/{txn}/complete",
                               json={"payment_method": method, **extra})


def invoice_of(txn):
    return Invoice.query.filter_by(transaction_id=txn).one()


def test_cash_sale_is_a_paid_invoice(till):
    txn = sale(till, qty=2)
    r = complete(till, txn, "cash")
    assert r.status_code == 200, r.get_json()
    inv = invoice_of(txn)
    assert (inv.status, inv.total, inv.kind) == ("paid", D("200.00"), "sale")
    assert Payment.query.one().method == "cash"
    assert r.get_json()["invoice"]["invoice_number"] == inv.invoice_number


def test_mpesa_completion_refused_until_money_arrives(till):
    txn = sale(till)
    r = complete(till, txn, "mpesa")
    assert r.status_code == 409 and "not received" in r.get_json()["error"]
    assert db.session.get(Transaction, txn).status == "open"
    assert Invoice.query.count() == 0          # nothing half-written


def test_mpesa_completion_after_payment_recorded(till):
    txn = sale(till)
    inv = billing.create_invoice(total=100, transaction_id=txn)
    billing.record_payment(method="mpesa_stk", amount=100, receipt="SJK0000001", invoice=inv)
    db.session.commit()
    r = complete(till, txn, "mpesa")
    assert r.status_code == 200, r.get_json()
    t = db.session.get(Transaction, txn)
    assert (t.status, t.mpesa_ref) == ("completed", "SJK0000001")
    assert Invoice.query.count() == 1


def test_credit_sale_within_limit(till):
    txn = sale(till, qty=3, customer=till["duka"])
    r = complete(till, txn, "credit")
    assert r.status_code == 200, r.get_json()
    inv = invoice_of(txn)
    assert (inv.kind, inv.status, inv.balance) == ("credit", "open", D("300.00"))
    assert inv.due_date is not None
    assert billing.customer_exposure(till["duka"]) == D("300.00")


def test_credit_limit_counts_open_invoices(till):
    complete(till, sale(till, qty=3, customer=till["duka"]), "credit")
    txn = sale(till, qty=3, customer=till["duka"])
    r = complete(till, txn, "credit")
    assert r.status_code == 400 and "credit limit" in r.get_json()["error"]
    assert db.session.get(Transaction, txn).status == "open"
    assert Invoice.query.count() == 1


def test_credit_payment_pays_oldest_first(till):
    for _ in range(2):
        complete(till, sale(till, qty=2, customer=till["duka"]), "credit")
    r = till["client"].post(f"/customers/{till['duka']}/credit/payment",
                            json={"amount": 300, "payment_method": "cash"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["owed"] == 100.0
    statuses = [i.status for i in Invoice.query.order_by(Invoice.id)]
    assert statuses == ["paid", "partial"]
    summary = till["client"].get(f"/customers/{till['duka']}/credit").get_json()
    assert (summary["owed"], summary["available"]) == (100.0, 400.0)


def test_credit_payment_by_mpesa_code(till):
    complete(till, sale(till, qty=2, customer=till["duka"]), "credit")
    r = till["client"].post(f"/customers/{till['duka']}/credit/payment",
                            json={"amount": 200, "payment_method": "mpesa",
                                  "mpesa_ref": "sjk0000009"})
    assert r.status_code == 200
    p = Payment.query.one()
    assert (p.method, p.mpesa_receipt, p.verified) == ("mpesa_manual", "SJK0000009", False)
    again = till["client"].post(f"/customers/{till['duka']}/credit/payment",
                                json={"amount": 200, "payment_method": "mpesa",
                                      "mpesa_ref": "SJK0000009"})
    assert again.status_code == 409


def test_split_cash_and_card(till):
    txn = sale(till, qty=3)
    r = complete(till, txn, "split", cash_amount=100, card_amount=200)
    assert r.status_code == 200, r.get_json()
    assert sorted((p.method, p.amount) for p in Payment.query) == [
        ("card", D("200.00")), ("cash", D("100.00"))]
    assert invoice_of(txn).status == "paid"


def test_split_with_mpesa_part_needs_it_received(till):
    txn = sale(till, qty=3)
    r = complete(till, txn, "split", cash_amount=100, mpesa_amount=200)
    assert r.status_code == 409
    inv = billing.create_invoice(total=300, transaction_id=txn)
    billing.record_payment(method="mpesa_c2b", amount=200, receipt="SJK0000002", invoice=inv)
    db.session.commit()
    r = complete(till, txn, "split", cash_amount=100, mpesa_amount=200)
    assert r.status_code == 200, r.get_json()
    assert invoice_of(txn).status == "paid"


def _void(till, txn):
    c = till["client"]
    from services import otp_service
    c.post(f"/pos/transaction/{txn}/void", json={"reason": "wrong item"})
    otp = otp_service.peek_otp(f"void:txn:{txn}")
    return c.post(f"/pos/transaction/{txn}/void", json={"reason": "wrong item", "otp": otp})


def test_void_cash_sale_hands_cash_back(till):
    txn = sale(till)
    complete(till, txn, "cash")
    r = _void(till, txn)
    assert r.status_code == 200, r.get_json()
    p = Payment.query.one()
    assert (p.status, p.amount) == ("reversed", D("0.00"))
    assert Invoice.query.one().status == "void"


def test_void_mpesa_sale_keeps_money_as_credit(till):
    txn = sale(till, customer=till["duka"])
    inv = billing.create_invoice(total=100, transaction_id=txn, customer_id=till["duka"])
    billing.record_payment(method="mpesa_stk", amount=100, receipt="SJK0000003", invoice=inv)
    db.session.commit()
    complete(till, txn, "mpesa")
    assert _void(till, txn).status_code == 200
    p = Payment.query.one()
    assert (p.amount, p.unallocated_amount) == (D("100.00"), D("100.00"))
    assert billing.customer_exposure(till["duka"]) == D("-100.00")


def test_partial_refund_is_a_credit_note(till):
    txn = sale(till, qty=3, customer=till["duka"])
    complete(till, txn, "credit")
    line = TransactionItem.query.filter_by(transaction_id=txn).one()
    r = till["client"].post(f"/pos/transaction/{txn}/refund",
                            json={"items": [{"transaction_item_id": line.id, "quantity": 1}],
                                  "reason": "damaged"})
    assert r.status_code == 200, r.get_json()
    inv = invoice_of(txn)
    assert (inv.amount_adjusted, inv.balance) == (D("100.00"), D("200.00"))


def test_refund_of_cash_sale_hands_cash_back(till):
    txn = sale(till, qty=2)
    complete(till, txn, "cash")
    line = TransactionItem.query.filter_by(transaction_id=txn).one()
    till["client"].post(f"/pos/transaction/{txn}/refund",
                        json={"items": [{"transaction_item_id": line.id, "quantity": 1}]})
    p = Payment.query.one()
    assert (p.amount, p.unallocated_amount) == (D("100.00"), D("0.00"))
    assert invoice_of(txn).status == "paid"


@pytest.mark.skipif(not on_mysql(), reason="row locks need MySQL")
def test_concurrent_credit_sales_cannot_both_pass_the_ceiling(till):
    """Limit 500; two KES 300 credit sales at the same moment: one wins."""
    app = till["app"]
    txns = [sale(till, qty=3, customer=till["duka"]) for _ in range(2)]
    results, barrier = [], threading.Barrier(2)
    cookie = till["client"].get_cookie(app.config["SESSION_COOKIE_NAME"]).value

    def worker(txn):
        with app.app_context():
            client = app.test_client()
            client.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie)
            barrier.wait()
            results.append(client.post(f"/pos/transaction/{txn}/complete",
                                       json={"payment_method": "credit"}).status_code)

    threads = [threading.Thread(target=worker, args=(t,)) for t in txns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 400]
    with tenant_scope(app.acme_id):
        assert billing.customer_exposure(till["duka"]) == D("300.00")


def test_running_total_includes_the_line_just_added_and_removed(till):
    """Found by the STK tests: the total lagged one line behind."""
    c = till["client"]
    txn = c.post("/pos/transaction/new", json={}).get_json()["transaction"]["id"]
    r = c.post(f"/pos/transaction/{txn}/add_item",
               json={"inventory_item_id": till["item"], "quantity": 2})
    assert r.get_json()["transaction"]["total"] == 200.0
    line = r.get_json()["line"]["id"]
    r = c.post(f"/pos/transaction/{txn}/add_item", json={"inventory_item_id": till["item"]})
    assert r.get_json()["transaction"]["total"] == 300.0
    r = c.delete(f"/pos/transaction/{txn}/remove_item/{line}")
    assert r.get_json()["transaction"]["total"] == 100.0


# --- review findings (step 3) ------------------------------------------------------

def refund(till, txn, qty):
    line = TransactionItem.query.filter_by(transaction_id=txn).one()
    return till["client"].post(f"/pos/transaction/{txn}/refund",
                               json={"items": [{"transaction_item_id": line.id,
                                                "quantity": qty}]})


def test_refund_cannot_be_replayed(till):
    """#8: returning one of three units twice more must not refund KES 200."""
    txn = sale(till, qty=3, customer=till["duka"])
    complete(till, txn, "credit")
    assert refund(till, txn, 1).status_code == 200
    r = refund(till, txn, 3)                    # only 2 are left
    assert r.status_code == 400 and "left to refund" in r.get_json()["error"]
    assert refund(till, txn, 2).status_code == 200
    assert refund(till, txn, 1).status_code == 400     # sale is now fully refunded
    inv = invoice_of(txn)
    assert inv.amount_adjusted == D("300.00")
    assert db.session.get(Transaction, txn).status == "refunded"


def test_duplicate_lines_in_one_request_are_added_up(till):
    txn = sale(till, qty=2, customer=till["duka"])
    complete(till, txn, "credit")
    line = TransactionItem.query.filter_by(transaction_id=txn).one()
    r = till["client"].post(f"/pos/transaction/{txn}/refund", json={"items": [
        {"transaction_item_id": line.id, "quantity": 2},
        {"transaction_item_id": line.id, "quantity": 2}]})
    assert r.status_code == 400
    assert invoice_of(txn).amount_adjusted == D("0.00")


def test_refund_includes_tax_and_full_return_clears_the_invoice(till, monkeypatch):
    """#9: a KES 100 item + 16% tax = KES 116; returning it credits KES 116."""
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0.16)
    txn = sale(till, qty=3, customer=till["duka"])
    complete(till, txn, "credit")
    assert invoice_of(txn).total == D("348.00")
    r = refund(till, txn, 1)
    assert r.get_json()["refunded_value"] == 116.0
    refund(till, txn, 2)
    inv = invoice_of(txn)
    assert (inv.amount_adjusted, inv.balance) == (D("348.00"), D("0.00"))


def test_online_card_completion_needs_the_payment_recorded(till, app, monkeypatch):
    """#10: with online cards on, the till must not record its own card
    payment as well as the provider's."""
    monkeypatch.setitem(app.config, "CARD_PAYMENTS_ENABLED", True)
    txn = sale(till)
    r = complete(till, txn, "card")
    assert r.status_code == 409 and Payment.query.count() == 0
    inv = billing.create_invoice(total=100, transaction_id=txn)
    billing.record_payment(method="card", amount=100, receipt="FLWX1", invoice=inv)
    db.session.commit()
    assert complete(till, txn, "card").status_code == 200
    assert Payment.query.count() == 1


def test_terminal_card_is_recorded_when_online_cards_are_off(till):
    txn = sale(till)
    assert complete(till, txn, "card", card_ref="PDQ-778").status_code == 200
    assert [p.method for p in Payment.query] == ["card"]


def test_split_must_match_what_mpesa_brought_in(till):
    """#11: KES 250 M-Pesa received but the split says 200: refuse rather
    than quietly drop KES 50 of cash."""
    txn = sale(till, qty=3)
    inv = billing.create_invoice(total=300, transaction_id=txn)
    billing.record_payment(method="mpesa_c2b", amount=250, receipt="SJK0000040", invoice=inv)
    db.session.commit()
    r = complete(till, txn, "split", mpesa_amount=200, cash_amount=100)
    assert r.status_code == 409 and "250" in r.get_json()["error"]
    assert Payment.query.count() == 1
    assert complete(till, txn, "split", mpesa_amount=250, cash_amount=50).status_code == 200


def test_old_credit_sale_refund_credits_the_opening_balance(till):
    """#12: a credit sale from before invoices existed: its debt sits in the
    customer's opening-balance invoice, so returns must reduce that."""
    txn = sale(till, qty=3, customer=till["duka"])
    t = db.session.get(Transaction, txn)
    t.status, t.payment_method = "completed", "credit"
    opening = billing.create_invoice(total=1000, kind="opening_balance",
                                     customer_id=till["duka"])
    db.session.commit()
    opening_id = opening.id
    assert refund(till, txn, 1).status_code == 200
    assert db.session.get(Invoice, opening_id).balance == D("900.00")
    assert billing.customer_exposure(till["duka"]) == D("900.00")


def _threads(app, cookie, calls):
    results, barrier = [], threading.Barrier(len(calls))

    def worker(method, url, body):
        with app.app_context():
            client = app.test_client()
            client.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie)
            barrier.wait()
            results.append(client.open(url, method=method, json=body).status_code)

    threads = [threading.Thread(target=worker, args=c) for c in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(results)


@pytest.mark.skipif(not on_mysql(), reason="row locks need MySQL")
def test_double_tap_on_complete_makes_one_invoice(till):
    """#6: two 'complete' requests for one sale at the same moment."""
    app = till["app"]
    txn = sale(till)
    cookie = till["client"].get_cookie(app.config["SESSION_COOKIE_NAME"]).value
    url = f"/pos/transaction/{txn}/complete"
    results = _threads(app, cookie, [("POST", url, {"payment_method": "cash"})] * 2)
    assert results == [200, 400]
    with tenant_scope(app.acme_id):
        assert Invoice.query.count() == 1 and Payment.query.count() == 1


@pytest.mark.skipif(not on_mysql(), reason="row locks need MySQL")
def test_concurrent_assign_cannot_spend_a_payment_twice(till, login_as):
    """#5: one KES 100 payment assigned to two invoices at the same moment."""
    app = till["app"]
    a = billing.create_invoice(total=100)
    b = billing.create_invoice(total=100)
    p = billing.record_payment(method="mpesa_c2b", amount=100, receipt="SJK0000050",
                               bill_ref="NOPE")
    db.session.commit()
    ids = (a.id, b.id, p.id)
    owner = login_as("superuser")
    cookie = owner.get_cookie(app.config["SESSION_COOKIE_NAME"]).value
    _threads(app, cookie, [("POST", f"/payments/{ids[2]}/assign", {"invoice_id": ids[0]}),
                           ("POST", f"/payments/{ids[2]}/assign", {"invoice_id": ids[1]})])
    with tenant_scope(app.acme_id):
        from models import PaymentAllocation
        live = PaymentAllocation.query.filter(PaymentAllocation.voided_at.is_(None)).all()
        assert sum(x.amount for x in live) == D("100.00")
