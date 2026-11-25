"""Pharmacy vertical: the controlled-drug register, controlled sales at the
counter, chronic refills, the refill link, the dashboard and the PDF."""
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import (Batch, ControlledDrugEntry, Customer, InventoryItem, Invoice, Order,
                    Prescription, RegisterIsAppendOnly, StockMovement, User, db)
from services import controlled, stock
from services import orders as order_service
from services.clock import today_nairobi
from tests.conftest import on_mysql

D = Decimal
RECEIPT = controlled.Register(supplier_name="Dawa Ltd", reference_doc_no="DN-1")
PATIENT = {"prescriber_name": "Dr Otieno", "prescriber_reg_no": "KMPDC-123",
           "patient_name": "Wanjiku Kamau", "patient_national_id": "12345678",
           "patient_address": "Kilimani, Nairobi"}


def entries(item_id):
    return (ControlledDrugEntry.query.filter_by(inventory_item_id=item_id)
            .order_by(ControlledDrugEntry.seq).all())


def chain_ok(rows):
    return all(b.seq == a.seq + 1 and b.opening_balance == a.closing_balance
               for a, b in zip(rows, rows[1:]))


@pytest.fixture
def morphine(app):
    item = InventoryItem(name="Morphine 10mg", price=D("50"), is_controlled=True,
                         drug_schedule="CD2", is_drug=True)
    db.session.add(item)
    db.session.flush()
    stock.receive(item, 4, batch_number="M1", expiry_date=date(2027, 1, 1), register=RECEIPT)
    stock.receive(item, 10, batch_number="M2", expiry_date=date(2027, 6, 1),
                  register=controlled.Register(supplier_name="Dawa Ltd", reference_doc_no="DN-2"))
    db.session.commit()
    return item.id


# --- the register ---------------------------------------------------------------

def test_controlled_stock_needs_its_register_entry(app, morphine):
    item = db.session.get(InventoryItem, morphine)
    with pytest.raises(controlled.RegisterIncomplete, match="delivery note"):
        stock.receive(item, 5, register=controlled.Register(supplier_name="X"))
    with pytest.raises(stock.StockError):                 # RegisterIncomplete is a StockError
        stock.take(item, 1)
    with pytest.raises(controlled.RegisterIncomplete, match="witness"):
        stock.adjust(item, -1)
    db.session.rollback()
    rows = entries(morphine)
    assert [(r.seq, r.entry_type, r.batch_number, r.quantity_in, r.opening_balance,
             r.closing_balance) for r in rows] == [(1, "receipt", "M1", 4, 0, 4),
                                                   (2, "receipt", "M2", 10, 4, 14)]


def test_register_rows_cannot_be_changed_or_deleted(app, morphine):
    row = entries(morphine)[0]
    row.quantity_in = 400
    with pytest.raises(RegisterIsAppendOnly):
        db.session.flush()
    db.session.rollback()
    db.session.delete(entries(morphine)[0])
    with pytest.raises(RegisterIsAppendOnly):
        db.session.flush()
    db.session.rollback()


def test_write_off_of_expired_stock_is_a_witnessed_destruction(app, morphine):
    item = db.session.get(InventoryItem, morphine)
    stock.adjust(item, -2, reason="expired_write_off",
                 register=controlled.Register(witness_name="Dr Mwangi", witness_reg_no="PPB-9"))
    last = entries(morphine)[-1]
    assert (last.entry_type, last.quantity_out, last.closing_balance, last.witness_reg_no) == \
        ("destruction", 2, 12, "PPB-9")


def test_marking_an_item_controlled_opens_its_register(app, login_as):
    item = InventoryItem(name="Diazepam 5mg", price=D("20"))
    db.session.add(item)
    db.session.flush()
    stock.receive(item, 6, batch_number="D1")
    stock.receive(item, 3, batch_number="D2")
    item_id = item.id
    db.session.commit()
    admin = login_as("admin")
    assert admin.put(f"/inventory/items/{item_id}", json={"is_controlled": True}).status_code == 200
    rows = entries(item_id)
    assert [(r.entry_type, r.batch_number, r.opening_balance, r.closing_balance) for r in rows] == \
        [("opening", "D1", 0, 6), ("opening", "D2", 6, 9)]
    r = admin.put(f"/inventory/items/{item_id}", json={"is_controlled": False})
    assert r.status_code == 409


def test_restock_and_po_receipts_need_the_document(app, morphine, login_as):
    admin = login_as("admin")
    r = admin.post(f"/inventory/items/{morphine}/restock", json={"quantity": 5})
    assert r.status_code == 400 and "delivery note" in r.get_json()["error"]
    r = admin.post(f"/inventory/items/{morphine}/restock",
                   json={"quantity": 5, "supplier_name": "Dawa Ltd", "reference_doc_no": "DN-3"})
    assert r.status_code == 200
    assert entries(morphine)[-1].reference_doc_no == "DN-3"


# --- selling at the counter -------------------------------------------------------

@pytest.fixture
def counter(app, morphine, login_as, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    patient = Customer(name="Wanjiku Kamau", phone_number="0722000555", code="WK")
    db.session.add(patient)
    db.session.commit()
    patient_id = patient.id
    pharmacist = login_as("pharmacist")
    with tenant_scope(app.acme_id):
        user = User.query.filter_by(role="pharmacist").one()
        user.ppb_reg_no = "PPB-0042"
        user.name = "Pharm. Achieng"
        db.session.commit()
        user_id = user.id
    pharmacist.post("/pos/shift/open", json={"opening_float": 0})
    return {"client": pharmacist, "item": morphine, "patient": patient_id, "user": user_id}


def new_sale(c, counter, qty):
    txn = c.post("/pos/transaction/new",
                 json={"customer_id": counter["patient"]}).get_json()["transaction"]["id"]
    r = c.post(f"/pos/transaction/{txn}/add_item",
               json={"inventory_item_id": counter["item"], "quantity": qty})
    assert r.status_code == 201, r.get_json()
    return txn


def complete(c, txn, **register):
    return c.post(f"/pos/transaction/{txn}/complete",
                  json={"payment_method": "cash", "amount_tendered": 5000, "register": register})


def test_cashier_cannot_sell_a_controlled_drug(app, counter, login_as):
    cashier = login_as("cashier", email="till@example.com")
    cashier.post("/pos/shift/open", json={"opening_float": 0})
    txn = cashier.post("/pos/transaction/new", json={}).get_json()["transaction"]["id"]
    r = cashier.post(f"/pos/transaction/{txn}/add_item",
                     json={"inventory_item_id": counter["item"], "quantity": 1})
    assert r.status_code == 403 and "pharmacist" in r.get_json()["error"]


def test_missing_register_details_refuse_the_sale_before_anything_is_written(app, counter):
    c = counter["client"]
    txn = new_sale(c, counter, 5)
    r = complete(c, txn, prescriber_name="Dr Otieno")
    assert r.status_code == 400
    assert set(r.get_json()["register_missing"]) == {"prescriber_reg_no", "patient_national_id",
                                                     "patient_address"}
    assert Invoice.query.count() == 0 and len(entries(counter["item"])) == 2
    assert StockMovement.query.filter_by(reason="sale").count() == 0


def test_controlled_sale_writes_one_row_per_batch_with_snapshots(app, counter):
    c = counter["client"]
    txn = new_sale(c, counter, 5)                       # 4 from M1, 1 from M2
    r = complete(c, txn, **PATIENT)
    assert r.status_code == 200, r.get_json()
    rows = [e for e in entries(counter["item"]) if e.entry_type == "dispense"]
    assert [(e.batch_number, e.quantity_out, e.opening_balance, e.closing_balance) for e in rows] == \
        [("M1", 4, 14, 10), ("M2", 1, 10, 9)]
    assert {(e.patient_national_id, e.prescriber_reg_no, e.pharmacist_reg_no, e.transaction_id)
            for e in rows} == {("12345678", "KMPDC-123", "PPB-0042", txn)}
    assert chain_ok(entries(counter["item"]))
    # The register keeps the pharmacist's number after the user record changes.
    with tenant_scope(app.acme_id):
        user = db.session.get(User, counter["user"])
        user.ppb_reg_no = "PPB-9999"
        db.session.commit()
    assert {e.pharmacist_reg_no for e in entries(counter["item"]) if e.entry_type == "dispense"} \
        == {"PPB-0042"}


def test_pharmacist_without_a_ppb_number_cannot_dispense(app, counter):
    with tenant_scope(app.acme_id):
        db.session.get(User, counter["user"]).ppb_reg_no = None
        db.session.commit()
    c = counter["client"]
    r = complete(c, new_sale(c, counter, 1), **PATIENT)
    assert r.status_code == 400 and "pharmacist_reg_no" in r.get_json()["register_missing"]


def test_void_writes_witnessed_returns_and_keeps_the_originals(app, counter):
    from services import otp_service
    c = counter["client"]
    txn = new_sale(c, counter, 5)
    complete(c, txn, **PATIENT)
    originals = [(e.id, e.quantity_out) for e in entries(counter["item"])
                 if e.entry_type == "dispense"]
    c.post(f"/pos/transaction/{txn}/void", json={"reason": "wrong patient"})
    otp = otp_service.peek_otp(f"void:txn:{txn}")
    r = c.post(f"/pos/transaction/{txn}/void", json={"reason": "wrong patient", "otp": otp})
    assert r.status_code == 400 and "witness" in r.get_json()["error"]
    c.post(f"/pos/transaction/{txn}/void", json={"reason": "wrong patient"})
    otp = otp_service.peek_otp(f"void:txn:{txn}")
    r = c.post(f"/pos/transaction/{txn}/void", json={"reason": "wrong patient", "otp": otp,
                                                     "witness_name": "Manager Njoroge"})
    assert r.status_code == 200, r.get_json()
    rows = entries(counter["item"])
    returns = [e for e in rows if e.entry_type == "return"]
    assert sorted((e.batch_number, e.quantity_in) for e in returns) == [("M1", 4), ("M2", 1)]
    assert {e.reverses_entry_id for e in returns} == {i for i, _ in originals}
    assert [(e.id, e.quantity_out) for e in rows if e.entry_type == "dispense"] == originals
    assert chain_ok(rows) and rows[-1].closing_balance == 14


def test_controlled_drugs_are_not_sold_through_remote_orders(app, morphine):
    item = db.session.get(InventoryItem, morphine)
    with pytest.raises(order_service.OrderError, match="controlled"):
        order_service.create_order([order_service.Line(item, 1, D("50"))], pay_now=True)


def test_register_pdf_and_json(app, counter):
    c = counter["client"]
    complete(c, new_sale(c, counter, 2), **PATIENT)
    body = c.get(f"/pharmacy/register?item_id={counter['item']}").get_json()
    assert len(body["entries"]) == 3 and body["breaks"] == []
    r = c.get("/pharmacy/register.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert r.data[:5] == b"%PDF-" and "attachment" in r.headers["Content-Disposition"]


@pytest.mark.skipif(not on_mysql(), reason="row locks need MySQL")
def test_concurrent_dispensing_keeps_the_register_gapless(app, morphine):
    register = controlled.Register(**PATIENT, pharmacist_name="P", pharmacist_reg_no="PPB-1")
    barrier, errors = threading.Barrier(8), []

    def worker():
        with app.app_context(), tenant_scope(app.acme_id):
            try:
                barrier.wait()
                stock.take(db.session.get(InventoryItem, morphine), 1, register=register)
                db.session.commit()
            except Exception as e:          # noqa: BLE001  report, then fail below
                errors.append(repr(e))
                db.session.rollback()
            finally:
                db.session.remove()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    db.session.expire_all()
    rows = entries(morphine)
    assert [r.seq for r in rows] == list(range(1, 11)) and chain_ok(rows)
    assert rows[-1].closing_balance == 6 == db.session.get(InventoryItem, morphine).stock


# --- chronic refills ----------------------------------------------------------------

@pytest.fixture
def chronic(app, login_as):
    item = InventoryItem(name="Metformin 500mg", price=D("120"), is_drug=True)
    patient = Customer(name="Wanjiku Kamau", phone_number="0722000666", code="WK2")
    db.session.add_all([item, patient])
    db.session.flush()
    stock.receive(item, 100, batch_number="MF1", expiry_date=date(2027, 12, 1))
    db.session.commit()
    pharmacist = login_as("pharmacist")
    r = pharmacist.post("/pharmacy/prescriptions", json={
        "customer_id": patient.id, "doctor_name": "Dr Otieno", "prescriber_reg_no": "K-1",
        "is_chronic": True, "duration_days": 30, "refills_remaining": 1,
        "items": [{"inventory_item_id": item.id, "quantity_prescribed": 60}]})
    assert r.status_code == 201, r.get_json()
    rx = r.get_json()["prescription"]
    pharmacist.post("/pos/shift/open", json={"opening_float": 0})
    return {"client": pharmacist, "rx": rx["id"], "item": item.id, "patient": patient.id}


def dispense_cycle(chronic):
    c = chronic["client"]
    txn = c.post("/pos/transaction/new",
                 json={"customer_id": chronic["patient"]}).get_json()["transaction"]["id"]
    c.post(f"/pos/transaction/{txn}/add_item",
           json={"inventory_item_id": chronic["item"], "quantity": 60})
    rx = c.get(f"/pharmacy/prescriptions/{chronic['rx']}").get_json()
    return c.post(f"/pharmacy/prescriptions/{chronic['rx']}/dispense", json={
        "transaction_id": txn,
        "items": [{"prescription_item_id": rx["items"][0]["id"], "quantity_dispensed": 60}]})


def test_refill_cycles_set_due_dates_and_run_out(app, chronic):
    assert dispense_cycle(chronic).status_code == 200
    rx = db.session.get(Prescription, chronic["rx"])
    assert rx.refill_due_date == today_nairobi() + timedelta(days=30)
    assert rx.refill_token and rx.refills_remaining == 1
    assert dispense_cycle(chronic).status_code == 200            # the one refill
    db.session.expire_all()
    rx = db.session.get(Prescription, chronic["rx"])
    assert (rx.refills_remaining, rx.refill_status, rx.refill_due_date) == (0, "ended", None)
    r = dispense_cycle(chronic)
    assert r.status_code == 400 and "refills" in r.get_json()["error"]


def test_refill_schedule_buckets(app, chronic):
    today = today_nairobi()
    for due, bucket in ((today - timedelta(days=2), "overdue"), (today, "today"),
                        (today + timedelta(days=4), "next_7_days")):
        db.session.get(Prescription, chronic["rx"]).refill_due_date = due
        db.session.commit()
        schedule = chronic["client"].get("/pharmacy/dashboard").get_json()["refills"]
        assert [r["id"] for r in schedule[bucket]] == [chronic["rx"]], bucket


def test_refill_link_orders_and_reuses_the_pending_order(app, chronic, client):
    dispense_cycle(chronic)
    rx = db.session.get(Prescription, chronic["rx"])
    url = f"/o/acme/r/{rx.refill_token}"
    html = client.get(url).get_data(as_text=True)
    assert "Wanjiku K." in html and "Kamau" not in html and "Metformin" in html
    assert "0722000666" not in html
    r = client.post(url + "/order", json={"fulfilment": "delivery"})
    assert r.status_code == 400                                   # needs an address
    r = client.post(url + "/order", json={"fulfilment": "delivery", "address": "Kilimani"})
    assert r.status_code == 201, r.get_json()
    pay_url = r.get_json()["pay_url"]
    order = Order.query.one()
    assert (order.prescription_id, order.delivery_address, order.channel) == \
        (rx.id, "Kilimani", "public_link")
    assert client.get(pay_url).status_code == 200
    again = client.post(url + "/order", json={"fulfilment": "pickup"})
    assert again.status_code == 200 and again.get_json()["pay_url"] == pay_url
    assert Order.query.count() == 1
    assert client.get(f"/o/acme/r/{rx.refill_token.upper()}x").status_code == 404


def test_refill_link_with_a_controlled_drug_asks_for_a_call(app, chronic, client):
    rx = db.session.get(Prescription, chronic["rx"])
    db.session.get(InventoryItem, chronic["item"]).is_controlled = True
    db.session.commit()
    url = f"/o/acme/r/{rx.refill_token}"
    html = client.get(url).get_data(as_text=True)
    assert "Ask the pharmacist to call me" in html and "Continue to pay" not in html
    assert client.post(url + "/order", json={}).status_code == 409
    assert client.post(url + "/callback").status_code == 200
    callbacks = chronic["client"].get("/pharmacy/dashboard").get_json()["callbacks"]
    assert [c["id"] for c in callbacks] == [rx.id]
    assert chronic["client"].put(f"/pharmacy/prescriptions/{rx.id}/refill",
                                 json={"callback_done": True}).status_code == 200
    assert chronic["client"].get("/pharmacy/dashboard").get_json()["callbacks"] == []


def test_expiry_radar_buckets(app, login_as):
    item = InventoryItem(name="Amoxil", price=D("10"))
    db.session.add(item)
    db.session.flush()
    today = today_nairobi()
    for number, days in (("GONE", -1), ("E20", 20), ("E45", 45), ("E80", 80), ("E200", 200)):
        stock.receive(item, 2, batch_number=number, expiry_date=today + timedelta(days=days))
    db.session.commit()
    radar = login_as("pharmacist").get("/pharmacy/dashboard").get_json()["expiry"]
    assert {k: [b["batch_number"] for b in v] for k, v in radar.items()} == \
        {"expired": ["GONE"], "30": ["E20"], "60": ["E45"], "90": ["E80"]}


def test_landing_page_shows_each_vertical_panel_to_staff(app, login_as, other_tenant):
    assert "Pharmacy today" in login_as("pharmacist").get("/dashboard").get_data(as_text=True)
    assert "Pharmacy today" not in login_as("cashier").get("/dashboard").get_data(as_text=True)
    page = login_as("admin", email="boss@globex.co", tenant_id=other_tenant).get("/dashboard")
    assert "Deni and collections" in page.get_data(as_text=True)
