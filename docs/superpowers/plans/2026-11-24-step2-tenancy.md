# Step 2: Tenancy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every row belongs to a tenant and no request, CLI command or test can read or write another tenant's rows, on SQLite and MySQL, with existing data migrated into a default tenant.

**Architecture:** A `TenantMixin` on every model plus two session hooks in `auth/tenancy.py`: `do_orm_execute` scopes every ORM select, bulk update and bulk delete to the bound tenant and refuses anything it cannot scope; `before_flush` stamps and checks `tenant_id` on writes. The bound tenant lives in a `ContextVar`; a `before_request` hook binds it from the session for every request, so no route can skip it. Migrations `0002` (expand) and `0003` (contract) move existing data into a default tenant.

**Tech Stack:** as step 1, plus `cryptography` (already installed) for Fernet-encrypted tenant secrets. CI: GitHub Actions with a `mysql:5.7` service.

**Spec:** `docs/superpowers/specs/2026-11-24-vertical-saas-pivot-design.md` (Tenancy, Schema, Migration plan, Tests). Step 1 plan for conventions: `docs/superpowers/plans/2026-11-24-step1-app-factory-and-migrations.md`.

## Global Constraints

- Fail closed: an ORM query on a tenant model with no tenant bound raises `TenantNotBound`; it never returns all rows.
- `users.tenant_id` may be NULL only when `is_platform_admin` is true; `audit_logs.tenant_id` may be NULL only for rows written inside `unscoped()`. Every other tenant column is NOT NULL.
- Phones stored as `254XXXXXXXXX` (`services/phone.py: normalize_ke_phone`), enforced by `@validates` on `User.phone_number` and `Customer.phone_number`.
- `email` stays globally unique on `users`; the other eight legacy uniques become `(tenant_id, column)`.
- Migrations: `op.batch_alter_table(..., naming_convention=NAMING_CONVENTION)`, `recreate='auto'`; existing constraint names found by column with the inspector, never assumed.
- Every test runs on SQLite locally and on MySQL 5.7 in CI from the same code.
- No commit carries a Claude co-author line.

## Decisions made in this plan (Mark can overturn)

- **`superuser` role now means tenant owner.** The first user of a tenant gets it; every existing `superuser` gate (user management, customer update/delete) keeps working inside that tenant. Platform operators are `users.is_platform_admin`, set at login from env `PLATFORM_ADMIN_EMAILS` (replaces the hard-coded `SUPERUSER_EMAIL`).
- **`/register` becomes business signup:** business name, vertical, owner name, email, phone, password. Creates the tenant (14-day trial) and its owner, approved. Staff are added by the owner through the existing users API, now tenant-scoped. The old "register and wait for approval" path goes, since a pending user would belong to no business.
- **Google login no longer creates accounts.** An unknown Google email is sent to signup with a message; a known one logs into its tenant.
- **`/onboarding` becomes the owner's business settings page** (name and vertical). `BUSINESS_CONFIGS` shrinks to `pharmacy`, `distributor`, `institution`. Seed catalogues: pharmacy keeps its own, distributor uses the supermarket catalogue, institution the general one.
- **Platform admins act inside a tenant** by choosing it on `/platform/tenants`; that only sets `session['tenant_id']`.

## Review Focus

1. A lazy-loaded relationship, a `union()`, or a `session.get()` after a tenant switch returning another tenant's row. Tests in Task 4.
2. A route that takes an `<int:id>` and returns, edits or deletes tenant A's row when called by tenant B. Sweep test in Task 7.
3. Migrating a real legacy database: unnamed MySQL uniques, a phone that will not normalise, two customers whose phones collide once normalised. Tests in Task 5.
4. A logged-in session whose user was removed from the tenant, or whose tenant's trial ended: the next request must not still act as that tenant. Tests in Task 6.
5. Background code (CLI, future scheduler) running with nothing bound: must raise, not read across tenants. Test in Task 4.

---

### Task 1: MySQL CI lane

**Files:**
- Create: `.github/workflows/tests.yml`
- Modify: `tests/conftest.py`, `tests/test_migrations.py`, `tests/legacy_schema.py`
- Create: `scripts/local_mysql.sh`

**Interfaces:**
- Produces: fixture `database_url` (function scope): `sqlite:///<tmp_path>/db.sqlite` by default; when env `TEST_MYSQL_URL` (server URL, no database, for example `mysql+pymysql://root:pw@127.0.0.1:3306`) is set, creates database `t_<uuid8>`, yields its URL, drops it after. The `app` fixture and `tests/test_migrations.py::_app` take their URI from it.
- Produces: `tests/legacy_schema.py: create_legacy_schema(engine)` builds the 0001 shape by upgrading a scratch SQLite to `0001_baseline`, reflecting it, nulling constraint names and creating that on `engine`. It no longer reads the live models (which gain tenant columns in Task 3).
- Produces: `scripts/local_mysql.sh start|stop|url`: private `mysqld` (Homebrew) with datadir `/tmp/mao-mysql`, port `33071`, socket `/tmp/mao-mysql.sock`, `--initialize-insecure`. Never touches the developer's own MySQL.

- [ ] **Step 1:** Add the `database_url` fixture and route both app builders through it.
- [ ] **Step 2:** Make `test_legacy_database_stamps_with_only_unique_name_drift` dialect-aware: on MySQL the legacy uniques reflect as named indexes, so collect `(table, column)` from both `add_constraint` and `remove_index`/`remove_constraint` ops; the set must still equal `LEGACY_UNIQUE_COLUMNS`. Skip `test_disable_sqlite_foreign_keys` when not on SQLite.
- [ ] **Step 3:** Run the whole suite against the local private MySQL: `scripts/local_mysql.sh start && TEST_MYSQL_URL=$(scripts/local_mysql.sh url) pytest -q`. Expected: all pass. Fix anything MySQL-specific in code, not by skipping.
- [ ] **Step 4:** `.github/workflows/tests.yml`: on `push` (all branches) and `pull_request`. Job `sqlite`: Python 3.11, `pip install -r requirements.txt`, `pytest -q`. Job `mysql57`: service `mysql:5.7` (`MYSQL_ROOT_PASSWORD=root`, health check `mysqladmin ping`), `TEST_MYSQL_URL=mysql+pymysql://root:root@127.0.0.1:3306`, `pytest -q`. No secrets, no `.env`, no deploy.
- [ ] **Step 5:** Commit, push, and watch the run with `gh run watch`. Expected: both jobs green. A red MySQL 5.7 job is fixed before Task 2 starts.

---

### Task 2: Phone normaliser and encrypted column type

**Files:**
- Create: `services/phone.py`, `models/types.py`
- Modify: `services/mpesa_service.py:48-67` (delegate)
- Test: `tests/test_phone.py`, `tests/test_encrypted_type.py`

**Interfaces:**
- Produces: `normalize_ke_phone(raw: str) -> str` returning `254XXXXXXXXX`; raises `InvalidPhone(ValueError)`. Accepts `07…`, `01…`, `+254…`, `254…`, `7…`, `1…`, with spaces/dashes. `mpesa_service.format_phone` becomes a thin alias (existing `tests/test_pos_services.py::TestFormatPhone` must still pass).
- Produces: `EncryptedString(length: int)`: SQLAlchemy `TypeDecorator` over `String`, Fernet with key from `app.config["TENANT_SECRETS_KEY"]` (falls back to env `TENANT_SECRETS_KEY`); `None` stays `None`; raises `SecretsKeyMissing` when a non-null value is written with no key. Ciphertext stored, plaintext returned.

- [ ] **Step 1: Tests.** `test_phone.py`: the seven valid forms from `TestFormatPhone` plus `"0110 123 456"` → `"254110123456"`; invalid: `""`, `"12345"`, `"+25756098389"` (Burundi), `"0812345678"` (not a Kenyan mobile prefix: only `7` and `1` are accepted after the country code). `test_encrypted_type.py`: round trip through a table, raw column value is not the plaintext, `None` round-trips, missing key raises.
- [ ] **Step 2:** Run, see them fail. Implement. Run, see them pass.
- [ ] **Step 3:** Commit `feat: Kenyan phone normaliser and Fernet-encrypted column type`.

---

### Task 3: Models package, Tenant model, TenantMixin

**Files:**
- Create: `models/__init__.py`, `models/base.py`, `models/core.py`; delete `models.py`
- Modify: `.env.example`, `config.py` (`TENANT_SECRETS_KEY`, `PLATFORM_ADMIN_EMAILS`)
- Test: `tests/test_tenant_models.py`

**Interfaces:**
- Produces `models/base.py`: `db`, `NAMING_CONVENTION` (moved verbatim), `VERTICALS = ("pharmacy", "distributor", "institution")`, `SUBSCRIPTION_STATUSES = ("trial", "active", "past_due", "cancelled")`, `TenantMixin` (declared attr `tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)`), `Tenant` model with the spec's `tenants` columns (encrypted ones as `EncryptedString`; `callback_token` default `secrets.token_urlsafe(24)`; `trial_ends_at` default now + 14 days) and `Tenant.is_active_subscription() -> bool` (trial not past `trial_ends_at`, or `active`).
- Produces `models/core.py`: every existing model, each with `TenantMixin`; `AuditLog.tenant_id` and `User.tenant_id` overridden `nullable=True`; `User.is_platform_admin = db.Column(db.Boolean, default=False, nullable=False)`; the eight per-tenant uniques as `UniqueConstraint("tenant_id", <col>)` in `__table_args__` (names from the convention: `uq_<table>_tenant_id`; give each an explicit name `uq_<table>_tenant_<col>` instead, since the convention uses only column 0); `@validates("phone_number")` on `User` (nullable) and `Customer` calling `normalize_ke_phone`. `SystemConfig` is removed.
- `models/__init__.py` re-exports every name the old module exported, so `from models import X` keeps working everywhere.

- [ ] **Step 1: Tests** in `test_tenant_models.py`: every mapped class except `Tenant` subclasses `TenantMixin` (one-item allowlist); `Tenant.slug` unique; trial default 14 days; secrets columns never in `Tenant.to_dict()`; `Customer(phone_number="0712 345 678").phone_number == "254712345678"`; a bad phone raises `InvalidPhone`.
- [ ] **Step 2:** Implement. Existing tests will break on missing tenant binding until Task 4; keep them red only inside this task's branch commit by running just `tests/test_tenant_models.py` (with tenancy hooks not yet installed, writes work).
- [ ] **Step 3:** Commit `feat: models package with Tenant and TenantMixin`.

---

### Task 4: Tenancy hooks

**Files:**
- Create: `auth/tenancy.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_tenancy_hooks.py`

**Interfaces:**
- Produces in `auth/tenancy.py`:
  - Exceptions: `TenantNotBound(RuntimeError)`, `CrossTenantWrite(RuntimeError)`, `UnscopedStatement(RuntimeError)`.
  - `current_tenant_id() -> int | None`; `bind_tenant(tenant_id: int | None) -> contextvars.Token`; `reset_tenant(token) -> None`; `@contextmanager tenant_scope(tenant_id)`; `@contextmanager unscoped()`; `in_unscoped() -> bool`. Changing the bound id (bind, reset, entering/leaving a scope or `unscoped`) calls `db.session.expunge_all()` only when the effective id changes.
  - Listeners on `sqlalchemy.orm.Session`, registered once at module import (guarded by a module flag), and `models/__init__.py` imports `auth.tenancy` so they are always installed:
    - `do_orm_execute`: if `in_unscoped()`: pass through. Else: non-ORM statement → `UnscopedStatement`; `is_insert` on a tenant mapper → `UnscopedStatement`; any tenant mapper in `state.all_mappers` with nothing bound → `TenantNotBound`; `is_select` → `.options(with_loader_criteria(TenantMixin, lambda cls: cls.tenant_id == tid, include_aliases=True))` with `tid` a local; `is_update`/`is_delete` on a tenant mapper → `.where(mapper.class_.tenant_id == tid)`.
    - `before_flush`: for `session.new` tenant objects: stamp `tenant_id` when None and bound; allow None only for `AuditLog` inside `unscoped()` and for `User` with `is_platform_admin`; otherwise None and unbound → `TenantNotBound`; a set `tenant_id` different from the bound one → `CrossTenantWrite`. For `session.dirty`: a changed `tenant_id` → `CrossTenantWrite`.
- `tests/conftest.py`: `app` fixture creates tenant `acme` (pharmacy) inside `unscoped()` and binds it for the test; new fixture `other_tenant` creates `globex` (distributor) and returns its id. `login_as` also sets `session["tenant_id"]` (default `acme`).

- [ ] **Step 1: Tests** in `test_tenancy_hooks.py` (two tenants, rows with the same SKU and phone in each):
  - unbound `Customer.query.all()` raises `TenantNotBound`; unbound `db.session.get(Customer, 1)` raises.
  - bound A sees only A's customers via `query`, `select()`, `paginate`, a join, and a lazy-loaded `customer.orders`.
  - same statement object executed under A then B returns each tenant's rows (catches a cached tenant id); same for a `union()` of two selects.
  - `session.get(Customer, a_id)` after switching to B returns `None`.
  - bulk `update(Customer).values(name="x")` and `delete(Order)` under B leave A untouched; legacy `Customer.query.update(...)` too.
  - `session.execute(text("select 1"))` and `session.execute(insert(Customer).values(...))` raise `UnscopedStatement`; both work inside `unscoped()`.
  - adding a customer with A's `tenant_id` while bound to B raises `CrossTenantWrite`; moving a customer to another tenant raises.
  - `AuditLog` with no tenant: allowed in `unscoped()`, refused outside. `User(tenant_id=None, is_platform_admin=False)` refused.
  - `tenant_scope` nesting restores the outer tenant on exit; leaving `unscoped()` empties the identity map.
- [ ] **Step 2:** Run, see them fail. Implement. Run the new tests, see them pass, then the whole suite (old tests now run inside `acme`). Fix fixtures, not hooks, when an old test was relying on no tenant.
- [ ] **Step 3:** Commit `feat: fail-closed tenant scoping hooks`.

---

### Task 5: Migrations 0002 and 0003

**Files:**
- Create: `migrations/versions/0002_tenants_expand.py`, `migrations/versions/0003_tenants_contract.py`, `migrations/helpers.py`
- Modify: `tests/test_migrations.py`
- Test: `tests/test_migrations_tenancy.py`

**Interfaces:**
- Produces `migrations/helpers.py`: `find_unique_constraint(inspector, table, columns) -> str | None` (name of the unique constraint or unique index over exactly `columns`, else None); `TENANT_TABLES` (the 18 tables).
- `0002_tenants_expand` (`down_revision="0001_baseline"`): create `tenants` (all spec columns); add nullable `tenant_id` to the 18 tables; add `users.is_platform_admin` (server default false). If any of the 18 tables has a row: insert the default tenant (name from `system_config.business_name` or `"Default Business"`, slug from the name, vertical `pharmacy` if `business_type == "pharmacy"` else `distributor`, status `active`) and set `tenant_id` on every row. Downgrade: drop the column, `is_platform_admin`, and `tenants`.
- `0003_tenants_contract` (`down_revision="0002_tenants_expand"`): (1) normalise `customers.phone_number` and `users.phone_number` with `normalize_ke_phone`; collect every unparseable value and every collision (two rows, same tenant, same normalised phone) and raise one `RuntimeError` listing them all before changing anything; (2) per table, in `batch_alter_table(table, naming_convention=NAMING_CONVENTION)`: `tenant_id` NOT NULL (except `users`, `audit_logs`), FK `fk_<table>_tenant_id_tenants` with `ON DELETE CASCADE`, index `ix_<table>_tenant_id`; (3) drop each legacy unique found by `find_unique_constraint` and create the per-tenant one (`users.email` renamed to `uq_users_email`); (4) drop `system_config`. Downgrade: refuses with a clear error if more than one tenant exists; otherwise restores the global uniques and nullable columns and recreates `system_config` from the tenant's name and vertical.

- [ ] **Step 1: Tests** in `test_migrations_tenancy.py`, each starting from `create_legacy_schema` + rows + `stamp("0001_baseline")`:
  - empty database → `upgrade` creates no tenant.
  - rows and `system_config` (`business_type=pharmacy`, `business_name="Kilimani Chemist"`) → one tenant named that, vertical `pharmacy`, every row carries its id, `system_config` gone.
  - rows but empty `system_config` → `"Default Business"`, `distributor`.
  - customer phones `0712345678` and `+254 722 000 111` end as `254712345678`, `254722000111`.
  - an unparseable phone, and separately two customers `0712345678` / `254712345678`: upgrade raises, the error names every offending row, and the schema is still at `0002` (step 0003 changed nothing).
  - after upgrade, `compare_metadata` against the models is `[]` (closes the stamping drift from step 1's review).
  - downgrade to `0001_baseline` with one tenant restores the legacy shape; with two tenants it refuses.
- [ ] **Step 2:** Replace step 1's drift test: it now upgrades to head and asserts zero diff.
- [ ] **Step 3:** Run on SQLite, then on local MySQL (`scripts/local_mysql.sh`). Both green.
- [ ] **Step 4:** Commit `feat: migrations 0002/0003 move data into a default tenant`.

---

### Task 6: Request binding, auth, signup, platform admins

**Files:**
- Modify: `app.py`, `auth/auth_middleware.py`, `auth/auth_routes.py`, `api/users.py`, `api/customers.py`, `services/seed_service.py`, `templates/register.html`, `templates/onboarding.html`, `templates/index.html` (message display only)
- Create: `api/platform.py`, `templates/platform_tenants.html`, `templates/trial_ended.html`
- Test: `tests/test_tenant_auth.py`

**Interfaces:**
- `app.py`: `before_request` `bind_request_tenant()`: if the session has `user_id`, load the user inside `unscoped()`; clear the session and redirect to login (API: 401) if the user is gone, not approved, or no longer in `session["tenant_id"]` (platform admins exempt from the membership check). Load the tenant; if `not tenant.is_active_subscription()`: UI renders `trial_ended.html` (402), API returns `402 {"error": "Subscription inactive"}`; platform admins pass. Bind its id, set `g.current_tenant`; `teardown_request` resets the token. `get_user_context()` reads name and vertical from `g.current_tenant`. `BUSINESS_CONFIGS` keys: `pharmacy`, `distributor`, `institution`.
- `auth/auth_middleware.py`: `tenant_required` (UI redirect / API 401 when `g.current_tenant` is missing); every existing role decorator runs it first.
- `auth/auth_routes.py`: `_populate_session` also stores `tenant_id`; local login and Google login look the user up inside `unscoped()`; Google login no longer creates users; `is_platform_admin` set from `Config.PLATFORM_ADMIN_EMAILS` (comma-separated) at every login. `/auth/register` (POST) is the business signup, writing tenant and owner (`role="superuser"`, `status="approved"`) in one transaction inside `unscoped()`; slug from the business name, made unique with `-2`, `-3`.
- `api/platform.py` blueprint `/platform`: `GET /tenants` (list) and `POST /tenants/<slug>/act` (set `session["tenant_id"]`), both platform-admin only.
- `api/customers.py`, `api/users.py`: an `InvalidPhone` returns 400 with `{"error": "Invalid Kenyan phone number"}` instead of 500. `api/users.py` role choices add `cashier`, `pharmacist`, `stock_clerk`.

- [ ] **Step 1: Tests** in `test_tenant_auth.py`: signup creates tenant + owner and logs in; duplicate email refused; second business with the same name gets `-2` slug; login binds the right tenant; a user deleted mid-session is logged out on the next request; a user switched to another tenant mid-session is logged out; expired trial gives 402 on UI and API, platform admin still passes; Google callback for an unknown email redirects to `/register` and creates nothing; `PLATFORM_ADMIN_EMAILS` sets the flag; platform admin can act as a tenant, a normal owner gets 403 on `/platform/*`; register customer with phone `"+25756098389"` → 400.
- [ ] **Step 2:** Run, fail, implement, pass. Update `test_customers.py` / `test_orders.py` to Kenyan numbers.
- [ ] **Step 3:** Commit `feat: tenant-bound requests, business signup, platform admins`.

---

### Task 7: Isolation sweep and bypass ban

**Files:**
- Test: `tests/test_isolation_sweep.py`, `tests/test_no_bypass.py`

**Interfaces:** consumes everything above.

- [ ] **Step 1:** `test_isolation_sweep.py`: seed tenant A with one row in every table, each text field containing the marker `ALPHA-SECRET`. Log in as tenant B's owner. For every URL rule with an `<int:…>` argument and method GET, PUT or DELETE, call it with each of A's ids (PUT with body `{"name": "pwned"}`). Assert: no response body contains `ALPHA-SECRET`; no status is 5xx; after the sweep, every A row is byte-for-byte unchanged (compare a dump taken before, inside `unscoped()`). Also: list endpoints (`/customers/view_customers`, `/orders/view_orders`, `/inventory/items`, `/pos/...` lists, `/reports/*`) as B contain no marker.
- [ ] **Step 2:** `test_no_bypass.py`: AST scan of `api/`, `services/`, `auth/`, `app.py` for `text(`, `.__table__`, `db.engine`, `.connection(`, `bulk_insert_mappings`, `bulk_update_mappings`, `insert(`, `db.metadata.tables`; allowlist is empty. And: every `unscoped()` call site is in the allowlist `{auth/tenancy.py, auth/auth_routes.py, app.py:bind_request_tenant, api/platform.py}`; a new call site fails the test until added deliberately.
- [ ] **Step 3:** Fix every leak found in the route code (expected: none, since scoping is in the session; any hit is a real bug and gets its own regression test).
- [ ] **Step 4:** Full suite on SQLite and local MySQL; push; CI green on both lanes.
- [ ] **Step 5:** Commit `test: cross-tenant route sweep and filter-bypass ban`.

---

## Done when

- Both CI lanes green on `feat/vertical-saas`.
- `flask db upgrade` on a copy of a legacy database (built by `create_legacy_schema` on MySQL) ends at head with zero model drift.
- Codex review (gpt-6.1-sol, medium) run on the step 2 diff, findings fixed or answered.
- Then stop for review before step 3.
