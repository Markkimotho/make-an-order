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
                          tenant_scope)
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
    """The subquery is filtered to A too, so B's value comes back NULL.
    (MySQL refuses an UPDATE that selects from its own table, error 1093,
    which is also fine: either way B's value never reaches A.)"""
    from sqlalchemy.exc import OperationalError
    with tenant_scope(ab["A"]):
        try:
            db.session.execute(
                update(Customer).where(Customer.id == ab["A_customer"]).values(
                    email=select(Customer.name).where(Customer.id == ab["B_customer"])
                    .scalar_subquery()))
            db.session.commit()
        except OperationalError as exc:
            db.session.rollback()
            assert "1093" in str(exc)
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


# 10. a second session keeps no foreign objects across a switch -----------------------------

def test_other_sessions_are_cleared_on_switch(ab):
    other = Session(db.engine)
    try:
        with tenant_scope(ab["B"]):
            held = other.get(Customer, ab["B_customer"])
            assert held.name == SECRET
        with tenant_scope(ab["A"]):
            assert other.get(Customer, ab["B_customer"]) is None
    finally:
        other.close()


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
