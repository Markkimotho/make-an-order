"""The fail-closed tenant filter (auth/tenancy.py).

Two tenants with overlapping data (same SKU, same phone, same code). Every
way the code reads or writes rows must see only the bound tenant's.
"""
from decimal import Decimal

import pytest
from sqlalchemy import delete, insert, select, text, union, update

from auth.tenancy import (CrossTenantWrite, TenantNotBound, UnscopedStatement,
                          bind_tenant, current_tenant_id, in_unscoped,
                          reset_tenant, tenant_scope, unscoped)
from models import AuditLog, Customer, InventoryItem, Order, User, db


@pytest.fixture
def two_tenants(app, other_tenant):
    """acme and globex, each with one customer, order and item."""
    ids = {}
    for name, tid in (("acme", app.acme_id), ("globex", other_tenant)):
        with tenant_scope(tid):
            c = Customer(name=f"{name} customer", phone_number="0712345678", code="C1")
            db.session.add(c)
            db.session.flush()
            db.session.add(Order(customer_id=c.id, item=f"{name} order", amount=Decimal("10")))
            db.session.add(InventoryItem(name=f"{name} item", price=Decimal("5"), sku="SKU1"))
            db.session.commit()
            ids[name] = {"tenant": tid, "customer": c.id}
    return ids


def names(rows):
    return sorted(r.name for r in rows)


# --- unbound -------------------------------------------------------------

def test_unbound_query_raises(two_tenants):
    with tenant_scope(None):
        with pytest.raises(TenantNotBound):
            Customer.query.all()
        with pytest.raises(TenantNotBound):
            db.session.execute(select(Customer)).all()


def test_unbound_get_raises(two_tenants):
    with tenant_scope(None):
        with pytest.raises(TenantNotBound):
            db.session.get(Customer, two_tenants["acme"]["customer"])


def test_unbound_write_raises(two_tenants):
    with tenant_scope(None):
        db.session.add(Customer(name="Orphan", phone_number="0722000000", code="O"))
        with pytest.raises(TenantNotBound):
            db.session.commit()
        db.session.rollback()


# --- reads ---------------------------------------------------------------

def test_every_read_path_sees_only_bound_tenant(two_tenants):
    with tenant_scope(two_tenants["acme"]["tenant"]):
        assert names(Customer.query.all()) == ["acme customer"]
        assert names(db.session.execute(select(Customer)).scalars()) == ["acme customer"]
        assert Customer.query.count() == 1
        page = db.paginate(select(Customer), page=1, per_page=10)
        assert names(page.items) == ["acme customer"]
        joined = db.session.execute(
            select(Order.item, Customer.name).join(Customer, Customer.id == Order.customer_id)
        ).all()
        assert joined == [("acme order", "acme customer")]


def test_lazy_loaded_relationship_is_scoped(two_tenants):
    with tenant_scope(two_tenants["acme"]["tenant"]):
        customer = Customer.query.one()
        assert [o.item for o in customer.orders] == ["acme order"]


def test_same_statement_object_under_each_tenant(two_tenants):
    """Catches a tenant id baked into a cached statement."""
    stmt = select(Customer).where(Customer.code == "C1")
    seen = []
    for name in ("acme", "globex", "acme"):
        with tenant_scope(two_tenants[name]["tenant"]):
            seen.append(names(db.session.execute(stmt).scalars()))
    assert seen == [["acme customer"], ["globex customer"], ["acme customer"]]


def test_union_under_each_tenant(two_tenants):
    stmt = union(select(Customer.name), select(InventoryItem.name))
    for name in ("acme", "globex"):
        with tenant_scope(two_tenants[name]["tenant"]):
            rows = sorted(r[0] for r in db.session.execute(stmt))
            assert rows == [f"{name} customer", f"{name} item"]


def test_get_after_switch_returns_nothing(two_tenants):
    acme_customer = two_tenants["acme"]["customer"]
    with tenant_scope(two_tenants["acme"]["tenant"]):
        # Hold a strong reference: the identity map is weak, so without one
        # the object could vanish by garbage collection and hide a leak.
        loaded = db.session.get(Customer, acme_customer)
        assert loaded is not None
        with tenant_scope(two_tenants["globex"]["tenant"]):
            assert db.session.get(Customer, acme_customer) is None
        assert loaded.name == "acme customer"


# --- bulk writes -----------------------------------------------------------

def test_bulk_update_and_delete_are_scoped(two_tenants):
    with tenant_scope(two_tenants["globex"]["tenant"]):
        db.session.execute(update(Customer).values(name="renamed"))
        db.session.execute(delete(Order))
        Customer.query.filter_by(code="C1").update({"code": "C9"})
        db.session.commit()
    with tenant_scope(two_tenants["acme"]["tenant"]):
        assert names(Customer.query.all()) == ["acme customer"]
        assert Customer.query.one().code == "C1"
        assert Order.query.count() == 1


# --- statements the filter cannot scope ---------------------------------------

def test_text_and_orm_insert_refused(two_tenants):
    with pytest.raises(UnscopedStatement):
        db.session.execute(text("SELECT 1"))
    with pytest.raises(UnscopedStatement):
        db.session.execute(insert(Customer).values(name="x", phone_number="254700000000",
                                                   code="X", tenant_id=two_tenants["acme"]["tenant"]))


def test_unscoped_allows_them_and_sees_all(two_tenants):
    with unscoped():
        assert db.session.execute(text("SELECT 1")).scalar() == 1
        assert names(Customer.query.all()) == ["acme customer", "globex customer"]


# --- single-row writes --------------------------------------------------------

def test_new_rows_are_stamped(two_tenants):
    with tenant_scope(two_tenants["globex"]["tenant"]):
        c = Customer(name="New", phone_number="0722111222", code="N1")
        db.session.add(c)
        db.session.commit()
        assert c.tenant_id == two_tenants["globex"]["tenant"]


def test_adding_row_for_other_tenant_raises(two_tenants):
    with tenant_scope(two_tenants["globex"]["tenant"]):
        with pytest.raises(CrossTenantWrite):
            db.session.add(Customer(name="Sneaky", phone_number="0722111223", code="S",
                                    tenant_id=two_tenants["acme"]["tenant"]))
        db.session.rollback()


def test_moving_row_to_other_tenant_raises(two_tenants):
    with tenant_scope(two_tenants["acme"]["tenant"]):
        c = Customer.query.one()
        c.tenant_id = two_tenants["globex"]["tenant"]
        with pytest.raises(CrossTenantWrite):
            db.session.commit()
        db.session.rollback()


# --- nullable exceptions ------------------------------------------------------

def test_audit_log_without_tenant_only_in_unscoped(two_tenants):
    with tenant_scope(None):
        db.session.add(AuditLog(action="platform.boot"))
        with pytest.raises(TenantNotBound):
            db.session.commit()
        db.session.rollback()
    with unscoped(), tenant_scope(None):
        db.session.add(AuditLog(action="platform.boot"))
        db.session.commit()


def test_user_without_tenant_only_for_platform_admin(two_tenants):
    with unscoped(), tenant_scope(None):
        db.session.add(User(email="staff@x.co", role="user", status="approved"))
        with pytest.raises(TenantNotBound):
            db.session.commit()
        db.session.rollback()
        db.session.add(User(email="ops@x.co", role="superuser", status="approved",
                            is_platform_admin=True))
        db.session.commit()


# --- scope bookkeeping --------------------------------------------------------

def test_scopes_nest_and_restore(app, other_tenant):
    outer = current_tenant_id()
    with tenant_scope(other_tenant):
        assert current_tenant_id() == other_tenant
        with unscoped():
            assert in_unscoped()
        assert not in_unscoped()
    assert current_tenant_id() == outer
    token = bind_tenant(None)
    assert current_tenant_id() is None
    reset_tenant(token)
    assert current_tenant_id() == outer


def test_leaving_unscoped_empties_identity_map(two_tenants):
    with unscoped():
        rows = Customer.query.all()
        assert len(db.session.identity_map) >= 2
    assert len(db.session.identity_map) == 0
    assert all(db.inspect(r).detached for r in rows)
