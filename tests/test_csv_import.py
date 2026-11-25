"""flask tenants import-csv: validate every row first, report rejects,
strict imports nothing, reruns update instead of duplicating."""
from datetime import date
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import Batch, Customer, InventoryItem, Member, PriceTier, TierPrice, db
from services.csv_import import ImportError_, import_csv

ITEMS = """name,sku,price,cost_price,category,stock,batch_number,expiry_date,is_returnable_container
Unga 2kg,UNGA2,200,150,Flour,40,U-1,2027-03-01,
Gas 6kg,GAS6,1000,700,Gas,10,,,yes
Bad price,BAD1,abc,,,,,,
,NONAME,10,,,,,,
Dup,UNGA2,10,,,,,,
"""


def test_items_import_reports_rejects_and_creates_batches(app):
    report = import_csv("items", ITEMS)
    assert (report.created, report.updated) == (2, 0)
    assert [n for n, _ in report.rejected] == [4, 5, 6]
    assert "not a number" in report.rejected[0][1] and "same sku as line 2" in report.rejected[2][1]
    unga = InventoryItem.query.filter_by(sku="UNGA2").one()
    batch = Batch.query.filter_by(inventory_item_id=unga.id).one()
    assert (unga.stock, batch.batch_number, batch.expiry_date) == (40, "U-1", date(2027, 3, 1))
    assert InventoryItem.query.filter_by(sku="GAS6").one().is_returnable_container


def test_rerun_updates_and_never_touches_stock(app):
    import_csv("items", ITEMS)
    report = import_csv("items", "name,sku,price,stock\nUnga 2kg,UNGA2,210,999\n")
    assert (report.created, report.updated) == (0, 1)
    unga = InventoryItem.query.filter_by(sku="UNGA2").one()
    assert (unga.price, unga.stock) == (Decimal("210.00"), 40)


def test_strict_writes_nothing_when_a_row_is_bad(app):
    report = import_csv("items", ITEMS, strict=True)
    assert report.rejected and not report.written and InventoryItem.query.count() == 0


def test_missing_columns_and_unknown_kind(app):
    with pytest.raises(ImportError_, match="missing columns: price"):
        import_csv("items", "name\nUnga\n")
    with pytest.raises(ImportError_):
        import_csv("cars", "x\n")


def test_customers_by_phone_with_tiers(app):
    text = ("name,phone,code,credit_limit,credit_term_days,price_tier,route_zone\n"
            "Mama Mboga,0712 000 111,MM1,5000,14,Wholesale,Kawangware\n"
            "Duka Two,+254712000222,D2,,30,,\n"
            "Bad,12345,B,,,,\n"
            "Same,254712000111,S,,,,\n")
    report = import_csv("customers", text)
    assert (report.created, [n for n, _ in report.rejected]) == (2, [4, 5])
    mama = Customer.query.filter_by(phone_number="254712000111").one()
    assert (mama.credit_limit, mama.credit_term_days, mama.account_ref) == (Decimal("5000.00"), 14, "MM1")
    assert db.session.get(PriceTier, mama.price_tier_id).name == "Wholesale"
    again = import_csv("customers", "name,phone,credit_limit\nMama Mboga Ltd,0712000111,8000\n")
    assert (again.created, again.updated) == (0, 1)
    assert Customer.query.filter_by(phone_number="254712000111").one().name == "Mama Mboga Ltd"


def test_members_and_tier_prices(app):
    report = import_csv("members", "member_ref,name,group_label,guardian_phone\n"
                                   "f1/023,Brian Otieno,Form 1 East,0712000101\n"
                                   "F1-023,Duplicate,,\n")
    assert report.created == 1 and report.rejected[0][0] == 3
    assert Member.query.one().member_ref == "F1023"
    import_csv("items", ITEMS)
    report = import_csv("tier_prices", "tier,sku,price\nWholesale,UNGA2,180\nWholesale,NOPE,1\n")
    assert report.created == 1 and "no item with SKU NOPE" in report.rejected[0][1]
    assert TierPrice.query.one().price == Decimal("180.00")


def test_cli(app, tmp_path):
    path = tmp_path / "members.csv"
    path.write_text("member_ref,name\nF2/001,Amina Yusuf\n")
    result = app.test_cli_runner().invoke(args=["tenants", "import-csv", "acme", "members",
                                                str(path)])
    assert result.exit_code == 0, result.output
    assert "created 1" in result.output
    with tenant_scope(app.acme_id):
        assert Member.query.filter_by(member_ref="F2001").count() == 1
    bad = tmp_path / "bad.csv"
    bad.write_text("member_ref,name\n,No number\n")
    result = app.test_cli_runner().invoke(args=["tenants", "import-csv", "acme", "members",
                                                str(bad), "--strict"])
    assert result.exit_code == 1 and "nothing written" in result.output
