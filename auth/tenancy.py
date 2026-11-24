"""Fail-closed tenant scoping for every ORM query and write.

The bound tenant lives in a ContextVar, so it follows the request, CLI
command or test that set it. Two session hooks enforce it:

do_orm_execute (every query the ORM runs)
    select            -> WHERE tenant_id = :bound, on every tenant table in
                         the statement, including joins, aliases, unions and
                         lazy/refresh loads
    bulk update/delete-> WHERE tenant_id = :bound added explicitly
    ORM insert()      -> refused (it would skip before_flush); use add()
    non-ORM statement -> refused (text(), Core table statements)
    nothing bound     -> TenantNotBound, never "all tenants"

before_flush (every add/change/delete)
    new rows get tenant_id stamped; a row for another tenant, a changed
    tenant_id, or a foreign key pointing at another tenant's row raises
    CrossTenantWrite; changes with nothing bound raise TenantNotBound.

before_attach
    an object belonging to another tenant cannot enter the session
    (session.add or merge of an object loaded under a different scope).

unscoped() switches all of this off for the few platform paths that must
see every tenant (login lookup, signup, slug resolution, the scheduler's
tenant loop). tests/test_no_bypass.py keeps the list of call sites.

Whenever the effective scope changes, the session's identity map is
emptied, so session.get() can never hand back an object loaded under a
different tenant.
"""
import threading
import weakref
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session, with_loader_criteria
from sqlalchemy.orm.context import FromStatement
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import BindParameter, TextClause
from sqlalchemy.sql.selectable import Alias, Select, TableClause, TextualSelect

from models.base import TenantMixin, db


class TenantNotBound(RuntimeError):
    """A tenant table was used with no tenant bound."""


class CrossTenantWrite(RuntimeError):
    """A write would create, move or delete a row of another tenant."""


class UnscopedStatement(RuntimeError):
    """A statement the tenant filter cannot scope (Core/text, ORM insert())."""


_tenant: ContextVar = ContextVar("tenant_id", default=None)
_unscoped: ContextVar = ContextVar("unscoped", default=False)

_UNSCOPED = object()


def current_tenant_id():
    return _tenant.get()


def in_unscoped():
    return _unscoped.get()


def _effective_scope():
    return _UNSCOPED if in_unscoped() else current_tenant_id()


_local = threading.local()


def _thread_sessions():
    """Sessions that have begun a transaction in this thread (weakly held)."""
    sessions = getattr(_local, "sessions", None)
    if sessions is None:
        sessions = _local.sessions = weakref.WeakSet()
    return sessions


def _clear_identity_map():
    """Empty every session this thread has used, not only db.session: any
    session's identity map could otherwise hand back another tenant's row."""
    for session in list(_thread_sessions()):
        session.expunge_all()
    try:
        db.session.expunge_all()
    except RuntimeError:
        pass  # no application context: no Flask session to clear


def _clear_if_changed(before):
    if _effective_scope() != before:
        _clear_identity_map()


def bind_tenant(tenant_id):
    """Bind ``tenant_id`` (or None). Returns a token for :func:`reset_tenant`."""
    before = _effective_scope()
    token = _tenant.set(tenant_id)
    _clear_if_changed(before)
    return token


def reset_tenant(token):
    before = _effective_scope()
    _tenant.reset(token)
    _clear_if_changed(before)


@contextmanager
def tenant_scope(tenant_id):
    token = bind_tenant(tenant_id)
    try:
        yield
    finally:
        reset_tenant(token)


@contextmanager
def unscoped():
    """See every tenant. Platform paths only; leaves an empty identity map."""
    before = _effective_scope()
    token = _unscoped.set(True)
    _clear_if_changed(before)
    try:
        yield
    finally:
        _unscoped.reset(token)
        _clear_identity_map()


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

# Internal execution option for the hooks' own lookups (never set by app
# code; tests/test_no_bypass.py bans the name outside this module).
_INTERNAL = "tenancy_internal"


def _is_tenant_mapper(mapper):
    return issubclass(mapper.class_, TenantMixin)


def _tenant_table_names():
    return {m.local_table.name for m in db.Model.registry.mappers if _is_tenant_mapper(m)}


def _inspect_statement(statement):
    """(touches a tenant table anywhere, contains raw SQL text anywhere).

    Walks the whole statement, subqueries included: the unbound check must
    see a tenant table hidden in a scalar subquery, and text() fragments
    cannot be scoped at all.
    """
    tenant_tables = _tenant_table_names()
    touches = has_text = False
    for element in visitors.iterate(statement):
        if isinstance(element, (TextClause, TextualSelect)):
            has_text = True
        name = getattr(element, "name", None) if isinstance(element, TableClause) else None
        if name in tenant_tables:
            touches = True
    return touches, has_text


def _updated_values(statement):
    """{column name: value expression} set by an UPDATE statement."""
    values = {}
    for source in (getattr(statement, "_values", None) or {},
                   dict(getattr(statement, "_ordered_values", None) or ())):
        for key, value in source.items():
            name = getattr(key, "key", None) or getattr(key, "name", None) or str(key)
            values[name] = value
    return values


def _scope_statement(state):
    if in_unscoped() or state.execution_options.get(_INTERNAL):
        return
    statement = state.statement
    if not state.is_orm_statement or isinstance(statement, FromStatement):
        raise UnscopedStatement(
            "Core, text or from_statement() SQL cannot be tenant-scoped; use an ORM "
            "query, or wrap a platform path in unscoped()")

    touches_tenant, has_text = _inspect_statement(statement)
    if has_text and touches_tenant:
        raise UnscopedStatement("raw SQL text inside a tenant query cannot be scoped")

    tenant_mappers = [m for m in state.all_mappers if _is_tenant_mapper(m)]
    touches_tenant = touches_tenant or bool(tenant_mappers)

    if state.is_insert:
        if touches_tenant:
            raise UnscopedStatement("ORM insert() bypasses tenant stamping; use session.add()")
        return

    tid = current_tenant_id()
    if tid is None:
        if touches_tenant:
            raise TenantNotBound("no tenant bound for a query on tenant data")
        return

    if state.is_update or state.is_delete:
        if not tenant_mappers:
            return
        _check_bulk_write(state, statement, tenant_mappers)
        for mapper in tenant_mappers:
            statement = statement.where(mapper.class_.tenant_id == tid)

    # Applies to the statement's entities everywhere they occur, subqueries
    # included, for selects and for bulk update/delete.
    state.statement = statement.options(
        with_loader_criteria(TenantMixin, lambda cls: cls.tenant_id == tid,
                             include_aliases=True))


def _check_bulk_write(state, statement, tenant_mappers):
    """Bulk UPDATE/DELETE are kept to the simple shape the filter can prove
    safe: one plain tenant table, literal values, no subqueries or aliases
    of tenant tables (an aliased join inside a subquery is not reliably
    filtered), tenant_id untouched, references checked like a flush."""
    if isinstance(statement.table, Alias):
        raise UnscopedStatement("bulk update/delete on an aliased entity cannot be tenant-scoped")
    if state.is_executemany or isinstance(state.parameters, list):
        raise UnscopedStatement("bulk update/delete by parameter list is not supported")
    tenant_tables = _tenant_table_names()
    for element in visitors.iterate(statement):
        if element is statement:
            continue
        if isinstance(element, Select):
            raise UnscopedStatement("subqueries are not allowed in a bulk update/delete; "
                                    "load the rows with a query instead")
        if isinstance(element, Alias) and getattr(element.element, "name", None) in tenant_tables:
            raise UnscopedStatement("aliased tenant tables are not allowed in a bulk update/delete")
    if not state.is_update:
        return
    values = _updated_values(statement)
    if "tenant_id" in values:
        raise UnscopedStatement("tenant_id cannot be changed by a bulk update")
    mapper = tenant_mappers[0]
    for key, target in _tenant_references(mapper):
        if key not in values:
            continue
        value = values[key]
        if isinstance(value, BindParameter):
            value = value.value
        elif hasattr(value, "__clause_element__") or hasattr(value, "_is_clause_element"):
            raise UnscopedStatement(f"{key} must be set to a plain value in a bulk update")
        if value is not None and not _reference_visible(state.session, target, value, {}):
            raise CrossTenantWrite(
                f"bulk update sets {mapper.class_.__name__}.{key}={value}, which is not a "
                f"{target.__name__} of this tenant")


def _owner_of(session, instance):
    """The tenant an instance belongs to, or None for a new row.

    An expired or partly loaded object (after a commit, say) may not hold
    tenant_id in memory; if it has a database identity, the owner is read
    from the database instead of being mistaken for a new row.
    """
    state = inspect(instance)
    if "tenant_id" in state.dict:
        return state.dict["tenant_id"]
    if state.key is None:
        return None
    mapper = state.mapper
    pk_cols = mapper.primary_key
    condition = [col == value for col, value in zip(pk_cols, state.key[1])]
    owner = session.execute(
        select(mapper.class_.tenant_id).where(*condition)
        .execution_options(**{_INTERNAL: True})).scalar()
    return owner if owner is not None else _UNKNOWN_OWNER


_UNKNOWN_OWNER = object()


def _check_attach(session, instance):
    """No object of another tenant may enter a session (merge, add, ...)."""
    if in_unscoped() or not isinstance(instance, TenantMixin):
        return
    tid = current_tenant_id()
    if tid is None and inspect(instance).key is not None:
        raise TenantNotBound(f"cannot attach {type(instance).__name__}: no tenant bound")
    owner = _owner_of(session, instance)
    if owner is None:
        return   # a new row; before_flush stamps it
    if tid is None:
        raise TenantNotBound(f"cannot attach {type(instance).__name__}: no tenant bound")
    if owner != tid:
        raise CrossTenantWrite(
            f"{type(instance).__name__} of tenant {owner} attached while bound to tenant {tid}")


def check_merged(session, merged):
    """Called by models.base.TenantSession.merge with the merged object."""
    try:
        _check_attach(session, merged)
    except (CrossTenantWrite, TenantNotBound):
        session.expunge(merged)
        raise


def _nullable_allowed(obj, unscoped_now):
    from models.core import AuditLog, User
    if isinstance(obj, AuditLog):
        return unscoped_now
    if isinstance(obj, User):
        return bool(obj.is_platform_admin)
    return False


_FK_CACHE = {}


def _tenant_references(mapper):
    """[(attribute key, referenced class)] for FKs pointing at tenant tables."""
    if mapper in _FK_CACHE:
        return _FK_CACHE[mapper]
    by_table = {m.local_table: m.class_ for m in db.Model.registry.mappers
                if _is_tenant_mapper(m)}
    refs = []
    for prop in mapper.column_attrs:
        for column in prop.columns:
            if column.key == "tenant_id":
                continue
            for fk in column.foreign_keys:
                target = by_table.get(fk.column.table)
                if target is not None:
                    refs.append((prop.key, target))
    _FK_CACHE[mapper] = refs
    return refs


def _reference_visible(session, target, value, memo):
    """True if row ``value`` of ``target`` belongs to the bound tenant.

    ``memo`` caches answers for one flush, so 100 rows pointing at the same
    item cost one lookup, not 100. References to platform admins (users
    with no tenant) are allowed: they act inside a business and their
    actions are recorded against them.
    """
    from models.core import User
    key = (target, value)
    if key in memo:
        return memo[key]
    with session.no_autoflush:
        visible = session.get(target, value) is not None
        if not visible and target is User:
            row = session.execute(
                select(User.tenant_id, User.is_platform_admin).where(User.id == value)
                .execution_options(**{_INTERNAL: True})).first()
            visible = row is not None and row.tenant_id is None and bool(row.is_platform_admin)
    memo[key] = visible
    return visible


def _check_references(session, obj, only_changed, memo):
    state = inspect(obj)
    for key, target in _tenant_references(state.mapper):
        if only_changed and not state.attrs[key].history.has_changes():
            continue
        value = state.dict.get(key)
        if value is None:
            continue
        if not _reference_visible(session, target, value, memo):
            raise CrossTenantWrite(
                f"{type(obj).__name__}.{key}={value} does not refer to a "
                f"{target.__name__} of this tenant")


def _check_writes(session, flush_context, instances):
    tid = current_tenant_id()
    unscoped_now = in_unscoped()

    for obj in session.new:
        if not isinstance(obj, TenantMixin):
            continue
        if obj.tenant_id is None:
            if tid is not None:
                obj.tenant_id = tid
            elif not _nullable_allowed(obj, unscoped_now):
                raise TenantNotBound(
                    f"cannot add {type(obj).__name__}: no tenant bound")
        elif not unscoped_now:
            if tid is None:
                raise TenantNotBound(
                    f"cannot add {type(obj).__name__} for tenant {obj.tenant_id}: "
                    "no tenant bound")
            if obj.tenant_id != tid:
                raise CrossTenantWrite(
                    f"{type(obj).__name__} for tenant {obj.tenant_id} added "
                    f"while bound to tenant {tid}")
        if obj.tenant_id is None and not _nullable_allowed(obj, unscoped_now):
            raise TenantNotBound(f"{type(obj).__name__} needs a tenant")

    if unscoped_now:
        return

    memo = {}
    for obj in session.new:
        if isinstance(obj, TenantMixin):
            _check_references(session, obj, only_changed=False, memo=memo)

    for obj in session.dirty:
        if not isinstance(obj, TenantMixin) or not session.is_modified(obj):
            continue
        if tid is None:
            raise TenantNotBound(f"cannot update {type(obj).__name__}: no tenant bound")
        if inspect(obj).attrs.tenant_id.history.has_changes():
            raise CrossTenantWrite(f"{type(obj).__name__} cannot move between tenants")
        if obj.tenant_id is not None and obj.tenant_id != tid:
            raise CrossTenantWrite(
                f"{type(obj).__name__} of tenant {obj.tenant_id} changed while "
                f"bound to tenant {tid}")
        _check_references(session, obj, only_changed=True, memo=memo)

    for obj in session.deleted:
        if not isinstance(obj, TenantMixin):
            continue
        if tid is None:
            raise TenantNotBound(f"cannot delete {type(obj).__name__}: no tenant bound")
        if obj.tenant_id is not None and obj.tenant_id != tid:
            raise CrossTenantWrite(
                f"{type(obj).__name__} of tenant {obj.tenant_id} deleted while "
                f"bound to tenant {tid}")


def _register_session(session, transaction, connection):
    _thread_sessions().add(session)


def _checked_merge(original):
    def merge(self, instance, *args, **kwargs):
        merged = original(self, instance, *args, **kwargs)
        check_merged(self, merged)
        return merged
    merge._tenancy_checked = True
    return merge


def _install():
    # merge() attaches its copy before filling in attributes, so before_attach
    # cannot see whose row it is; check the result on every Session class.
    if not getattr(Session.merge, "_tenancy_checked", False):
        Session.merge = _checked_merge(Session.merge)
    for name, fn in (("do_orm_execute", _scope_statement),
                     ("before_flush", _check_writes),
                     ("before_attach", _check_attach),
                     ("after_begin", _register_session)):
        if not event.contains(Session, name, fn):
            event.listen(Session, name, fn)


_install()
