"""Helpers for env.py, kept separate so tests can import them.

env.py runs Alembic's context at import time, so anything worth unit
testing lives here instead.
"""


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
