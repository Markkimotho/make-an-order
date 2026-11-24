"""Cross-tenant sweep over every route.

Tenant A (acme) gets one row in every table, each carrying the marker
ALPHA-SECRET. Logged in as tenant B's owner, we call every route that takes
numeric ids, with every id A owns, using every method the route accepts,
plus every list/page route with no arguments. Nothing may show A's data,
change A's rows, or crash.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from auth.tenancy import tenant_scope, unscoped
from models import (AuditLog, CashMovement, Category, Customer, InventoryItem,
                    LoyaltyTransaction, Order, OrderItem, Prescription,
                    PrescriptionItem, PurchaseOrder, PurchaseOrderItem, Shift,
                    StockMovement, Supplier, Transaction, TransactionItem, User, db)
from tests.test_migrations import HEAD_TABLES

MARK = "ALPHA-SECRET"
SKIP_PREFIXES = ("/static", "/auth/", "/platform/")
SKIP_RULES = {"/logout"}
BODY = {"name": "pwned", "item": "pwned", "status": "cancelled", "quantity": 1,
        "amount": 1, "phone_number": "0799999999", "role": "user"}


def seed_tenant_a(tenant_id):
    """One row per table, every text field carrying the marker."""
    with tenant_scope(tenant_id):
        user = User(email="alpha@acme.co", name=MARK, role="cashier", status="approved",
                    phone_number="0700000009")
        cat = Category(name=f"{MARK} cat")
        sup = Supplier(name=f"{MARK} supplier", phone="0700000010")
        db.session.add_all([user, cat, sup])
        db.session.flush()
        item = InventoryItem(name=f"{MARK} item", price=Decimal("10"), stock=50,
                             sku="ALPHA-SKU", category_id=cat.id, supplier_id=sup.id,
                             is_drug=True, requires_prescription=True)
        cust = Customer(name=f"{MARK} customer", phone_number="0712000001", code="ALPHA")
        shift = Shift(cashier_id=user.id, opening_float=Decimal("100"))
        db.session.add_all([item, cust, shift])
        db.session.flush()
        order = Order(customer_id=cust.id, item=f"{MARK} order", amount=Decimal("10"))
        txn = Transaction(transaction_ref="ALPHA-TXN", customer_id=cust.id, cashier_id=user.id,
                          shift_id=shift.id, total=Decimal("10"), notes=MARK)
        po = PurchaseOrder(po_number="ALPHA-PO", supplier_id=sup.id, notes=MARK)
        rx = Prescription(customer_id=cust.id, doctor_name=f"{MARK} doctor",
                          prescription_date=date(2026, 11, 1))
        db.session.add_all([order, txn, po, rx])
        db.session.flush()
        db.session.add_all([
            OrderItem(order_id=order.id, inventory_item_id=item.id, item_name=MARK,
                      quantity=1, unit_price=Decimal("10"), subtotal=Decimal("10")),
            StockMovement(item_id=item.id, item_name=MARK, delta=5, reason="restock"),
            CashMovement(shift_id=shift.id, movement_type="cash_in", amount=Decimal("5"),
                         reason=MARK),
            TransactionItem(transaction_id=txn.id, inventory_item_id=item.id, item_name=MARK,
                            quantity=Decimal("1"), unit_price=Decimal("10"),
                            line_total=Decimal("10"), batch_number=MARK),
            PurchaseOrderItem(po_id=po.id, inventory_item_id=item.id,
                              quantity_ordered=Decimal("5"), unit_cost=Decimal("2"),
                              batch_number=MARK),
            PrescriptionItem(prescription_id=rx.id, inventory_item_id=item.id,
                             dosage_instructions=MARK, quantity_prescribed=Decimal("5")),
            LoyaltyTransaction(customer_id=cust.id, transaction_id=txn.id, points_earned=1,
                               balance_after=1, note=MARK),
            AuditLog(user_id=user.id, action="seed", entity_type=MARK),
        ])
        db.session.commit()


def snapshot(tenant_id):
    """Every row of every tenant table belonging to ``tenant_id``."""
    out = {}
    with unscoped():
        for table in sorted(HEAD_TABLES - {"tenants"}):
            rows = db.session.execute(
                text(f"SELECT * FROM {table} WHERE tenant_id = :t ORDER BY id"),
                {"t": tenant_id}).mappings().all()
            out[table] = [dict(r) for r in rows]
    return out


def all_ids(snap):
    return sorted({row["id"] for rows in snap.values() for row in rows})


def id_routes(app):
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith(SKIP_PREFIXES) or rule.rule in SKIP_RULES:
            continue
        converters = rule._converters
        if not converters:
            continue
        if all(type(c).__name__ == "IntegerConverter" for c in converters.values()):
            yield rule


def plain_routes(app):
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith(SKIP_PREFIXES) or rule.rule in SKIP_RULES:
            continue
        if not rule.arguments and "GET" in rule.methods:
            yield rule


def call(client, method, url):
    kwargs = {"json": BODY} if method in ("POST", "PUT", "PATCH") else {}
    return client.open(url, method=method, **kwargs)


@pytest.fixture
def swept(app, other_tenant, login_as):
    seed_tenant_a(app.acme_id)
    before = snapshot(app.acme_id)
    assert all(before[t] for t in before), "every table needs an A row for the sweep"
    client = login_as("superuser", tenant_id=other_tenant)
    return app, client, before


def test_routes_with_ids_never_reach_tenant_a(swept):
    app, client, before = swept
    ids = all_ids(before)
    rules = list(id_routes(app))
    # 40 at the time of writing: every route taking a numeric id. A drop
    # means the route filter broke, not that routes were removed.
    assert len(rules) >= 40, f"only {len(rules)} id routes found"
    leaks, crashes = [], []
    for rule in rules:
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            for value in ids:
                url = rule.build({arg: value for arg in rule.arguments})[1]
                response = call(client, method, url)
                body = response.get_data(as_text=True)
                if MARK in body:
                    leaks.append(f"{method} {url} -> {response.status_code}")
                if response.status_code >= 500:
                    crashes.append(f"{method} {url} -> {response.status_code}")
    assert leaks == [], "tenant A data visible to tenant B:\n" + "\n".join(leaks)
    assert crashes == [], "server errors during sweep:\n" + "\n".join(crashes)
    assert snapshot(app.acme_id) == before, "tenant B changed tenant A's rows"


def test_list_and_page_routes_show_nothing_of_tenant_a(swept):
    app, client, before = swept
    leaks, crashes = [], []
    for rule in plain_routes(app):
        response = client.get(rule.rule)
        if MARK in response.get_data(as_text=True):
            leaks.append(f"GET {rule.rule}")
        if response.status_code >= 500:
            crashes.append(f"GET {rule.rule} -> {response.status_code}")
    assert leaks == [], "tenant A data visible to tenant B:\n" + "\n".join(leaks)
    assert crashes == [], "server errors:\n" + "\n".join(crashes)
    assert snapshot(app.acme_id) == before


def test_tenant_a_still_sees_its_own_data(app, login_as):
    """Control: the marker is reachable by its owner, so the sweep would
    catch a leak if one existed."""
    seed_tenant_a(app.acme_id)
    client = login_as("superuser")
    body = client.get("/customers/view_customers").get_data(as_text=True)
    assert MARK in body
