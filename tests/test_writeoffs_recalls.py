"""Damaged, expired and recalled stock: batch write-offs, recalls, the
buyer trace and recall texts, the write-off report, the one low-stock rule
and the three stock pages."""
import csv
import io
from datetime import timedelta
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import (AuditLog, Batch, ControlledDrugEntry, Customer, InventoryItem, LineBatch,
                    SmsMessage, StockMovement, TransactionItem, User, db)
from services import controlled, recalls, stock
from services import orders as order_service
from services.clock import today_nairobi
from tests.conftest import set_vertical

D = Decimal
WITNESS = {"witness_name": "Dr Mwangi", "witness_reg_no": "PPB-9"}


def client_as(app, role, tenant_id=None):
    """A separate logged-in test client per role (one session each)."""
    tenant_id = tenant_id or app.acme_id
    email = f"{role}-{tenant_id}@stock.test"
    with tenant_scope(tenant_id):
        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User(email=email, name=f"{role.title()} Atieno", role=role,
                        status="approved", ppb_reg_no="PPB-0042")
            db.session.add(user)
            db.session.commit()
        user_id = user.id
    c = app.test_client()
    with c.session_transaction() as sess:
        sess.update(profile={"email": email, "name": role}, role=role, user_id=user_id,
                    tenant_id=tenant_id)
    c.user_id = user_id
    return c


def days(n):
    return today_nairobi() + timedelta(days=n)


def make_item(name, *batches, cost_price=None, **fields):
    """An item with batches given as (number, qty, expiry, unit_cost)."""
    item = InventoryItem(name=name, price=D("100"), cost_price=cost_price, **fields)
    db.session.add(item)
    db.session.flush()
    for number, qty, expiry, unit_cost in batches:
        stock.receive(item, qty, batch_number=number, expiry_date=expiry, unit_cost=unit_cost,
                      register=(controlled.Register(supplier_name="Dawa Ltd", reference_doc_no="DN-1")
                                if item.is_controlled else None))
    db.session.commit()
    return item.id


def batch_id(item_id, number):
    return Batch.query.filter_by(inventory_item_id=item_id, batch_number=number).one().id


def on_hand(item_id):
    return {b.batch_number: b.quantity_on_hand
            for b in Batch.query.filter_by(inventory_item_id=item_id).order_by(Batch.id)}


@pytest.fixture
def amoxil(app):
    """A: expires in 20 days (sold first), B: in 300 days."""
    return make_item("Amoxil 500mg", ("A", 10, days(20), D("40.00")),
                     ("B", 10, days(300), D("45.00")), cost_price=D("50"))


# --- write-offs in the stock service ------------------------------------------

def test_write_off_takes_from_the_chosen_batch_not_the_soonest(app, amoxil):
    item = db.session.get(InventoryItem, amoxil)
    summary = stock.write_off(item, 3, batch=db.session.get(Batch, batch_id(amoxil, "B")),
                              reason="damaged", note="dropped carton", user_id=None)
    db.session.commit()
    assert on_hand(amoxil) == {"A": 10, "B": 7}
    assert db.session.get(InventoryItem, amoxil).stock == 17
    assert (summary["quantity"], summary["unit_cost"], summary["cost"]) == (3, "45.00", "135.00")
    move = StockMovement.query.filter_by(item_id=amoxil, reason="write_off:damaged").one()
    assert move.delta == -3 and "batch B" in move.note and "KES 135.00" in move.note
    audit = AuditLog.query.filter_by(action="stock.write_off").one()
    assert audit.new_value["cost"] == "135.00" and audit.old_value == {"quantity_on_hand": 10}


def test_write_off_cost_falls_back_to_the_item_cost_price(app):
    item_id = make_item("Cetrizine", ("C1", 5, days(100), None), cost_price=D("12.50"))
    item = db.session.get(InventoryItem, item_id)
    summary = stock.write_off(item, 2, batch=batch_id(item_id, "C1"), reason="lost")
    assert summary["cost"] == "25.00"


def test_write_off_cannot_exceed_the_batch(app, amoxil):
    item = db.session.get(InventoryItem, amoxil)
    with pytest.raises(stock.StockError, match="has only 10"):
        stock.write_off(item, 11, batch=batch_id(amoxil, "A"), reason="damaged")
    db.session.rollback()
    assert on_hand(amoxil) == {"A": 10, "B": 10}


def test_expired_reason_only_for_an_expired_batch(app):
    item_id = make_item("Ventolin", ("OLD", 4, days(-3), D("10")), ("NEW", 4, days(30), D("10")))
    item = db.session.get(InventoryItem, item_id)
    with pytest.raises(stock.StockError, match="has not expired"):
        stock.write_off(item, 1, batch=batch_id(item_id, "NEW"), reason="expired")
    db.session.rollback()
    item = db.session.get(InventoryItem, item_id)
    stock.write_off(item, 4, batch=batch_id(item_id, "OLD"), reason="expired")
    db.session.commit()
    assert on_hand(item_id) == {"OLD": 0, "NEW": 4}


def test_unknown_reason_and_another_items_batch_are_refused(app, amoxil):
    other = make_item("Panadol", ("P1", 5, days(50), D("2")))
    item = db.session.get(InventoryItem, amoxil)
    with pytest.raises(stock.StockError, match="reason must be one of"):
        stock.write_off(item, 1, batch=batch_id(amoxil, "A"), reason="stolen-ish")
    with pytest.raises(stock.StockError, match="not Amoxil"):
        stock.write_off(item, 1, batch=batch_id(other, "P1"), reason="damaged")


def test_variant_items_cannot_be_written_off_by_batch(app, amoxil):
    shoes = InventoryItem(name="Crocs", price=D("900"), has_variants=True)
    db.session.add(shoes)
    db.session.commit()
    with pytest.raises(stock.StockError, match="sizes"):
        stock.write_off(shoes, 1, batch=batch_id(amoxil, "A"), reason="damaged")


def test_adjust_can_target_a_batch(app, amoxil):
    item = db.session.get(InventoryItem, amoxil)
    stock.adjust(item, -2, batch=batch_id(amoxil, "B"), reason="count")
    db.session.commit()
    assert on_hand(amoxil) == {"A": 10, "B": 8}


def test_controlled_write_off_is_one_witnessed_destruction_entry(app):
    item_id = make_item("Morphine 10mg", ("M1", 4, days(60), D("30")), ("M2", 10, days(200), D("30")),
                        is_controlled=True, drug_schedule="CD2", is_drug=True)
    item = db.session.get(InventoryItem, item_id)
    with pytest.raises(controlled.RegisterIncomplete, match="witness"):
        stock.write_off(item, 2, batch=batch_id(item_id, "M2"), reason="damaged", register=None)
    with pytest.raises(controlled.RegisterIncomplete, match="registration"):
        stock.write_off(item, 2, batch=batch_id(item_id, "M2"), reason="damaged",
                        register=controlled.Register(witness_name="Dr Mwangi"))
    db.session.rollback()
    before = ControlledDrugEntry.query.filter_by(inventory_item_id=item_id).count()
    item = db.session.get(InventoryItem, item_id)
    stock.write_off(item, 2, batch=batch_id(item_id, "M2"), reason="damaged",
                    register=controlled.Register(**WITNESS))
    db.session.commit()
    rows = (ControlledDrugEntry.query.filter_by(inventory_item_id=item_id)
            .order_by(ControlledDrugEntry.seq).all())
    assert len(rows) == before + 1
    last = rows[-1]
    assert (last.entry_type, last.batch_number, last.quantity_out, last.closing_balance,
            last.witness_name, last.witness_reg_no) == ("destruction", "M2", 2, 12, "Dr Mwangi", "PPB-9")
    # A count correction is an adjustment entry, still witnessed.
    item = db.session.get(InventoryItem, item_id)
    stock.write_off(item, 1, batch=batch_id(item_id, "M1"), reason="count_correction",
                    register=controlled.Register(**WITNESS))
    db.session.commit()
    last = (ControlledDrugEntry.query.filter_by(inventory_item_id=item_id)
            .order_by(ControlledDrugEntry.seq.desc()).first())
    assert (last.entry_type, last.batch_number, last.quantity_out) == ("adjustment", "M1", 1)


# --- the write-off API --------------------------------------------------------

def test_write_off_endpoint(app, amoxil):
    clerk = client_as(app, "stock_clerk")
    a = batch_id(amoxil, "A")
    r = clerk.post(f"/inventory/batches/{a}/write_off",
                   json={"quantity": 4, "reason": "damaged", "note": "water"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["write_off"]["cost"] == "160.00"
    assert r.get_json()["batch"]["quantity_on_hand"] == 6
    r = clerk.post(f"/inventory/batches/{a}/write_off", json={"quantity": 7, "reason": "damaged"})
    assert r.status_code == 400 and "has only 6" in r.get_json()["error"]
    r = clerk.post(f"/inventory/batches/{a}/write_off", json={"quantity": 1, "reason": "expired"})
    assert r.status_code == 400 and "not expired" in r.get_json()["error"]
    assert on_hand(amoxil) == {"A": 6, "B": 10}


def test_controlled_write_off_endpoint_needs_the_witness(app):
    item_id = make_item("Pethidine", ("X1", 5, days(-2), D("80")), is_controlled=True)
    pharmacist = client_as(app, "pharmacist")
    url = f"/inventory/batches/{batch_id(item_id, 'X1')}/write_off"
    r = pharmacist.post(url, json={"quantity": 5, "reason": "expired"})
    assert r.status_code == 400 and set(r.get_json()["register_missing"]) == {"witness_name",
                                                                              "witness_reg_no"}
    r = pharmacist.post(url, json={"quantity": 5, "reason": "expired", **WITNESS})
    assert r.status_code == 200, r.get_json()
    entry = (ControlledDrugEntry.query.filter_by(inventory_item_id=item_id, entry_type="destruction")
             .one())
    assert (entry.quantity_out, entry.witness_reg_no, entry.pharmacist_reg_no) == (5, "PPB-9", "PPB-0042")


def test_write_off_role_checks(app, amoxil):
    a = batch_id(amoxil, "A")
    cashier = client_as(app, "cashier")
    assert cashier.post(f"/inventory/batches/{a}/write_off",
                        json={"quantity": 1, "reason": "damaged"}).status_code == 403
    assert cashier.get("/inventory/batches").status_code == 403
    assert cashier.get("/inventory/write_offs").status_code == 403
    assert cashier.post(f"/inventory/batches/{a}/recall", json={"reason": "x"}).status_code == 403
    assert app.test_client().get("/inventory/batches").status_code == 401
    assert on_hand(amoxil) == {"A": 10, "B": 10}


def test_a_batch_of_another_business_is_not_found(app, amoxil, other_tenant):
    with tenant_scope(other_tenant):
        theirs = InventoryItem(name="Globex Amoxil", price=D("100"))
        db.session.add(theirs)
        db.session.flush()
        stock.receive(theirs, 5, batch_number="G1", expiry_date=days(100))
        db.session.commit()
        their_batch = Batch.query.filter_by(batch_number="G1").one().id
    admin = client_as(app, "admin")
    assert admin.post(f"/inventory/batches/{their_batch}/write_off",
                      json={"quantity": 1, "reason": "damaged"}).status_code == 404
    assert admin.post(f"/inventory/batches/{their_batch}/recall",
                      json={"reason": "x"}).status_code == 404
    assert admin.get(f"/inventory/batches/{their_batch}/buyers").status_code == 404
    assert admin.post(f"/inventory/batches/{their_batch}/recall/notify").status_code == 404
    assert their_batch not in [b["id"] for b in admin.get("/inventory/batches").get_json()["batches"]]
    with tenant_scope(other_tenant):
        assert Batch.query.filter_by(batch_number="G1").one().quantity_on_hand == 5


# --- batch list ---------------------------------------------------------------

def test_batch_list_statuses_and_values(app):
    item_id = make_item("Augmentin", ("EXP", 2, days(-1), D("10")), ("SOON", 3, days(10), D("10")),
                        ("LATER", 4, days(200), None), cost_price=D("20"))
    clerk = client_as(app, "stock_clerk")
    data = clerk.get(f"/inventory/batches?item_id={item_id}").get_json()
    by = {b["batch_number"]: b for b in data["batches"]}
    assert {k: v["status"] for k, v in by.items()} == {"EXP": "expired", "SOON": "expiring",
                                                       "LATER": "active"}
    assert (by["EXP"]["days_to_expiry"], by["SOON"]["days_to_expiry"]) == (-1, 10)
    assert by["LATER"]["value_at_cost"] == 80.0 and by["SOON"]["value_at_cost"] == 30.0
    assert data["value_at_cost"] == 130.0
    expired = clerk.get(f"/inventory/batches?status=expired&item_id={item_id}").get_json()
    assert [b["batch_number"] for b in expired["batches"]] == ["EXP"]
    active = clerk.get(f"/inventory/batches?status=active&item_id={item_id}").get_json()
    assert [b["batch_number"] for b in active["batches"]] == ["SOON", "LATER"]
    assert clerk.get("/inventory/batches?status=bogus").status_code == 400


# --- recalls ------------------------------------------------------------------

@pytest.fixture
def counter(app, monkeypatch, amoxil):
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    jane = Customer(name="Jane Wanjiku", phone_number="0722000111", code="JANE")
    otieno = Customer(name="Otieno Ouma", phone_number="0722000222", code="OTI")
    ken = Customer(name="Ken Kamau", phone_number="0722000333", code="KEN")
    db.session.add_all([jane, otieno, ken])
    db.session.commit()
    ids = {"jane": jane.id, "otieno": otieno.id, "ken": ken.id, "item": amoxil}
    cashier = client_as(app, "cashier")
    assert cashier.post("/pos/shift/open", json={"opening_float": 0}).status_code in (200, 201)
    return {"cashier": cashier, **ids}


def sell(counter, qty, customer=None):
    c = counter["cashier"]
    r = c.post("/pos/transaction/new", json={"customer_id": customer} if customer else {})
    txn = r.get_json()["transaction"]["id"]
    r = c.post(f"/pos/transaction/{txn}/add_item",
               json={"inventory_item_id": counter["item"], "quantity": qty})
    assert r.status_code == 201, r.get_json()
    r = c.post(f"/pos/transaction/{txn}/complete",
               json={"payment_method": "cash", "amount_tendered": 100000})
    return txn, r


def test_recall_holds_the_batch_and_sales_skip_it(app, counter):
    item = counter["item"]
    a = batch_id(item, "A")
    clerk = client_as(app, "stock_clerk")
    r = clerk.post(f"/inventory/batches/{a}/recall",
                   json={"reason": "PPB recall: wrong strength", "reference": "PPB/RC/77"})
    assert r.status_code == 200, r.get_json()
    batch = db.session.get(Batch, a)
    assert batch.quarantined and batch.recalled_at is not None
    assert (batch.recall_reason, batch.recall_ref) == ("PPB recall: wrong strength", "PPB/RC/77")
    assert AuditLog.query.filter_by(action="stock.recall", entity_id=a).count() == 1
    assert clerk.post(f"/inventory/batches/{a}/recall", json={"reason": "again"}).status_code == 400
    # A takes FEFO priority but is recalled: the sale comes from B.
    txn, r = sell(counter, 3)
    assert r.status_code == 200, r.get_json()
    line = TransactionItem.query.filter_by(transaction_id=txn).one()
    assert [(lb.batch_id, lb.quantity) for lb in LineBatch.query.filter_by(
        transaction_item_id=line.id)] == [(batch_id(item, "B"), 3)]
    assert on_hand(item) == {"A": 10, "B": 7}
    # Recalled stock cannot be received into either.
    with pytest.raises(stock.StockError, match="recalled"):
        stock.receive(db.session.get(InventoryItem, item), 5, batch_number="A")
    db.session.rollback()


def test_a_sale_fails_when_only_recalled_stock_is_left(app, counter):
    item = counter["item"]
    db.session.get(InventoryItem, item)
    stock.write_off(db.session.get(InventoryItem, item), 10, batch=batch_id(item, "B"),
                    reason="damaged")
    db.session.commit()
    clerk = client_as(app, "stock_clerk")
    assert clerk.post(f"/inventory/batches/{batch_id(item, 'A')}/recall",
                      json={"reason": "contamination"}).status_code == 200
    _, r = sell(counter, 1)
    assert r.status_code == 409 and "on hold" in r.get_json()["error"]
    assert on_hand(item) == {"A": 10, "B": 0}


def test_a_recalled_batch_is_never_released(app, amoxil):
    a = batch_id(amoxil, "A")
    admin = client_as(app, "admin")
    assert admin.post(f"/inventory/batches/{a}/recall", json={"reason": "recall"}).status_code == 200
    r = admin.put(f"/inventory/batches/{a}", json={"quarantined": False})
    assert r.status_code == 400 and "never released" in r.get_json()["error"]
    assert db.session.get(Batch, a).quarantined is True
    # Its stock leaves by write-off.
    r = admin.post(f"/inventory/batches/{a}/write_off",
                   json={"quantity": 10, "reason": "returned_to_supplier", "note": "CN-12"})
    assert r.status_code == 200
    assert on_hand(amoxil) == {"A": 0, "B": 10}


def test_buyers_are_net_of_returns_and_include_orders(app, counter):
    item = counter["item"]
    txn_jane, r = sell(counter, 3, counter["jane"])
    assert r.status_code == 200, r.get_json()
    _, r = sell(counter, 2, counter["otieno"])
    assert r.status_code == 200
    _, r = sell(counter, 1)                                   # walk-in
    assert r.status_code == 200
    line = TransactionItem.query.filter_by(transaction_id=txn_jane).one()
    r = counter["cashier"].post(f"/pos/transaction/{txn_jane}/refund",
                                json={"items": [{"transaction_item_id": line.id, "quantity": 1}]})
    assert r.status_code == 200, r.get_json()
    product = db.session.get(InventoryItem, item)
    order_service.create_order([order_service.Line(product, 2, D("100"))],
                               customer_id=counter["ken"], pay_now=True)
    db.session.commit()
    product = db.session.get(InventoryItem, item)
    cancelled, _ = order_service.create_order([order_service.Line(product, 1, D("100"))],
                                              customer_id=counter["ken"], pay_now=True)
    order_service.cancel(cancelled)
    db.session.commit()
    a = batch_id(item, "A")
    assert on_hand(item)["A"] == 10 - 3 - 2 - 1 + 1 - 2      # all FEFO from A
    stock.recall_batch(db.session.get(Batch, a), reason="recall")
    db.session.commit()
    clerk = client_as(app, "stock_clerk")
    data = clerk.get(f"/inventory/batches/{a}/buyers").get_json()
    got = sorted((b["source"], b["customer_name"], b["quantity"]) for b in data["buyers"])
    assert got == [("order", "Ken Kamau", 2), ("sale", "Jane Wanjiku", 2),
                   ("sale", "Otieno Ouma", 2), ("sale", "Walk-in", 1)]
    assert (data["units_out"], data["walk_in_units"], data["customers"],
            data["customers_with_phone"]) == (7, 1, 3, 3)
    assert {b["phone"] for b in data["buyers"] if b["customer_id"]} == \
        {"254722000111", "254722000222", "254722000333"}
    # Nothing from batch B went anywhere.
    assert recalls.recall_buyers(db.session.get(Batch, batch_id(item, "B"))) == []


def test_notify_texts_each_buyer_once(app, counter, sms_outbox):
    item = counter["item"]
    for customer in (counter["jane"], counter["jane"], counter["otieno"], None):
        assert sell(counter, 1, customer)[1].status_code == 200
    a = batch_id(item, "A")
    clerk = client_as(app, "stock_clerk")
    assert clerk.post(f"/inventory/batches/{a}/recall/notify").status_code == 403
    admin = client_as(app, "admin")
    r = admin.post(f"/inventory/batches/{a}/recall/notify")
    assert r.status_code == 400 and "not recalled" in r.get_json()["error"]
    assert clerk.post(f"/inventory/batches/{a}/recall", json={"reason": "recall"}).status_code == 200
    before = len(sms_outbox.sent)
    r = admin.post(f"/inventory/batches/{a}/recall/notify")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert (body["sent"], body["failed"], body["already_sent"], body["no_phone"],
            body["walk_in"]) == (2, 0, 0, 1, 1)
    texts = sms_outbox.sent[before:]
    assert sorted(t["to"][0] for t in texts) == ["+254722000111", "+254722000222"]
    message = texts[0]["message"]
    assert "Acme Pharmacy" in message and "Amoxil 500mg" in message and "batch A" in message
    assert "stop using" in message
    keys = {m.dedupe_key for m in SmsMessage.query.filter_by(kind="recall")}
    assert keys == {f"recall:{a}:{counter['jane']}", f"recall:{a}:{counter['otieno']}"}
    # A second press texts nobody.
    r = admin.post(f"/inventory/batches/{a}/recall/notify")
    assert (r.get_json()["sent"], r.get_json()["already_sent"]) == (0, 2)
    assert len(sms_outbox.sent) == before + 2
    assert AuditLog.query.filter_by(action="stock.recall_notify").count() == 2


def test_recalls_are_pharmacy_only(app, amoxil):
    set_vertical(app.acme_id, "distributor")
    pharmacist = client_as(app, "admin")
    a = batch_id(amoxil, "A")
    assert pharmacist.get("/stock-ui/recalls").status_code == 404
    assert pharmacist.get(f"/inventory/batches/{a}/buyers").status_code == 404
    assert pharmacist.post(f"/inventory/batches/{a}/recall", json={"reason": "x"}).status_code == 404
    # Batches and write-offs stay: distributors have batches too.
    assert pharmacist.get("/stock-ui/batches").status_code == 200
    assert pharmacist.get("/stock-ui/write-offs").status_code == 200
    assert db.session.get(Batch, a).recalled_at is None


# --- the write-off report -----------------------------------------------------

def test_write_off_report_totals_equal_the_rows(app, amoxil):
    other = make_item("ORS sachets", ("O1", 50, days(-5), D("7.50")))
    clerk = client_as(app, "stock_clerk")
    for bid, qty, reason in ((batch_id(amoxil, "A"), 2, "damaged"),
                             (batch_id(amoxil, "B"), 1, "damaged"),
                             (batch_id(other, "O1"), 20, "expired"),
                             (batch_id(amoxil, "B"), 3, "lost")):
        r = clerk.post(f"/inventory/batches/{bid}/write_off", json={"quantity": qty, "reason": reason})
        assert r.status_code == 200, r.get_json()
    data = clerk.get("/inventory/write_offs").get_json()
    rows = data["write_offs"]
    assert data["count"] == len(rows) == 4
    assert data["by_reason"]["damaged"] == {"quantity": 3, "cost": 125.0}     # 2x40 + 1x45
    assert data["by_reason"]["expired"] == {"quantity": 20, "cost": 150.0}
    assert data["by_reason"]["lost"] == {"quantity": 3, "cost": 135.0}
    assert data["by_reason"]["recalled"] == {"quantity": 0, "cost": 0.0}
    for reason, total in data["by_reason"].items():
        mine = [r for r in rows if r["reason"] == reason]
        assert total["quantity"] == sum(r["quantity"] for r in mine)
        assert D(str(total["cost"])) == sum((D(str(r["cost"])) for r in mine), D("0"))
    assert data["total_cost"] == 410.0 and data["total_quantity"] == 26
    month = today_nairobi().strftime("%Y-%m")
    assert data["by_month"][month]["damaged"]["cost"] == 125.0
    only_lost = clerk.get("/inventory/write_offs?reason=lost").get_json()
    assert only_lost["count"] == 1 and only_lost["total_cost"] == 135.0
    # Outside the window: nothing.
    old = clerk.get("/inventory/write_offs?from=2020-01-01&to=2020-01-31").get_json()
    assert old["count"] == 0 and old["total_cost"] == 0
    r = clerk.get("/inventory/write_offs?format=csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    lines = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
    assert lines[0][:6] == ["date", "item", "batch", "expiry", "quantity", "reason"]
    assert len(lines) == 5
    assert sum(D(line[7]) for line in lines[1:]) == D("410.00")
    assert clerk.get("/inventory/write_offs?reason=nope").status_code == 400
    assert clerk.get("/inventory/write_offs?from=2026-02-01&to=2026-01-01").status_code == 400


# --- low stock: one rule ------------------------------------------------------

def test_low_stock_endpoints_agree_on_stock_at_or_below_reorder_level(app):
    ids = {}
    for name, qty, level in (("Zero", 0, 5), ("Three", 3, 5), ("Five", 5, 5), ("Seven", 7, 5),
                             ("Eight", 8, 10), ("Four", 4, 2)):
        item = InventoryItem(name=name, price=D("10"), reorder_level=level)
        db.session.add(item)
        db.session.flush()
        if qty:
            stock.receive(item, qty)
        ids[name] = item.id
    db.session.commit()
    expected = {ids[n] for n in ("Zero", "Three", "Five", "Eight")}
    admin = client_as(app, "admin")
    low = admin.get("/inventory/low_stock").get_json()
    assert {i["id"] for i in low["items"]} == expected and low["count"] == 4
    assert low["rule"] == "stock <= reorder_level"
    assert {i["id"] for i in admin.get("/inventory/low_stock_reorder").get_json()} == expected
    assert {i["id"] for i in admin.get("/reports/low_stock").get_json()} == expected
    # The old ?threshold= no longer changes the answer.
    assert {i["id"] for i in admin.get("/inventory/low_stock?threshold=100").get_json()["items"]} \
        == expected
    assert client_as(app, "stock_clerk").get("/inventory/low_stock").status_code == 200


# --- pages --------------------------------------------------------------------

def test_the_three_pages_render_for_a_pharmacist(app, amoxil):
    pharmacist = client_as(app, "pharmacist")
    for url, marker in (("/stock-ui/batches", "Batches &amp; expiry"),
                        ("/stock-ui/recalls", "Text every customer"),
                        ("/stock-ui/write-offs", "Loss by month and reason")):
        r = pharmacist.get(url)
        assert r.status_code == 200, url
        assert marker in r.get_data(as_text=True), url
    nav = pharmacist.get("/dashboard").get_data(as_text=True)
    for url in ("/stock-ui/batches", "/stock-ui/recalls", "/stock-ui/write-offs"):
        assert url in nav


def test_a_cashier_is_sent_away_from_the_stock_pages(app):
    cashier = client_as(app, "cashier")
    for url in ("/stock-ui/batches", "/stock-ui/recalls", "/stock-ui/write-offs"):
        r = cashier.get(url)
        assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard"), url
