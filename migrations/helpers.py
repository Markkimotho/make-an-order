"""Shared helpers for revisions. No app imports beyond pure functions."""
import re
import secrets

import sqlalchemy as sa

# The 18 tables that gain tenant_id (every baseline table but system_config).
TENANT_TABLES = (
    "users", "categories", "suppliers", "inventory_items", "customers", "orders",
    "stock_movements", "order_items", "shifts", "cash_movements", "transactions",
    "transaction_items", "purchase_orders", "purchase_order_items", "prescriptions",
    "prescription_items", "loyalty_transactions", "audit_logs",
)

# Tables whose tenant_id stays nullable (platform admins, platform events).
NULLABLE_TENANT_TABLES = ("users", "audit_logs")

# Legacy single-column uniques that become (tenant_id, column), except
# users.email, which stays global.
PER_TENANT_UNIQUES = (
    ("categories", "name"),
    ("customers", "code"),
    ("customers", "phone_number"),
    ("inventory_items", "barcode"),
    ("inventory_items", "sku"),
    ("purchase_orders", "po_number"),
    ("transactions", "transaction_ref"),
)

# Vertical <-> legacy business_type.
BUSINESS_TYPE_TO_VERTICAL = {"pharmacy": "pharmacy"}   # anything else -> distributor
VERTICAL_TO_BUSINESS_TYPE = {"pharmacy": "pharmacy", "distributor": "supermarket",
                             "institution": "general"}


def find_unique_constraint(inspector, table, columns):
    """Name of the unique constraint or unique index over exactly ``columns``.

    Returns None when there is one but it is unnamed (SQLite legacy tables)
    or when there is none; callers that need to tell those apart use
    :func:`has_unique`.
    """
    wanted = list(columns)
    for uq in inspector.get_unique_constraints(table):
        if list(uq["column_names"]) == wanted:
            return uq["name"]
    for ix in inspector.get_indexes(table):
        if ix.get("unique") and list(ix["column_names"]) == wanted:
            return ix["name"]
    return None


def has_unique(inspector, table, columns):
    wanted = list(columns)
    if any(list(uq["column_names"]) == wanted for uq in inspector.get_unique_constraints(table)):
        return True
    return any(ix.get("unique") and list(ix["column_names"]) == wanted
               for ix in inspector.get_indexes(table))


def slugify(name, taken=()):
    """URL slug from a business name, unique against ``taken`` (-2, -3, ...)."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60] or "business"
    slug, n = base, 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


def new_callback_token():
    return secrets.token_urlsafe(24)


def tenants_table():
    """Lightweight Core table for data steps (not the full model)."""
    return sa.table(
        "tenants",
        sa.column("id", sa.Integer), sa.column("name", sa.String),
        sa.column("slug", sa.String), sa.column("vertical", sa.String),
        sa.column("daraja_env", sa.String), sa.column("callback_token", sa.String),
        sa.column("subscription_status", sa.String), sa.column("trial_ends_at", sa.DateTime),
        sa.column("created_at", sa.DateTime),
    )
