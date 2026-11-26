"""The owner's 'Today' panel: shaped by role and vertical, counts what needs
attention, and links each item to the screen that deals with it."""
from datetime import date, datetime, timedelta
from decimal import Decimal

from models import Batch, InventoryItem, Payment, db
from services import stock
from services.clock import today_nairobi
from tests.conftest import set_vertical
from tests.test_pos_owner import add, client_as, new_sale

D = Decimal


def _stock_setup():
    today = today_nairobi()
    low = InventoryItem(name="Insulin", price=D("900"), reorder_level=5)
    ok = InventoryItem(name="ORS", price=D("50"), reorder_level=2)
    db.session.add_all([low, ok])
    db.session.flush()
    stock.receive(low, 3, batch_number="INS-1", expiry_date=today + timedelta(days=10))
    stock.receive(ok, 40, batch_number="ORS-OLD", expiry_date=today - timedelta(days=3))
    stock.receive(ok, 40, batch_number="ORS-NEW", expiry_date=today + timedelta(days=400))
    db.session.commit()
    return ok.id


def test_admin_sees_money_stock_and_attention(app, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    _stock_setup()
    admin = client_as(app, "admin")
    admin.post("/pos/shift/open", json={"opening_float": 0})
    item = InventoryItem.query.filter_by(name="Insulin").one().id
    txn = new_sale(admin)
    add(admin, txn, item)
    admin.post("/payments/bank", json={"transaction_id": txn, "bank_name": "KCB",
                                       "reference": "DASH0001", "amount": 900})
    assert admin.post(f"/pos/transaction/{txn}/complete",
                      json={"payment_method": "bank"}).status_code == 200
    s = admin.get("/dashboard/summary").get_json()
    assert s["money"]["revenue"] == 900.0 and s["money"]["sales_count"] == 1
    assert len(s["money"]["last_7_days"]) == 7
    assert s["stock"]["low_stock"] == 1                       # insulin 2 left of level 5
    assert (s["stock"]["expired"], s["stock"]["expiring_30"]) == (1, 1)
    assert s["unverified_payments"] == {"count": 1, "total": 900.0}
    assert "backup" not in s                                  # owner only
    texts = " | ".join(a["text"] for a in s["attention"])
    assert "expired batch" in texts and "typed payment" in texts and "reorder level" in texts
    assert all(a["url"] for a in s["attention"])
    assert s["attention"][0]["level"] == "danger"             # most urgent first


def test_cashier_gets_no_money_or_stock(app):
    s = client_as(app, "cashier").get("/dashboard/summary").get_json()
    assert set(s) == {"date", "vertical", "attention"} and s["attention"] == []


def test_stock_clerk_sees_stock_not_money(app):
    _stock_setup()
    s = client_as(app, "stock_clerk").get("/dashboard/summary").get_json()
    assert "stock" in s and "money" not in s and "unverified_payments" not in s


def test_accountant_sees_money_not_stock(app):
    s = client_as(app, "accountant").get("/dashboard/summary").get_json()
    assert "money" in s and "stock" not in s


def test_owner_sees_backup_status(app, tmp_path):
    app.config["BACKUP_DIR"] = str(tmp_path)
    s = client_as(app, "superuser").get("/dashboard/summary").get_json()
    assert s["backup"]["has_backup"] is False and s["backup"]["stale"] is True
    assert any("backup" in a["text"].lower() for a in s["attention"])


def test_recalled_stock_on_the_shelf_is_urgent(app):
    _stock_setup()
    batch = Batch.query.filter_by(batch_number="ORS-NEW").one()
    stock.recall_batch(batch, reason="supplier notice", reference="PPB-1", user_id=None)
    db.session.commit()
    s = client_as(app, "pharmacist").get("/dashboard/summary").get_json()
    assert s["stock"]["recalled_on_hand"] == 1
    assert s["attention"][0]["text"].startswith("1 recalled batch")


def test_distributor_has_no_pharmacy_tiles_and_institution_no_stock_batches(app):
    _stock_setup()
    set_vertical(app.acme_id, "distributor")
    s = client_as(app, "admin").get("/dashboard/summary").get_json()
    assert "recalled_on_hand" not in s["stock"] and "expiring_30" in s["stock"]
    set_vertical(app.acme_id, "institution")
    s = client_as(app, "admin").get("/dashboard/summary").get_json()
    assert "expiring_30" not in s["stock"] and s["money"]["sales_count"] == 0


def test_dashboard_page_renders_today_panel(app):
    for role in ("superuser", "admin", "accountant", "pharmacist", "stock_clerk", "cashier"):
        html = client_as(app, role).get("/dashboard").get_data(as_text=True)
        assert ("todayPanel" in html) == (role != "cashier"), role


def test_summary_needs_login(client):
    assert client.get("/dashboard/summary").status_code == 401
