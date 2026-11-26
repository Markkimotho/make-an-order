"""Daily sales and profit, expenses and cashier reconciliation: the owner's
questions (what did we sell and make each day, gross vs net, does the cash
counted match the system) answered by services/reports and /reports/*."""
import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import (
    Batch, CashMovement, Customer, Expense, InventoryItem, ItemVariant, LineBatch, Payment,
    Shift, SmsMessage, Tenant, Transaction, TransactionItem, User, db,
)
from services import billing, stock
from services.clock import today_nairobi
from services.reports import profit, reconciliation
from tests.conftest import set_vertical
from tests.test_pos_owner import add, client_as, new_sale

D = Decimal


@pytest.fixture
def shop(app, monkeypatch):
    """A till with three items: Amoxil in two batches at different costs,
    Panadol with no cost anywhere, Zinc whose batch has no cost but the
    item has a cost price. A cashier has a shift open with a 1000 float."""
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    amox = InventoryItem(name="Amoxil 500mg", price=D("200"), cost_price=D("40"))
    nocost = InventoryItem(name="Panadol", price=D("100"))
    zinc = InventoryItem(name="Zinc", price=D("150"), cost_price=D("60"))
    db.session.add_all([amox, nocost, zinc])
    db.session.flush()
    stock.receive(amox, 2, batch_number="AMX-1", expiry_date=date(2027, 1, 1), unit_cost=D("50"))
    stock.receive(amox, 10, batch_number="AMX-2", expiry_date=date(2027, 6, 1), unit_cost=D("70"))
    stock.receive(nocost, 10, batch_number="PND-1", expiry_date=date(2027, 9, 1))
    stock.receive(zinc, 10, batch_number="ZNC-1", expiry_date=date(2027, 9, 1))
    db.session.commit()
    ids = {"amox": amox.id, "nocost": nocost.id, "zinc": zinc.id}
    admin = client_as(app, "admin", phone="0711000111")
    cashier = client_as(app, "cashier")
    accountant = client_as(app, "accountant")
    r = cashier.post("/pos/shift/open", json={"opening_float": 1000})
    assert r.status_code == 201, r.get_json()
    return {"app": app, "admin": admin, "cashier": cashier, "accountant": accountant,
            "shift": r.get_json()["shift"]["id"], **ids}


def sell(c, lines, method="cash", **body):
    """Ring up ``lines`` [(item_id, qty)] and complete with ``method``."""
    txn = new_sale(c)
    for item_id, qty in lines:
        assert add(c, txn, item_id, quantity=qty).status_code == 201
    r = c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": method, **body})
    assert r.status_code == 200, r.get_json()
    return txn


def pay_stk(txn_id, receipt):
    """Safaricom confirms an STK push for the whole of an open sale."""
    txn = db.session.get(Transaction, txn_id)
    invoice = billing.create_invoice(total=txn.total, transaction_id=txn.id)
    billing.record_payment(method="mpesa_stk", amount=txn.total, receipt=receipt, invoice=invoice)
    db.session.commit()


def set_time(txn_id, when):
    db.session.expire_all()
    db.session.get(Transaction, txn_id).created_at = when
    db.session.commit()


def today_row(c, day=None):
    day = (day or today_nairobi()).isoformat()
    r = c.get(f"/reports/daily?from={day}&to={day}")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["days"][0]


# --- cost of goods and gross profit -----------------------------------------------------

def test_cogs_comes_from_the_batches_a_sale_drew_from(shop):
    sell(shop["cashier"], [(shop["amox"], 3)])          # 2 from AMX-1 at 50, 1 from AMX-2 at 70
    row = today_row(shop["accountant"])
    assert (row["sales_count"], row["revenue"], row["cogs"]) == (1, 600.0, 170.0)
    assert row["gross_profit"] == 430.0 and row["gross_margin_pct"] == 71.7
    assert row["cost_missing_lines"] == 0 and row["net_profit"] == 430.0


def test_a_line_without_any_cost_is_flagged_not_hidden(shop):
    sell(shop["cashier"], [(shop["nocost"], 1), (shop["zinc"], 1)])
    row = today_row(shop["accountant"])
    # Zinc's batch has no cost: the item's cost price (60) is used.
    assert (row["revenue"], row["cogs"], row["gross_profit"]) == (250.0, 60.0, 190.0)
    assert row["cost_missing_lines"] == 1 and row["revenue_without_cost"] == 100.0
    summary = profit.summary(today_nairobi(), today_nairobi())
    flagged = {i["name"]: i["cost_missing"] for i in summary["top_items"]}
    assert flagged == {"Panadol": True, "Zinc": False}


def test_variant_items_use_the_item_cost_price(app, shop):
    shirt = InventoryItem(name="Shirt", price=D("800"), cost_price=D("500"), has_variants=True)
    db.session.add(shirt)
    db.session.flush()
    size = ItemVariant(inventory_item_id=shirt.id, label="M", stock=0)
    db.session.add(size)
    db.session.flush()
    stock.receive(shirt, 5, variant=size)
    txn = Transaction(transaction_ref="T-VAR-1", status="completed", payment_method="cash",
                      subtotal=D("1600"), total=D("1600"), tax_amount=0, discount_amount=0)
    db.session.add(txn)
    db.session.flush()
    line = TransactionItem(transaction_id=txn.id, inventory_item_id=shirt.id, item_name="Shirt M",
                           quantity=2, unit_price=D("800"), line_total=D("1600"))
    db.session.add(line)
    db.session.flush()
    stock.take(shirt, 2, transaction_item=line, variant=size)
    db.session.commit()
    assert LineBatch.query.count() == 0
    row = today_row(shop["accountant"])
    assert (row["revenue"], row["cogs"], row["gross_profit"]) == (1600.0, 1000.0, 600.0)


def test_tax_is_reported_apart_from_revenue(shop, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0.16)
    sell(shop["cashier"], [(shop["amox"], 1)])           # 200 + 32 VAT
    row = today_row(shop["accountant"])
    assert (row["revenue"], row["tax"], row["by_method"]["cash"]) == (200.0, 32.0, 200.0)


def test_discounts_are_line_plus_sale_discounts(shop):
    c = shop["admin"]
    c.post("/pos/shift/open", json={"opening_float": 0})
    txn = new_sale(c)
    assert add(c, txn, shop["amox"], quantity=2, discount_pct=5).status_code == 201   # 20 off
    assert c.post(f"/pos/transaction/{txn}/apply_discount",
                  json={"type": "flat", "value": 10}).status_code == 200
    assert c.post(f"/pos/transaction/{txn}/complete",
                  json={"payment_method": "cash"}).status_code == 200
    row = today_row(shop["accountant"])
    assert (row["discounts"], row["revenue"]) == (30.0, 370.0)


# --- refunds and days --------------------------------------------------------------------

def test_a_refund_comes_off_the_day_it_happened(shop):
    c = shop["cashier"]
    txn = sell(c, [(shop["amox"], 3)])
    yesterday = today_nairobi() - timedelta(days=1)
    set_time(txn, datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=9))
    line = TransactionItem.query.filter_by(transaction_id=txn).one().id
    r = c.post(f"/pos/transaction/{txn}/refund",
               json={"items": [{"transaction_item_id": line, "quantity": 1}], "reason": "wrong"})
    assert r.status_code == 200, r.get_json()
    data = shop["accountant"].get(f"/reports/daily?from={yesterday}&to={today_nairobi()}").get_json()
    before, today = data["days"]
    assert (before["revenue"], before["cogs"], before["refunds"]) == (600.0, 170.0, 0.0)
    # The last unit taken (AMX-2, cost 70) is the one put back.
    assert (today["sales_count"], today["refunds"], today["revenue"]) == (0, 200.0, -200.0)
    assert (today["cogs"], today["gross_profit"]) == (-70.0, -130.0)
    assert today["by_method"]["cash"] == -200.0
    t = data["totals"]
    assert (t["revenue"], t["cogs"], t["gross_profit"], t["sales_count"]) == (400.0, 100.0, 300.0, 1)
    # Items count the sale net of its refund.
    top = profit.summary(yesterday, today_nairobi())["top_items"][0]
    assert (top["qty"], top["revenue"], top["cogs"]) == (2.0, 400.0, 100.0)


def test_a_fully_refunded_sale_nets_to_nothing(shop, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0.16)
    c = shop["cashier"]
    txn = sell(c, [(shop["amox"], 2)])
    line = TransactionItem.query.filter_by(transaction_id=txn).one().id
    assert c.post(f"/pos/transaction/{txn}/refund",
                  json={"items": [{"transaction_item_id": line, "quantity": 2}]}).status_code == 200
    assert db.session.get(Transaction, txn).status == "refunded"
    row = today_row(shop["accountant"])
    assert (row["sales_count"], row["gross_sales"], row["refunds"]) == (1, 400.0, 400.0)
    assert (row["revenue"], row["tax"], row["cogs"], row["gross_profit"]) == (0.0, 0.0, 0.0, 0.0)


def test_voided_sales_never_count(shop):
    txn = sell(shop["cashier"], [(shop["amox"], 1)])
    t = db.session.get(Transaction, txn)
    t.status = "voided"
    db.session.commit()
    assert today_row(shop["accountant"])["sales_count"] == 0


def test_days_are_nairobi_days(shop):
    late = sell(shop["cashier"], [(shop["amox"], 1)])
    early = sell(shop["cashier"], [(shop["zinc"], 1)])
    set_time(late, datetime(2026, 11, 20, 22, 30))     # 01:30 on the 21st in Nairobi
    set_time(early, datetime(2026, 11, 20, 20, 59))    # 23:59 on the 20th in Nairobi
    days = profit.daily(date(2026, 11, 20), date(2026, 11, 21))
    assert [(d["date"], d["revenue"]) for d in days] == [("2026-11-20", 150.0),
                                                          ("2026-11-21", 200.0)]


def test_every_day_in_the_range_has_a_row(shop):
    sell(shop["cashier"], [(shop["amox"], 1)])
    d_to = today_nairobi()
    days = profit.daily(d_to - timedelta(days=6), d_to)
    assert len(days) == 7 and [d["sales_count"] for d in days] == [0] * 6 + [1]
    assert days[0]["gross_margin_pct"] is None and days[0]["revenue"] == 0.0


def test_payment_methods_split_revenue_including_bank(shop):
    c = shop["cashier"]
    sell(c, [(shop["amox"], 1)])                                     # cash 200
    bank = new_sale(c)
    add(c, bank, shop["amox"])
    assert c.post("/payments/bank", json={"transaction_id": bank, "bank_name": "Equity",
                                          "reference": "FT9001", "amount": 200}).status_code == 201
    assert c.post(f"/pos/transaction/{bank}/complete",
                  json={"payment_method": "bank"}).status_code == 200  # bank 200
    stk = new_sale(c)
    add(c, stk, shop["amox"])
    pay_stk(stk, "QAB1234567")
    assert c.post(f"/pos/transaction/{stk}/complete",
                  json={"payment_method": "mpesa"}).status_code == 200  # mpesa 200
    customer = Customer(name="Duka Moja", phone_number="0722000001", code="C1",
                        credit_limit=D("1000"))
    db.session.add(customer)
    db.session.commit()
    credit = new_sale(c, customer_id=customer.id)
    add(c, credit, shop["zinc"])
    assert c.post(f"/pos/transaction/{credit}/complete",
                  json={"payment_method": "credit"}).status_code == 200  # credit 150
    sell(c, [(shop["amox"], 1)], method="split", cash_amount=120, card_amount=80)
    row = today_row(shop["accountant"])
    assert row["by_method"] == {"cash": 320.0, "mpesa": 200.0, "bank": 200.0, "credit": 150.0,
                                "card": 80.0, "other": 0.0}
    assert sum(row["by_method"].values()) == row["revenue"] == 950.0


def test_daily_totals_equal_the_sum_of_sales(shop):
    c = shop["cashier"]
    for lines in ([(shop["amox"], 1)], [(shop["zinc"], 2)], [(shop["amox"], 2), (shop["zinc"], 1)]):
        sell(c, lines)
    d_to = today_nairobi()
    data = shop["accountant"].get(f"/reports/daily?from={d_to - timedelta(days=2)}&to={d_to}").get_json()
    sales = Transaction.query.filter(Transaction.status == "completed").all()
    expected = sum((t.total - t.tax_amount for t in sales), D("0"))
    assert data["totals"]["revenue"] == float(expected) == sum(d["revenue"] for d in data["days"])
    assert data["totals"]["sales_count"] == 3
    assert data["totals"]["cogs"] == sum(d["cogs"] for d in data["days"])


def test_net_profit_is_gross_profit_less_expenses(shop):
    sell(shop["cashier"], [(shop["amox"], 3)])                       # gross 430
    acc = shop["accountant"]
    for amount, cat in ((100, "utilities"), (50, "transport")):
        assert acc.post("/reports/expenses", json={"category": cat, "amount": amount,
                                                   "method": "mpesa"}).status_code == 201
    row = today_row(acc)
    assert (row["gross_profit"], row["expenses"], row["net_profit"]) == (430.0, 150.0, 280.0)
    summary = acc.get(f"/reports/daily?from={today_nairobi()}&to={today_nairobi()}").get_json()
    assert summary["expenses_by_category"] == {"transport": 50.0, "utilities": 100.0}
    assert summary["by_category"][0]["category"] == "Uncategorised"


def test_top_items_and_categories_rank_by_gross_profit(shop):
    from models import Category
    cat = Category(name="Antibiotics")
    db.session.add(cat)
    db.session.flush()
    db.session.get(InventoryItem, shop["amox"]).category_id = cat.id
    db.session.commit()
    sell(shop["cashier"], [(shop["amox"], 1), (shop["zinc"], 3)])  # amox 150 gp, zinc 270 gp
    s = profit.summary(today_nairobi(), today_nairobi())
    assert [(i["name"], i["gross_profit"]) for i in s["top_items"]] == [("Zinc", 270.0),
                                                                        ("Amoxil 500mg", 150.0)]
    assert {c["category"]: c["gross_profit"] for c in s["by_category"]} == {
        "Uncategorised": 270.0, "Antibiotics": 150.0}


def test_monthly_net_profit(shop):
    sell(shop["cashier"], [(shop["amox"], 1)])
    last_month = today_nairobi().replace(day=1) - timedelta(days=1)
    db.session.add(Expense(spent_on=last_month, category="rent", amount=D("300"), method="bank"))
    db.session.commit()
    months = shop["accountant"].get("/reports/monthly").get_json()["months"]
    assert len(months) == 12
    assert months[-1]["month"] == today_nairobi().strftime("%Y-%m")
    assert (months[-1]["revenue"], months[-1]["net_profit"]) == (200.0, 150.0)
    assert (months[-2]["expenses"], months[-2]["net_profit"]) == (300.0, -300.0)


# --- the API around the numbers -----------------------------------------------------------

def test_daily_csv(shop):
    sell(shop["cashier"], [(shop["amox"], 1)])
    d_to = today_nairobi()
    r = shop["accountant"].get(f"/reports/daily?from={d_to - timedelta(days=1)}&to={d_to}&format=csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "attachment" in r.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    assert [row["date"] for row in rows] == [str(d_to - timedelta(days=1)), str(d_to), "TOTAL"]
    assert rows[1]["revenue"] == "200.0" and rows[1]["cash"] == "200.0" and rows[2]["cogs"] == "50.0"


def test_daily_range_rules(shop):
    acc = shop["accountant"]
    assert len(acc.get("/reports/daily").get_json()["days"]) == 30
    assert acc.get("/reports/daily?from=2025-01-01&to=2026-01-02").status_code == 400
    assert acc.get("/reports/daily?from=2025-01-01&to=2026-01-01").status_code == 200   # 366 days
    assert acc.get("/reports/daily?from=2026-02-01&to=2026-01-01").status_code == 400
    assert acc.get("/reports/daily?from=yesterday").status_code == 400
    assert acc.get("/reports/reconciliation?date=13/11/2026").status_code == 400


def test_finance_roles_only(app, shop):
    acc, cashier = shop["accountant"], shop["cashier"]
    for url in ("/reports/daily", "/reports/monthly", "/reports/expenses",
                "/reports/reconciliation", f"/reports/reconciliation/shift/{shop['shift']}"):
        assert acc.get(url).status_code == 200, url
        assert cashier.get(url).status_code == 403, url
        assert app.test_client().get(url).status_code == 401, url
    assert cashier.post("/reports/expenses", json={"category": "rent", "amount": 1}).status_code == 403
    for url in ("/reports-dashboard", "/money-ui/expenses", "/money-ui/reconciliation"):
        assert cashier.get(url).status_code == 302, url


def test_pages_render_for_an_accountant(shop):
    acc = shop["accountant"]
    pages = {"/reports-dashboard": ["Daily report", "/reports/daily", "chart.js@4.4.3"],
             "/money-ui/expenses": ["Add an expense", "/reports/expenses"],
             "/money-ui/reconciliation": ["Reconciliation", "/reports/reconciliation"]}
    for url, needles in pages.items():
        r = acc.get(url)
        html = r.get_data(as_text=True)
        assert r.status_code == 200, url
        for needle in needles:
            assert needle in html, (url, needle)
    nav = acc.get("/dashboard").get_data(as_text=True)
    assert "/money-ui/expenses" in nav and "/money-ui/reconciliation" in nav
    assert "/reports/by_day" not in acc.get("/reports-dashboard").get_data(as_text=True)


# --- expenses ---------------------------------------------------------------------------

def test_expense_validation(shop):
    acc = shop["accountant"]
    future = (today_nairobi() + timedelta(days=1)).isoformat()
    bad = [{"category": "beer", "amount": 10},
           {"category": "rent", "amount": 10, "method": "cheque"},
           {"category": "rent", "amount": 0},
           {"category": "rent", "amount": -5},
           {"category": "rent", "amount": "abc"},
           {"category": "rent", "amount": "NaN"},
           {"category": "rent", "amount": 10, "spent_on": future},
           {"category": "rent", "amount": 10, "spent_on": "2026-13-01"}]
    for body in bad:
        assert acc.post("/reports/expenses", json=body).status_code == 400, body
    assert Expense.query.count() == 0
    r = acc.post("/reports/expenses", json={"category": "rent", "amount": "15000.50",
                                            "method": "bank", "note": "Nov rent",
                                            "spent_on": "2026-11-01"})
    assert r.status_code == 201
    e = r.get_json()["expense"]
    assert (e["amount"], e["spent_on"], e["shift_id"]) == (15000.5, "2026-11-01", None)
    listing = acc.get("/reports/expenses?from=2026-11-01&to=2026-11-30&category=rent").get_json()
    assert listing["total"] == 15000.5 and listing["by_month"][0]["by_category"] == {"rent": 15000.5}
    assert acc.get("/reports/expenses?from=2026-11-01&to=2026-11-30&method=cash").get_json()["total"] == 0
    assert acc.delete(f"/reports/expenses/{e['id']}").status_code == 200
    assert acc.delete(f"/reports/expenses/{e['id']}").status_code == 404


def test_cash_expense_from_the_drawer_is_a_cash_out(shop):
    admin = shop["admin"]
    shift = admin.post("/pos/shift/open", json={"opening_float": 500}).get_json()["shift"]["id"]
    r = admin.post("/reports/expenses", json={"category": "transport", "amount": 300,
                                              "method": "cash", "pay_from_drawer": True,
                                              "note": "boda"})
    assert r.status_code == 201 and r.get_json()["warning"] is None
    mv = CashMovement.query.filter_by(shift_id=shift).one()
    assert (mv.movement_type, mv.amount) == ("cash_out", D("300.00"))
    assert Expense.query.one().shift_id == shift
    rec = reconciliation.shift_report(db.session.get(Shift, shift))
    assert (rec["cash_out"], rec["expected_cash"]) == (300.0, 200.0)
    # Deleting it while the shift is open puts the cash back in the drawer.
    assert admin.delete(f"/reports/expenses/{Expense.query.one().id}").status_code == 200
    assert CashMovement.query.filter_by(shift_id=shift).count() == 0
    # Once the shift has closed, a drawer expense stays.
    admin.post("/reports/expenses", json={"category": "supplies", "amount": 50,
                                          "method": "cash", "pay_from_drawer": True})
    assert admin.post("/pos/shift/close", json={"closing_cash": 450}).status_code == 200
    assert admin.delete(f"/reports/expenses/{Expense.query.one().id}").status_code == 409
    # An accountant has no shift: recorded, with a warning, and no drawer touched.
    r = shop["accountant"].post("/reports/expenses", json={"category": "rent", "amount": 10,
                                                           "method": "cash", "pay_from_drawer": True})
    assert r.status_code == 201 and r.get_json()["warning"] and r.get_json()["cash_movement"] is None
    assert CashMovement.query.count() == 1


# --- reconciliation ---------------------------------------------------------------------

def test_shift_expected_cash_and_variance(shop, sms_outbox):
    c = shop["cashier"]
    sell(c, [(shop["amox"], 3)])                                     # 600 cash
    c.post("/pos/shift/cash_movement", json={"type": "cash_in", "amount": 200})
    c.post("/pos/shift/cash_movement", json={"type": "cash_out", "amount": 100})
    live = shop["accountant"].get(f"/reports/reconciliation/shift/{shop['shift']}").get_json()
    assert (live["expected_cash"], live["counted_cash"], live["variance"]) == (1700.0, None, None)
    r = c.post("/pos/shift/close", json={"closing_cash": 1650})
    assert r.get_json()["z_report"]["expected_cash"] == 1700.0
    rec = shop["accountant"].get(f"/reports/reconciliation/shift/{shop['shift']}").get_json()
    assert (rec["opening_float"], rec["cash_sales"], rec["cash_in"], rec["cash_out"]) == \
        (1000.0, 600.0, 200.0, 100.0)
    assert (rec["expected_cash"], rec["counted_cash"], rec["variance"]) == (1700.0, 1650.0, -50.0)
    assert rec["over_threshold"] is False and rec["status"] == "closed"
    day = shop["accountant"].get(f"/reports/reconciliation?date={today_nairobi()}").get_json()
    [cashier] = day["cashiers"]
    assert cashier["cashier_name"] == "Cashier Wanjiru" and cashier["variance"] == -50.0
    assert [s["shift_id"] for s in cashier["shifts"]] == [shop["shift"]]
    assert day["totals"]["expected_cash"] == 1700.0 and day["variance_alert"] == 500.0
    assert SmsMessage.query.filter_by(kind="cash_variance").count() == 0
    other = (today_nairobi() - timedelta(days=3)).isoformat()
    assert shop["accountant"].get(f"/reports/reconciliation?date={other}").get_json()["cashiers"] == []


def test_split_sale_cash_is_in_the_drawer(shop):
    c = shop["cashier"]
    sell(c, [(shop["amox"], 1)], method="split", cash_amount=120, card_amount=80)
    z = c.post("/pos/shift/close", json={"closing_cash": 1120}).get_json()["z_report"]
    assert (z["cash_sales"], z["expected_cash"]) == (120.0, 1120.0)
    assert db.session.get(Shift, shop["shift"]).cash_variance == D("0.00")


def test_mpesa_and_bank_checks(shop):
    c = shop["cashier"]
    stk = new_sale(c)
    add(c, stk, shop["amox"])
    pay_stk(stk, "QAB7654321")
    assert c.post(f"/pos/transaction/{stk}/complete", json={"payment_method": "mpesa"}).status_code == 200
    typed = new_sale(c)
    add(c, typed, shop["zinc"])
    assert c.post("/payments/manual", json={"transaction_id": typed, "receipt": "QTY1234567",
                                            "amount": 150}).status_code == 201
    assert c.post(f"/pos/transaction/{typed}/complete", json={"payment_method": "mpesa"}).status_code == 200
    bank = new_sale(c)
    add(c, bank, shop["amox"])
    c.post("/payments/bank", json={"transaction_id": bank, "bank_name": "KCB",
                                   "reference": "FT7777", "amount": 200})
    assert c.post(f"/pos/transaction/{bank}/complete", json={"payment_method": "bank"}).status_code == 200
    acc = shop["accountant"]
    rec = acc.get(f"/reports/reconciliation/shift/{shop['shift']}").get_json()
    assert (rec["mpesa_system"], rec["mpesa_confirmed"], rec["mpesa_unverified"]) == (350.0, 200.0, 150.0)
    assert rec["mpesa_unexplained"] == 0.0
    assert [x["receipt"] for x in rec["mpesa_unverified_codes"]] == ["QTY1234567"]
    assert (rec["bank_refs"], rec["bank_total"], rec["bank_unverified_count"]) == (1, 200.0, 1)
    assert rec["bank_unverified_refs"][0]["reference"] == "FT7777"
    assert rec["cash_sales"] == 0.0 and rec["expected_cash"] == 1000.0
    bank_payment = Payment.query.filter_by(method="bank").one().id
    assert acc.post(f"/payments/{bank_payment}/verify").status_code == 200
    rec = acc.get(f"/reports/reconciliation/shift/{shop['shift']}").get_json()
    assert (rec["bank_unverified_count"], rec["bank_unverified"]) == (0, 0.0)
    # A code the accountant rejects leaves the M-Pesa sale unexplained.
    typed_payment = Payment.query.filter_by(method="mpesa_manual").one().id
    assert acc.post(f"/payments/{typed_payment}/reject", json={"reason": "no"}).status_code == 200
    rec = acc.get(f"/reports/reconciliation/shift/{shop['shift']}").get_json()
    assert (rec["mpesa_unverified"], rec["mpesa_unexplained"]) == (0.0, 150.0)


def test_debt_paid_by_mpesa_is_not_the_shifts_mpesa_sales(shop):
    from models import Invoice
    c = shop["cashier"]
    customer = Customer(name="Duka Mbili", phone_number="0722000002", code="C2",
                        credit_limit=D("1000"))
    db.session.add(customer)
    db.session.commit()
    txn = new_sale(c, customer_id=customer.id)
    add(c, txn, shop["amox"])
    assert c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "credit"}).status_code == 200
    invoice = Invoice.query.filter_by(transaction_id=txn).one().id
    assert c.post("/payments/manual", json={"invoice_id": invoice, "receipt": "QDEBT12345",
                                            "amount": 200}).status_code == 201
    rec = reconciliation.shift_report(db.session.get(Shift, shop["shift"]))
    assert (rec["mpesa_system"], rec["mpesa_unverified"], rec["mpesa_unexplained"]) == (0.0, 0.0, 0.0)
    assert rec["expected_cash"] == 1000.0
    # Revenue counts it once, as a credit sale.
    assert today_row(shop["accountant"])["by_method"]["credit"] == 200.0


def test_variance_sms_goes_once_and_only_over_the_line(app, shop, sms_outbox):
    c = shop["cashier"]
    sell(c, [(shop["amox"], 1)])                                     # expected 1200
    assert c.post("/pos/shift/close", json={"closing_cash": 600}).status_code == 200   # -600
    msgs = SmsMessage.query.filter_by(kind="cash_variance").all()
    assert [m.dedupe_key for m in msgs] == [f"variance:{shop['shift']}:254711000111"]
    assert "CASH SHORT" in msgs[0].body and "KES 600.00" in msgs[0].body
    assert len(sms_outbox.sent) == 1
    # Asking again (a retry, a second tap) texts nobody.
    reconciliation.alert_variance(db.session.get(Shift, shop["shift"]))
    assert SmsMessage.query.filter_by(kind="cash_variance").count() == 1 and len(sms_outbox.sent) == 1
    # Under the line: no text. Over it but the day shows it in red.
    assert c.post("/pos/shift/open", json={"opening_float": 0}).status_code == 201
    assert c.post("/pos/shift/close", json={"closing_cash": 400}).status_code == 200   # +400
    assert SmsMessage.query.filter_by(kind="cash_variance").count() == 1
    day = shop["accountant"].get("/reports/reconciliation").get_json()
    [cashier] = day["cashiers"]
    assert cashier["over_threshold"] is True and cashier["variance"] == -200.0
    assert [s["over_threshold"] for s in cashier["shifts"]] == [True, False]


def test_a_failing_sms_never_fails_the_close(shop, monkeypatch):
    import services.reports.reconciliation as rec

    def boom(shift):
        raise RuntimeError("outbox down")
    monkeypatch.setattr(rec, "alert_variance", boom)
    r = shop["cashier"].post("/pos/shift/close", json={"closing_cash": 0})
    assert r.status_code == 200
    assert db.session.get(Shift, shop["shift"]).status == "closed"


# --- verticals and tenants --------------------------------------------------------------

def test_a_distributor_gets_the_money_reports(app, login_as, other_tenant):
    c = login_as("accountant", tenant_id=other_tenant)
    assert c.get("/reports/daily").status_code == 200
    assert c.get("/reports/reconciliation").status_code == 200
    assert c.post("/reports/expenses", json={"category": "transport", "amount": 90,
                                             "method": "mpesa"}).status_code == 201
    for url in ("/reports-dashboard", "/money-ui/expenses", "/money-ui/reconciliation"):
        assert c.get(url).status_code == 200, url


def test_an_institution_with_no_sales_gets_a_report(app, login_as):
    set_vertical(app.acme_id, "institution")
    c = login_as("accountant")
    assert c.get("/pos/shift/current").status_code == 404                # no POS here
    c.post("/reports/expenses", json={"category": "supplies", "amount": 250, "method": "bank"})
    data = c.get("/reports/daily").get_json()
    assert len(data["days"]) == 30 and data["totals"]["sales_count"] == 0
    assert (data["totals"]["revenue"], data["totals"]["net_profit"]) == (0.0, -250.0)
    assert data["top_items"] == [] and data["totals"]["gross_margin_pct"] is None
    assert c.get("/reports/reconciliation").get_json()["cashiers"] == []
    for url in ("/reports-dashboard", "/money-ui/expenses", "/money-ui/reconciliation"):
        assert c.get(url).status_code == 200, url


def test_another_business_never_sees_these_numbers(app, shop, login_as, other_tenant):
    sell(shop["cashier"], [(shop["amox"], 3)])
    acc = shop["accountant"]
    expense = acc.post("/reports/expenses", json={"category": "rent", "amount": 999,
                                                  "method": "bank"}).get_json()["expense"]["id"]
    assert today_row(acc)["revenue"] == 600.0
    globex = login_as("accountant", tenant_id=other_tenant)
    row = today_row(globex)
    assert (row["revenue"], row["sales_count"], row["expenses"]) == (0.0, 0, 0.0)
    assert globex.get("/reports/expenses").get_json()["expenses"] == []
    assert globex.delete(f"/reports/expenses/{expense}").status_code == 404
    assert globex.get(f"/reports/reconciliation/shift/{shop['shift']}").status_code == 404
    assert globex.get("/reports/reconciliation").get_json()["cashiers"] == []
    with tenant_scope(other_tenant):
        assert Expense.query.count() == 0
    assert Expense.query.count() == 1


# --- review fixes -----------------------------------------------------------------

def _move_refunds(txn_id, when):
    """Date the refund records (stock movements and credit notes) of a sale."""
    from models import Invoice, InvoiceAdjustment, StockMovement
    ref = db.session.get(Transaction, txn_id).transaction_ref
    for m in StockMovement.query.filter_by(note=f"REFUND {ref}", reason="refund"):
        m.created_at = when
    inv = Invoice.query.filter_by(transaction_id=txn_id).one()
    for adj in InvoiceAdjustment.query.filter_by(invoice_id=inv.id, reason="return"):
        adj.created_at = when
    db.session.commit()


def test_each_refund_takes_its_own_cost_on_its_own_day(shop):
    """Two units at different batch costs returned on two days: each day loses
    the cost of the unit returned that day (70 then 50), not half of 120."""
    c = shop["cashier"]
    txn = sell(c, [(shop["amox"], 3)])          # AMX-1 x2 at 50, AMX-2 x1 at 70
    today = today_nairobi()
    day1, day2 = today - timedelta(days=2), today - timedelta(days=1)
    set_time(txn, datetime.combine(day1 - timedelta(days=1), datetime.min.time()) + timedelta(hours=9))
    line = TransactionItem.query.filter_by(transaction_id=txn).one().id
    c.post(f"/pos/transaction/{txn}/refund", json={"items": [{"transaction_item_id": line,
                                                              "quantity": 1}]})
    _move_refunds(txn, datetime.combine(day1, datetime.min.time()) + timedelta(hours=9))
    c.post(f"/pos/transaction/{txn}/refund", json={"items": [{"transaction_item_id": line,
                                                              "quantity": 1}]})
    from models import StockMovement
    ref = db.session.get(Transaction, txn).transaction_ref
    newest = (StockMovement.query.filter_by(note=f"REFUND {ref}")
              .order_by(StockMovement.id.desc()).first())
    newest.created_at = datetime.combine(day2, datetime.min.time()) + timedelta(hours=9)
    db.session.commit()
    days = {r["date"]: r for r in shop["accountant"].get(
        f"/reports/daily?from={day1}&to={day2}").get_json()["days"]}
    assert days[day1.isoformat()]["cogs"] == -70.0
    assert days[day2.isoformat()]["cogs"] == -50.0


def test_past_profit_keeps_the_cost_price_of_the_day_it_sold(shop):
    """Zinc's batch has no cost; the sale used the item cost (60). Raising the
    cost price later must not change that sale's profit."""
    txn = sell(shop["cashier"], [(shop["zinc"], 1)])
    assert TransactionItem.query.filter_by(transaction_id=txn).one().unit_cost == D("60.00")
    before = today_row(shop["accountant"])["cogs"]
    item = db.session.get(InventoryItem, shop["zinc"])
    item.cost_price = D("95")
    db.session.commit()
    assert today_row(shop["accountant"])["cogs"] == before == 60.0
