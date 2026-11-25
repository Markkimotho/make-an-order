"""services/orders.py: an order, its invoice and its stock together; pay-now
holds released by the sweep; late payments flagged; cancels."""
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import (Batch, Customer, InventoryItem, Invoice, ItemVariant, LineBatch, Order,
                    OrderItem, StockMovement, db)
from services import billing, stock
from services import orders as order_service
from services.orders import Line
from tests.conftest import on_mysql

D = Decimal
NOW = datetime(2026, 11, 25, 9, 0)


def batches_of(item_id):
    return {b.batch_number: b.quantity_on_hand
            for b in Batch.query.filter_by(inventory_item_id=item_id).order_by(Batch.id)}


@pytest.fixture
def shop(app):
    item = InventoryItem(name="Amoxil", price=D("100"))
    duka = Customer(name="Duka", phone_number="0712000123", code="DK", credit_limit=D("10000"))
    db.session.add_all([item, duka])
    db.session.flush()
    stock.receive(item, 3, batch_number="A", expiry_date=date(2027, 1, 1))
    stock.receive(item, 10, batch_number="B", expiry_date=date(2027, 6, 1))
    db.session.commit()
    return {"item": item.id, "duka": duka.id}


def pay_now(shop, qty=5, now=NOW):
    item = db.session.get(InventoryItem, shop["item"])
    order, invoice = order_service.create_order([Line(item, qty, D("100"))],
                                                customer_id=shop["duka"], channel="public_link",
                                                pay_now=True, now=now)
    db.session.commit()
    return order.id, invoice.id


def test_pay_now_order_holds_stock_with_a_sale_invoice(app, shop):
    order_id, invoice_id = pay_now(shop)
    order, invoice = db.session.get(Order, order_id), db.session.get(Invoice, invoice_id)
    assert order.order_number == "ORD000001" and order.channel == "public_link"
    assert order.reserved_until == NOW + timedelta(minutes=15)
    assert (invoice.kind, invoice.due_date, invoice.total, invoice.order_id) == \
        ("sale", None, D("500.00"), order_id)
    assert batches_of(shop["item"]) == {"A": 0, "B": 8}
    assert LineBatch.query.count() == 2
    assert StockMovement.query.filter_by(reason="reserve").one().delta == -5
    assert billing.customer_exposure(shop["duka"]) == 0         # not debt


def test_credit_order_keeps_stock_and_is_debt(app, shop):
    item = db.session.get(InventoryItem, shop["item"])
    order, invoice = order_service.create_order([Line(item, 2, D("100"))],
                                                customer_id=shop["duka"], pay_now=False,
                                                due_date=date(2026, 12, 25))
    db.session.commit()
    assert order.reserved_until is None and invoice.kind == "credit"
    assert billing.customer_exposure(shop["duka"]) == D("200.00")
    with pytest.raises(order_service.OrderError, match="due date"):
        order_service.create_order([Line(item, 1, D("100"))], pay_now=False)


def test_order_numbers_count_up(app, shop):
    assert [db.session.get(Order, pay_now(shop, 1)[0]).order_number for _ in range(3)] == \
        ["ORD000001", "ORD000002", "ORD000003"]


def test_bad_orders_are_refused_whole(app, shop):
    item = db.session.get(InventoryItem, shop["item"])
    with pytest.raises(stock.InsufficientStock):
        order_service.create_order([Line(item, 99, D("100"))], pay_now=True)
    db.session.rollback()
    assert Order.query.count() == 0 and Invoice.query.count() == 0
    assert batches_of(shop["item"]) == {"A": 3, "B": 10}
    for bad in ([], [Line(item, 0, D("100"))], [Line(item, 1, D("-1"))], [Line(item, 1, D("0"))]):
        with pytest.raises(order_service.OrderError):
            order_service.create_order(bad, pay_now=True)


def test_unpaid_hold_is_released_by_the_sweep(app, shop):
    order_id, invoice_id = pay_now(shop)
    assert order_service.release_expired(NOW + timedelta(minutes=14))["released"] == 0
    counts = order_service.release_expired(NOW + timedelta(minutes=16))
    assert counts["released"] == 1
    order, invoice = db.session.get(Order, order_id), db.session.get(Invoice, invoice_id)
    assert (order.fulfillment_status, order.reserved_until, invoice.status) == \
        ("released", None, "expired")
    assert batches_of(shop["item"]) == {"A": 3, "B": 10}
    assert order_service.release_expired(NOW + timedelta(hours=1))["released"] == 0
    assert order_service.release(order) is False                  # twice: nothing more


def test_paid_order_is_never_released(app, shop):
    order_id, invoice_id = pay_now(shop)
    billing.record_payment(method="cash", amount=D("500"),
                           invoice=db.session.get(Invoice, invoice_id))
    db.session.commit()
    counts = order_service.release_expired(NOW + timedelta(minutes=30))
    assert counts == {"released": 0, "kept_paid": 1, "restock_needed": 0, "re_held": 0}
    assert db.session.get(Order, order_id).reserved_until is None
    assert batches_of(shop["item"]) == {"A": 0, "B": 8}


def test_part_paid_order_keeps_its_stock(app, shop):
    order_id, invoice_id = pay_now(shop)
    billing.record_payment(method="cash", amount=D("100"),
                           invoice=db.session.get(Invoice, invoice_id))
    db.session.commit()
    assert order_service.release_expired(NOW + timedelta(minutes=30))["kept_paid"] == 1
    assert sum(batches_of(shop["item"]).values()) == 8


def test_payment_after_release_reopens_and_flags_restock(app, shop):
    order_id, invoice_id = pay_now(shop)
    order_service.release_expired(NOW + timedelta(minutes=16))
    billing.record_payment(method="mpesa_c2b", amount=D("500"), receipt="SLT0000001",
                           invoice=db.session.get(Invoice, invoice_id))
    db.session.commit()
    assert db.session.get(Invoice, invoice_id).status == "paid"
    counts = order_service.release_expired(NOW + timedelta(minutes=20))
    assert counts["restock_needed"] == 1
    assert db.session.get(Order, order_id).fulfillment_status == "restock_needed"
    assert order_service.release_expired(NOW + timedelta(minutes=30))["restock_needed"] == 0


def test_cancel_returns_stock_and_turns_money_into_credit(app, shop):
    order_id, invoice_id = pay_now(shop)
    billing.record_payment(method="mpesa_c2b", amount=D("200"), receipt="SLT0000002",
                           invoice=db.session.get(Invoice, invoice_id))
    db.session.commit()
    order_service.cancel(db.session.get(Order, order_id))
    db.session.commit()
    assert db.session.get(Invoice, invoice_id).status == "void"
    assert batches_of(shop["item"]) == {"A": 3, "B": 10}
    assert billing.customer_exposure(shop["duka"]) == D("-200.00")     # their credit
    with pytest.raises(order_service.OrderError):
        order_service.cancel(db.session.get(Order, order_id))


def test_sized_items_need_a_size_and_use_its_stock(app):
    shirt = InventoryItem(name="Shirt", price=D("800"), has_variants=True)
    db.session.add(shirt)
    db.session.flush()
    size = ItemVariant(inventory_item_id=shirt.id, label="Size 32")
    db.session.add(size)
    db.session.flush()
    stock.receive(shirt, 4, variant=size)
    with pytest.raises(order_service.OrderError, match="sizes"):
        order_service.create_order([Line(shirt, 1, D("800"))], pay_now=True)
    order, _ = order_service.create_order([Line(shirt, 3, D("800"), variant=size)], pay_now=True,
                                          now=NOW)
    db.session.commit()
    assert size.stock == 1 and OrderItem.query.one().item_name == "Shirt (Size 32)"
    order_service.release_expired(NOW + timedelta(minutes=16))
    db.session.refresh(size)
    assert size.stock == 4


# --- the staff screens ------------------------------------------------------------

def test_shop_order_gets_an_invoice_and_cancel_voids_it(app, shop, login_as):
    admin = login_as("admin")
    r = admin.post("/inventory/place_order", json={"customer_id": shop["duka"],
                                                   "items": [{"id": shop["item"], "quantity": 4}]})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["order_number"] == "ORD000001" and body["invoice"]["kind"] == "credit"
    assert body["invoice"]["due_date"] is not None
    assert batches_of(shop["item"]) == {"A": 0, "B": 9}
    r = admin.put(f"/orders/{body['order_id']}/status", json={"status": "cancelled"})
    assert r.status_code == 200
    db.session.expire_all()
    assert batches_of(shop["item"]) == {"A": 3, "B": 10}
    assert Invoice.query.one().status == "void"


def test_order_lines_from_before_batches_go_back_on_hold(app, shop, login_as):
    """No line_batches rows: nothing says which batch they came from."""
    order = Order(customer_id=shop["duka"], item="Amoxil ×2", amount=D("200"))
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderItem(order_id=order.id, inventory_item_id=shop["item"],
                             item_name="Amoxil", quantity=2, unit_price=D("100"),
                             subtotal=D("200")))
    db.session.commit()
    order_id = order.id
    r = login_as("admin").delete(f"/orders/delete_orders/{order_id}")
    assert r.status_code == 200
    db.session.expire_all()
    held = Batch.query.filter_by(quarantined=True).one()
    assert held.quantity_on_hand == 2


def test_sweep_command_releases_holds(app, shop):
    order_id, _ = pay_now(shop, now=datetime.utcnow() - timedelta(hours=1))
    result = app.test_cli_runner().invoke(args=["payments-sweep"])
    assert result.exit_code == 0, result.output
    assert "orders_released': 1" in result.output
    with tenant_scope(app.acme_id):
        db.session.expire_all()
        assert db.session.get(Order, order_id).fulfillment_status == "released"


@pytest.mark.skipif(not on_mysql(), reason="row locks need MySQL")
def test_two_sweeps_release_once(app, shop):
    order_id, _ = pay_now(shop)
    results, barrier = [], threading.Barrier(2)

    def worker():
        with app.app_context(), tenant_scope(app.acme_id):
            barrier.wait()
            results.append(order_service.release_expired(NOW + timedelta(minutes=16))["released"])
            db.session.remove()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [0, 1]
    db.session.expire_all()
    assert batches_of(shop["item"]) == {"A": 3, "B": 10}


# --- review fixes (Codex, step 5) ----------------------------------------------------

def test_every_credit_order_passes_the_ceiling_including_the_shop_screen(app, shop, login_as):
    broke = Customer(name="No credit", phone_number="0712000124", code="NC")
    db.session.add(broke)
    db.session.commit()
    r = login_as("admin").post("/inventory/place_order", json={
        "customer_id": broke.id, "items": [{"id": shop["item"], "quantity": 1}]})
    assert r.status_code == 409 and "credit" in r.get_json()["error"]
    assert Order.query.count() == 0 and batches_of(shop["item"]) == {"A": 3, "B": 10}


def test_reversed_payment_puts_the_order_back_on_hold(app, shop):
    order_id, invoice_id = pay_now(shop)
    payment = billing.record_payment(method="mpesa_c2b", amount=D("500"), receipt="SLT0000009",
                                     invoice=db.session.get(Invoice, invoice_id))
    db.session.commit()
    order_service.release_expired(NOW + timedelta(minutes=20))      # paid: hold cleared
    assert db.session.get(Order, order_id).reserved_until is None
    billing.reverse_payment(payment)
    db.session.commit()
    later = datetime.utcnow()
    counts = order_service.release_expired(later)
    assert counts["re_held"] == 1
    assert db.session.get(Order, order_id).reserved_until > later
    assert order_service.release_expired(later + timedelta(minutes=16))["released"] == 1
    assert batches_of(shop["item"]) == {"A": 3, "B": 10}


def test_collected_orders_cannot_be_cancelled_by_status(app, shop, login_as):
    order_id, invoice_id = pay_now(shop)
    order = db.session.get(Order, order_id)
    order.fulfillment_status = "collected"
    db.session.commit()
    r = login_as("admin").put(f"/orders/{order_id}/status", json={"status": "refunded"})
    assert r.status_code == 409
    assert db.session.get(Order, order_id).status == "pending"
    assert batches_of(shop["item"]) == {"A": 0, "B": 8}
