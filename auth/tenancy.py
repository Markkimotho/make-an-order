"""Fail-closed tenant scoping for every ORM query and write.

The bound tenant lives in a ContextVar, so it follows the request, CLI
command or test that set it. The rules, enforced by session hooks:

Reads (do_orm_execute)
    Every ORM select is filtered to the bound tenant wherever a tenant table
    appears: joins, aliases, unions, subqueries, lazy, eager and refresh
    loads. With nothing bound, a statement touching tenant data anywhere
    (even in a scalar subquery) raises TenantNotBound.

What the filter will not run outside unscoped() (UnscopedStatement)
    - Core or text() SQL, from_statement(), or text() inside a tenant query.
    - Bulk insert(), update() and delete() on tenant tables, including
      Query.update/delete. Every bypass found in review was a bulk-write
      shape; the app needs none. Change rows by loading them (with
      .with_for_update() when it must be atomic) and editing them.
    - Any session other than the app's db.session. One session per request
      is what lets a scope change empty the identity map completely.

Writes (before_flush)
    New rows are stamped with the bound tenant. Adding, changing or deleting
    another tenant's row, moving a row between tenants, or pointing a
    foreign key at another tenant's row raises CrossTenantWrite. Any change
    with nothing bound raises TenantNotBound.

Attaching (before_attach, merge)
    An object of another tenant cannot enter the session through add() or
    merge(), including an expired one whose tenant_id is not in memory.

Scope changes
    Whenever the effective scope changes, the session's identity map is
    emptied, so a get() cannot return an object loaded under another scope.
    Unflushed changes are discarded then (and logged): finish work before
    switching. Values already loaded on an object a caller keeps holding
    stay readable in Python; the guarantees are about what queries return
    and what is written.

unscoped() switches all of this off for the few platform paths that must
see every tenant; tests/test_no_bypass.py lists the call sites.
"""
import logging
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session, with_loader_criteria
from sqlalchemy.orm.context import FromStatement
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.selectable import TableClause, TextualSelect

from models.base import TenantMixin, db

logger = logging.getLogger(__name__)


class TenantNotBound(RuntimeError):
    """A tenant table was used with no tenant bound."""


class CrossTenantWrite(RuntimeError):
    """A write would create, move, delete or reference another tenant's row."""


class UnscopedStatement(RuntimeError):
    """A statement or session the tenant filter cannot scope."""


_tenant: ContextVar = ContextVar("tenant_id", default=None)
_unscoped: ContextVar = ContextVar("unscoped", default=False)

_UNSCOPED = object()

# Execution option for the hooks' own lookups. Never set by app code;
# tests/test_no_bypass.py bans the name outside this module.
_INTERNAL = "tenancy_internal"


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def current_tenant_id():
    return _tenant.get()


def in_unscoped():
    return _unscoped.get()


def _effective_scope():
    return _UNSCOPED if in_unscoped() else current_tenant_id()


def _app_session():
    """The app's Session for this application context, or None outside one."""
    try:
        return db.session()
    except RuntimeError:
        return None


def _clear_identity_map():
    session = _app_session()
    if session is None:
        return
    pending = len(session.new) + len(session.dirty) + len(session.deleted)
    if pending:
        logger.warning("Tenant scope changed with %d unflushed change(s); discarded", pending)
    session.expunge_all()


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
    """See every tenant. Platform paths only.

    Leaving the outermost unscoped() empties the identity map; a nested one
    does not, since the effective scope does not change.
    """
    before = _effective_scope()
    token = _unscoped.set(True)
    _clear_if_changed(before)
    try:
        yield
    finally:
        inner_before = _effective_scope()
        _unscoped.reset(token)
        _clear_if_changed(inner_before)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_tenant_mapper(mapper):
    return issubclass(mapper.class_, TenantMixin)


def _tenant_table_names():
    return {m.local_table.name for m in db.Model.registry.mappers if _is_tenant_mapper(m)}


def _inspect_statement(statement):
    """(touches a tenant table anywhere, contains raw SQL text anywhere)."""
    tenant_tables = _tenant_table_names()
    touches = has_text = False
    for element in visitors.iterate(statement):
        if isinstance(element, (TextClause, TextualSelect)):
            has_text = True
        if isinstance(element, TableClause) and element.name in tenant_tables:
            touches = True
    return touches, has_text


def _foreign_session(session):
    return session is not _app_session()


# ---------------------------------------------------------------------------
# do_orm_execute
# ---------------------------------------------------------------------------

def _scope_statement(state):
    if in_unscoped() or state.execution_options.get(_INTERNAL):
        return
    statement = state.statement
    if not state.is_orm_statement or isinstance(statement, FromStatement):
        raise UnscopedStatement(
            "Core, text or from_statement() SQL cannot be tenant-scoped; use an ORM "
            "query, or wrap a platform path in unscoped()")

    touches_tenant, has_text = _inspect_statement(statement)
    touches_tenant = touches_tenant or any(_is_tenant_mapper(m) for m in state.all_mappers)
    if not touches_tenant:
        return
    if has_text:
        raise UnscopedStatement("raw SQL text inside a tenant query cannot be scoped")
    if _foreign_session(state.session):
        raise UnscopedStatement("tenant data is only available through db.session")
    if state.is_insert or state.is_update or state.is_delete:
        raise UnscopedStatement(
            "bulk insert/update/delete on tenant tables is not allowed; load the rows "
            "(with_for_update() if it must be atomic) and change them, or add() new ones")

    tid = current_tenant_id()
    if tid is None:
        raise TenantNotBound("no tenant bound for a query on tenant data")
    state.statement = statement.options(
        with_loader_criteria(TenantMixin, lambda cls: cls.tenant_id == tid,
                             include_aliases=True))


# ---------------------------------------------------------------------------
# Attaching objects
# ---------------------------------------------------------------------------

_UNKNOWN_OWNER = object()


def _owner_of(session, instance):
    """The tenant an instance belongs to, or None for a new row.

    An expired object (after a commit, say) may not hold tenant_id in
    memory; if it has a database identity, its owner is read from the
    database instead of being mistaken for a new row.
    """
    state = inspect(instance)
    if "tenant_id" in state.dict:
        return state.dict["tenant_id"]
    if state.key is None:
        return None
    mapper = state.mapper
    condition = [col == value for col, value in zip(mapper.primary_key, state.key[1])]
    owner = session.execute(
        select(mapper.class_.tenant_id).where(*condition)
        .execution_options(**{_INTERNAL: True})).scalar()
    return owner if owner is not None else _UNKNOWN_OWNER


def _check_attach(session, instance):
    """No object of another tenant may enter a session."""
    if in_unscoped() or not isinstance(instance, TenantMixin):
        return
    if _foreign_session(session):
        raise UnscopedStatement("tenant data is only available through db.session")
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
    """merge() fills in its copy after attaching it, so check the result."""
    try:
        _check_attach(session, merged)
    except (CrossTenantWrite, TenantNotBound, UnscopedStatement):
        if merged in session:
            session.expunge(merged)
        raise


def _checked_merge(original):
    def merge(self, instance, *args, **kwargs):
        merged = original(self, instance, *args, **kwargs)
        check_merged(self, merged)
        return merged
    merge._tenancy_checked = True
    return merge


# ---------------------------------------------------------------------------
# before_flush
# ---------------------------------------------------------------------------

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


def _pending_with_id(session, target, value):
    for obj in session.new:
        if isinstance(obj, target) and inspect(obj).dict.get("id") == value:
            return obj
    return None


def _reference_visible(session, target, value, memo):
    """True if row ``value`` of ``target`` belongs to the bound tenant.

    Finds parents flushed earlier, parents pending in this same flush, and
    platform administrators (who act inside a business whatever business, if
    any, their own account belongs to). ``memo`` caches answers for one
    flush, so many rows sharing a reference cost one lookup.
    """
    from models.core import User
    key = (target, value)
    if key in memo:
        return memo[key]
    with session.no_autoflush:
        visible = (session.get(target, value) is not None
                   or _pending_with_id(session, target, value) is not None)
        if not visible and target is User:
            admin = session.execute(
                select(User.is_platform_admin).where(User.id == value)
                .execution_options(**{_INTERNAL: True})).scalar()
            visible = bool(admin)
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
    tenant_rows = [o for o in (*session.new, *session.dirty, *session.deleted)
                   if isinstance(o, TenantMixin)]
    if tenant_rows and not unscoped_now and _foreign_session(session):
        raise UnscopedStatement("tenant data is only available through db.session")

    for obj in session.new:
        if not isinstance(obj, TenantMixin):
            continue
        if obj.tenant_id is None:
            if tid is not None:
                obj.tenant_id = tid
            elif not _nullable_allowed(obj, unscoped_now):
                raise TenantNotBound(f"cannot add {type(obj).__name__}: no tenant bound")
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


# ---------------------------------------------------------------------------
# Install (once per process)
# ---------------------------------------------------------------------------

def _install():
    if not getattr(Session.merge, "_tenancy_checked", False):
        Session.merge = _checked_merge(Session.merge)
    for name, fn in (("do_orm_execute", _scope_statement),
                     ("before_flush", _check_writes),
                     ("before_attach", _check_attach)):
        if not event.contains(Session, name, fn):
            event.listen(Session, name, fn)


_install()
