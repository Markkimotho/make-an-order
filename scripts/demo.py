"""Build a demo database you can click through, and run it.

    python scripts/demo.py seed [--db PATH]   # fresh demo database (deletes PATH first)
    python scripts/demo.py run  [--db PATH] [--port 5001]

Default PATH is instance/demo.sqlite. Everything goes through the real app
(migrations, services, the POS API), so the demo shows what the code does.

Three businesses, one per vertical. Every demo user's password is
DEMO_PASSWORD below; the sign-in emails are printed by `seed`.

- Nuru Pharmacy (pharmacy, slug nuru): owner, admin, pharmacist, cashier,
  stock clerk and accountant; medicines in batches (one expired, some
  expiring soon, some below reorder level, one controlled drug); a supplier;
  patients, one on a chronic prescription; two weeks of sales by cash,
  M-Pesa code and bank (some still to verify) with discounts and a refund;
  expenses; closed shifts with a small cash variance.
- Baraka Distributors (distributor, slug baraka): owner.
- Moi Girls School (institution, slug moi-girls): owner.

This is for local testing only. It uses SQLite, no Daraja or Africa's
Talking keys, and a fixed password.
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEMO_PASSWORD = "demo-pass-2026"
DEFAULT_DB = ROOT / "instance" / "demo.sqlite"
D = Decimal

STAFF = (  # (role, name)
    ("superuser", "Wanjiku Kamau"),
    ("admin", "Otieno Ochieng"),
    ("pharmacist", "Dr. Amina Hassan"),
    ("cashier", "Brian Mutua"),
    ("stock_clerk", "Grace Njeri"),
    ("accountant", "Peter Kiprop"),
)


def make_app(db_path):
    os.environ.setdefault("TENANT_SECRETS_KEY", "Y2hhbmdlLW1lLWRlbW8ta2V5LTMyLWJ5dGVzLWxvbmc=")
    from app import create_app
    return create_app({
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        "SECRET_KEY": "demo-only-secret",
        "RATELIMIT_STORAGE_URI": "memory://",
        "SINGLE_BUSINESS_INSTALL": False,
        "BACKUP_DIR": str(ROOT / "instance" / "demo-backups"),
    })


def email(slug, role):
    return f"{role.replace('_', '')}@{slug}.demo"


def seed(db_path):
    from flask_migrate import upgrade

    from auth.tenancy import tenant_scope, unscoped
    from models import Tenant, User, db

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    app = make_app(db_path)
    with app.app_context():
        upgrade()
        ids = {}
        with unscoped():
            for name, slug, vertical in (("Nuru Pharmacy", "nuru", "pharmacy"),
                                         ("Baraka Distributors", "baraka", "distributor"),
                                         ("Moi Girls School", "moi-girls", "institution")):
                t = Tenant(name=name, slug=slug, vertical=vertical, subscription_status="active",
                           kra_pin="P051234567X" if vertical == "pharmacy" else None,
                           ppb_premises_license="PPB/PR/2026/0412" if vertical == "pharmacy"
                           else None)
                db.session.add(t)
                db.session.flush()
                ids[slug] = t.id
            db.session.commit()
        users = {}
        for slug, tid in ids.items():
            roles = STAFF if slug == "nuru" else STAFF[:1]
            with tenant_scope(tid):
                for n, (role, name) in enumerate(roles):
                    u = User(email=email(slug, role), name=name, role=role, status="approved",
                             phone_number=f"07120001{n}{len(slug)}",
                             ppb_reg_no="PPB-12345" if role == "pharmacist" else None)
                    u.set_password(DEMO_PASSWORD)
                    db.session.add(u)
                    db.session.flush()
                    users[(slug, role)] = u.id
                db.session.commit()
        with tenant_scope(ids["nuru"]):
            _pharmacy(app, ids["nuru"], users)
    print(f"Demo database: {db_path}")
    print(f"Password for every demo user: {DEMO_PASSWORD}")
    for (slug, role), _ in sorted(users.items()):
        print(f"  {slug:10} {role:12} {email(slug, role)}")


def _client(app, tenant_id, user_id, role):
    c = app.test_client()
    with c.session_transaction() as s:
        s.update(profile={"email": f"{role}@demo"}, role=role, user_id=user_id,
                 tenant_id=tenant_id)
    return c


def _ok(r, *codes):
    assert r.status_code in (codes or (200, 201)), (r.status_code, r.get_json())
    return r.get_json()


def _pharmacy(app, tid, users):
    from models import (Category, Customer, Expense, InventoryItem, Shift, Supplier,
                        Transaction, db)
    from services import stock
    from services.clock import today_nairobi
    from services.controlled import Register

    today = today_nairobi()
    sup = Supplier(name="Harleys Ltd", phone="0722000100", contact_name="Sales desk")
    cats = {n: Category(name=n) for n in ("Antibiotics", "Pain relief", "Chronic care",
                                          "Supplements", "Controlled")}
    db.session.add(sup)
    db.session.add_all(cats.values())
    db.session.flush()
    catalogue = (
        # name, category, price, cost, reorder, markup, batches [(no, qty, expiry days, cost)]
        ("Amoxil 500mg caps x21", "Antibiotics", 350, 210, 10, 40,
         [("AMX-2401", 6, 400, 200), ("AMX-2409", 30, 600, 215)]),
        ("Augmentin 625mg x14", "Antibiotics", 1450, 980, 5, 35, [("AUG-77", 12, 300, 980)]),
        ("Panadol Extra x24", "Pain relief", 120, 70, 20, 50,
         [("PNX-OLD", 8, -5, 65), ("PNX-118", 60, 25, 70)]),
        ("Brufen 400mg x30", "Pain relief", 180, 100, 15, 45, [("BRF-9", 9, 70, 100)]),
        ("Metformin 500mg x60", "Chronic care", 240, 130, 10, 50, [("MET-33", 80, 500, 130)]),
        ("Amlodipine 5mg x30", "Chronic care", 300, 160, 10, 50, [("AML-5", 4, 45, 160)]),
        ("Zinc + Vit C x20", "Supplements", 450, 260, 8, 40, [("ZNC-1", 25, 365, 260)]),
        ("ORS sachet", "Supplements", 40, 15, 30, 100, [("ORS-21", 100, 700, 15)]),
    )
    items = {}
    for name, cat, price, cost, reorder, markup, batches in catalogue:
        it = InventoryItem(name=name, price=D(price), cost_price=D(cost), reorder_level=reorder,
                           category_id=cats[cat].id, supplier_id=sup.id, markup_pct=D(markup),
                           price_mode="manual", is_drug=True,
                           requires_prescription=(cat in ("Antibiotics", "Chronic care")),
                           reorder_quantity=reorder * 3)
        db.session.add(it)
        db.session.flush()
        for number, qty, days, bcost in batches:
            stock.receive(it, qty, batch_number=number, expiry_date=today + timedelta(days=days),
                          unit_cost=D(bcost))
        items[name] = it.id
    morphine = InventoryItem(name="Morphine 10mg/ml inj", price=D("900"), cost_price=D("600"),
                             reorder_level=2, category_id=cats["Controlled"].id, is_drug=True,
                             is_controlled=True, drug_schedule="Schedule 1",
                             requires_prescription=True)
    db.session.add(morphine)
    db.session.flush()
    stock.receive(morphine, 5, batch_number="MOR-3", expiry_date=today + timedelta(days=300),
                  unit_cost=D("600"),
                  register=Register(supplier_name="Harleys Ltd", reference_doc_no="DN-5521"))
    patients = [Customer(name=n, phone_number=p, code=c) for n, p, c in (
        ("Baraka Mwangi", "0733000111", "P00001"), ("Achieng Otieno", "0722123456", "P00002"),
        ("Hassan Ali", "0711987654", "P00003"))]
    db.session.add_all(patients)
    db.session.commit()
    pids = [p.id for p in patients]

    pharm = _client(app, tid, users[("nuru", "pharmacist")], "pharmacist")
    rx_id = _ok(pharm.post("/pharmacy/prescriptions", json={
        "customer_id": pids[0], "doctor_name": "Dr. Otieno (KNH)", "prescriber_reg_no": "A-4471",
        "is_chronic": True, "duration_days": 30, "refills_remaining": 5,
        "items": [{"inventory_item_id": items["Metformin 500mg x60"], "quantity_prescribed": 1,
                   "dosage_instructions": "1 tablet twice a day after meals"},
                  {"inventory_item_id": items["Amlodipine 5mg x30"], "quantity_prescribed": 1,
                   "dosage_instructions": "1 tablet in the morning"}]}))["prescription"]["id"]

    cashier = _client(app, tid, users[("nuru", "cashier")], "cashier")
    plan = (  # days ago, patient index or None, [(item, qty)], method, extra
        # Prescription medicines are dispensed by the pharmacist against the
        # patient's prescription; the cashier sells over-the-counter lines.
        (13, 0, [("Metformin 500mg x60", 1), ("Amlodipine 5mg x30", 1)], "cash",
         {"dispense": True}),
        (12, None, [("Panadol Extra x24", 2), ("ORS sachet", 4)], "cash", {}),
        (10, 1, [("Zinc + Vit C x20", 1)], "mpesa", {"code": "SKA1B2C3D4"}),
        (9, None, [("Brufen 400mg x30", 1)], "cash", {}),
        (7, 2, [("Zinc + Vit C x20", 2), ("Brufen 400mg x30", 1)], "bank",
         {"bank": "Equity", "ref": "FT26318XK2"}),
        (5, None, [("Panadol Extra x24", 3)], "cash", {"discount_pct": 5}),
        (3, 1, [("Zinc + Vit C x20", 2), ("ORS sachet", 2)], "mpesa", {"code": "SKB9Z8Y7X6"}),
        (1, 0, [("Panadol Extra x24", 1)], "cash", {}),
        (0, None, [("Panadol Extra x24", 1), ("Brufen 400mg x30", 1)], "cash", {}),
        (0, 2, [("Zinc + Vit C x20", 1)], "bank", {"bank": "KCB", "ref": "KCB55120098"}),
    )
    for days_ago, who, lines, method, extra in plan:
        role = "pharmacist" if extra.get("dispense") else "cashier"
        seller, seller_id = (pharm if extra.get("dispense") else cashier), users[("nuru", role)]
        _ok(seller.post("/pos/shift/open", json={"opening_float": 2000}), 200, 201, 400)
        body = {"customer_id": pids[who]} if who is not None else {}
        txn = _ok(seller.post("/pos/transaction/new", json=body))["transaction"]["id"]
        for name, qty in lines:
            payload = {"inventory_item_id": items[name], "quantity": qty}
            if extra.get("discount_pct"):
                payload["discount_pct"] = extra["discount_pct"]
            _ok(seller.post(f"/pos/transaction/{txn}/add_item", json=payload))
        if extra.get("dispense"):
            rx = _ok(pharm.get(f"/pharmacy/prescriptions/{rx_id}"))
            _ok(pharm.post(f"/pharmacy/prescriptions/{rx_id}/dispense", json={
                "transaction_id": txn,
                "items": [{"prescription_item_id": it["id"], "quantity_dispensed": 1}
                          for it in rx["items"]]}))
        total = seller.get(f"/pos/transaction/{txn}").get_json()["total"]
        if method == "mpesa":
            _ok(cashier.post("/payments/manual", json={"transaction_id": txn,
                                                       "receipt": extra["code"],
                                                       "amount": total}))
        elif method == "bank":
            _ok(cashier.post("/payments/bank", json={"transaction_id": txn,
                                                     "bank_name": extra["bank"],
                                                     "reference": extra["ref"],
                                                     "amount": total}))
        _ok(seller.post(f"/pos/transaction/{txn}/complete", json={"payment_method": method}))
        when = datetime.utcnow() - timedelta(days=days_ago, hours=2)
        db.session.get(Transaction, txn).created_at = when
        db.session.commit()
        if days_ago:
            # Close the day's shift with a small variance now and then.
            shift = Shift.query.filter_by(status="open", cashier_id=seller_id).first()
            counted = None
            if shift is not None:
                from services.reports.reconciliation import shift_report
                expected = D(str(shift_report(shift)["expected_cash"]))
                counted = expected - (D("150") if days_ago == 5 else 0)
                _ok(seller.post("/pos/shift/close", json={"closing_cash": float(counted)}))
                s = db.session.get(Shift, shift.id)
                s.opened_at = when - timedelta(hours=6)
                s.closed_at = when + timedelta(hours=4)
                db.session.commit()

    # A refund of one ORS sachet from the second sale.
    first_cash = Transaction.query.order_by(Transaction.id).all()[1]
    ors_line = next(li for li in first_cash.items if li.item_name.startswith("ORS"))
    _ok(cashier.post(f"/pos/transaction/{first_cash.id}/refund",
                     json={"items": [{"transaction_item_id": ors_line.id, "quantity": 1}],
                           "reason": "damaged sachet"}))

    for days_ago, cat, amount, note in ((14, "rent", 45000, "December rent"),
                                        (6, "utilities", 3200, "KPLC tokens"),
                                        (2, "salaries", 18000, "Casual staff"),
                                        (0, "transport", 600, "Delivery boda")):
        db.session.add(Expense(spent_on=today - timedelta(days=days_ago), category=cat,
                               amount=D(amount), method="cash" if amount < 5000 else "bank",
                               note=note, created_by=users[("nuru", "accountant")]))
    db.session.commit()


def run(db_path, port):
    app = make_app(db_path)
    app.config["SESSION_COOKIE_SECURE"] = False
    print(f"Open http://localhost:{port}  (password {DEMO_PASSWORD})")
    app.run(host="127.0.0.1", port=port, debug=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("seed", "run"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()
    if args.command == "seed":
        seed(args.db.resolve())
    else:
        run(args.db.resolve(), args.port)


if __name__ == "__main__":
    main()
