"""Activity log, staff performance and the roles-and-permissions page."""
import csv
import io
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope, unscoped
from models import AuditLog, InventoryItem, Transaction, User, db
from services import otp_service, stock
from tests.test_pos_owner import add, client_as, new_sale

D = Decimal


def audit(tenant_id, action, user_id=None, when=None, **details):
    with tenant_scope(tenant_id):
        row = AuditLog(action=action, user_id=user_id, new_value=details or None,
                       ip_address="10.1.1.1")
        if when is not None:
            row.created_at = when
        db.session.add(row)
        db.session.commit()
        return row.id


def staff(tenant_id, email, role="cashier", name=None):
    with tenant_scope(tenant_id):
        u = User(email=email, name=name or email.split("@")[0], role=role, status="approved")
        db.session.add(u)
        db.session.commit()
        return u.id


# --- activity log --------------------------------------------------------------

def test_activity_is_this_business_only(login_as, app, other_tenant):
    mine = staff(app.acme_id, "wanjiru@acme.co", name="Wanjiru")
    theirs = staff(other_tenant, "otieno@globex.co")
    audit(app.acme_id, "transaction.complete", mine, ref="T-1")
    audit(other_tenant, "transaction.complete", theirs, ref="SECRET-GLOBEX")
    audit(other_tenant, "login.failed", theirs, email="otieno@globex.co")
    data = login_as("admin").get("/users/activity").get_json()
    assert [r["action"] for r in data["rows"]] == ["transaction.complete"]
    row = data["rows"][0]
    assert row["user_name"] == "Wanjiru" and row["action_label"] == "Sale completed"
    assert row["ip_address"] == "10.1.1.1"
    assert "SECRET-GLOBEX" not in str(data) and "otieno" not in str(data)
    assert data["failed_logins_24h"]["count"] == 0


def test_platform_failures_with_no_business_never_show(client, login_as, app):
    client.post("/auth/login/local", data={"email": "ghost@x.co", "password": "nope-nope"})
    with unscoped():
        assert AuditLog.query.filter_by(action="login.failed").count() == 1
    data = login_as("admin").get("/users/activity").get_json()
    assert data["rows"] == [] and data["failed_logins_24h"]["count"] == 0


def test_activity_filters(login_as, app):
    a = staff(app.acme_id, "a@acme.co")
    b = staff(app.acme_id, "b@acme.co")
    audit(app.acme_id, "discount.line", a)
    audit(app.acme_id, "discount.override", b)
    audit(app.acme_id, "transaction.void", a)
    audit(app.acme_id, "login", a)
    # 22:30 UTC on 1 March is 01:30 on 2 March in Nairobi.
    audit(app.acme_id, "payment.manual", b, when=datetime(2026, 3, 1, 22, 30))
    admin = login_as("admin")

    def actions(query):
        r = admin.get("/users/activity?" + query)
        assert r.status_code == 200, r.get_json()
        return sorted(x["action"] for x in r.get_json()["rows"])

    assert actions(f"user_id={a}") == ["discount.line", "login", "transaction.void"]
    assert actions("action=discount.*") == ["discount.line", "discount.override"]
    assert actions(f"action=discount.*&user_id={b}") == ["discount.override"]
    assert actions("action=login") == ["login"]
    assert actions("action=discount") == []                 # exact unless it ends in *
    assert actions("action=discount_*") == []               # _ is not a wildcard
    assert actions("from=2026-03-02&to=2026-03-02") == ["payment.manual"]
    assert actions("from=2026-03-01&to=2026-03-01") == []
    assert admin.get("/users/activity?from=2026-13-01").status_code == 400
    assert admin.get("/users/activity?user_id=abc").status_code == 400


def test_activity_pages_newest_first(login_as, app):
    base = datetime(2026, 5, 1, 8, 0)
    for n in range(55):
        audit(app.acme_id, "shift.open", when=base + timedelta(minutes=n), n=n)
    admin = login_as("admin")
    first = admin.get("/users/activity").get_json()
    assert (first["total"], first["pages"], len(first["rows"])) == (55, 2, 50)
    assert first["rows"][0]["details"]["n"] == 54
    second = admin.get("/users/activity?page=2").get_json()
    assert [r["details"]["n"] for r in second["rows"]] == [4, 3, 2, 1, 0]


def test_failed_logins_summary(client, login_as, app):
    uid = staff(app.acme_id, "clerk@acme.co")
    with tenant_scope(app.acme_id):
        db.session.get(User, uid).set_password("right-pass-1")
        db.session.commit()
    for _ in range(3):
        client.post("/auth/login/local", data={"email": "clerk@acme.co", "password": "bad-pass"})
    audit(app.acme_id, "login.failed", uid, when=datetime.utcnow() - timedelta(hours=30),
          email="clerk@acme.co")                          # too old to count
    summary = login_as("admin").get("/users/activity").get_json()["failed_logins_24h"]
    assert summary["count"] == 3
    assert summary["by_user"][0]["user_id"] == uid and summary["by_user"][0]["count"] == 3
    assert summary["by_user"][0]["email"] == "clerk@acme.co"


def test_activity_hides_secret_looking_values(login_as, app):
    audit(app.acme_id, "discount.override", otp="123456", manager_otp_code="999",
          percent=15)
    row = login_as("admin").get("/users/activity").get_json()["rows"][0]
    assert row["details"] == {"percent": 15}


@pytest.mark.parametrize("role", ["cashier", "accountant", "pharmacist", "user"])
def test_activity_is_for_admins(login_as, role):
    assert login_as(role).get("/users/activity").status_code == 403


# --- performance -------------------------------------------------------------------

@pytest.fixture
def shop(app, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "TAX_RATE", 0)
    item = InventoryItem(name="Amoxil 500mg", price=D("200"))
    other = InventoryItem(name="Panadol", price=D("100"))
    db.session.add_all([item, other])
    db.session.flush()
    stock.receive(item, 20, batch_number="AMX-7", expiry_date=date(2027, 3, 1))
    stock.receive(other, 20, batch_number="PND-1", expiry_date=date(2027, 9, 1))
    db.session.commit()
    ids = {"item": item.id, "other": other.id}       # requests empty the identity map
    admin = client_as(app, "admin", phone="0711000111")
    cashier = client_as(app, "cashier")
    pharmacist = client_as(app, "pharmacist")
    assert cashier.post("/pos/shift/open", json={"opening_float": 0}).status_code in (200, 201)
    return {"admin": admin, "cashier": cashier, "pharmacist": pharmacist, **ids}


def _pay(c, txn):
    r = c.post(f"/pos/transaction/{txn}/complete", json={"payment_method": "cash"})
    assert r.status_code == 200, r.get_json()


def _person(report, user_id):
    return next(p for p in report["people"] if p["user_id"] == user_id)


def test_performance_splits_cashier_and_salesperson(app, shop):
    c, p = shop["cashier"], shop["pharmacist"]
    # 1: served by the pharmacist, 10% line discount: 200 - 20 = 180.
    t1 = new_sale(c, served_by_id=p.user_id)
    assert add(c, t1, shop["item"], discount_pct=10).status_code == 201
    _pay(c, t1)
    # 2: the cashier serves, sale discount KES 10 on 100.
    t2 = new_sale(c)
    add(c, t2, shop["other"])
    assert c.post(f"/pos/transaction/{t2}/apply_discount",
                  json={"type": "flat", "value": 10}).status_code == 200
    _pay(c, t2)
    # 3: served by the pharmacist, voided before payment.
    t3 = new_sale(c, served_by_id=p.user_id)
    add(c, t3, shop["other"])
    assert c.post(f"/pos/transaction/{t3}/void", json={"reason": "changed mind"}).status_code == 200
    # 4: served by the pharmacist, paid, then fully refunded.
    t4 = new_sale(c, served_by_id=p.user_id)
    line = add(c, t4, shop["item"]).get_json()["line"]["id"]
    _pay(c, t4)
    r = c.post(f"/pos/transaction/{t4}/refund",
               json={"items": [{"transaction_item_id": line, "quantity": 1}], "reason": "wrong"})
    assert r.status_code == 200
    # Close the drawer KES 50 short.
    shift = c.get("/pos/shift/current").get_json()["shift"]
    assert shift is not None
    closed = c.post("/pos/shift/close", json={"closing_cash": 0}).get_json()
    variance = closed["z_report"]["shift"]["cash_variance"]
    assert variance < 0

    r = shop["admin"].get("/staff/performance")
    assert r.status_code == 200
    report = r.get_json()
    cashier, pharmacist = _person(report, c.user_id), _person(report, p.user_id)

    assert cashier["as_cashier"] == {
        "sales_count": 3, "sales_value": 470.0, "average_basket": 156.67, "discounts": 30.0,
        "voids_count": 1, "voids_value": 100.0, "refunds_count": 1, "refunds_value": 200.0,
        "shifts_closed": 1, "shifts_with_variance": 1, "cash_variance_total": variance,
    }
    assert cashier["as_salesperson"] == {
        "sales_count": 1, "sales_value": 90.0, "average_basket": 90.0, "discounts": 10.0,
        "voids_count": 0, "voids_value": 0.0, "refunds_count": 0, "refunds_value": 0.0,
    }
    assert pharmacist["as_salesperson"] == {
        "sales_count": 2, "sales_value": 380.0, "average_basket": 190.0, "discounts": 20.0,
        "voids_count": 1, "voids_value": 100.0, "refunds_count": 1, "refunds_value": 200.0,
    }
    assert pharmacist["as_cashier"]["sales_count"] == 0
    assert pharmacist["name"] == "Pharmacist Wanjiru" and pharmacist["role"] == "pharmacist"

    csv_r = shop["admin"].get("/staff/performance?format=csv")
    assert csv_r.status_code == 200 and csv_r.mimetype == "text/csv"
    rows = list(csv.DictReader(io.StringIO(csv_r.get_data(as_text=True))))
    ph = next(x for x in rows if x["person"] == "Pharmacist Wanjiru"
              and x["capacity"] == "salesperson")
    assert (ph["sales_count"], ph["sales_value"], ph["voids_count"]) == ("2", "380.00", "1")


def test_completed_sale_void_counts_for_both(app, shop):
    c, p = shop["cashier"], shop["pharmacist"]
    t = new_sale(c, served_by_id=p.user_id)
    add(c, t, shop["item"])
    _pay(c, t)
    assert c.post(f"/pos/transaction/{t}/void", json={}).status_code == 202
    code = otp_service.peek_otp(f"void:txn:{t}")
    assert c.post(f"/pos/transaction/{t}/void", json={"otp": code}).status_code == 200
    report = shop["admin"].get("/staff/performance").get_json()
    assert _person(report, c.user_id)["as_cashier"]["voids_value"] == 200.0
    assert _person(report, p.user_id)["as_salesperson"]["voids_count"] == 1
    assert _person(report, p.user_id)["as_salesperson"]["sales_count"] == 0   # voided: not a sale


def test_performance_uses_nairobi_days(app, shop):
    c = shop["cashier"]
    t = new_sale(c)
    add(c, t, shop["other"])
    _pay(c, t)
    with tenant_scope(app.acme_id):
        db.session.get(Transaction, t).created_at = datetime(2026, 1, 1, 22, 0)   # 01:00 on 2 Jan EAT
        db.session.commit()
    admin = shop["admin"]
    jan2 = admin.get("/staff/performance?from=2026-01-02&to=2026-01-02").get_json()
    assert _person(jan2, c.user_id)["as_cashier"]["sales_count"] == 1
    jan1 = admin.get("/staff/performance?from=2026-01-01&to=2026-01-01").get_json()
    assert all(x["as_cashier"]["sales_count"] == 0 for x in jan1["people"])
    assert admin.get("/staff/performance?from=2026-02-01&to=2026-01-01").status_code == 400
    assert admin.get("/staff/performance?from=bad").status_code == 400


def test_performance_is_this_business_only(app, shop, other_tenant):
    c = shop["cashier"]
    t = new_sale(c)
    add(c, t, shop["other"])
    _pay(c, t)
    globex_admin = client_as_other(app, other_tenant)
    report = globex_admin.get("/staff/performance").get_json()
    assert report["people"] == []


def client_as_other(app, tenant_id):
    uid = staff(tenant_id, "boss@globex.co", role="admin")
    c = app.test_client()
    with c.session_transaction() as sess:
        sess.update(profile={"email": "boss@globex.co"}, role="admin", user_id=uid,
                    tenant_id=tenant_id)
    return c


def test_accountant_reads_performance_cashier_cannot(login_as):
    assert login_as("accountant").get("/staff/performance").status_code == 200
    assert login_as("superuser").get("/staff/performance").status_code == 200
    for role in ("cashier", "pharmacist", "stock_clerk", "user"):
        assert login_as(role).get("/staff/performance").status_code == 403


def test_performance_needs_login(client):
    assert client.get("/staff/performance").status_code == 401


# --- roles and permissions ---------------------------------------------------------

def _allowed_cells(html, role):
    return len(re.findall(rf'data-role="{role}" data-allowed="yes"', html))


def test_permission_matrix_is_read_from_the_role_tuples(login_as, monkeypatch):
    import auth.auth_middleware as mw
    from auth.permissions import CAPABILITIES
    admin = login_as("admin")
    html = admin.get("/staff-ui/permissions").get_data(as_text=True)
    finance_rows = sum(1 for _, source in CAPABILITIES if source == "FINANCE_ROLES")
    assert finance_rows >= 1
    before = _allowed_cells(html, "cashier")
    assert _allowed_cells(html, "accountant") == finance_rows

    monkeypatch.setattr(mw, "FINANCE_ROLES", mw.FINANCE_ROLES + ("cashier",))
    html = admin.get("/staff-ui/permissions").get_data(as_text=True)
    assert _allowed_cells(html, "cashier") == before + finance_rows

    monkeypatch.setattr(mw, "FINANCE_ROLES", ("admin", "superuser"))
    html = admin.get("/staff-ui/permissions").get_data(as_text=True)
    assert _allowed_cells(html, "accountant") == 0


def test_matrix_matches_the_tuples_row_by_row():
    import auth.auth_middleware as mw
    from auth.permissions import CAPABILITIES, matrix
    for (capability, source), row in zip(CAPABILITIES, matrix()["rows"]):
        expected = set(getattr(mw, source)) if isinstance(source, str) else set(source)
        assert {r for r, ok in row["allowed"].items() if ok} == expected, capability


def test_every_role_has_a_description():
    from auth.permissions import ROLE_DESCRIPTIONS
    from models import ALL_ROLES
    assert set(ROLE_DESCRIPTIONS) == set(ALL_ROLES)


@pytest.mark.parametrize("url", ["/staff-ui/permissions", "/staff-ui/activity",
                                 "/staff-ui/performance", "/users-ui"])
@pytest.mark.parametrize("role", ["admin", "superuser"])
def test_staff_pages_open_for_admins(login_as, url, role):
    r = login_as(role).get(url)
    assert r.status_code == 200, url


def test_nav_lists_the_staff_pages(login_as):
    html = login_as("admin").get("/dashboard").get_data(as_text=True)
    for label in ("Roles &amp; permissions", "Performance", "Activity log"):
        assert label in html


def test_accountant_sees_performance_not_admin_pages(login_as):
    acc = login_as("accountant")
    assert acc.get("/staff-ui/performance").status_code == 200
    assert acc.get("/staff-ui/permissions").status_code == 302
    assert acc.get("/staff-ui/activity").status_code == 302
