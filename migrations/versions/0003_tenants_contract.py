"""Tenants, contract phase: enforce tenant_id and per-tenant uniqueness.

1. Normalise customers.phone_number and users.phone_number to 254XXXXXXXXX.
   Every unparseable number and every collision (two rows of one tenant
   that normalise to the same number) is collected first; if there are
   any, the revision raises listing all of them and changes nothing.
2. tenant_id gets a foreign key (ON DELETE CASCADE) and an index on all 18
   tables, and becomes NOT NULL except on users (platform admins) and
   audit_logs (platform events).
3. The legacy single-column uniques become (tenant_id, column). They are
   found by column with the inspector: on a database built by the old
   create_all() they are unnamed (SQLite) or named after the column
   (MySQL: "email", "sku", ...). users.email stays global, renamed to
   uq_users_email. users gains (tenant_id, phone_number).
4. system_config is dropped; its values now live on the tenant.

Revision ID: 0003_tenants_contract
Revises: 0002_tenants_expand
Create Date: 2026-11-24
"""
from collections import defaultdict

import sqlalchemy as sa
from alembic import op

from migrations.helpers import (NULLABLE_TENANT_TABLES, PER_TENANT_UNIQUES,
                                TENANT_TABLES, VERTICAL_TO_BUSINESS_TYPE,
                                find_uniques)
from services.phone import InvalidPhone, normalize_ke_phone

revision = "0003_tenants_contract"
down_revision = "0002_tenants_expand"
branch_labels = None
depends_on = None

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class PhoneMigrationError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade():
    bind = op.get_bind()
    _normalise_phones(bind)

    for table in TENANT_TABLES:
        with op.batch_alter_table(table, naming_convention=NAMING_CONVENTION) as batch:
            if table not in NULLABLE_TENANT_TABLES:
                batch.alter_column("tenant_id", existing_type=sa.Integer(), nullable=False)
            batch.create_index(f"ix_{table}_tenant_id", ["tenant_id"])
            batch.create_foreign_key(f"fk_{table}_tenant_id_tenants", "tenants",
                                     ["tenant_id"], ["id"], ondelete="CASCADE")

    _swap_uniques(bind)
    op.drop_table("system_config")


def _plan_phone_changes(bind, table, nullable):
    t = sa.table(table, sa.column("id", sa.Integer), sa.column("tenant_id", sa.Integer),
                 sa.column("phone_number", sa.String))
    rows = bind.execute(sa.select(t.c.id, t.c.tenant_id, t.c.phone_number)).all()
    problems, changes = [], {}
    seen = defaultdict(list)
    for row_id, tenant_id, phone in rows:
        if phone is None or str(phone).strip() == "":
            if nullable:
                # Empty strings would collide under the new (tenant, phone)
                # unique constraint; NULLs do not.
                if phone is not None:
                    changes[row_id] = None
                continue
            problems.append(f"{table}.id={row_id}: phone is empty")
            continue
        try:
            normalised = normalize_ke_phone(phone)
        except InvalidPhone:
            problems.append(f"{table}.id={row_id}: {phone!r} is not a Kenyan mobile number")
            continue
        seen[(tenant_id, normalised)].append((row_id, phone))
        if normalised != phone:
            changes[row_id] = normalised
    for (tenant_id, normalised), owners in seen.items():
        if len(owners) > 1:
            listed = ", ".join(f"id={i} ({p!r})" for i, p in owners)
            problems.append(f"{table}: {listed} all become {normalised} in tenant {tenant_id}")
    return t, problems, changes


def _normalise_phones(bind):
    plans = [_plan_phone_changes(bind, "customers", nullable=False),
             _plan_phone_changes(bind, "users", nullable=True)]
    problems = [p for _, ps, _ in plans for p in ps]
    if problems:
        raise PhoneMigrationError(
            "Phone numbers must be fixed before this migration can run "
            "(nothing was changed):\n  " + "\n  ".join(problems))
    for t, _, changes in plans:
        for row_id, normalised in changes.items():
            bind.execute(t.update().where(t.c.id == row_id).values(phone_number=normalised))


def _drop_unique(batch, inspector, table, column):
    """Drop every unique constraint and unique index on exactly ``column``."""
    for kind, name in find_uniques(inspector, table, [column]):
        if kind == "index":
            batch.drop_index(name)
        else:
            # Unnamed (SQLite legacy): batch mode names it by the convention.
            batch.drop_constraint(name or f"uq_{table}_{column}", type_="unique")


def _swap_uniques(bind):
    inspector = sa.inspect(bind)
    by_table = defaultdict(list)
    for table, column in PER_TENANT_UNIQUES:
        by_table[table].append(column)

    for table, columns in by_table.items():
        with op.batch_alter_table(table, naming_convention=NAMING_CONVENTION) as batch:
            for column in columns:
                _drop_unique(batch, inspector, table, column)
                batch.create_unique_constraint(f"uq_{table}_tenant_{column}",
                                               ["tenant_id", column])

    email_uniques = find_uniques(inspector, "users", ["email"])
    with op.batch_alter_table("users", naming_convention=NAMING_CONVENTION) as batch:
        if email_uniques != [("constraint", "uq_users_email")]:
            _drop_unique(batch, inspector, "users", "email")
            batch.create_unique_constraint("uq_users_email", ["email"])
        batch.create_unique_constraint("uq_users_tenant_phone_number",
                                       ["tenant_id", "phone_number"])


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade():
    bind = op.get_bind()
    tenants = sa.table("tenants", sa.column("id", sa.Integer), sa.column("name", sa.String),
                       sa.column("vertical", sa.String))
    rows = bind.execute(sa.select(tenants.c.id, tenants.c.name, tenants.c.vertical)).all()
    if len(rows) > 1:
        raise RuntimeError(
            f"Cannot downgrade below 0003 with {len(rows)} tenants: the global "
            "unique constraints would collide. Export and remove all but one tenant first.")

    config = op.create_table(
        "system_config",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_system_config")),
        sa.UniqueConstraint("key", name=op.f("uq_system_config_key")),
    )
    if rows:
        _, name, vertical = rows[0]
        op.bulk_insert(config, [
            {"key": "business_name", "value": name},
            {"key": "business_type", "value": VERTICAL_TO_BUSINESS_TYPE.get(vertical, "general")},
        ])

    with op.batch_alter_table("users", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_constraint("uq_users_tenant_phone_number", type_="unique")

    by_table = defaultdict(list)
    for table, column in PER_TENANT_UNIQUES:
        by_table[table].append(column)
    for table, columns in by_table.items():
        with op.batch_alter_table(table, naming_convention=NAMING_CONVENTION) as batch:
            for column in columns:
                batch.drop_constraint(f"uq_{table}_tenant_{column}", type_="unique")
                batch.create_unique_constraint(f"uq_{table}_{column}", [column])

    for table in reversed(TENANT_TABLES):
        with op.batch_alter_table(table, naming_convention=NAMING_CONVENTION) as batch:
            batch.drop_constraint(f"fk_{table}_tenant_id_tenants", type_="foreignkey")
            batch.drop_index(f"ix_{table}_tenant_id")
            if table not in NULLABLE_TENANT_TABLES:
                batch.alter_column("tenant_id", existing_type=sa.Integer(), nullable=True)
