"""Audit-log service.

Single entry point :func:`log` writes an :class:`AuditLog` row capturing who
did what to which entity and the before/after values. Designed to be called
from inside the same DB session as the action being audited so that the
audit row is committed atomically with the change.

If a Flask request context is available we also capture ``request.remote_addr``.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from models import AuditLog, db

logger = logging.getLogger(__name__)


def _safe_jsonify(value: Any) -> Any:
    """Return a JSON-serialisable copy of ``value``.

    Falls back to ``str(value)`` for unknown types so the audit write never
    fails because of a stray Decimal/datetime.
    """
    if value is None:
        return None
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        try:
            return json.loads(json.dumps(value, default=str))
        except Exception:
            return str(value)


def log(
    user_id: int | None,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    old_value: Any = None,
    new_value: Any = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Write an audit-log row.

    The row is added to ``db.session`` and flushed but NOT committed — let
    the surrounding business transaction commit so the audit and the action
    are atomic.

    Returns the persisted (flushed) :class:`AuditLog`.
    """
    if ip_address is None:
        # Best-effort: pull from Flask if present, but don't require it.
        try:
            from flask import has_request_context, request  # local import to avoid hard dep
            if has_request_context():
                ip_address = request.headers.get("X-Forwarded-For", request.remote_addr)
        except Exception:
            pass

    row = AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_value=_safe_jsonify(old_value),
        new_value=_safe_jsonify(new_value),
        ip_address=ip_address,
    )
    try:
        db.session.add(row)
        db.session.flush()
    except Exception as e:
        # NEVER let auditing break the calling action.
        logger.error("audit_service.log failed: %s", e, exc_info=True)
    return row
