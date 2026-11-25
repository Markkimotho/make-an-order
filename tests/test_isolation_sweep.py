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
from models import (AuditLog, CashMovement, Category, Customer, InventoryItem, Invoice,
                    ItemVariant, SmsMessage,
                    InvoiceAdjustment, Payment, PaymentAllocation, PaymentIntent,
                    LoyaltyTransaction, Order, OrderItem, Prescription,
                    PrescriptionItem, PurchaseOrder, PurchaseOrderItem, Shift,
                    StockMovement, Supplier, Transaction, TransactionItem, User, db)
from services import stock
from tests.test_migrations import HEAD_TABLES

MARK = "ALPHA-SECRET"
# Everything identifying about tenant A that must never reach tenant B.
LEAK_MARKERS = (MARK, "ALPHA-SKU", "ALPHA-TXN", "ALPHA-PO", "alpha@x.co", "ALPHARCPT",
                "INVALP01",
                "254712000001", "254712000009", "254712000010")
STRING_ARGS = ("ALPHA-SKU", "ALPHA-TXN", "ALPHA-PO", "ws_CO_ALPHA", "ALPHARCPT", "INVALP01")
SKIP_PREFIXES = ("/static", "/auth/", "/platform/")
SKIP_RULES = {"/logout"}
FK_FIELDS = ("customer_id", "inventory_item_id", "item_id", "supplier_id", "category_id",
             "transaction_id", "prescription_id", "prescription_item_id", "shift_id",
             "po_id", "order_id", "user_id", "cashier_id")


def body_with(foreign_id):
    """A request body that points every reference at one of A's ids."""
    body = {"name": "pwned", "item": "pwned", "status": "cancelled", "quantity": 1,
            "amount": 1, "phone_number": "0799999999", "role": "user", "code": "PWN",
            "price": 1, "reason": "manual", "delta": 1, "points": 1,
            "items": [{key: foreign_id for key in FK_FIELDS} | {"quantity": 1,
                       "quantity_prescribed": 1, "quantity_dispensed": 1,
                       "quantity_received": 1, "unit_cost": 1}]}
    body.update({key: foreign_id for key in FK_FIELDS})
    return body


def leaked(text_body):
    return [m for m in LEAK_MARKERS if m in text_body]


def seed_tenant(tenant_id, mark, tag, phone_base):
    """One row per table, every text field carrying ``mark``."""
    with tenant_scope(tenant_id):
        user = User(email=f"{tag.lower()}@x.co", name=mark, role="cashier", status="approved",
                    phone_number=f"0{phone_base}09")
        cat = Category(name=f"{mark} cat")
        sup = Supplier(name=f"{mark} supplier", phone=f"0{phone_base}10")
        db.session.add_all([user, cat, sup])
        db.session.flush()
        item = InventoryItem(name=f"{mark} item", price=Decimal("10"),
                             sku=f"{tag}-SKU", category_id=cat.id, supplier_id=sup.id,
                             is_drug=True, requires_prescription=True)
        cust = Customer(name=f"{mark} customer", phone_number=f"0{phone_base}01", code=tag)
        shift = Shift(cashier_id=user.id, opening_float=Decimal("100"))
        sized = InventoryItem(name=f"{mark} sized", price=Decimal("10"), has_variants=True)
        db.session.add_all([item, sized, cust, shift])
        db.session.flush()
        variant = ItemVariant(inventory_item_id=sized.id, label=mark)
        db.session.add(variant)
        db.session.flush()
        stock.receive(item, 50, batch_number=mark, expiry_date=date(2027, 1, 1))
        stock.receive(sized, 4, variant=variant)
        from services.controlled import Register
        morphine = InventoryItem(name=f"{mark} morphine", price=Decimal("10"), is_controlled=True)
        db.session.add(morphine)
        db.session.flush()
        stock.receive(morphine, 5, batch_number=f"{mark}-CD",
                      register=Register(supplier_name=mark, reference_doc_no=mark))
        order = Order(customer_id=cust.id, item=f"{mark} order", amount=Decimal("10"))
        txn = Transaction(transaction_ref=f"{tag}-TXN", customer_id=cust.id, cashier_id=user.id,
                          shift_id=shift.id, total=Decimal("10"), notes=mark,
                          checkout_request_id=f"ws_CO_{tag}")
        po = PurchaseOrder(po_number=f"{tag}-PO", supplier_id=sup.id, notes=mark)
        rx = Prescription(customer_id=cust.id, doctor_name=f"{mark} doctor",
                          prescription_date=date(2026, 11, 1))
        db.session.add_all([order, txn, po, rx])
        db.session.flush()
        db.session.add_all([
            OrderItem(order_id=order.id, inventory_item_id=item.id, item_name=mark,
                      quantity=1, unit_price=Decimal("10"), subtotal=Decimal("10")),
            StockMovement(item_id=item.id, item_name=mark, delta=5, reason="restock"),
            CashMovement(shift_id=shift.id, movement_type="cash_in", amount=Decimal("5"),
                         reason=mark),
            PurchaseOrderItem(po_id=po.id, inventory_item_id=item.id,
                              quantity_ordered=Decimal("5"), unit_cost=Decimal("2"),
                              batch_number=mark),
            PrescriptionItem(prescription_id=rx.id, inventory_item_id=item.id,
                             dosage_instructions=mark, quantity_prescribed=Decimal("5")),
            LoyaltyTransaction(customer_id=cust.id, transaction_id=txn.id, points_earned=1,
                               balance_after=1, note=mark),
            AuditLog(user_id=user.id, action="seed", entity_type=mark),
            SmsMessage(to_phone=f"254{phone_base[1:]}01", body=mark, kind="seed",
                       status="sent", provider_message_id=f"ATXid_{tag}",
                       dedupe_key=f"seed:{tag}"),
        ])
        line = TransactionItem(transaction_id=txn.id, inventory_item_id=item.id, item_name=mark,
                               quantity=Decimal("1"), unit_price=Decimal("10"),
                               line_total=Decimal("10"), batch_number=mark)
        db.session.add(line)
        db.session.flush()
        stock.take(item, 1, transaction_item=line)          # one line_batches row
        invoice = Invoice(invoice_number=f"INV{tag[:3]}01", kind="credit", customer_id=cust.id,
                          transaction_id=txn.id, order_id=order.id, total=Decimal("100"),
                          notes=mark)
        payment = Payment(method="mpesa_c2b", amount=Decimal("40"), mpesa_receipt=f"{tag}RCPT",
                          payer_name=mark, bill_ref_raw=f"INV{tag[:3]}01", customer_id=cust.id,
                          match_status="matched")
        db.session.add_all([invoice, payment])
        db.session.flush()
        db.session.add_all([
            PaymentAllocation(payment_id=payment.id, invoice_id=invoice.id, amount=Decimal("40")),
            InvoiceAdjustment(invoice_id=invoice.id, amount=Decimal("5"), reason="return",
                              note=mark),
            PaymentIntent(invoice_id=invoice.id, phone=f"0{phone_base}01", amount=Decimal("60"),
                          checkout_request_id=f"ws_CO_INV_{tag}", status="pending",
                          result_desc=mark),
        ])
        db.session.commit()


def seed_tenant_a(tenant_id):
    seed_tenant(tenant_id, MARK, "ALPHA", "7120000")


def seed_tenant_b(tenant_id):
    seed_tenant(tenant_id, "BETA-OWN", "BETA", "7330000")


def row_counts():
    with unscoped():
        return {t: db.session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                for t in sorted(HEAD_TABLES)}


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


def _swept(rule):
    return not (rule.rule.startswith(SKIP_PREFIXES) or rule.rule in SKIP_RULES)


def id_routes(app):
    for rule in app.url_map.iter_rules():
        converters = rule._converters
        if _swept(rule) and converters and all(
                type(c).__name__ == "IntegerConverter" for c in converters.values()):
            yield rule


def string_routes(app):
    for rule in app.url_map.iter_rules():
        converters = rule._converters
        if _swept(rule) and converters and any(
                type(c).__name__ != "IntegerConverter" for c in converters.values()):
            yield rule


def foreign_references(tenant_id):
    """Rows of ``tenant_id`` whose foreign keys point at another tenant's row."""
    bad = []
    with unscoped():
        insp = db.inspect(db.engine)
        for table in sorted(HEAD_TABLES - {"tenants"}):
            for fk in insp.get_foreign_keys(table):
                target = fk["referred_table"]
                if target == "tenants":
                    continue
                col = fk["constrained_columns"][0]
                rows = db.session.execute(text(
                    f"SELECT t.id, t.{col} FROM {table} t JOIN {target} r ON r.id = t.{col} "
                    f"WHERE t.tenant_id = :me AND (r.tenant_id IS NULL OR r.tenant_id != :me)"),
                    {"me": tenant_id}).all()
                bad += [f"{table}.id={rid} {col} -> {target}.id={ref}" for rid, ref in rows]
    return bad


def plain_routes(app):
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith(SKIP_PREFIXES) or rule.rule in SKIP_RULES:
            continue
        if not rule.arguments and "GET" in rule.methods:
            yield rule


def call(client, method, url, foreign_id):
    kwargs = {"json": body_with(foreign_id)} if method in ("POST", "PUT", "PATCH") else {}
    return client.open(url, method=method, **kwargs)


@pytest.fixture
def no_network(monkeypatch):
    """Provider calls fail fast instead of reaching Safaricom/Flutterwave."""
    from services import flutterwave_service
    from services.daraja import DarajaUnavailable, client as daraja_client

    def offline(*a, **k):
        raise DarajaUnavailable("offline in tests")

    def offline_card(*a, **k):
        raise flutterwave_service.FlutterwaveError("offline in tests")

    for name in ("stk_push", "query", "register_c2b", "_fetch_token"):
        monkeypatch.setattr(daraja_client.DarajaClient, name, offline)
    for name in ("verify_transaction", "create_payment_link"):
        monkeypatch.setattr(flutterwave_service, name, offline_card)


@pytest.fixture
def swept(app, other_tenant, login_as, no_network):
    seed_tenant_a(app.acme_id)
    before = snapshot(app.acme_id)
    assert all(before[t] for t in before), "every table needs an A row for the sweep"
    client = login_as("superuser", tenant_id=other_tenant)
    app.globex_id = other_tenant
    return app, client, before


def sweep(app, client, ids):
    """Call every swept route; return (leaks, crashes)."""
    leaks, crashes = [], []

    def check(method, url, response):
        # Binary bodies (the register PDF) are checked through the JSON view
        # built from the same query; decode leniently so they do not crash.
        found = leaked(response.get_data().decode("utf-8", "replace"))
        if found:
            leaks.append(f"{method} {url} -> {response.status_code} shows {found}")
        # 502 is the app reporting a payment provider as down (stubbed offline
        # here), which is correct; anything else 5xx is a crash.
        if response.status_code >= 500 and response.status_code != 502:
            crashes.append(f"{method} {url} -> {response.status_code}")

    for rule in id_routes(app):
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            for value in ids:
                url = rule.build({arg: value for arg in rule.arguments})[1]
                check(method, url, call(client, method, url, value))
    for rule in string_routes(app):
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            for value in STRING_ARGS:
                args = {arg: (value if type(conv).__name__ != "IntegerConverter" else ids[0])
                        for arg, conv in rule._converters.items()}
                url = rule.build(args)[1]
                check(method, url, call(client, method, url, ids[0]))
    for rule in plain_routes(app):
        check("GET", rule.rule, client.get(rule.rule))
    for rule in app.url_map.iter_rules():
        if _swept(rule) and not rule.arguments and "POST" in rule.methods:
            check("POST", rule.rule, call(client, "POST", rule.rule, ids[0]))
    return leaks, crashes


def test_routes_with_ids_never_reach_tenant_a(swept):
    app, client, before = swept
    ids = all_ids(before)
    # 40 at the time of writing: every route taking a numeric id. A drop
    # means the route filter broke, not that routes were removed.
    assert len(list(id_routes(app))) >= 40
    assert len(list(string_routes(app))) >= 3
    leaks, crashes = sweep(app, client, ids)
    assert leaks == [], "tenant A data visible to tenant B:\n" + "\n".join(leaks)
    assert crashes == [], "server errors during sweep:\n" + "\n".join(crashes)
    assert snapshot(app.acme_id) == before, "tenant B changed tenant A's rows"
    bad = foreign_references(app.globex_id)
    assert bad == [], "tenant B rows now reference tenant A rows:\n" + "\n".join(bad)


def test_own_resources_cannot_be_pointed_at_tenant_a(app, other_tenant, login_as, no_network):
    """B edits B's own rows through every id route, with A's ids in every
    reference field of the body. No B row may end up referencing an A row."""
    seed_tenant_a(app.acme_id)
    seed_tenant_b(other_tenant)
    a_before = snapshot(app.acme_id)
    b_ids = all_ids(snapshot(other_tenant))
    a_ids = all_ids(a_before)
    client = login_as("superuser", tenant_id=other_tenant)
    crashes = []
    for rule in id_routes(app):
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            for own in b_ids:
                for foreign in a_ids:
                    url = rule.build({arg: own for arg in rule.arguments})[1]
                    r = call(client, method, url, foreign)
                    if r.status_code >= 500 and r.status_code != 502:
                        crashes.append(f"{method} {url} -> {r.status_code}")
    assert crashes == [], "server errors:\n" + "\n".join(crashes)
    assert snapshot(app.acme_id) == a_before
    bad = foreign_references(other_tenant)
    assert bad == [], "B rows now reference A rows:\n" + "\n".join(bad)


def test_anonymous_requests_reach_nothing(app, client, no_network):
    """No session at all: nothing leaks, nothing crashes, and not a single
    row is created or changed in any tenant."""
    seed_tenant_a(app.acme_id)
    before = snapshot(app.acme_id)
    counts = row_counts()
    leaks, crashes = sweep(app, client, all_ids(before))
    assert leaks == [], "tenant A data visible anonymously:\n" + "\n".join(leaks)
    assert crashes == [], "server errors for anonymous requests:\n" + "\n".join(crashes)
    assert snapshot(app.acme_id) == before
    assert row_counts() == counts, "anonymous requests created rows"


def test_tenant_a_still_sees_its_own_data(app, login_as):
    """Control: the marker is reachable by its owner, so the sweep would
    catch a leak if one existed."""
    seed_tenant_a(app.acme_id)
    client = login_as("superuser")
    body = client.get("/customers/view_customers").get_data(as_text=True)
    assert MARK in body
