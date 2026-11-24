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
    new rows get tenant_id stamped; a row for another tenant, or a changed
    tenant_id, raises CrossTenantWrite.

unscoped() switches all of this off for the few platform paths that must
see every tenant (login lookup, signup, slug resolution, the scheduler's
tenant loop). tests/test_no_bypass.py keeps the list of call sites.

Whenever the effective scope changes, the session's identity map is
emptied, so session.get() can never hand back an object loaded under a
different tenant.
"""
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, with_loader_criteria

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


def _clear_identity_map():
    try:
        db.session.expunge_all()
    except RuntimeError:
        pass  # no application context: no session to clear


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

def _is_tenant_mapper(mapper):
    return issubclass(mapper.class_, TenantMixin)


def _scope_statement(state):
    if in_unscoped():
        return
    if not state.is_orm_statement:
        raise UnscopedStatement(
            "Core/text statement on the ORM session cannot be tenant-scoped; "
            "use an ORM select or wrap a platform path in unscoped()")

    tenant_mappers = [m for m in state.all_mappers if _is_tenant_mapper(m)]

    if state.is_insert:
        if tenant_mappers:
            raise UnscopedStatement(
                "ORM insert() bypasses tenant stamping; use session.add()")
        return

    tid = current_tenant_id()
    if tid is None:
        if tenant_mappers:
            names = ", ".join(sorted(m.class_.__name__ for m in tenant_mappers))
            raise TenantNotBound(f"no tenant bound for query on {names}")
        return

    if state.is_select:
        state.statement = state.statement.options(
            with_loader_criteria(
                TenantMixin,
                lambda cls: cls.tenant_id == tid,
                include_aliases=True,
            )
        )
    elif state.is_update or state.is_delete:
        for mapper in tenant_mappers:
            state.statement = state.statement.where(mapper.class_.tenant_id == tid)


def _nullable_allowed(obj, unscoped_now):
    from models.core import AuditLog, User
    if isinstance(obj, AuditLog):
        return unscoped_now
    if isinstance(obj, User):
        return bool(obj.is_platform_admin)
    return False


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

    for obj in session.dirty:
        if not isinstance(obj, TenantMixin):
            continue
        history = inspect(obj).attrs.tenant_id.history
        if history.has_changes():
            raise CrossTenantWrite(f"{type(obj).__name__} cannot move between tenants")
        if obj.tenant_id is not None and tid is not None and obj.tenant_id != tid:
            raise CrossTenantWrite(
                f"{type(obj).__name__} of tenant {obj.tenant_id} changed while "
                f"bound to tenant {tid}")

    for obj in session.deleted:
        if not isinstance(obj, TenantMixin):
            continue
        if tid is None:
            raise TenantNotBound(f"cannot delete {type(obj).__name__}: no tenant bound")
        if obj.tenant_id is not None and obj.tenant_id != tid:
            raise CrossTenantWrite(
                f"{type(obj).__name__} of tenant {obj.tenant_id} deleted while "
                f"bound to tenant {tid}")


def _install():
    if not event.contains(Session, "do_orm_execute", _scope_statement):
        event.listen(Session, "do_orm_execute", _scope_statement)
    if not event.contains(Session, "before_flush", _check_writes):
        event.listen(Session, "before_flush", _check_writes)


_install()
