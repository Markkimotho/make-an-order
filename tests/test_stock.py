"""services/stock.py: batches, FEFO, line batches, variants, and the routes
that change stock through it."""
import random
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import (Batch, Customer, InventoryItem, ItemVariant, LineBatch, OrderItem,
                    StockMovement, Supplier, TransactionItem, db)
from services import stock
from services.clock import today_nairobi
from tests.conftest import on_mysql

D = Decimal
TODAY = date(2026, 11, 24)


def make_item(name="Amoxil 500", **fields):
    item = InventoryItem(name=name, price=D("100"), **fields)
    db.session.add(item)
    db.session.flush()
    return item


def batches_of(item):
    """{batch_number: quantity} for an item or item id (ids survive requests,
    which empty the session)."""
    item_id = item if isinstance(item, int) else item.id
    rows = Batch.query.filter_by(inventory_item_id=item_id).order_by(Batch.id).all()
    return {b.batch_number: b.quantity_on_hand for b in rows}


def assert_invariant(item):
    if isinstance(item, int):
        item = db.session.get(InventoryItem, item)
    else:
        db.session.refresh(item)
    model, col = ((ItemVariant, ItemVariant.stock) if item.has_variants
                  else (Batch, Batch.quantity_on_hand))
    total = db.session.query(db.func.coalesce(db.func.sum(col), 0)).filter(
        model.inventory_item_id == item.id).scalar()
    assert item.stock == int(total)


def line(item, qty):
    """A sale line the stock service can record batches against."""
    from models import Shift, Transaction, User
    user = User.query.first() or User(email="c@x.co", name="c", role="cashier", status="approved")
    db.session.add(user)
    db.session.flush()
    shift = Shift(cashier_id=user.id, opening_float=D("0"))
    db.session.add(shift)
    db.session.flush()
    txn = Transaction(transaction_ref=f"T{random.randrange(10**9)}", cashier_id=user.id,
                      shift_id=shift.id, total=D("0"))
    db.session.add(txn)
    db.session.flush()
    li = TransactionItem(transaction_id=txn.id, inventory_item_id=item.id, item_name=item.name,
                         quantity=D(qty), unit_price=D("100"), line_total=D("100") * qty)
    db.session.add(li)
    db.session.flush()
    return li


# --- clock -----------------------------------------------------------------

def test_today_is_nairobi_not_utc():
    assert today_nairobi(datetime(2026, 11, 24, 20, 59)) == date(2026, 11, 24)
    assert today_nairobi(datetime(2026, 11, 24, 21, 0)) == date(2026, 11, 25)   # 00:00 EAT


# --- receive -----------------------------------------------------------------

def test_receive_into_default_named_and_existing_batches(app):
    item = make_item()
    stock.receive(item, 5)
    stock.receive(item, 10, batch_number="AMX-1", expiry_date=date(2027, 1, 1), unit_cost=D("12"))
    stock.receive(item, 3, batch_number="AMX-1")
    assert batches_of(item) == {"UNBATCHED": 5, "AMX-1": 13}
    assert_invariant(item)
    assert [m.delta for m in StockMovement.query.filter_by(item_id=item.id)] == [5, 10, 3]


def test_receive_refuses_a_second_expiry_for_one_batch(app):
    item = make_item()
    stock.receive(item, 5, batch_number="AMX-1", expiry_date=date(2027, 1, 1))
    with pytest.raises(stock.StockError, match="already has expiry"):
        stock.receive(item, 5, batch_number="AMX-1", expiry_date=date(2027, 6, 1))


@pytest.mark.parametrize("qty", [0, -3])
def test_quantities_must_be_positive(app, qty):
    item = make_item()
    with pytest.raises(stock.StockError):
        stock.receive(item, qty)
    with pytest.raises(stock.StockError):
        stock.take(item, qty)


# --- take: FEFO ----------------------------------------------------------------

def test_take_soonest_expiry_first_and_undated_last(app):
    item = make_item()
    stock.receive(item, 5, batch_number="LATE", expiry_date=date(2027, 6, 1))
    stock.receive(item, 5, batch_number="UNDATED")
    stock.receive(item, 5, batch_number="SOON", expiry_date=date(2027, 1, 1))
    taken = stock.take(item, 8, today=TODAY)
    assert [(b.batch_number, n) for b, n in taken] == [("SOON", 5), ("LATE", 3)]
    assert batches_of(item) == {"LATE": 2, "UNDATED": 5, "SOON": 0}
    assert stock.batch_label(taken) == "SOON x5, LATE x3"
    assert_invariant(item)


def test_expired_batches_are_never_sold(app):
    item = make_item()
    stock.receive(item, 6, batch_number="OLD", expiry_date=TODAY - timedelta(days=1))
    stock.receive(item, 4, batch_number="NEW", expiry_date=TODAY + timedelta(days=30))
    with pytest.raises(stock.InsufficientStock) as e:
        stock.take(item, 5, today=TODAY)
    assert (e.value.sellable, e.value.expired) == (4, 6)
    assert "6 more expired" in str(e.value)
    assert batches_of(item) == {"OLD": 6, "NEW": 4}        # nothing moved
    taken = stock.take(item, 4, today=TODAY)
    assert [(b.batch_number, n) for b, n in taken] == [("NEW", 4)]


def test_expiring_today_is_still_sellable(app):
    item = make_item()
    stock.receive(item, 2, batch_number="EDGE", expiry_date=TODAY)
    assert stock.take(item, 2, today=TODAY)[0][1] == 2


def test_negative_adjustment_writes_off_expired_stock(app):
    item = make_item()
    stock.receive(item, 6, batch_number="OLD", expiry_date=date(2020, 1, 1))
    stock.adjust(item, -6, reason="expired_write_off")
    assert batches_of(item) == {"OLD": 0} and item.stock == 0


def test_oversell_raises_and_changes_nothing(app):
    item = make_item()
    stock.receive(item, 3)
    with pytest.raises(stock.InsufficientStock):
        stock.take(item, 4)
    assert batches_of(item) == {"UNBATCHED": 3}
    assert LineBatch.query.count() == 0


# --- line batches, voids and returns --------------------------------------------

def test_multi_batch_sale_records_each_batch_and_give_back_restores_exactly(app):
    item = make_item()
    stock.receive(item, 4, batch_number="A", expiry_date=date(2027, 1, 1))
    stock.receive(item, 10, batch_number="B", expiry_date=date(2027, 2, 1))
    li = line(item, 10)
    stock.take(item, 10, transaction_item=li, today=TODAY)
    rows = LineBatch.query.filter_by(transaction_item_id=li.id).order_by(LineBatch.id).all()
    assert [(r.batch_id, r.quantity) for r in rows] == [
        (Batch.query.filter_by(batch_number="A").one().id, 4),
        (Batch.query.filter_by(batch_number="B").one().id, 6)]
    stock.give_back(item, 10, transaction_item=li, reason="cancel")
    assert batches_of(item) == {"A": 4, "B": 10}
    assert [r.returned_quantity for r in rows] == [4, 6]
    assert_invariant(item)


def test_partial_returns_go_back_to_the_batch_last_taken_and_never_twice(app):
    item = make_item()
    stock.receive(item, 4, batch_number="A", expiry_date=date(2027, 1, 1))
    stock.receive(item, 10, batch_number="B", expiry_date=date(2027, 2, 1))
    li = line(item, 10)
    stock.take(item, 10, transaction_item=li, today=TODAY)
    stock.give_back(item, 3, transaction_item=li)
    assert batches_of(item) == {"A": 0, "B": 7}
    stock.give_back(item, 5, transaction_item=li)           # 3 more from B, then 2 into A
    assert batches_of(item) == {"A": 2, "B": 10}
    stock.give_back(item, 2, transaction_item=li)
    assert batches_of(item) == {"A": 4, "B": 10}
    # Beyond what the line took: lands in the default batch, not in A or B.
    stock.give_back(item, 1, transaction_item=li)
    assert batches_of(item) == {"A": 4, "B": 10, "UNBATCHED": 1}
    assert_invariant(item)


def test_order_lines_record_batches_too(app):
    item = make_item()
    stock.receive(item, 5, batch_number="A")
    cust = Customer(name="Duka", phone_number="0712000444", code="D1")
    db.session.add(cust)
    db.session.flush()
    from models import Order
    order = Order(customer_id=cust.id, item="x", amount=D("200"))
    db.session.add(order)
    db.session.flush()
    oi = OrderItem(order_id=order.id, inventory_item_id=item.id, item_name="x", quantity=2,
                   unit_price=D("100"), subtotal=D("200"))
    db.session.add(oi)
    db.session.flush()
    stock.take(item, 2, order_item=oi)
    assert LineBatch.query.filter_by(order_item_id=oi.id).one().quantity == 2
    stock.give_back(item, 2, order_item=oi)
    assert batches_of(item) == {"A": 5}


# --- variants --------------------------------------------------------------------

def test_variant_items_keep_stock_per_size(app):
    item = make_item("School shirt", has_variants=True)
    small, large = ItemVariant(inventory_item_id=item.id, label="Size 28"), \
        ItemVariant(inventory_item_id=item.id, label="Size 32")
    db.session.add_all([small, large])
    db.session.flush()
    stock.receive(item, 5, variant=small)
    stock.receive(item, 3, variant=large)
    stock.take(item, 2, variant=small)
    assert (small.stock, large.stock, item.stock) == (3, 3, 6)
    assert Batch.query.count() == 0
    with pytest.raises(stock.StockError, match="choose one"):
        stock.take(item, 1)
    with pytest.raises(stock.InsufficientStock):
        stock.take(item, 4, variant=large)
    other = make_item("Tie", has_variants=True)
    with pytest.raises(stock.StockError, match="another item"):
        stock.receive(other, 1, variant=small)
    assert_invariant(item)


# --- invariant under a random sequence ---------------------------------------------

def test_stock_equals_sum_of_batches_after_random_operations(app):
    rng = random.Random(20261124)
    item = make_item()
    lines = []
    for step in range(200):
        op = rng.choice(["receive", "take", "give_back", "adjust"])
        try:
            if op == "receive":
                stock.receive(item, rng.randint(1, 9),
                              batch_number=rng.choice(["A", "B", "C", None]))
            elif op == "take":
                li = line(item, 1) if rng.random() < 0.5 else None
                stock.take(item, rng.randint(1, 6), transaction_item=li, today=TODAY)
                if li is not None:
                    lines.append(li)
            elif op == "give_back" and lines:
                stock.give_back(item, rng.randint(1, 4), transaction_item=rng.choice(lines))
            elif op == "adjust":
                stock.adjust(item, rng.choice([-3, -1, 2, 5]))
        except stock.StockError:
            pass
        assert_invariant(item)
        assert all(q >= 0 for q in batches_of(item).values()), step
    for lb in LineBatch.query:
        assert 0 <= lb.returned_quantity <= lb.quantity


# --- routes ---------------------------------------------------------------------

@pytest.fixture
def admin(login_as):
    return login_as("admin")


def test_create_item_with_stock_makes_its_first_batch(app, admin):
    r = admin.post("/inventory/items", json={"name": "Flagyl", "price": 50, "stock": 12, "category": "Antibiotics",
                                              "batch_number": "FL-9", "expiry_date": "2027-04-01",
                                              "cost_price": 30})
    assert r.status_code == 201, r.get_json()
    item = InventoryItem.query.filter_by(name="Flagyl").one()
    b = Batch.query.filter_by(inventory_item_id=item.id).one()
    assert (b.batch_number, b.expiry_date, b.quantity_on_hand, float(b.unit_cost), item.stock) == \
        ("FL-9", date(2027, 4, 1), 12, 30.0, 12)
    assert (item.batch_number, item.expiry_date) == ("FL-9", date(2027, 4, 1))


def test_restock_adds_a_named_batch(app, admin):
    item = make_item().id
    db.session.commit()
    r = admin.post(f"/inventory/items/{item}/restock",
                   json={"quantity": 7, "batch_number": "R-1", "expiry_date": "2027-08-01"})
    assert r.status_code == 200 and r.get_json()["new_stock"] == 7
    assert batches_of(item) == {"R-1": 7}


def test_item_edit_sets_expiry_on_a_single_batch_and_refuses_with_several(app, admin):
    item = make_item()
    stock.receive(item, 5, batch_number="ONLY")
    item_id = item.id
    db.session.commit()
    r = admin.put(f"/inventory/items/{item_id}", json={"expiry_date": "2027-02-02"})
    assert r.status_code == 200
    assert Batch.query.filter_by(batch_number="ONLY").one().expiry_date == date(2027, 2, 2)
    stock.receive(db.session.get(InventoryItem, item_id), 5, batch_number="TWO")
    db.session.commit()
    r = admin.put(f"/inventory/items/{item_id}", json={"expiry_date": "2027-03-03"})
    assert r.status_code == 409


def test_batch_routes(app, admin):
    item = make_item()
    stock.receive(item, 5, batch_number="X1", expiry_date=date(2027, 5, 1))
    stock.receive(item, 5, batch_number="X2")
    item_id = item.id
    db.session.commit()
    rows = admin.get(f"/inventory/items/{item_id}/batches").get_json()
    assert [b["batch_number"] for b in rows] == ["X1", "X2"]
    x2 = rows[1]["id"]
    r = admin.put(f"/inventory/batches/{x2}", json={"batch_number": "X1"})
    assert r.status_code == 400                                  # duplicate number
    r = admin.put(f"/inventory/batches/{x2}", json={"expiry_date": "2027-09-09",
                                                    "quantity_on_hand": 999})
    assert r.status_code == 200 and r.get_json()["batch"]["quantity_on_hand"] == 5
    assert admin.put("/inventory/batches/999999", json={}).status_code == 404


def test_stock_adjustment_route(app, admin):
    item = make_item()
    stock.receive(item, 3)
    item_id = item.id
    db.session.commit()
    r = admin.post("/inventory/stock_adjustment",
                   json={"inventory_item_id": item_id, "delta": -5, "reason": "shrinkage"})
    assert r.status_code == 400 and batches_of(item_id) == {"UNBATCHED": 3}
    r = admin.post("/inventory/stock_adjustment",
                   json={"inventory_item_id": item_id, "delta": -2, "reason": "shrinkage"})
    assert r.status_code == 200 and r.get_json()["item"]["stock"] == 1


def test_expiring_lists_batches_with_days_left(app, admin):
    item = make_item()
    today = today_nairobi()
    stock.receive(item, 4, batch_number="GONE", expiry_date=today - timedelta(days=2))
    stock.receive(item, 6, batch_number="SOON", expiry_date=today + timedelta(days=10))
    stock.receive(item, 9, batch_number="LATER", expiry_date=today + timedelta(days=200))
    db.session.commit()
    rows = admin.get("/inventory/expiring?days=30").get_json()
    assert [(r["batch_number"], r["quantity_on_hand"], r["days_to_expiry"], r["expired"])
            for r in rows] == [("GONE", 4, -2, True), ("SOON", 6, 10, False)]
    report = admin.get("/reports/expiry?days=30").get_json()
    assert [i["batch_number"] for i in report["items"]] == ["GONE", "SOON"]


def test_purchase_order_receipt_is_a_batch_per_delivery(app, admin):
    item = make_item()
    sup = Supplier(name="Dawa Ltd", phone="0712000555")
    db.session.add(sup)
    db.session.flush()
    item, sup_id = item.id, sup.id
    db.session.commit()
    r = admin.post("/inventory/purchase_orders", json={
        "supplier_id": sup_id, "expected_delivery_date": "2026-12-01",
        "items": [{"inventory_item_id": item, "quantity_ordered": 10, "unit_cost": 25}]})
    assert r.status_code == 201, r.get_json()
    po = r.get_json()["purchase_order"]
    assert (po["total"], po["expected_delivery_date"], po["supplier_name"]) == \
        (250.0, "2026-12-01", "Dawa Ltd")
    pi = po["items"][0]["id"]
    admin.put(f"/inventory/purchase_orders/{po['id']}/status", json={"status": "sent"})
    url = f"/inventory/purchase_orders/{po['id']}/receive"
    assert admin.post(url, json={"items": [{"po_item_id": pi, "quantity_received": 4,
                                            "batch_number": "D1",
                                            "expiry_date": "2027-01-01"}]}).status_code == 200
    assert admin.post(url, json={"items": [{"po_item_id": pi,
                                            "quantity_received": 6}]}).status_code == 200
    assert batches_of(item) == {"D1": 4, f"PO-{po['po_number']}": 6}
    unit_costs = {float(b.unit_cost) for b in Batch.query.filter_by(inventory_item_id=item)}
    assert unit_costs == {25.0}
    assert db.session.get(InventoryItem, item).stock == 10
    assert admin.get(f"/inventory/purchase_orders/{po['id']}").get_json()["status"] == "received"


def test_shop_order_over_stock_is_refused_whole(app, admin):
    a, b = make_item("A"), make_item("B")
    stock.receive(a, 5)
    stock.receive(b, 1)
    cust = Customer(name="Duka", phone_number="0712000666", code="D2")
    db.session.add(cust)
    db.session.flush()
    a, b, cust_id = a.id, b.id, cust.id
    db.session.commit()
    r = admin.post("/inventory/place_order", json={"customer_id": cust_id, "items": [
        {"id": a, "quantity": 2}, {"id": b, "quantity": 3}]})
    assert r.status_code == 400 and "Only 1" in r.get_json()["error"]
    assert (batches_of(a), batches_of(b)) == ({"UNBATCHED": 5}, {"UNBATCHED": 1})
    assert OrderItem.query.count() == 0


@pytest.fixture
def till(app, login_as, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    item = make_item()
    stock.receive(item, 4, batch_number="A", expiry_date=date(2027, 1, 1))
    stock.receive(item, 10, batch_number="B", expiry_date=date(2027, 2, 1))
    db.session.commit()
    item_id = item.id
    client = login_as("admin")
    client.post("/pos/shift/open", json={"opening_float": 0})
    return client, item_id


def sell(client, item_id, qty):
    txn = client.post("/pos/transaction/new", json={}).get_json()["transaction"]["id"]
    r = client.post(f"/pos/transaction/{txn}/add_item",
                    json={"inventory_item_id": item_id, "quantity": qty})
    assert r.status_code in (200, 201), r.get_json()
    return txn


def test_pos_sale_void_and_refund_move_exact_batches(app, till):
    client, item = till
    txn = sell(client, item, 10)
    assert client.post(f"/pos/transaction/{txn}/complete",
                       json={"payment_method": "cash", "amount_tendered": 1000}).status_code == 200
    assert batches_of(item) == {"A": 0, "B": 4}
    line_ = TransactionItem.query.filter_by(transaction_id=txn).one()
    assert line_.batch_number == "A x4, B x6"
    r = client.post(f"/pos/transaction/{txn}/refund",
                    json={"items": [{"transaction_item_id": line_.id, "quantity": 7}]})
    assert r.status_code == 200, r.get_json()
    # B's 6 go back first (taken last), then 1 of A's 4.
    assert batches_of(item) == {"A": 1, "B": 10}

    txn2 = sell(client, item, 3)
    client.post(f"/pos/transaction/{txn2}/complete",
                json={"payment_method": "cash", "amount_tendered": 300})
    assert batches_of(item) == {"A": 0, "B": 8}
    from services import otp_service
    client.post(f"/pos/transaction/{txn2}/void", json={"reason": "wrong item"})
    otp = otp_service.peek_otp(f"void:txn:{txn2}")
    assert client.post(f"/pos/transaction/{txn2}/void",
                       json={"reason": "wrong item", "otp": otp}).status_code == 200
    assert batches_of(item) == {"A": 1, "B": 10}
    assert_invariant(item)


def test_pos_checkout_over_sellable_stock_is_409_and_records_nothing(app, till):
    from models import Invoice, Payment
    client, item_id = till
    txn = sell(client, item_id, 14)
    stock.adjust(db.session.get(InventoryItem, item_id), -1)   # another till took one
    db.session.commit()
    r = client.post(f"/pos/transaction/{txn}/complete",
                    json={"payment_method": "cash", "amount_tendered": 1400})
    assert r.status_code == 409 and "Only 13" in r.get_json()["error"]
    assert Invoice.query.count() == 0 and Payment.query.count() == 0
    assert sum(batches_of(item_id).values()) == 13 and LineBatch.query.count() == 0


@pytest.mark.skipif(not on_mysql(), reason="row locks need MySQL")
def test_two_tills_selling_the_last_unit(app, login_as, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    item = make_item()
    stock.receive(item, 1, batch_number="LAST")
    item_id = item.id
    db.session.commit()
    client = login_as("admin")
    client.post("/pos/shift/open", json={"opening_float": 0})
    txns = [sell(client, item_id, 1) for _ in range(2)]
    cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"]).value
    results, barrier = [], threading.Barrier(2)

    def worker(txn):
        with app.app_context():
            c = app.test_client()
            c.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie)
            barrier.wait()
            results.append(c.post(f"/pos/transaction/{txn}/complete",
                                  json={"payment_method": "cash",
                                        "amount_tendered": 100}).status_code)

    threads = [threading.Thread(target=worker, args=(t,)) for t in txns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409]
    with tenant_scope(app.acme_id):
        db.session.expire_all()
        assert batches_of(item_id) == {"LAST": 0}
        assert LineBatch.query.count() == 1


def test_item_list_reads_batches_in_one_query(app, admin):
    """The old screens' expiry/batch columns come from one batch query for
    the whole list, not one (or three) per item."""
    from sqlalchemy import event
    for n in range(12):
        item = make_item(f"Item {n}")
        stock.receive(item, 3, batch_number=f"B{n}", expiry_date=date(2027, 1, 1 + n))
    db.session.commit()
    statements = []

    def count(conn, cursor, statement, *args):
        if "FROM batches" in statement:
            statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", count)
    try:
        rows = admin.get("/inventory/items").get_json()
    finally:
        event.remove(db.engine, "before_cursor_execute", count)
    assert len(statements) == 1
    first = next(r for r in rows if r["name"] == "Item 0")
    assert (first["batch_number"], first["expiry_date"]) == ("B0", "2027-01-01")
