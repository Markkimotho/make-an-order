import logging
import os
import sys
from logging.config import fileConfig

from flask import current_app

from alembic import context

sys.path.insert(0, os.path.dirname(__file__))
from env_helpers import (compare_server_default,  # noqa: E402
                         disable_sqlite_foreign_keys,
                         restore_sqlite_foreign_keys)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging. Keep existing loggers:
# the default would silence the app's own loggers when upgrade() runs
# in-process (tests, CLI).
fileConfig(config.config_file_name, disable_existing_loggers=False)
logger = logging.getLogger('alembic.env')


def get_engine():
    # Flask-SQLAlchemy >= 3 (this project pins 3.1.1).
    return current_app.extensions['migrate'].db.engine


def get_engine_url():
    try:
        return get_engine().url.render_as_string(hide_password=False).replace(
            '%', '%%')
    except AttributeError:
        return str(get_engine().url).replace('%', '%%')


# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
config.set_main_option('sqlalchemy.url', get_engine_url())
target_db = current_app.extensions['migrate'].db

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_metadata():
    if hasattr(target_db, 'metadatas'):
        return target_db.metadatas[None]
    return target_db.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=get_metadata(), literal_binds=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    # this callback is used to prevent an auto-migration from being generated
    # when there are no changes to the schema
    # reference: http://alembic.zzzcomputing.com/en/latest/cookbook.html
    def process_revision_directives(context, revision, directives):
        if getattr(config.cmd_opts, 'autogenerate', False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info('No changes in schema detected.')

    conf_args = current_app.extensions['migrate'].configure_args
    if conf_args.get("process_revision_directives") is None:
        conf_args["process_revision_directives"] = process_revision_directives
    # SQLite cannot ALTER/DROP constraints in place; batch mode rebuilds the
    # table there and emits plain ALTER TABLE on MySQL (recreate='auto').
    conf_args.setdefault("render_as_batch", True)
    conf_args.setdefault("compare_type", True)
    conf_args.setdefault("compare_server_default", compare_server_default)

    connectable = get_engine()

    with connectable.connect() as connection:
        # Must run before begin_transaction(): SQLite ignores the PRAGMA
        # inside a transaction.
        previous_fk = disable_sqlite_foreign_keys(connection)
        context.configure(
            connection=connection,
            target_metadata=get_metadata(),
            **conf_args
        )

        try:
            with context.begin_transaction():
                context.run_migrations()
        finally:
            restore_sqlite_foreign_keys(connection, previous_fk)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
