"""Pharmacy patients and their monthly medication history
(verticals/pharmacy/history.py, /pharmacy/patients*, /patients-ui*)."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import (Batch, Customer, InventoryItem, Invoice, LineBatch, Order, Prescription,
                    PrescriptionItem, Transaction, TransactionItem, User, db)
from services import stock
from tests.conftest import create_tenant, set_vertical
from verticals.pharmacy import history

D = Decimal
TODAY = date(2026, 11, 26)


@pytest.fixture
def ward(app):
    """One patient, two staff, one batched medicine, and a way to record a
    sale at a chosen UTC time."""
    pharmacist = User(email="achieng@ph.test", name="Pharm Achieng", role="pharmacist",
                      status="approved")
    cashier = User(email="otieno@ph.test", name="Cashier Otieno", role="cashier",
                   status="approved")
    item = InventoryItem(name="Amoxil 500mg", price=D("20"))
    patient = Customer(name="Wanjiku Kamau", phone_number="0722000777", code="WK7")
    db.session.add_all([pharmacist, cashier, item, patient])
    db.session.flush()
    stock.receive(item, 500, batch_number="AMX-1", expiry_date=date(2027, 3, 1))
    db.session.commit()
    batch = Batch.query.filter_by(batch_number="AMX-1").one()
    state = {"patient": patient.id, "item": item.id, "batch": batch.id,
             "pharmacist": pharmacist.id, "cashier": cashier.id, "n": 0}

    def sale(when, qty=10, status="completed", served_by=None, refunded=0,
             customer=None, discount_ratio=None):
        state["n"] += 1
        line_total = D(qty) * D("20")
        total = line_total * (discount_ratio or 1)
        txn = Transaction(transaction_ref=f"TXN-{state['n']:03d}",
                          customer_id=customer or state["patient"],
                          cashier_id=state["cashier"], served_by_id=served_by, status=status,
                          subtotal=line_total, total=total, created_at=when)
        db.session.add(txn)
        db.session.flush()
        line = TransactionItem(transaction_id=txn.id, inventory_item_id=state["item"],
                               item_name="Amoxil 500mg", quantity=D(qty), unit_price=D("20"),
                               line_total=line_total, refunded_quantity=D(refunded))
        db.session.add(line)
        db.session.flush()
        db.session.add(LineBatch(transaction_item_id=line.id, batch_id=state["batch"],
                                 quantity=qty, returned_quantity=refunded))
        db.session.commit()
        return txn.id

    state["sale"] = sale
    return state


def months_of(out):
    return [m["month"] for m in out["months"]]


# --- the service ------------------------------------------------------------

def test_sales_group_into_nairobi_months(app, ward):
    ward["sale"](datetime(2026, 10, 31, 22, 30))      # 01:30 on 1 November in Nairobi
    ward["sale"](datetime(2026, 10, 31, 20, 0))       # 23:00 on 31 October in Nairobi
    out = history.medication_history(ward["patient"], 12, today=TODAY)
    assert months_of(out) == ["2026-11", "2026-10"]
    nov, oct_ = out["months"]
    assert nov["label"] == "November 2026" and oct_["label"] == "October 2026"
    assert (nov["rows"][0]["date"], nov["rows"][0]["time"]) == ("2026-11-01", "01:30")
    assert (oct_["rows"][0]["date"], oct_["rows"][0]["time"]) == ("2026-10-31", "23:00")
    assert out["window"] == {"months": 12, "from": "2025-12-01", "to": "2026-11-30"}


def test_window_starts_at_nairobi_midnight(app, ward):
    ward["sale"](datetime(2025, 11, 30, 20, 59))      # 23:59 on 30 Nov 2025: outside
    ward["sale"](datetime(2025, 11, 30, 21, 0))       # 00:00 on 1 Dec 2025: inside
    out = history.medication_history(ward["patient"], 12, today=TODAY)
    assert [r["reference"] for m in out["months"] for r in m["rows"]] == ["TXN-002"]
    wider = history.medication_history(ward["patient"], 24, today=TODAY)
    assert len([r for m in wider["months"] for r in m["rows"]]) == 2


def test_voided_and_open_sales_are_left_out(app, ward):
    ward["sale"](datetime(2026, 11, 2, 8), status="voided")
    ward["sale"](datetime(2026, 11, 3, 8), status="open")
    kept = ward["sale"](datetime(2026, 11, 4, 8))
    out = history.medication_history(ward["patient"], 12, today=TODAY)
    rows = [r for m in out["months"] for r in m["rows"]]
    assert [r["reference"] for r in rows] == [db.session.get(Transaction, kept).transaction_ref]


def test_rows_carry_batch_expiry_and_who_served(app, ward):
    ward["sale"](datetime(2026, 11, 5, 8), served_by=ward["pharmacist"])
    ward["sale"](datetime(2026, 11, 6, 8))              # nobody named: the cashier
    out = history.medication_history(ward["patient"], 12, today=TODAY)
    newest, oldest = out["months"][0]["rows"]
    assert newest["served_by"] == "Cashier Otieno"
    assert oldest["served_by"] == "Pharm Achieng"
    assert oldest["batches"] == [{"batch_number": "AMX-1", "expiry_date": "2027-03-01",
                                  "quantity": 10}]
    assert (oldest["source"], oldest["source_label"], oldest["prescriber"]) == \
        ("counter", "Counter sale", None)


def test_refunds_are_shown_net_and_summed_per_month(app, ward):
    ward["sale"](datetime(2026, 11, 5, 8), qty=10, refunded=4)
    ward["sale"](datetime(2026, 11, 6, 8), qty=5, refunded=5, status="refunded")
    ward["sale"](datetime(2026, 11, 7, 8), qty=10, discount_ratio=D("0.5"))
    out = history.medication_history(ward["patient"], 12, today=TODAY)
    month = out["months"][0]
    by_ref = {r["reference"]: r for r in month["rows"]}
    part = by_ref["TXN-001"]
    assert (part["quantity"], part["quantity_sold"], part["refunded_quantity"],
            part["refunded"], part["amount"]) == (6.0, 10.0, 4.0, True, 120.0)
    full = by_ref["TXN-002"]
    assert (full["quantity"], full["refunded"], full["amount"]) == (0.0, True, 0.0)
    assert by_ref["TXN-003"]["amount"] == 100.0       # what was paid after the discount
    assert (month["items"], month["total_spent"]) == (2, 220.0)
    assert out["total_spent"] == 220.0


def test_months_must_be_between_1_and_36(app, ward):
    for bad in (0, 37, -1, "abc", None):
        with pytest.raises(history.HistoryError):
            history.medication_history(ward["patient"], bad, today=TODAY)
    assert history.medication_history(ward["patient"], 36, today=TODAY)["window"]["from"] \
        == "2023-12-01"
    assert history.medication_history(999999, 12) is None


def test_month_window_crosses_years():
    assert history.month_window(1, date(2026, 1, 15)) == (date(2026, 1, 1), date(2026, 2, 1))
    assert history.month_window(3, date(2026, 1, 15)) == (date(2025, 11, 1), date(2026, 2, 1))
    assert history.month_window(12, date(2026, 12, 31)) == (date(2026, 1, 1), date(2027, 1, 1))


# --- prescriptions and refills, through the real screens ----------------------

@pytest.fixture
def chronic(app, login_as, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    item = InventoryItem(name="Metformin 500mg", price=D("2"), is_drug=True)
    patient = Customer(name="Baraka Mwangi", phone_number="0733000111", code="BM1")
    db.session.add_all([item, patient])
    db.session.flush()
    stock.receive(item, 500, batch_number="MF1", expiry_date=date(2027, 12, 1))
    db.session.commit()
    pharmacist = login_as("pharmacist")
    r = pharmacist.post("/pharmacy/prescriptions", json={
        "customer_id": patient.id, "doctor_name": "Dr Otieno", "prescriber_reg_no": "K-1",
        "is_chronic": True, "duration_days": 30, "refills_remaining": 3,
        "items": [{"inventory_item_id": item.id, "quantity_prescribed": 60,
                   "dosage_instructions": "1 tablet twice a day"}]})
    assert r.status_code == 201, r.get_json()
    assert pharmacist.post("/pos/shift/open", json={"opening_float": 0}).status_code in (200, 201)
    return {"client": pharmacist, "rx": r.get_json()["prescription"]["id"], "item": item.id,
            "patient": patient.id}


def dispense_and_pay(chronic, customer=True):
    c = chronic["client"]
    body = {"customer_id": chronic["patient"]} if customer else {}
    txn = c.post("/pos/transaction/new", json=body).get_json()["transaction"]["id"]
    r = c.post(f"/pos/transaction/{txn}/add_item",
               json={"inventory_item_id": chronic["item"], "quantity": 60})
    assert r.status_code == 201, r.get_json()
    rx = c.get(f"/pharmacy/prescriptions/{chronic['rx']}").get_json()
    r = c.post(f"/pharmacy/prescriptions/{chronic['rx']}/dispense", json={
        "transaction_id": txn,
        "items": [{"prescription_item_id": rx["items"][0]["id"], "quantity_dispensed": 60}]})
    assert r.status_code == 200, r.get_json()
    r = c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "cash"})
    assert r.status_code == 200, r.get_json()
    return txn


def test_prescription_and_refill_rows(app, chronic, client):
    from services import billing
    first = dispense_and_pay(chronic, customer=False)     # sale has no customer: the Rx links it
    rx = db.session.get(Prescription, chronic["rx"])
    r = client.post(f"/o/acme/r/{rx.refill_token}/order", json={"fulfilment": "pickup"})
    assert r.status_code == 201, r.get_json()
    order = Order.query.filter_by(prescription_id=rx.id).one()
    order_id, order_number = order.id, order.order_number
    invoice = Invoice.query.filter_by(order_id=order_id).one()
    billing.record_payment(method="cash", amount=invoice.total, invoice=invoice)
    db.session.commit()
    pending = chronic["client"].get(f"/pharmacy/patients/{chronic['patient']}/medications")
    assert [r["source"] for m in pending.get_json()["months"] for r in m["rows"]] == \
        ["prescription"]                                   # not handed over yet
    assert chronic["client"].post(f"/pharmacy/orders/{order_id}/hand-over").status_code == 200
    second = dispense_and_pay(chronic)                     # the next refill, at the counter
    # Pin the times (the clock's resolution differs per database).
    db.session.get(Transaction, first).created_at = datetime(2026, 11, 10, 8)
    db.session.get(Order, order_id).picked_up_at = datetime(2026, 11, 15, 8)
    db.session.get(Transaction, second).created_at = datetime(2026, 11, 20, 8)
    db.session.commit()

    out = history.medication_history(chronic["patient"], 1, today=TODAY)
    rows = [r for m in out["months"] for r in m["rows"]]
    assert [r["source"] for r in rows] == ["prescription", "refill", "prescription"]
    refs = [r["reference"] for r in rows]
    assert refs[0] == db.session.get(Transaction, second).transaction_ref
    assert refs[2] == db.session.get(Transaction, first).transaction_ref   # earlier cycle
    for row in rows:
        assert (row["item_name"], row["quantity"]) == ("Metformin 500mg", 60.0)
        assert row["prescriber"] == "Dr Otieno"
        assert row["prescription_id"] == chronic["rx"]
        assert row["dosage"] == "1 tablet twice a day"
        assert row["batches"][0]["batch_number"] == "MF1"
        assert row["batches"][0]["expiry_date"] == "2027-12-01"
        assert row["served_by"] == "Test pharmacist"
    assert rows[1]["source_label"] == "Refill order" and rows[1]["reference"] == order_number
    assert out["months"][0]["items"] == 3 and out["months"][0]["total_spent"] == 360.0

    current = out["current_medicines"]
    assert len(current) == 1
    med = current[0]
    rx = db.session.get(Prescription, chronic["rx"])
    assert (med["prescription_id"], med["duration_days"], med["refills_remaining"],
            med["next_refill_due"]) == (rx.id, 30, 1, rx.refill_due_date.isoformat())
    assert med["items"] == [{"name": "Metformin 500mg", "dosage": "1 tablet twice a day",
                             "quantity": 60.0}]


def test_patient_list_shows_last_visit_chronic_and_next_refill(app, ward):
    ward["sale"](datetime(2026, 11, 20, 22, 0))            # 21 Nov in Nairobi
    rx = Prescription(customer_id=ward["patient"], doctor_name="Dr Njeri", is_chronic=True,
                      duration_days=30, refill_status="active",
                      refill_due_date=date(2026, 11, 24))
    paused = Prescription(customer_id=ward["patient"], is_chronic=True, duration_days=30,
                          refill_status="paused", refill_due_date=date(2026, 11, 1))
    db.session.add_all([rx, paused])
    db.session.flush()
    db.session.add(PrescriptionItem(prescription_id=rx.id, inventory_item_id=ward["item"],
                                    quantity_prescribed=D("30")))
    newcomer = Customer(name="Akinyi Odhiambo", phone_number="0711222333", code="AO1")
    db.session.add(newcomer)
    db.session.commit()
    rows = history.patient_list(today=TODAY)
    assert [r["name"] for r in rows] == ["Wanjiku Kamau", "Akinyi Odhiambo"]  # served first
    top = rows[0]
    assert (top["last_visit"], top["chronic_count"], top["next_refill_due"],
            top["days_to_refill"]) == ("2026-11-21", 1, "2026-11-24", -2)
    assert (rows[1]["last_visit"], rows[1]["chronic_count"]) == (None, 0)


@pytest.mark.parametrize("q", ["0722000777", "+254722000777", "254722000777", "0722 000",
                               "722000", "wanjiku", "KAMAU", "WK7"])
def test_search_by_phone_prefix_variants_name_and_code(app, ward, q):
    db.session.add(Customer(name="Someone Else", phone_number="0799111222", code="SE1"))
    db.session.commit()
    assert [r["id"] for r in history.patient_list(q)] == [ward["patient"]]


# --- the API ------------------------------------------------------------------

def test_api_lists_and_searches(app, ward, login_as):
    c = login_as("pharmacist")
    r = c.get("/pharmacy/patients?q=%2B2547220")
    assert r.status_code == 200
    assert [p["id"] for p in r.get_json()["patients"]] == [ward["patient"]]
    assert c.get("/pharmacy/patients?limit=x").status_code == 400


def test_api_months_bounds(app, ward, login_as):
    c = login_as("pharmacist")
    url = f"/pharmacy/patients/{ward['patient']}/medications"
    for bad in ("0", "37", "abc", "1.5"):
        r = c.get(f"{url}?months={bad}")
        assert r.status_code == 400 and "months" in r.get_json()["error"], bad
    for good in ("1", "36"):
        assert c.get(f"{url}?months={good}").status_code == 200
    assert c.get(url).get_json()["window"]["months"] == 12


@pytest.mark.parametrize("role", ["pharmacist", "admin", "superuser"])
def test_pharmacy_roles_may_read(app, ward, login_as, role):
    c = login_as(role)
    assert c.get("/pharmacy/patients").status_code == 200
    assert c.get(f"/pharmacy/patients/{ward['patient']}/medications").status_code == 200


@pytest.mark.parametrize("role", ["cashier", "stock_clerk", "accountant"])
def test_other_roles_are_refused(app, ward, login_as, role):
    c = login_as(role)
    assert c.get("/pharmacy/patients").status_code == 403
    assert c.get(f"/pharmacy/patients/{ward['patient']}/medications").status_code == 403
    page = c.get("/patients-ui")
    assert page.status_code == 302 and page.headers["Location"].endswith("/dashboard")
    assert c.get(f"/patients-ui/{ward['patient']}").status_code == 302


def test_logged_out_is_refused(app, ward, client):
    assert client.get("/pharmacy/patients").status_code == 401
    assert client.get("/patients-ui").status_code == 302


def test_only_the_pharmacy_vertical_has_patients(app, ward, login_as):
    c = login_as("pharmacist")
    set_vertical(app.acme_id, "distributor")
    assert c.get("/pharmacy/patients").status_code == 404
    assert c.get(f"/pharmacy/patients/{ward['patient']}/medications").status_code == 404
    assert c.get("/patients-ui").status_code == 404
    assert c.get(f"/patients-ui/{ward['patient']}").status_code == 404


def test_another_businesss_patient_is_not_found(app, ward, login_as):
    other = create_tenant("Beta Chemists", "beta", "pharmacy")
    with tenant_scope(other):
        theirs = Customer(name="Beta Patient", phone_number="0700999888", code="BP1")
        db.session.add(theirs)
        db.session.commit()
        their_id = theirs.id
    c = login_as("pharmacist")
    r = c.get(f"/pharmacy/patients/{their_id}/medications")
    assert r.status_code == 404 and r.get_json() == {"error": "Patient not found"}
    assert c.get(f"/patients-ui/{their_id}").status_code == 404
    assert "Beta Patient" not in c.get("/pharmacy/patients?q=Beta").get_data(as_text=True)
    assert c.get("/pharmacy/patients/999999/medications").status_code == 404


# --- the pages ------------------------------------------------------------------

def test_pages_render_for_a_pharmacist(app, ward, login_as):
    c = login_as("pharmacist")
    page = c.get("/patients-ui")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "/pharmacy/patients?q=" in html and "Search by name" in html

    page = c.get(f"/patients-ui/{ward['patient']}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Wanjiku Kamau" in html and "254722000777" in html
    assert "window.print()" in html and "@media print" in html and "nav.navbar" in html
    assert f"/pharmacy/patients/${{encodeURIComponent(patientId)}}/medications" in html
    assert "/pharmacy-ui" in html                       # quick link to prescriptions


def test_nav_and_dashboard_link_to_patients(app, login_as):
    html = login_as("pharmacist").get("/dashboard").get_data(as_text=True)
    assert 'href="/patients-ui"' in html


def test_a_shared_checkout_only_brings_in_the_prescribed_lines(app, chronic):
    """A walk-in sale holding this patient's prescription and someone else's
    cough syrup: only the prescribed medicine lands in this patient's history."""
    syrup = InventoryItem(name="Cough syrup", price=D("150"))
    db.session.add(syrup)
    db.session.flush()
    stock.receive(syrup, 5, batch_number="CS1", expiry_date=date(2027, 6, 1))
    db.session.commit()
    syrup_id = syrup.id
    c = chronic["client"]
    txn = c.post("/pos/transaction/new", json={}).get_json()["transaction"]["id"]
    c.post(f"/pos/transaction/{txn}/add_item", json={"inventory_item_id": chronic["item"],
                                                      "quantity": 60})
    c.post(f"/pos/transaction/{txn}/add_item", json={"inventory_item_id": syrup_id,
                                                      "quantity": 1})
    rx = c.get(f"/pharmacy/prescriptions/{chronic['rx']}").get_json()
    c.post(f"/pharmacy/prescriptions/{chronic['rx']}/dispense", json={
        "transaction_id": txn,
        "items": [{"prescription_item_id": rx["items"][0]["id"], "quantity_dispensed": 60}]})
    assert c.post(f"/pos/transaction/{txn}/complete",
                  json={"payment_method": "cash"}).status_code == 200
    out = history.medication_history(chronic["patient"], 12)
    names = [r["item_name"] for m in out["months"] for r in m["rows"]]
    assert names == ["Metformin 500mg"]
    assert out["months"][0]["total_spent"] == 120.0
