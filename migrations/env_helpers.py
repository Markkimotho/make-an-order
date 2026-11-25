"""Helpers for env.py, kept separate so tests can import them.

env.py runs Alembic's context at import time, so anything worth unit
testing lives here instead.
"""


import re

_NOW_FORMS = {"now()", "current_timestamp", "current_timestamp()", "localtimestamp",
              "localtimestamp()"}


def _normalise_default(text):
    if text is None:
        return None
    t = str(text).strip().strip("'\"").lower()
    while t.startswith("(") and t.endswith(")") and t.count("(") == t.count(")"):
        t = t[1:-1].strip()
    t = re.sub(r"\s+", "", t)
    if t in _NOW_FORMS:
        return "current_timestamp"
    t = _BOOLEAN_FORMS.get(t, t)
    try:                                   # MySQL reports 0 as '0.000' on DECIMAL
        from decimal import Decimal, InvalidOperation
        return format(Decimal(t).normalize(), "f")
    except (InvalidOperation, ValueError):
        return t


_BOOLEAN_FORMS = {"false": "0", "true": "1"}


def compare_server_default(context, inspected_column, metadata_column,
                           inspected_default, metadata_default,
                           rendered_metadata_default):
    """Alembic hook: ignore defaults that differ only in spelling.

    Models declare server_default=func.now() and sa.false(). MySQL reports
    them back as CURRENT_TIMESTAMP and '0'; SQLite as (CURRENT_TIMESTAMP)
    and 0. Without this, autogenerate reports a change on every such column
    on MySQL. Returns False (same) when both sides normalise to the same
    value, None to let Alembic decide otherwise.
    """
    if inspected_default is None or rendered_metadata_default is None:
        return None
    if _normalise_default(inspected_default) == _normalise_default(rendered_metadata_default):
        return False
    return None


def disable_sqlite_foreign_keys(connection):
    """Turn SQLite foreign-key enforcement off for this connection.

    Batch migrations rebuild a table by copy-and-rename; with enforcement on,
    dropping a parent table that others reference fails. SQLite ignores this
    PRAGMA inside a transaction, so it must run before
    ``context.begin_transaction()``. SQLAlchemy 2.0 autobegins on the first
    statement, so we commit right after: otherwise Alembic sees an external
    transaction, never commits, and the revision is rolled back on close.

    Returns the previous setting (True/False) on SQLite, None elsewhere.
    """
    if connection.dialect.name != "sqlite":
        return None
    previous = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar())
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    connection.commit()
    return previous


def restore_sqlite_foreign_keys(connection, previous):
    """Put enforcement back as it was before the migration run."""
    if previous is None or connection.dialect.name != "sqlite":
        return
    connection.exec_driver_sql(f"PRAGMA foreign_keys={'ON' if previous else 'OFF'}")
    connection.commit()
