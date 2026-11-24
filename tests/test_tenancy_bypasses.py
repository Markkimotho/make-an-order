"""Regression tests for the step 2 Codex review (gpt-6.1-sol).

Each test reproduces one way the first version of auth/tenancy.py could be
stepped around. All of them read or wrote another tenant's data before the
fix.
"""
from decimal import Decimal

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, aliased

from auth.tenancy import (CrossTenantWrite, TenantNotBound, UnscopedStatement,
                          tenant_scope, unscoped)
from models import Customer, InventoryItem, StockMovement, db

SECRET = "B SECRET"


@pytest.fixture
def ab(app, other_tenant):
    """Customer + item in acme (A) and globex (B)."""
    ids = {"A": app.acme_id, "B": other_tenant}
    for key, name in (("A", "A customer"), ("B", SECRET)):
        with tenant_scope(ids[key]):
            c = Customer(name=name, phone_number="0712345678", code="C1")
            i = InventoryItem(name=f"{key} item", price=Decimal("5"), sku="S1")
            db.session.add_all([c, i])
            db.session.commit()
            ids[f"{key}_customer"], ids[f"{key}_item"] = c.id, i.id
    return ids


def names_in(tenant_id):
    with tenant_scope(tenant_id):
        return sorted(c.name for c in Customer.query.all())


# 2. aliased bulk update ------------------------------------------------------

def test_bulk_update_on_alias_is_refused(ab):
    with tenant_scope(ab["A"]):
        with pytest.raises(UnscopedStatement):
            db.session.execute(update(aliased(Customer)).values(name="pwned"))
        db.session.rollback()
        with pytest.raises(UnscopedStatement):
            db.session.query(aliased(Customer)).update({"name": "pwned"})
        db.session.rollback()
    assert names_in(ab["B"]) == [SECRET]


# 3. subquery inside a bulk update --------------------------------------------

def test_bulk_update_subquery_cannot_read_other_tenant(ab):
    """Subqueries in bulk writes are refused outright (the second review
    pass showed an aliased join inside one is not reliably filtered)."""
    with tenant_scope(ab["A"]):
        with pytest.raises(UnscopedStatement):
            db.session.execute(
                update(Customer).where(Customer.id == ab["A_customer"]).values(
                    email=select(Customer.name).where(Customer.id == ab["B_customer"])
                    .scalar_subquery()))
        db.session.rollback()
        values = [(c.name, c.email) for c in Customer.query.all()]
    assert values == [("A customer", None)]


# 4. merge(load=False) / refresh of a foreign object -----------------------------

def test_merge_of_foreign_object_is_refused(ab):
    with tenant_scope(ab["B"]):
        foreign = db.session.get(Customer, ab["B_customer"])
        assert foreign.name == SECRET
    with tenant_scope(ab["A"]):
        with pytest.raises(CrossTenantWrite):
            db.session.merge(foreign, load=False)
        assert db.session.get(Customer, ab["B_customer"]) is None
        with pytest.raises(Exception):
            db.session.merge(foreign)          # load=True: B's row is invisible -> treated as new
            db.session.flush()
        db.session.rollback()
    assert names_in(ab["B"]) == [SECRET]


def test_reattaching_foreign_object_with_add_is_refused(ab):
    with tenant_scope(ab["B"]):
        foreign = db.session.get(Customer, ab["B_customer"])
    with tenant_scope(ab["A"]):
        with pytest.raises(CrossTenantWrite):
            db.session.add(foreign)


# 5. ORM-wrapped textual SQL ------------------------------------------------------

def test_from_statement_text_is_refused(ab):
    with tenant_scope(ab["A"]):
        with pytest.raises(UnscopedStatement):
            db.session.execute(select(Customer).from_statement(text("SELECT * FROM customers")))
        with pytest.raises(UnscopedStatement):
            db.session.execute(select(Customer).where(text("1=1")))


# 6. unbound scalar subquery ----------------------------------------------------------

def test_unbound_subquery_on_tenant_table_raises(ab):
    with tenant_scope(None):
        with pytest.raises(TenantNotBound):
            db.session.execute(
                select(select(Customer.name).where(Customer.id == ab["B_customer"]).scalar_subquery()))


# 7. bulk update that moves rows between tenants ---------------------------------------

def test_bulk_update_of_tenant_id_is_refused(ab):
    with tenant_scope(ab["A"]):
        with pytest.raises(UnscopedStatement):
            Customer.query.filter_by(id=ab["A_customer"]).update({"tenant_id": ab["B"]})
        db.session.rollback()
        with pytest.raises(UnscopedStatement):
            db.session.execute(update(Customer).values(tenant_id=ab["B"]))
        db.session.rollback()
    assert names_in(ab["A"]) == ["A customer"]


# 9. dirty object flushed with nothing bound ----------------------------------------------

def test_reattach_with_no_tenant_bound_is_refused(ab):
    with tenant_scope(ab["B"]):
        customer = db.session.get(Customer, ab["B_customer"])
    with tenant_scope(None):
        with pytest.raises(TenantNotBound):
            db.session.add(customer)


def test_pending_change_is_dropped_when_scope_switches(ab):
    """Switching tenant empties the session, so an unflushed change made
    under B can never be flushed under another scope."""
    with tenant_scope(ab["B"]):
        customer = db.session.get(Customer, ab["B_customer"])
        customer.name = "changed"
        with tenant_scope(None):
            db.session.flush()
        assert db.session.get(Customer, ab["B_customer"]).name == SECRET


# 10. other sessions cannot touch tenant data at all ------------------------------

def test_other_sessions_cannot_read_or_write_tenant_data(ab):
    """Only db.session may touch tenant tables, so a scope switch can empty
    every identity map that could hold tenant rows."""
    other = Session(db.engine)
    try:
        with tenant_scope(ab["B"]):
            with pytest.raises(UnscopedStatement):
                other.get(Customer, ab["B_customer"])
            with pytest.raises(UnscopedStatement):
                other.execute(select(Customer)).all()
            other.rollback()
            with pytest.raises(UnscopedStatement):
                other.add(Customer(name="x", phone_number="0722333444", code="X",
                                   tenant_id=ab["B"]))
            other.rollback()
    finally:
        other.close()


def test_pending_only_secondary_session_cannot_flush(ab):
    """A session that only holds a pending object (never queried) used to
    survive a scope switch and stamp the row with the new tenant."""
    other = Session(db.engine)
    try:
        with tenant_scope(ab["A"]):
            with pytest.raises(UnscopedStatement):
                other.add(Customer(name="pending", phone_number="0722333445", code="P"))
        with tenant_scope(ab["B"]):
            other.flush()          # nothing was accepted, so nothing to write
    finally:
        other.close()
    assert names_in(ab["B"]) == [SECRET]


# 11. foreign keys must point inside the tenant -----------------------------------------------

def test_reference_to_other_tenants_row_is_refused(ab):
    with tenant_scope(ab["B"]):
        db.session.add(StockMovement(item_id=ab["A_item"], item_name="x", delta=1,
                                     reason="manual"))
        with pytest.raises(CrossTenantWrite):
            db.session.commit()
        db.session.rollback()


def test_changing_a_reference_to_other_tenants_row_is_refused(ab):
    with tenant_scope(ab["B"]):
        item = db.session.get(InventoryItem, ab["B_item"])
        movement = StockMovement(item_id=item.id, item_name="ok", delta=1, reason="manual")
        db.session.add(movement)
        db.session.commit()
        movement.item_id = ab["A_item"]
        with pytest.raises(CrossTenantWrite):
            db.session.commit()
        db.session.rollback()


def test_reference_inside_the_tenant_is_fine(ab):
    with tenant_scope(ab["A"]):
        db.session.add(StockMovement(item_id=ab["A_item"], item_name="ok", delta=1,
                                     reason="manual"))
        db.session.commit()


def test_cross_tenant_write_escaping_a_route_is_a_404(app, ab, login_as):
    """Backstop: a route that forgets to check a reference gets a 404, not
    a 500 with a stack trace, and nothing is written."""
    @app.route("/inventory/_careless_movement", methods=["POST"])
    def careless():
        db.session.add(StockMovement(item_id=ab["B_item"], item_name="x", delta=1,
                                     reason="manual"))
        db.session.commit()
        return "written"

    r = login_as("admin").post("/inventory/_careless_movement")
    assert r.status_code == 404 and r.get_json() == {"error": "Not found"}
    with tenant_scope(ab["A"]):
        assert StockMovement.query.count() == 0


# --- second review pass ----------------------------------------------------------

def test_expired_foreign_object_cannot_be_reattached(ab):
    """After a commit, objects are expired: tenant_id is not in memory.
    Such an object must not pass for a new row when re-added elsewhere."""
    with tenant_scope(ab["B"]):
        foreign = db.session.get(Customer, ab["B_customer"])
        db.session.commit()                      # expires every attribute
        assert "tenant_id" not in db.inspect(foreign).dict
    with tenant_scope(ab["A"]):
        with pytest.raises(CrossTenantWrite):
            db.session.add(foreign)
        assert names_in(ab["A"]) == ["A customer"]


def test_merge_on_a_plain_session_is_checked(ab):
    with tenant_scope(ab["B"]):
        foreign = db.session.get(Customer, ab["B_customer"])
    other = Session(db.engine)
    try:
        with tenant_scope(ab["A"]):
            with pytest.raises((CrossTenantWrite, UnscopedStatement)):
                other.merge(foreign, load=False)
            assert foreign not in other
    finally:
        other.close()


def test_subquery_with_aliased_join_in_bulk_write_is_refused(ab):
    item = aliased(InventoryItem)
    sub = select(Customer.id).join(item, item.id == Customer.id)
    with tenant_scope(ab["A"]):
        with pytest.raises(UnscopedStatement):
            db.session.execute(update(Customer).where(Customer.id.in_(sub)).values(name="x"))
        db.session.rollback()
        from sqlalchemy import delete
        with pytest.raises(UnscopedStatement):
            db.session.execute(delete(Customer).where(Customer.id.in_(select(Customer.id))))
        db.session.rollback()
    assert names_in(ab["B"]) == [SECRET]


def test_bulk_update_cannot_point_a_reference_at_another_tenant(ab):
    """Covers both shapes from the second review: values in the statement
    and values supplied at execution time (bindparam / parameter dict)."""
    from sqlalchemy import bindparam
    with tenant_scope(ab["B"]):
        db.session.add(StockMovement(item_id=ab["B_item"], item_name="m", delta=1,
                                     reason="manual"))
        db.session.commit()
        for attempt in (
            lambda: db.session.execute(update(StockMovement).values(item_id=ab["A_item"])),
            lambda: db.session.execute(update(StockMovement).values(item_id=bindparam("ref")),
                                       {"ref": ab["A_item"]}),
            lambda: db.session.execute(update(Customer).where(Customer.id == ab["B_customer"]),
                                       {"tenant_id": ab["A"]}),
            lambda: db.session.execute(
                update(Customer).where(Customer.id == ab["B_customer"],
                                       InventoryItem.id == ab["A_item"])
                .values(name=InventoryItem.name)),
        ):
            with pytest.raises(UnscopedStatement):
                attempt()
            db.session.rollback()
        assert StockMovement.query.one().item_id == ab["B_item"]
    assert names_in(ab["B"]) == [SECRET]


def test_reference_checks_are_cached_within_a_flush(ab):
    from sqlalchemy import event
    selects = []

    def count(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    engine = db.engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        with tenant_scope(ab["A"]):
            db.session.add_all([StockMovement(item_id=ab["A_item"], item_name="same", delta=1,
                                              reason="manual") for _ in range(100)])
            db.session.flush()
            db.session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    # Exactly one lookup: the reference is checked once, not once per row
    # (and not skipped, which zero lookups would mean).
    assert len(selects) == 1, f"{len(selects)} lookups for 100 rows with one reference"



# --- third review round ------------------------------------------------------------

def test_nested_unscoped_keeps_the_outer_unit_of_work(ab):
    with unscoped():
        customer = Customer.query.filter_by(name=SECRET).one()
        customer.email = "kept@x.co"
        with unscoped():
            pass
        db.session.commit()
    with tenant_scope(ab["B"]):
        assert Customer.query.one().email == "kept@x.co"


def test_reference_to_parent_pending_in_same_flush(ab):
    from models import Order
    with tenant_scope(ab["A"]):
        db.session.add(Customer(id=100, name="New", phone_number="0722444555", code="N"))
        db.session.add(Order(customer_id=100, item="x", amount=Decimal("1")))
        db.session.commit()
        assert Order.query.filter_by(customer_id=100).count() == 1


def test_platform_admin_with_own_tenant_can_act_in_another(ab):
    """grant-platform-admin keeps the user's own business; acting in another
    business must still be able to record them as cashier or in audit logs."""
    from models import AuditLog, Shift, User
    with tenant_scope(ab["A"]):
        admin = User(email="ops@acme.co", role="superuser", status="approved",
                     is_platform_admin=True)
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
    with tenant_scope(ab["B"]):
        db.session.add(Shift(cashier_id=admin_id, opening_float=Decimal("0")))
        db.session.add(AuditLog(user_id=admin_id, action="platform.act"))
        db.session.commit()
    with tenant_scope(ab["A"]):
        ordinary = User(email="clerk@acme.co", role="cashier", status="approved")
        db.session.add(ordinary)
        db.session.commit()
        clerk_id = ordinary.id
    with tenant_scope(ab["B"]):
        db.session.add(Shift(cashier_id=clerk_id, opening_float=Decimal("0")))
        with pytest.raises(CrossTenantWrite):
            db.session.commit()
        db.session.rollback()


# --- final pass: legacy bulk helpers skip every hook -------------------------------

@pytest.mark.parametrize("helper", ["insert", "update", "save"])
def test_legacy_bulk_helpers_are_refused(ab, helper):
    """bulk_*_mappings and bulk_save_objects write without do_orm_execute or
    before_flush. A probe showed all three writing another tenant's rows."""
    row = {"name": "X", "phone_number": "254700000001", "code": "X", "tenant_id": ab["B"],
           "loyalty_points": 0, "credit_limit": 0, "credit_balance": 0,
           "customer_type": "retail"}
    attempts = {
        "insert": lambda: db.session.bulk_insert_mappings(Customer, [row]),
        "update": lambda: db.session.bulk_update_mappings(
            Customer, [{"id": ab["B_customer"], "name": "pwned"}]),
        "save": lambda: db.session.bulk_save_objects(
            [Customer(name="Y", phone_number="0700000002", code="Y", tenant_id=ab["B"])]),
    }
    with tenant_scope(ab["A"]):
        with pytest.raises(UnscopedStatement):
            attempts[helper]()
        db.session.rollback()
    assert names_in(ab["B"]) == [SECRET]
    with unscoped():
        assert Customer.query.count() == 2
