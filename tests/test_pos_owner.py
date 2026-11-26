"""The till the pharmacy owner asked for: percentage discounts with a cap,
bank payments confirmed before the receipt, payment verification, who
served the customer, the patient picker and the printed receipt."""
from datetime import date
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import AuditLog, Customer, InventoryItem, Invoice, Payment, Transaction, User, db
from services import otp_service, stock

D = Decimal


def client_as(app, role, phone=None):
    """A separate logged-in test client per role (one session each)."""
    email = f"{role}@till.test"
    with tenant_scope(app.acme_id):
        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User(email=email, name=f"{role.title()} Wanjiru", role=role,
                        status="approved", phone_number=phone)
            db.session.add(user)
            db.session.commit()
        user_id = user.id
    c = app.test_client()
    with c.session_transaction() as sess:
        sess.update(profile={"email": email, "name": role}, role=role, user_id=user_id,
                    tenant_id=app.acme_id)
    c.user_id = user_id
    return c


@pytest.fixture
def till(app, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    item = InventoryItem(name="Amoxil 500mg", price=D("200"))
    other = InventoryItem(name="Panadol", price=D("100"))
    db.session.add_all([item, other])
    db.session.flush()
    stock.receive(item, 20, batch_number="AMX-7", expiry_date=date(2027, 3, 1))
    stock.receive(other, 20, batch_number="PND-1", expiry_date=date(2027, 9, 1))
    db.session.commit()
    ids = {"item": item.id, "other": other.id}
    admin = client_as(app, "admin", phone="0711000111")   # gets the manager code SMS
    cashier = client_as(app, "cashier")
    assert cashier.post("/pos/shift/open", json={"opening_float": 0}).status_code in (200, 201)
    return {"cashier": cashier, "admin": admin, "app": app, **ids}


def new_sale(c, **body):
    r = c.post("/pos/transaction/new", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["transaction"]["id"]


def add(c, txn, item, **body):
    return c.post(f"/pos/transaction/{txn}/add_item",
                  json={"inventory_item_id": item, "quantity": 1, **body})


# --- discounts -------------------------------------------------------------

def test_line_discount_by_percent(till):
    c = till["cashier"]
    txn = new_sale(c)
    r = add(c, txn, till["item"], discount_pct=10)
    assert r.status_code == 201
    line = r.get_json()["line"]
    assert (line["discount"], line["line_total"]) == (20.0, 180.0)
    assert AuditLog.query.filter_by(action="discount.line").count() == 1


def test_cashier_over_the_cap_needs_a_manager_code(till, sms_outbox):
    c = till["cashier"]
    txn = new_sale(c)
    r = add(c, txn, till["item"], discount_pct=15)
    assert r.status_code == 202 and r.get_json()["otp_required"]
    assert r.get_json()["max_discount_pct"] == 10.0
    assert db.session.get(Transaction, txn).items == []         # nothing written yet
    assert any("+254711000111" in s["to"] for s in sms_outbox.sent)
    assert add(c, txn, till["item"], discount_pct=15, otp="000000").status_code == 403
    code = otp_service.peek_otp(f"discount:txn:{txn}")
    r = add(c, txn, till["item"], discount_pct=15, otp=code)
    assert r.status_code == 201 and r.get_json()["line"]["discount"] == 30.0
    assert AuditLog.query.filter_by(action="discount.override").count() == 1
    # A code is good once.
    assert add(c, txn, till["other"], discount_pct=50, otp=code).status_code == 403


def test_admin_gives_any_discount(till):
    c = till["admin"]
    c.post("/pos/shift/open", json={"opening_float": 0})
    txn = new_sale(c)
    assert add(c, txn, till["item"], discount_pct=50).status_code == 201


def test_cap_counts_line_and_sale_discounts_together(till):
    c = till["cashier"]
    txn = new_sale(c)
    add(c, txn, till["item"], discount_pct=8)                        # 16 of 200
    r = c.post(f"/pos/transaction/{txn}/apply_discount", json={"type": "flat", "value": 10})
    assert r.status_code == 202                                       # 26 of 200 = 13%
    r = c.post(f"/pos/transaction/{txn}/apply_discount", json={"type": "flat", "value": 4})
    assert r.status_code == 200                                       # 20 of 200 = 10%
    assert r.get_json()["transaction"]["total"] == 180.0


def test_discount_an_existing_line_and_clear_sale_discount_on_remove(till):
    c = till["cashier"]
    txn = new_sale(c)
    line = add(c, txn, till["item"]).get_json()["line"]["id"]
    other = add(c, txn, till["other"]).get_json()["line"]["id"]
    r = c.post(f"/pos/transaction/{txn}/line/{line}/discount", json={"percent": 5})
    assert r.status_code == 200 and r.get_json()["line"]["line_total"] == 190.0
    assert c.post(f"/pos/transaction/{txn}/apply_discount",
                  json={"type": "flat", "value": 15}).status_code == 200
    # Removing an item would make the sale discount a bigger share: it is cleared.
    r = c.delete(f"/pos/transaction/{txn}/remove_item/{other}")
    body = r.get_json()
    assert body["discount_cleared"] and body["transaction"]["discount_amount"] == 0.0
    assert c.post(f"/pos/transaction/{txn}/line/{line}/discount",
                  json={"percent": 101}).status_code == 400


# --- bank payments ------------------------------------------------------------

def test_bank_sale_completes_only_after_the_reference_is_recorded(till):
    c = till["cashier"]
    txn = new_sale(c)
    add(c, txn, till["item"])
    r = c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "bank"})
    assert r.status_code == 409 and "not recorded" in r.get_json()["error"]
    r = c.post("/payments/bank", json={"transaction_id": txn, "bank_name": "Equity",
                                       "reference": "ft24 330x9", "amount": 200})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["payment"]["verified"] is False
    r = c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "bank"})
    assert r.status_code == 200, r.get_json()
    receipt = r.get_json()["receipt"]
    assert receipt["unverified"] is True
    assert receipt["payments"][0]["reference"] == "FT24330X9"


def test_a_bank_reference_is_used_once(till):
    c = till["cashier"]
    first, second = new_sale(c), None
    add(c, first, till["item"])
    assert c.post("/payments/bank", json={"transaction_id": first, "bank_name": "KCB",
                                          "reference": "REF12345", "amount": 200}).status_code == 201
    c.post(f"/pos/transaction/{first}/complete", json={"payment_method": "bank"})
    second = new_sale(c)
    add(c, second, till["item"])
    r = c.post("/payments/bank", json={"transaction_id": second, "bank_name": "KCB",
                                       "reference": "ref 12345", "amount": 200})
    assert r.status_code == 409 and "already recorded" in r.get_json()["error"]
    assert c.post("/payments/bank", json={"transaction_id": second, "bank_name": "",
                                          "reference": "X1234", "amount": 200}).status_code == 400


def _bank_sale(till, reference="FT0001"):
    c = till["cashier"]
    txn = new_sale(c)
    add(c, txn, till["item"])
    c.post("/payments/bank", json={"transaction_id": txn, "bank_name": "Equity",
                                   "reference": reference, "amount": 200})
    assert c.post(f"/pos/transaction/{txn}/complete",
                  json={"payment_method": "bank"}).status_code == 200
    return txn


def test_accountant_verifies_a_bank_payment(app, till):
    txn = _bank_sale(till)
    accountant = client_as(app, "accountant")
    assert till["cashier"].get("/payments/unverified").status_code == 403
    listing = accountant.get("/payments/unverified").get_json()
    assert listing["count"] == 1 and listing["total"] == 200.0
    row = listing["payments"][0]
    assert row["recorded_by_name"] == "Cashier Wanjiru" and row["invoices"]
    r = accountant.post(f"/payments/{row['id']}/verify")
    assert r.status_code == 200
    p = db.session.get(Payment, row["id"])
    assert p.verified and p.verified_by == accountant.user_id and p.verified_at
    assert accountant.post(f"/payments/{row['id']}/verify").status_code == 409
    assert accountant.get("/payments/unverified").get_json()["count"] == 0
    assert Invoice.query.filter_by(transaction_id=txn).one().status == "paid"


def test_rejecting_a_bank_payment_reopens_the_invoice(app, till):
    txn = _bank_sale(till, "FT0002")
    payment_id = Payment.query.filter_by(method="bank").one().id
    r = till["admin"].post(f"/payments/{payment_id}/reject", json={"reason": "not on statement"})
    assert r.status_code == 200
    db.session.expire_all()
    assert db.session.get(Payment, payment_id).status == "reversed"
    inv = Invoice.query.filter_by(transaction_id=txn).one()
    assert inv.status == "open" and inv.balance == D("200.00")
    assert AuditLog.query.filter_by(action="payment.reject").count() == 1


def test_cash_payments_are_not_in_the_verify_list(app, till):
    c = till["cashier"]
    txn = new_sale(c)
    add(c, txn, till["item"])
    c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "cash"})
    cash_id = Payment.query.one().id
    assert till["admin"].get("/payments/unverified").get_json()["count"] == 0
    assert till["admin"].post(f"/payments/{cash_id}/verify").status_code == 404


# --- who served, which patient ----------------------------------------------------

def test_served_by_and_customer_on_a_sale(app, till):
    c = till["cashier"]
    pharmacist = client_as(app, "pharmacist")
    staff = c.get("/pos/staff").get_json()
    assert {s["id"] for s in staff} >= {c.user_id, pharmacist.user_id}
    txn = new_sale(c, served_by_id=pharmacist.user_id)
    t = db.session.get(Transaction, txn)
    assert (t.cashier_id, t.served_by_id) == (c.user_id, pharmacist.user_id)
    assert c.post("/pos/transaction/new", json={"served_by_id": 99999}).status_code == 400

    created = c.post("/pos/customers", json={"name": "Achieng Otieno",
                                              "phone_number": "0722 123 456"})
    assert created.status_code == 201
    patient = created.get_json()
    assert patient["phone_number"] == "254722123456" and patient["code"].startswith("P")
    dup = c.post("/pos/customers", json={"name": "A. Otieno", "phone_number": "+254722123456"})
    assert dup.status_code == 409 and dup.get_json()["customer"]["id"] == patient["id"]
    assert [x["id"] for x in c.get("/pos/customers?q=0722123").get_json()] == [patient["id"]]
    assert [x["id"] for x in c.get("/pos/customers?q=achieng").get_json()] == [patient["id"]]

    r = c.patch(f"/pos/transaction/{txn}", json={"customer_id": patient["id"],
                                                 "served_by_id": c.user_id})
    assert r.status_code == 200
    t = db.session.get(Transaction, txn)
    db.session.refresh(t)
    assert (t.customer_id, t.served_by_id) == (patient["id"], c.user_id)


def test_printed_receipt_shows_batch_expiry_and_who_served(app, till):
    c = till["cashier"]
    pharmacist = client_as(app, "pharmacist")
    txn = new_sale(c, served_by_id=pharmacist.user_id)
    add(c, txn, till["item"])
    assert c.get(f"/pos-ui/receipt/{txn}").status_code == 404      # not paid yet
    c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "cash"})
    html = c.get(f"/pos-ui/receipt/{txn}?print=1").get_data(as_text=True)
    assert "Batch AMX-7" in html and "exp 2027-03-01" in html
    assert "Served by" in html and "Pharmacist Wanjiru" in html
    assert "Acme Pharmacy" in html and "window.print()" in html


def test_receipt_page_is_per_business(app, till, other_tenant):
    from tests.conftest import set_vertical  # noqa: F401
    c = till["cashier"]
    txn = new_sale(c)
    add(c, txn, till["item"])
    c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "cash"})
    with tenant_scope(other_tenant):
        outsider = User(email="o@globex.test", role="cashier", status="approved")
        db.session.add(outsider)
        db.session.commit()
        oid = outsider.id
    o = app.test_client()
    with o.session_transaction() as sess:
        sess.update(profile={"email": "o@globex.test"}, role="cashier", user_id=oid,
                    tenant_id=other_tenant)
    assert o.get(f"/pos-ui/receipt/{txn}").status_code == 404


# --- review fixes -----------------------------------------------------------------

def test_removing_padding_lines_cannot_keep_a_big_discount(till):
    """Discount 9% of a padded cart onto one line, remove the padding: the
    cap is checked again at checkout."""
    c = till["cashier"]
    txn = new_sale(c)
    for _ in range(4):
        add(c, txn, till["item"])                       # 800 of padding
    line = add(c, txn, till["other"]).get_json()["line"]["id"]
    assert c.post(f"/pos/transaction/{txn}/line/{line}/discount",
                  json={"amount": 80}).status_code == 200   # 80 of 900 = 8.9%
    for li in db.session.get(Transaction, txn).items:
        if li.id != line:
            c.delete(f"/pos/transaction/{txn}/remove_item/{li.id}")
    r = c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "cash"})
    assert r.status_code == 202 and r.get_json()["otp_required"]   # 80% now
    assert db.session.get(Transaction, txn).status == "open"
    code = otp_service.peek_otp(f"discount:txn:{txn}")
    r = c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "cash", "otp": code})
    assert r.status_code == 200, r.get_json()


def test_manager_approval_covers_the_sale_up_to_the_approved_percent(till):
    c = till["cashier"]
    txn = new_sale(c)
    add(c, txn, till["item"], discount_pct=15)                       # 202, needs a code
    code = otp_service.peek_otp(f"discount:txn:{txn}")
    assert add(c, txn, till["item"], discount_pct=15, otp=code).status_code == 201
    # Checkout at the approved 15% needs no second code.
    r = c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "cash"})
    assert r.status_code == 200, r.get_json()


def test_customer_cannot_be_cleared_once_payment_started(till):
    from services import billing
    c = till["cashier"]
    patient = c.post("/pos/customers", json={"name": "Juma", "phone_number": "0733000111"}
                     ).get_json()
    txn = new_sale(c, customer_id=patient["id"])
    add(c, txn, till["item"])
    billing.create_invoice(total=200, transaction_id=txn, customer_id=patient["id"])
    db.session.commit()
    assert c.patch(f"/pos/transaction/{txn}", json={"customer_id": None}).status_code == 409
    assert c.patch(f"/pos/transaction/{txn}",
                   json={"customer_id": patient["id"]}).status_code == 200   # unchanged is fine


def test_receipt_shows_only_this_sales_share_of_a_payment(till):
    from services import billing
    from services.receipt_service import build_receipt
    c = till["cashier"]
    first, second = new_sale(c), new_sale(c)
    add(c, first, till["item"])
    add(c, second, till["item"])
    inv1 = billing.create_invoice(total=200, transaction_id=first)
    inv2 = billing.create_invoice(total=200, transaction_id=second)
    db.session.flush()
    p = billing.record_payment(method="mpesa_stk", amount=400, receipt="SPLIT00001", invoice=inv1)
    billing.allocate(p, inv2)
    db.session.commit()
    assert c.post(f"/pos/transaction/{first}/complete",
                  json={"payment_method": "mpesa"}).status_code == 200
    assert [x["amount"] for x in build_receipt(first)["payments"]] == [200.0]


@pytest.mark.skipif(not __import__("tests.conftest", fromlist=["on_mysql"]).on_mysql(),
                    reason="row locks need MySQL")
def test_parallel_line_discounts_cannot_both_pass_the_cap(till):
    """Three KES 25 discounts on three KES 200 lines at once: all three would
    be 75 of 600 (12.5%, over the 10% cap). Locked, two pass and the third
    is asked for a manager code."""
    import threading
    app, c = till["app"], till["cashier"]
    txn = new_sale(c)
    lines = [add(c, txn, till["item"]).get_json()["line"]["id"] for _ in range(3)]  # 600
    cookie = c.get_cookie(app.config["SESSION_COOKIE_NAME"]).value
    results, barrier = [], threading.Barrier(3)

    def worker(line):
        with app.app_context():
            client = app.test_client()
            client.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie)
            barrier.wait()
            results.append(client.post(f"/pos/transaction/{txn}/line/{line}/discount",
                                       json={"amount": 25}).status_code)   # 75 of 600 = 12.5%

    threads = [threading.Thread(target=worker, args=(line,)) for line in lines]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 200, 202]
    with tenant_scope(app.acme_id):
        t = db.session.get(Transaction, txn)
        assert sum(li.discount for li in t.items) == D("50.00")


def _shift_numbers(app, client):
    shift = client.get("/pos/shift/current").get_json()["shift"]
    from models import Shift
    from services.reports.reconciliation import shift_report
    return shift_report(db.session.get(Shift, shift["id"]))


def test_partial_cash_refund_leaves_the_drawer_balanced(app, till):
    """KES 400 sale, KES 200 handed back: expected cash is 200, no false shortage."""
    c = till["cashier"]
    txn = new_sale(c)
    a = add(c, txn, till["item"]).get_json()["line"]["id"]
    add(c, txn, till["item"])
    assert c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "cash"}).status_code == 200
    r = c.post(f"/pos/transaction/{txn}/refund",
               json={"items": [{"transaction_item_id": a, "quantity": 1}], "reason": "wrong item"})
    assert r.status_code == 200, r.get_json()
    report = _shift_numbers(app, c)
    assert report["cash_sales"] == 400.0 and report["cash_out"] == 200.0
    assert report["expected_cash"] == 200.0


def test_full_cash_refund_and_void_hand_cash_out_of_the_drawer(app, till):
    c = till["cashier"]
    first, second = new_sale(c), new_sale(c)
    for txn in (first, second):
        line = add(c, txn, till["item"]).get_json()["line"]["id"]
        c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "cash"})
    line = db.session.get(Transaction, first).items[0].id
    c.post(f"/pos/transaction/{first}/refund",
           json={"items": [{"transaction_item_id": line, "quantity": 1}], "reason": "x"})
    r = c.post(f"/pos/transaction/{second}/void", json={"reason": "mistake"})
    code = otp_service.peek_otp(f"void:txn:{second}")
    assert c.post(f"/pos/transaction/{second}/void",
                  json={"reason": "mistake", "otp": code}).status_code == 200
    report = _shift_numbers(app, c)
    assert (report["cash_sales"], report["cash_out"], report["expected_cash"]) == (400.0, 400.0, 0.0)
