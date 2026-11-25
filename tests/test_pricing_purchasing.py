"""Purchase order generation and automatic purchase and selling prices
(the pharmacy owner's request): services/pricing.py, the pricing API and
page, re-pricing on delivery, low-stock PO drafts and the PO PDF."""
import base64
import re
import zlib
from datetime import date
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import (AuditLog, InventoryItem, PurchaseOrder, PurchaseOrderItem, Supplier,
                    Tenant, User, db)
from services import pricing, stock

D = Decimal


def client_as(app, role):
    """A separate logged-in test client per role (one session each)."""
    email = f"{role}@pricing.test"
    with tenant_scope(app.acme_id):
        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User(email=email, name=f"{role.title()} Achieng", role=role,
                        status="approved")
            db.session.add(user)
            db.session.commit()
        user_id = user.id
    c = app.test_client()
    with c.session_transaction() as sess:
        sess.update(profile={"email": email, "name": role}, role=role, user_id=user_id,
                    tenant_id=app.acme_id)
    c.user_id = user_id
    return c


def make_item(name, *, cost=None, price="100", stock_qty=0, cost_at_receipt=None, **fields):
    item = InventoryItem(name=name, price=D(price),
                         cost_price=D(cost) if cost is not None else None, **fields)
    db.session.add(item)
    db.session.flush()
    if stock_qty:
        stock.receive(item, stock_qty, batch_number=f"{name[:3].upper()}-1",
                      expiry_date=date(2028, 1, 1), unit_cost=cost_at_receipt)
    db.session.commit()
    return item


def audits(action, item_id=None):
    q = AuditLog.query.filter_by(action=action)
    if item_id is not None:
        q = q.filter_by(entity_id=item_id)
    return q.order_by(AuditLog.id).all()


def fresh(model, id_):
    db.session.expire_all()
    return db.session.get(model, id_)


# --- selling_price, margin and parsing (pure) --------------------------------

@pytest.mark.parametrize("cost, markup, rounding, expected", [
    ("100", "30", 5, "130.00"),        # exact multiple: no bump
    ("100", "30", 10, "130.00"),
    ("100", "30", 1, "130.00"),
    ("101", "30", 1, "132.00"),        # 131.30 up to the shilling
    ("101", "30", 5, "135.00"),
    ("101", "30", 10, "140.00"),
    ("10", "25", 5, "15.00"),          # 12.50 up to 15
    ("33.33", "50", 1, "50.00"),       # 49.995
    ("0.01", "30", 1, "1.00"),         # tiny cost still costs a shilling
    ("0.01", "30", 10, "10.00"),
    ("0", "30", 5, "0.00"),
    ("100", "0", 1, "100.00"),         # 0 markup: cost, rounded up
    ("99.50", "0", 1, "100.00"),
    ("99.50", "0", 5, "100.00"),
    ("80", "25", 5, "100.00"),
    ("1234.56", "1000", 10, "13590.00"),
    (D("0.10"), D("200"), 1, "1.00"),
    (25, 20, 5, "30.00"),              # ints
    (0.1, 0, 1, "1.00"),               # a float goes through str, not binary
])
def test_selling_price_table(cost, markup, rounding, expected):
    out = pricing.selling_price(cost, markup, rounding)
    assert isinstance(out, Decimal)
    assert out == D(expected) and str(out) == expected


@pytest.mark.parametrize("cost, markup, rounding", [
    ("-1", "30", 5), ("10", "-0.01", 5), ("10", "30", 2), ("10", "30", 0),
    ("10", "30", True), (None, "30", 5), ("abc", "30", 5), ("10", None, 5),
    ("NaN", "30", 5), ("10", "Infinity", 5), ("", "30", 5),
])
def test_selling_price_bad_inputs_raise(cost, markup, rounding):
    with pytest.raises(ValueError):
        pricing.selling_price(cost, markup, rounding)


@pytest.mark.parametrize("cost, price, expected", [
    ("100", "130", D("23.08")), ("100", "100", D("0.00")), ("120", "100", D("-20.00")),
    (None, "100", None), ("100", None, None), ("100", "0", None), ("0", "50", D("100.00")),
])
def test_margin_pct(cost, price, expected):
    assert pricing.margin_pct(cost, price) == expected


@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), (0, D("0.00")), ("30", D("30.00")), (1000, D("1000.00")),
    (12.5, D("12.50")),
])
def test_parse_markup_ok(value, expected):
    assert pricing.parse_markup(value) == expected


@pytest.mark.parametrize("value", [-1, 1000.01, "abc", True, "NaN"])
def test_parse_markup_rejects(value):
    with pytest.raises(ValueError):
        pricing.parse_markup(value)


@pytest.mark.parametrize("value, expected", [(None, None), ("", None), (1, 1), ("24", 24),
                                             (12.0, 12)])
def test_parse_reorder_quantity_ok(value, expected):
    assert pricing.parse_reorder_quantity(value) == expected


@pytest.mark.parametrize("value", [0, -3, 1.5, "x", True])
def test_parse_reorder_quantity_rejects(value):
    with pytest.raises(ValueError):
        pricing.parse_reorder_quantity(value)


@pytest.mark.parametrize("value, ok", [(1, True), (5, True), (10, True), ("5", True),
                                       (2, False), (0, False), (None, False), ("x", False),
                                       (True, False), (5.5, False)])
def test_parse_rounding(value, ok):
    if ok:
        assert pricing.parse_rounding(value) == int(value)
    else:
        with pytest.raises(ValueError):
            pricing.parse_rounding(value)


def test_effective_markup_item_then_business_default(app):
    tenant = db.session.get(Tenant, app.acme_id)
    own = make_item("Own", cost="10", markup_pct=D("50"))
    default = make_item("Default", cost="10")
    assert pricing.effective_markup(own, tenant) == D("50")
    assert pricing.effective_markup(default, tenant) == D("30")    # migration default
    assert tenant.price_rounding == 5


# --- reprice_on_receipt ------------------------------------------------------

def test_reprice_auto_item_sets_cost_price_and_audits(app):
    item = make_item("Amoxil", cost="100", price="130", price_mode="auto")
    out = pricing.reprice_on_receipt(item, D("120"), user_id=None)
    db.session.commit()
    assert out == D("160.00")                     # 156 rounded up to 5
    item = fresh(InventoryItem, item.id)
    assert (item.cost_price, item.price) == (D("120.00"), D("160.00"))
    [row] = audits("pricing.auto", item.id)
    assert row.old_value == {"price": "130.00"}
    assert row.new_value["price"] == "160.00" and row.new_value["cost_price"] == "120.00"
    assert row.new_value["previous_cost_price"] == "100.00"


def test_reprice_unchanged_price_writes_no_audit(app):
    item = make_item("Panadol", cost="100", price="130", price_mode="auto")
    assert pricing.reprice_on_receipt(item, D("100"), None) == D("130.00")
    db.session.commit()
    assert audits("pricing.auto") == []


def test_reprice_manual_item_keeps_price_but_suggests(app):
    item = make_item("Manual", cost="100", price="125")
    assert pricing.reprice_on_receipt(item, "200", None) == D("260.00")
    db.session.commit()
    item = fresh(InventoryItem, item.id)
    assert (item.cost_price, item.price) == (D("200.00"), D("125.00"))
    assert audits("pricing.auto") == []


def test_reprice_bonus_or_missing_cost_leaves_cost_alone(app):
    item = make_item("Bonus", cost="100", price="130", price_mode="auto")
    assert pricing.reprice_on_receipt(item, 0, None) == D("130.00")
    assert pricing.reprice_on_receipt(item, None, None) == D("130.00")
    assert item.cost_price == D("100")
    no_cost = make_item("NoCost", price="50", price_mode="auto")
    assert pricing.reprice_on_receipt(no_cost, None, None) is None
    assert no_cost.price == D("50")
    with pytest.raises(ValueError):
        pricing.reprice_on_receipt(item, "-1", None)


# --- receipts through the API ------------------------------------------------

def _po(admin, supplier_id, lines):
    r = admin.post("/inventory/purchase_orders", json={
        "supplier_id": supplier_id,
        "items": [{"inventory_item_id": i, "quantity_ordered": q, "unit_cost": c}
                  for i, q, c in lines]})
    assert r.status_code == 201, r.get_json()
    po = r.get_json()["purchase_order"]
    assert admin.put(f"/inventory/purchase_orders/{po['id']}/status",
                     json={"status": "sent"}).status_code == 200
    return po


def test_po_receive_reprices_auto_item_and_audits(app):
    admin = client_as(app, "admin")
    sup = Supplier(name="Harleys")
    db.session.add(sup)
    db.session.commit()
    auto = make_item("Augmentin", cost="400", price="520", price_mode="auto",
                     markup_pct=D("25"))
    manual = make_item("Ventolin", cost="300", price="450")
    po = _po(admin, sup.id, [(auto.id, 10, "480.00"), (manual.id, 5, "330")])
    lines = {li["inventory_item_id"]: li["id"] for li in po["items"]}
    r = admin.post(f"/inventory/purchase_orders/{po['id']}/receive", json={"items": [
        {"po_item_id": lines[auto.id], "quantity_received": 10, "batch_number": "AUG-9"},
        {"po_item_id": lines[manual.id], "quantity_received": 5, "batch_number": "VEN-2"}]})
    assert r.status_code == 200, r.get_json()

    auto, manual = fresh(InventoryItem, auto.id), fresh(InventoryItem, manual.id)
    assert (auto.stock, auto.cost_price, auto.price) == (10, D("480.00"), D("600.00"))
    assert (manual.cost_price, manual.price) == (D("330.00"), D("450.00"))
    [row] = audits("pricing.auto", auto.id)
    assert row.user_id == admin.user_id
    assert (row.old_value["price"], row.new_value["price"]) == ("520.00", "600.00")
    assert audits("pricing.auto", manual.id) == []
    assert manual.to_dict()["suggested_price"] == 430.0     # 330 x 1.3 = 429 -> 430


def test_restock_with_unit_cost_reprices_auto_item(app):
    admin = client_as(app, "admin")
    item = make_item("Zinc", cost="50", price="65", price_mode="auto")
    r = admin.post(f"/inventory/items/{item.id}/restock",
                   json={"quantity": 4, "unit_cost": "60", "batch_number": "Z-2"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["price"] == 80.0 and r.get_json()["suggested_price"] == 80.0
    item = fresh(InventoryItem, item.id)
    assert (item.stock, item.cost_price, item.price) == (4, D("60.00"), D("80.00"))
    assert len(audits("pricing.auto", item.id)) == 1
    # Without a unit cost nothing about the price changes.
    r = admin.post(f"/inventory/items/{item.id}/restock", json={"quantity": 1})
    assert r.status_code == 200 and r.get_json()["suggested_price"] is None
    assert len(audits("pricing.auto", item.id)) == 1
    assert admin.post(f"/inventory/items/{item.id}/restock",
                      json={"quantity": 1, "unit_cost": "-5"}).status_code == 400
    assert admin.post(f"/inventory/items/{item.id}/restock",
                      json={"quantity": 1, "unit_cost": "abc"}).status_code == 400


# --- items API -----------------------------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("markup_pct", -1), ("markup_pct", 1001), ("markup_pct", "lots"),
    ("price_mode", "fixed"), ("price_mode", None),
    ("reorder_quantity", 0), ("reorder_quantity", -2), ("reorder_quantity", 2.5),
])
def test_item_create_and_update_validate_pricing_fields(app, field, value):
    admin = client_as(app, "admin")
    body = {"name": "Bad", "category": "Drugs", "price": 10, field: value}
    r = admin.post("/inventory/items", json=body)
    assert r.status_code == 400, r.get_json()
    assert field in r.get_json()["error"]
    item = make_item("Existing", cost="10", price="20")
    r = admin.put(f"/inventory/items/{item.id}", json={field: value})
    assert r.status_code == 400
    assert fresh(InventoryItem, item.id).price == D("20")


def test_item_create_auto_computes_price_from_cost(app):
    admin = client_as(app, "admin")
    r = admin.post("/inventory/items", json={
        "name": "Cetirizine", "category": "Drugs", "cost_price": "42",
        "price_mode": "auto", "markup_pct": 40, "reorder_quantity": 24})
    assert r.status_code == 201, r.get_json()
    out = r.get_json()["item"]
    assert out["price"] == 60.0                     # 58.80 up to 60
    assert (out["price_mode"], out["markup_pct"], out["reorder_quantity"]) == ("auto", 40.0, 24)
    assert out["suggested_price"] == 60.0 and out["margin_pct"] == 30.0
    # Manual (the default) still needs a price.
    r = admin.post("/inventory/items", json={"name": "NoPrice", "category": "Drugs",
                                             "cost_price": "42"})
    assert r.status_code == 400
    r = admin.post("/inventory/items", json={"name": "Neg", "category": "Drugs",
                                             "cost_price": "-1", "price_mode": "auto"})
    assert r.status_code == 400


def test_item_update_audits_manual_price_and_auto_switch(app):
    admin = client_as(app, "admin")
    item = make_item("Brufen", cost="20", price="30")
    r = admin.put(f"/inventory/items/{item.id}", json={"price": "35", "markup_pct": None,
                                                       "reorder_quantity": None})
    assert r.status_code == 200, r.get_json()
    [row] = audits("pricing.manual", item.id)
    assert (row.old_value["price"], row.new_value["price"]) == ("30.00", "35.00")
    # Same price again: no audit.
    assert admin.put(f"/inventory/items/{item.id}", json={"price": 35}).status_code == 200
    assert len(audits("pricing.manual", item.id)) == 1
    # Switching to auto prices from cost now (20 x 1.5 = 30).
    r = admin.put(f"/inventory/items/{item.id}", json={"price_mode": "auto", "markup_pct": 50})
    assert r.status_code == 200
    assert r.get_json()["item"]["price"] == 30.0
    assert len(audits("pricing.auto", item.id)) == 1
    assert admin.put(f"/inventory/items/{item.id}", json={"price": -1}).status_code == 400


def test_item_to_dict_has_pricing_fields(app):
    item = make_item("Dict", cost="100", price="90", reorder_quantity=12)
    out = item.to_dict()
    assert out["reorder_quantity"] == 12 and out["price_mode"] == "manual"
    assert out["markup_pct"] is None
    assert out["suggested_price"] == 130.0 and out["margin_pct"] == pytest.approx(-11.11)
    assert make_item("Costless").to_dict()["suggested_price"] is None


# --- pricing API ---------------------------------------------------------------

def test_pricing_table_flags_below_cost_and_low_margin(app):
    admin = client_as(app, "admin")
    below = make_item("Below", cost="100", price="90").id
    thin = make_item("Thin", cost="100", price="105").id
    fine = make_item("Fine", cost="100", price="130", price_mode="auto").id
    make_item("Unknown", price="40")
    r = admin.get("/inventory/pricing")
    assert r.status_code == 200
    data = r.get_json()
    rows = {row["name"]: row for row in data["items"]}
    assert rows["Below"]["below_cost"] and rows["Below"]["flagged"]
    assert rows["Thin"]["low_margin"] and not rows["Thin"]["below_cost"]
    assert not rows["Fine"]["flagged"] and not rows["Fine"]["differs"]
    assert rows["Unknown"]["suggested_price"] is None and not rows["Unknown"]["flagged"]
    assert rows["Below"]["markup_used"] == 30.0 and rows["Below"]["suggested_price"] == 130.0
    assert data["defaults"] == {"default_markup_pct": 30.0, "price_rounding": 5}
    assert data["counts"] == {"items": 4, "below_cost": 1, "low_margin": 2, "no_cost": 1}
    flagged = admin.get("/inventory/pricing?flagged=1").get_json()["items"]
    assert {row["id"] for row in flagged} == {below, thin}
    assert fine not in {row["id"] for row in flagged}


def test_pricing_api_is_for_admins(app):
    for role in ("cashier", "stock_clerk", "pharmacist", "accountant"):
        c = client_as(app, role)
        assert c.get("/inventory/pricing").status_code == 403
        assert c.post("/inventory/pricing/apply", json={"all_auto": True}).status_code == 403
        assert c.put("/inventory/pricing/defaults",
                     json={"default_markup_pct": 10}).status_code == 403
    assert app.test_client().get("/inventory/pricing").status_code == 401


def test_apply_suggested_by_ids_and_all_auto(app):
    admin = client_as(app, "admin")
    a = make_item("A", cost="100", price="90").id
    b = make_item("B", cost="10", price="10", price_mode="auto").id
    c = make_item("C", price="10", price_mode="auto").id        # no cost: skipped
    d = make_item("D", cost="100", price="100").id              # not chosen
    r = admin.post("/inventory/pricing/apply", json={"item_ids": [a]})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["updated"] == [a]
    assert fresh(InventoryItem, a).price == D("130.00")
    assert fresh(InventoryItem, d).price == D("100.00")
    [row] = audits("pricing.apply", a)
    assert (row.old_value["price"], row.new_value["price"]) == ("90.00", "130.00")

    r = admin.post("/inventory/pricing/apply", json={"all_auto": True})
    assert r.status_code == 200
    assert r.get_json()["updated"] == [b] and r.get_json()["skipped_no_cost"] == [c]
    assert fresh(InventoryItem, b).price == D("15.00")       # 13 up to 15
    # Applying again changes nothing and audits nothing.
    assert admin.post("/inventory/pricing/apply",
                      json={"all_auto": True}).get_json()["updated"] == []
    assert len(audits("pricing.apply")) == 2


def test_apply_suggested_refuses_bad_and_foreign_ids(app, other_tenant):
    admin = client_as(app, "admin")
    mine = make_item("Mine", cost="100", price="90").id
    with tenant_scope(other_tenant):
        theirs = InventoryItem(name="Theirs", price=D("5"), cost_price=D("100"))
        db.session.add(theirs)
        db.session.commit()
        theirs_id = theirs.id
    assert admin.post("/inventory/pricing/apply", json={}).status_code == 400
    assert admin.post("/inventory/pricing/apply", json={"item_ids": []}).status_code == 400
    assert admin.post("/inventory/pricing/apply",
                      json={"item_ids": [mine, 99999]}).status_code == 404
    assert admin.post("/inventory/pricing/apply",
                      json={"item_ids": [mine, theirs_id]}).status_code == 404
    assert admin.post("/inventory/pricing/apply", json={"item_ids": ["x"]}).status_code == 404
    assert fresh(InventoryItem, mine).price == D("90")
    with tenant_scope(other_tenant):
        assert db.session.get(InventoryItem, theirs_id).price == D("5")


@pytest.mark.parametrize("role", ["admin", "superuser"])
def test_defaults_update_by_admin_or_owner(app, role):
    c = client_as(app, role)
    r = c.put("/inventory/pricing/defaults", json={"default_markup_pct": "33.5",
                                                   "price_rounding": 10})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["defaults"] == {"default_markup_pct": 33.5, "price_rounding": 10}
    tenant = fresh(Tenant, app.acme_id)
    assert (tenant.default_markup_pct, tenant.price_rounding) == (D("33.50"), 10)
    [row] = audits("tenant.pricing_defaults")
    assert row.old_value == {"default_markup_pct": 30.0, "price_rounding": 5}
    # One field alone.
    assert c.put("/inventory/pricing/defaults", json={"price_rounding": 1}).status_code == 200
    assert fresh(Tenant, app.acme_id).default_markup_pct == D("33.50")


@pytest.mark.parametrize("body", [
    {}, {"price_rounding": 2}, {"price_rounding": "x"}, {"price_rounding": None},
    {"default_markup_pct": -1}, {"default_markup_pct": 1000.5}, {"default_markup_pct": None},
    {"default_markup_pct": "abc"}, {"default_markup_pct": 20, "price_rounding": 3},
])
def test_defaults_update_validation(app, body):
    admin = client_as(app, "admin")
    assert admin.put("/inventory/pricing/defaults", json=body).status_code == 400
    tenant = fresh(Tenant, app.acme_id)
    assert (tenant.default_markup_pct, tenant.price_rounding) == (D("30"), 5)
    assert audits("tenant.pricing_defaults") == []


def test_new_defaults_change_suggested_prices(app):
    admin = client_as(app, "admin")
    item = make_item("Follow", cost="100", price="130").id
    admin.put("/inventory/pricing/defaults", json={"default_markup_pct": 50,
                                                   "price_rounding": 1})
    [row] = admin.get("/inventory/pricing").get_json()["items"]
    assert row["id"] == item and row["suggested_price"] == 150.0 and row["differs"]


# --- draft purchase order from low stock -----------------------------------------

@pytest.fixture
def low_stock(app):
    """Two suppliers; items at, below and above their reorder levels."""
    harleys, dawa = Supplier(name="Harleys"), Supplier(name="Dawa Ltd")
    db.session.add_all([harleys, dawa])
    db.session.commit()
    items = {
        # stock 2 <= 5, reorder_quantity 50, last batch cost 12.50
        "amox": make_item("Amoxil", cost="11", stock_qty=2, cost_at_receipt=D("12.50"),
                          reorder_level=5, reorder_quantity=50, supplier_id=harleys.id),
        # stock 3 <= 4, no reorder qty: 2 x 4 - 3 = 5, no batch cost: cost_price 20
        "pan": make_item("Panadol", cost="20", stock_qty=3, reorder_level=4,
                         supplier_id=dawa.id),
        # stock 0, level 0: max(0 - 0, 1) = 1, no cost at all: 0
        "zero": make_item("Zinc", stock_qty=0, reorder_level=0),
        # above its level: never ordered
        "full": make_item("Full", cost="5", stock_qty=50, reorder_level=5,
                          supplier_id=harleys.id),
        # variants: skipped
        "shirt": make_item("Shirt", stock_qty=0, reorder_level=5, has_variants=True,
                           supplier_id=harleys.id),
    }
    return {"harleys": harleys.id, "dawa": dawa.id, **{k: v.id for k, v in items.items()}}


def _lines(po):
    return {li["inventory_item_id"]: (li["quantity_ordered"], li["unit_cost"])
            for li in po["items"]}


def test_draft_from_low_stock_all_items(app, low_stock):
    clerk = client_as(app, "stock_clerk")
    r = clerk.post("/inventory/purchase_orders/draft_from_low_stock", json={})
    assert r.status_code == 201, r.get_json()
    po = r.get_json()["purchase_order"]
    assert po["status"] == "draft" and po["supplier_id"] is None
    assert po["ordered_by"] == clerk.user_id
    assert re.fullmatch(r"PO-\d{8}-\d{4}", po["po_number"])
    assert _lines(po) == {low_stock["amox"]: (50.0, 12.5),
                          low_stock["pan"]: (5.0, 20.0),
                          low_stock["zero"]: (1.0, 0.0)}
    assert po["total"] == 725.0
    assert {li["item_name"] for li in po["items"]} == {"Amoxil", "Panadol", "Zinc"}
    [row] = audits("po.create")
    assert row.new_value["source"] == "low_stock"


def test_draft_from_low_stock_for_one_supplier(app, low_stock):
    clerk = client_as(app, "stock_clerk")
    r = clerk.post("/inventory/purchase_orders/draft_from_low_stock",
                   json={"supplier_id": low_stock["harleys"]})
    assert r.status_code == 201
    po = r.get_json()["purchase_order"]
    assert po["supplier_id"] == low_stock["harleys"] and po["supplier_name"] == "Harleys"
    assert _lines(po) == {low_stock["amox"]: (50.0, 12.5)}
    # The next draft gets the next number.
    r = clerk.post("/inventory/purchase_orders/draft_from_low_stock",
                   json={"supplier_id": low_stock["dawa"]})
    assert r.get_json()["purchase_order"]["po_number"].endswith("-0002")


def test_draft_from_low_stock_refusals(app, low_stock, other_tenant):
    clerk = client_as(app, "stock_clerk")
    with tenant_scope(other_tenant):
        foreign = Supplier(name="Foreign")
        db.session.add(foreign)
        db.session.commit()
        foreign_id = foreign.id
    for sid in (99999, foreign_id, "x"):
        r = clerk.post("/inventory/purchase_orders/draft_from_low_stock",
                       json={"supplier_id": sid})
        assert r.status_code == 400
    # A supplier with nothing low.
    quiet = Supplier(name="Quiet")
    db.session.add(quiet)
    db.session.commit()
    r = clerk.post("/inventory/purchase_orders/draft_from_low_stock",
                   json={"supplier_id": quiet.id})
    assert r.status_code == 400 and "Quiet" in r.get_json()["error"]
    assert PurchaseOrder.query.count() == 0
    assert client_as(app, "cashier").post(
        "/inventory/purchase_orders/draft_from_low_stock", json={}).status_code == 403


def test_draft_with_nothing_low_is_400(app):
    make_item("Plenty", cost="5", stock_qty=100, reorder_level=5)
    r = client_as(app, "admin").post("/inventory/purchase_orders/draft_from_low_stock")
    assert r.status_code == 400
    assert PurchaseOrder.query.count() == 0


# --- purchase order PDF --------------------------------------------------------------

def _pdf_text(raw):
    """Text of every content stream: ReportLab writes them ASCII85 then
    Flate encoded."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        body = m.group(1).strip()
        if body.endswith(b"~>"):
            body = base64.a85decode(body[:-2])
        try:
            body = zlib.decompress(body)
        except zlib.error:
            pass
        out.append(body.decode("latin-1"))
    return "\n".join(out)


def test_po_pdf(app, low_stock):
    with tenant_scope(app.acme_id):
        t = db.session.get(Tenant, app.acme_id)
        t.kra_pin, t.ppb_premises_license = "P051234567Z", "PPB/123"
        db.session.commit()
    clerk = client_as(app, "stock_clerk")
    po = clerk.post("/inventory/purchase_orders/draft_from_low_stock",
                    json={"supplier_id": low_stock["harleys"]}).get_json()["purchase_order"]
    r = clerk.get(f"/inventory/purchase_orders/{po['id']}.pdf")
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.data.startswith(b"%PDF") and len(r.data) > 1000
    text = _pdf_text(r.data)
    for expected in (po["po_number"], "Acme Pharmacy", "P051234567Z", "PPB/123", "Harleys",
                     "Amoxil", "625.00", "Stock_Clerk Achieng"):
        assert expected in text, expected


def test_po_pdf_other_tenant_is_404(app, other_tenant):
    with tenant_scope(other_tenant):
        po = PurchaseOrder(po_number="PO-20260101-0001", status="draft")
        db.session.add(po)
        db.session.flush()
        db.session.add(PurchaseOrderItem(po_id=po.id, quantity_ordered=1, unit_cost=1,
                                         quantity_received=0))
        db.session.commit()
        po_id = po.id
    admin = client_as(app, "admin")
    assert admin.get(f"/inventory/purchase_orders/{po_id}.pdf").status_code == 404
    assert admin.get("/inventory/purchase_orders/99999.pdf").status_code == 404
    assert client_as(app, "cashier").get(
        f"/inventory/purchase_orders/{po_id}.pdf").status_code == 403


# --- pages ------------------------------------------------------------------------------

def test_pricing_page_for_admin_redirects_cashier(app):
    admin = client_as(app, "admin")
    r = admin.get("/purchasing-ui/pricing")
    assert r.status_code == 200
    assert b"Default markup" in r.data and b"/inventory/pricing" in r.data
    r = client_as(app, "cashier").get("/purchasing-ui/pricing")
    assert r.status_code == 302 and "/purchasing-ui" not in r.headers["Location"]
    assert app.test_client().get("/purchasing-ui/pricing").status_code == 302


def test_pricing_page_in_admin_nav_only(app):
    assert b'href="/purchasing-ui/pricing"' in client_as(app, "admin").get("/dashboard").data
    assert b"/purchasing-ui/pricing" not in client_as(app, "stock_clerk").get("/dashboard").data


def test_purchase_orders_page_has_draft_and_pdf(app):
    r = client_as(app, "stock_clerk").get("/purchase-orders-ui")
    assert r.status_code == 200
    assert b"draft_from_low_stock" in r.data and b".pdf" in r.data
