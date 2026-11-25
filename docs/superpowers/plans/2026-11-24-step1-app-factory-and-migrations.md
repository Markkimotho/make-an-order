# Step 1: App Factory and Migration Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the app importable and testable with no network I/O, and put the current 19-table schema under Alembic, so step 2 (tenancy) can build on real migrations and a green test suite.

**Architecture:** `app.py` becomes `create_app(config_override=None)`; every route, hook and error handler moves inside it with unchanged endpoint names, and the import-time database, seeding and Africa's Talking calls are removed. Flask-Migrate owns the schema from revision `0001_baseline`, generated from the models under a fixed naming convention. Tests share one `conftest.py` that builds a fresh app on a file-backed SQLite database per test.

**Tech Stack:** Flask 3.0.3, Flask-SQLAlchemy 3.1.1, SQLAlchemy 2.0.58 (pinned `>=2.0.55,<2.1`), Flask-Migrate 4.0.7 / Alembic 1.14.0, pytest 8.3.3.

**Spec:** `docs/superpowers/specs/2026-11-24-vertical-saas-pivot-design.md` (section "On approval", step 1, plus "Migration plan" and "Code layout").

## Global Constraints

- `app = create_app()` stays at the bottom of `app.py`; `Procfile` (`web: gunicorn app:app`) is unchanged.
- Importing `app`, or calling `create_app()`, opens no MySQL, Redis or Africa's Talking connection.
- Endpoint names stay exactly as today (`index`, `dashboard`, `login_page`, `onboarding`, `pos_page`, ...); templates call them with `url_for`.
- `ProxyFix(app.wsgi_app, x_for=PROXY_FIX_X_FOR, x_proto=PROXY_FIX_X_PROTO)`, both from config, default `1`.
- `SQLAlchemy==2.0.58` in `requirements.txt`; the design's floor is `>=2.0.55,<2.1`.
- `migrations/env.py`: `render_as_batch=True`; on SQLite, `PRAGMA foreign_keys=OFF` runs on the connection before `context.begin_transaction()`.
- Batch operations keep Alembic's default `recreate='auto'`; never `'always'`.
- Revision id of the baseline is the literal string `0001_baseline`.
- No application behavior changes in this step. Stale tests are updated to match today's behavior, never the reverse.
- Commit messages carry no Claude co-author line (project rule).

## Review Focus

1. An existing MySQL database built by the old `db.create_all()` is stamped, not upgraded: `flask db stamp 0001_baseline` must leave a schema Alembic sees as matching the models. Test in Task 4 (`test_create_all_database_stamps_clean`).
2. SMS called when Africa's Talking is not configured: it must return `False` and log, never raise, because checkout calls it. Test in Task 1 (`test_send_without_credentials_returns_false`).
3. Building two apps in one process (every test does this): no duplicated log handlers, no blueprint registration error. Test in Task 2 (`test_create_app_twice`).
4. Gunicorn path: `from app import app` gives a fully wired app (POS, payments and auth routes present). Test in Task 2 (`test_module_app_has_all_blueprints`).
5. A request behind Heroku's router: `request.remote_addr` is the forwarded client IP, and a second, untrusted `X-Forwarded-For` hop is not believed. Test in Task 2 (`test_proxy_fix_trusts_one_hop`).

---

### Task 1: Dependencies and I/O-free service imports

**Files:**
- Modify: `requirements.txt` (SQLAlchemy line)
- Modify: `services/sms_service.py:1-40`
- Test: `tests/test_sms_service.py` (new)

**Interfaces:**
- Produces: `services.sms_service._client() -> object | None` returns the Africa's Talking SMS client, initialising it on first call; `None` when `AT_USERNAME` or `AT_API_KEY` is unset or initialisation raises. `_send(message, recipients) -> bool` keeps its signature. `SendSMS` methods unchanged.

- [ ] **Step 1: Check the changelog claim before pinning**

Read https://docs.sqlalchemy.org/en/20/changelog/changelog_20.html, section 2.0.55. Confirm two entries: `with_loader_criteria()` reusing cached bound parameters with `union()`/`CompoundSelect`, and `ORMExecuteState.is_update` / `is_delete` with `Select.from_statement()`. Write what you found (confirmed / not found, with the issue numbers) into the commit message of Step 7. If neither is there, still pin 2.0.58 (newest 2.0 on PyPI) and say so.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_sms_service.py
import importlib, sys

def _fresh_sms(monkeypatch, **env):
    for key in ("AT_USERNAME", "AT_API_KEY", "AT_SENDER_ID"):
        monkeypatch.delenv(key, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("config", None)
    sys.modules.pop("services.sms_service", None)
    return importlib.import_module("services.sms_service")

def test_import_without_credentials_does_not_raise(monkeypatch):
    _fresh_sms(monkeypatch)

def test_send_without_credentials_returns_false(monkeypatch):
    sms = _fresh_sms(monkeypatch)
    assert sms.SendSMS.send_payment_confirmation("0712345678", 100, "TXN1") is False

def test_client_initialises_once(monkeypatch):
    sms = _fresh_sms(monkeypatch, AT_USERNAME="sandbox", AT_API_KEY="k")
    calls = []
    monkeypatch.setattr(sms.africastalking, "initialize", lambda u, k: calls.append((u, k)))
    monkeypatch.setattr(sms.africastalking, "SMS", object(), raising=False)
    sms._client(); sms._client()
    assert calls == [("sandbox", "k")]
```

- [ ] **Step 3: Run to verify they fail**

Run: `pytest tests/test_sms_service.py -v`
Expected: collection error `RuntimeError: Invalid username and/or api_key` (today's import-time initialise).

- [ ] **Step 4: Make `services/sms_service.py` lazy**

Delete the module-level `africastalking.initialize(...)` and `sms = africastalking.SMS`. Add `_client()` with a module-level cache (`_sms = None`, `_initialised = False`); `_send` calls `_client()` and returns `False` with a warning log when it is `None`. Read `Config.AT_*` inside `_client()`, not at import.

- [ ] **Step 5: Pin SQLAlchemy**

In `requirements.txt` replace `SQLAlchemy==2.0.36` with `SQLAlchemy==2.0.58`. Install into the dev venv: `pip install -r requirements.txt`.

- [ ] **Step 6: Run to verify they pass, and that nothing else regressed**

Run: `pytest tests/test_sms_service.py tests/test_pos_services.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt services/sms_service.py tests/test_sms_service.py
git commit -m "fix: lazy Africa's Talking client, pin SQLAlchemy 2.0.58"
```

---

### Task 2: Application factory

**Files:**
- Modify: `app.py` (whole file restructured), `config.py`, `models.py:1-4`, `.env.example`
- Test: `tests/test_app_factory.py` (new)

**Interfaces:**
- Consumes: lazy `services.sms_service` from Task 1.
- Produces:
  - `app.create_app(config_override: dict | None = None) -> Flask`. Order inside: load `Config`, apply `config_override`, `ProxyFix`, logging, `db.init_app`, `Migrate(app, db)`, OAuth registration, blueprints, core routes, CLI commands. Returns the app.
  - `app.app`: module-level `create_app()` result.
  - `app.BUSINESS_CONFIGS`, `app.get_user_context()`: stay module-level, unchanged.
  - CLI `flask create-db`: wraps today's `services.database_service.create_database()`.
  - `models.db = SQLAlchemy(metadata=MetaData(naming_convention=NAMING_CONVENTION))` with
    `NAMING_CONVENTION = {"ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s", "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s", "pk": "pk_%(table_name)s"}` exported from `models.py`.
  - `Config.PROXY_FIX_X_FOR`, `Config.PROXY_FIX_X_PROTO`: `int(os.environ.get(..., "1"))`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_app_factory.py
import subprocess, sys, textwrap
from pathlib import Path
from app import create_app
from models import db, NAMING_CONVENTION

def _app(tmp_path, **extra):
    return create_app({"TESTING": True, "SECRET_KEY": "t",
                       "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path/'t.db'}", **extra})

def test_import_does_no_network_io():
    code = textwrap.dedent("""
        import MySQLdb, redis, socket
        def boom(*a, **k): raise AssertionError("network I/O at import")
        MySQLdb.connect = boom
        redis.Redis.from_url = boom
        socket.create_connection = boom
        import app
        assert app.app is not None
    """)
    env = {"PATH": "/usr/bin:/bin", "MYSQL_HOST": "10.255.255.1"}
    repo = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

def test_create_app_twice(tmp_path):
    a, b = _app(tmp_path / "a"), _app(tmp_path / "b")
    assert a is not b
    assert len(a.logger.handlers) == len(b.logger.handlers)

def test_module_app_has_all_blueprints():
    from app import app
    rules = {r.rule for r in app.url_map.iter_rules()}
    for path in ("/pos/transaction/new", "/payments/mpesa/callback", "/auth/login", "/dashboard", "/onboarding"):
        assert path in rules

def test_proxy_fix_trusts_one_hop(tmp_path):
    app = _app(tmp_path)
    app.add_url_rule("/_ip", "_ip", lambda: __import__("flask").request.remote_addr)
    r = app.test_client().get("/_ip", headers={"X-Forwarded-For": "6.6.6.6, 196.201.214.200"})
    assert r.get_data(as_text=True) == "196.201.214.200"

def test_proxy_fix_hops_configurable(tmp_path):
    app = _app(tmp_path, PROXY_FIX_X_FOR=2)
    app.add_url_rule("/_ip", "_ip", lambda: __import__("flask").request.remote_addr)
    r = app.test_client().get("/_ip", headers={"X-Forwarded-For": "6.6.6.6, 196.201.214.200"})
    assert r.get_data(as_text=True) == "6.6.6.6"

def test_naming_convention_on_metadata():
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}
    for key, pattern in NAMING_CONVENTION.items():
        assert db.metadata.naming_convention[key] == pattern

def test_no_tables_created_at_startup(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        assert db.inspect(db.engine).get_table_names() == []
```

`test_proxy_fix_hops_configurable` needs `PROXY_FIX_X_FOR` read from `app.config` after the override, so apply `ProxyFix` after `config_override`.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_app_factory.py -v`
Expected: `ImportError: cannot import name 'create_app'`.

- [ ] **Step 3: Restructure `app.py`**

- Move the bodies of `migrate_schema()`, the `with app.app_context()` startup block, `print(...)` progress lines and the module-level `create_database()` call out. Delete `migrate_schema()` entirely (Alembic replaces it). `create_database()` becomes the `create-db` CLI command.
- Every `@app.route`, `@app.before_request`, `@app.errorhandler` moves into `_register_core_routes(app)`, called from `create_app`, using `app.add_url_rule(..., endpoint=<same name>)` or nested decorators. Endpoint names unchanged.
- `app.secret_key = os.getenv("APP_SECRET_KEY")` is dropped; `Config.SECRET_KEY` already reads it and `config_override` can replace it.
- Logging: attach the rotating file handler only when `not app.debug and not app.testing`, and only if no `RotatingFileHandler` is already on `app.logger`.
- Startup seeding is removed. `onboarding()` still calls `seed_inventory(btype)` as today.

- [ ] **Step 4: Add `NAMING_CONVENTION` and the metadata to `models.py`; add the two `PROXY_FIX_*` settings to `config.py` and `.env.example`**

- [ ] **Step 5: Run to verify they pass**

Run: `pytest tests/test_app_factory.py tests/test_sms_service.py -v`
Expected: all PASS.

- [ ] **Step 6: Smoke the real entry points**

Run: `env -i PATH=$PATH HOME=$HOME python -c "import app; print(len(list(app.app.url_map.iter_rules())))"` → prints a number, no traceback.
Run: `gunicorn --check-config app:app` → exits 0.

- [ ] **Step 7: Commit**

```bash
git add app.py config.py models.py .env.example tests/test_app_factory.py
git commit -m "refactor: create_app factory, configurable ProxyFix, no I/O at import"
```

---

### Task 3: Shared test fixtures and stale test repair

**Files:**
- Create: `tests/conftest.py`
- Modify: `tests/test_auth.py`, `tests/test_customers.py`, `tests/test_models.py`, `tests/test_orders.py`, `tests/test_pos_models.py`

**Interfaces:**
- Consumes: `create_app`, `models.db` from Task 2.
- Produces (fixtures in `tests/conftest.py`):
  - `app` → `create_app({"TESTING": True, "SECRET_KEY": "test", "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path/'app.db'}"})`, inside an app context, `db.create_all()` before yield, `db.session.remove(); db.drop_all()` after. Task 4 swaps `create_all` for `upgrade`.
  - `client` → `app.test_client()`.
  - `login_as(role: str = "admin", email: str = "test@example.com") -> FlaskClient`: a fixture returning a function that sets `profile`, `role`, `user_id` in the session of `client` and returns it.

Baseline before this task (fake AT credentials, SQLite): 47 pass, 29 fail. Every failure is a test written before role-based access (`2e3e4a5`) or before later model changes. The fixes, by cause:

| Cause | Tests | Fix |
|---|---|---|
| Routes now need admin (`admin_api_required`) or superuser | all of `test_customers.py` and `test_orders.py` that expect success | use `login_as("admin")`; update and delete customer routes need `login_as("superuser")` (`api/customers.py:100,128`) |
| Unauthenticated API calls now return JSON 401, not a 302 redirect | `test_*_unauthorized` | assert `401` |
| `/login` renders the login page (200); `/` renders the landing page when logged out and redirects only when `profile` is set | `test_auth.py` | assert today's codes; `/logout` redirects to `/` with no message |
| `Customer.to_dict()` gained loyalty and credit fields | `test_models.py::test_customer_to_dict` | assert the full current dict |
| `PurchaseOrder` has no `total`; item FK is `po_id`; no `line_total` | `test_pos_models.py::TestPurchaseOrder::test_create_po_with_items` | use the current columns (`models.py:427-479`) |

- [ ] **Step 1: Write `tests/conftest.py` with the three fixtures above**

- [ ] **Step 2: Remove every per-file `client` fixture and the `from app import app, db` lines; import `db` from `models`; apply the fixes in the table**

Keep each test's intent. Where a test asserted behavior that no longer exists (the logout message), assert what the route does today and keep the test.

- [ ] **Step 3: Run the whole suite with no environment at all**

Run: `env -i PATH=$PATH HOME=$HOME pytest -q`
Expected: every test passes (76 existing plus Tasks 1-2), no MySQL, no credentials.

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: shared app fixtures, update stale tests to current auth and models"
```

---

### Task 4: Alembic baseline

**Files:**
- Create: `migrations/` (via `flask db init`), `migrations/versions/0001_baseline.py`
- Modify: `migrations/env.py`, `tests/conftest.py`, `README.md`
- Test: `tests/test_migrations.py` (new)

**Interfaces:**
- Consumes: `create_app`, `NAMING_CONVENTION`, fixtures from Task 3.
- Produces:
  - Revision `0001_baseline` (`down_revision = None`): creates the 19 current tables exactly as the models declare them, every constraint and index named by `NAMING_CONVENTION`; `downgrade()` drops them in reverse FK order.
  - `migrations/env.py: disable_sqlite_foreign_keys(connection) -> None`: runs `PRAGMA foreign_keys=OFF` when `connection.dialect.name == "sqlite"`, otherwise does nothing. Called in `run_migrations_online()` before `context.begin_transaction()`.
  - `tests/conftest.py` `app` fixture now runs `flask_migrate.upgrade()` instead of `db.create_all()`, and `downgrade(revision="base")` on teardown.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_migrations.py
import sqlite3
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from flask_migrate import upgrade, downgrade, stamp
from app import create_app
from models import db

def _app(tmp_path):
    return create_app({"TESTING": True, "SECRET_KEY": "t",
                       "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path/'m.db'}"})

def _diff(app):
    with app.app_context(), db.engine.connect() as conn:
        return compare_metadata(MigrationContext.configure(conn, opts={"render_as_batch": True}), db.metadata)

def test_round_trip(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        upgrade()
        assert len(db.inspect(db.engine).get_table_names()) == 20   # 19 + alembic_version
        downgrade(revision="base")
        assert db.inspect(db.engine).get_table_names() == ["alembic_version"]
        upgrade()

def test_baseline_matches_models(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        upgrade()
    assert _diff(app) == []

def test_every_constraint_named(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        upgrade()
        insp = db.inspect(db.engine)
        for t in insp.get_table_names():
            assert all(fk["name"] for fk in insp.get_foreign_keys(t)), t
            assert all(uq["name"] for uq in insp.get_unique_constraints(t)), t

def test_create_all_database_stamps_clean(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        db.create_all()
        stamp(revision="0001_baseline")
        upgrade()          # no-op, must not raise
    assert _diff(app) == []
```

The foreign-key helper must be importable without running Alembic's `env.py` (which executes at import). Put `disable_sqlite_foreign_keys` in `migrations/env_helpers.py`, import it from `env.py`, and test it directly:

```python
def test_disable_sqlite_foreign_keys(tmp_path):
    from sqlalchemy import create_engine
    from migrations.env_helpers import disable_sqlite_foreign_keys
    eng = create_engine(f"sqlite:///{tmp_path/'fk.db'}")
    with eng.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        disable_sqlite_foreign_keys(conn)
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
```

Add an empty `migrations/__init__.py` so the import resolves.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_migrations.py -v`
Expected: FAIL, no `migrations` directory.

- [ ] **Step 3: `flask db init`, then edit `migrations/env.py`**

Set `render_as_batch=True` in both `context.configure(...)` calls (offline and online). In `run_migrations_online()`, call `disable_sqlite_foreign_keys(connection)` right after the connection opens and before `with context.begin_transaction():`.

- [ ] **Step 4: Generate the baseline**

Against an empty SQLite file: `SQLALCHEMY_DATABASE_URI=sqlite:////tmp/baseline.db flask db migrate --rev-id 0001_baseline -m baseline`. Rename the file to `migrations/versions/0001_baseline.py`. Review it by hand: 19 `create_table` calls; every `ForeignKeyConstraint`, `UniqueConstraint`, `PrimaryKeyConstraint` and index carries a `name=`; server defaults (`server_default=sa.text(...)`) match `models.py`; downgrade drops child tables before parents.

- [ ] **Step 5: Switch `tests/conftest.py` `app` fixture to `upgrade()` / `downgrade(revision="base")`**

- [ ] **Step 6: Run the whole suite**

Run: `env -i PATH=$PATH HOME=$HOME pytest -q`
Expected: every test passes, including all of `tests/test_migrations.py`.

- [ ] **Step 7: Update `README.md` setup section**

Replace the "tables are created on startup" instructions with:
- New database: `flask create-db` (local MySQL only), then `flask db upgrade`.
- Existing database created by the old startup code: `flask db stamp 0001_baseline` once, then `flask db upgrade` from then on.
- Tests: `pytest` needs no database, credentials or network.

- [ ] **Step 8: Commit and push**

```bash
git add migrations/ tests/ README.md
git commit -m "feat: Alembic baseline 0001 with named constraints and SQLite-safe env"
git push
```

---

## Done when

- `env -i PATH=$PATH HOME=$HOME pytest -q` is all green on a machine with no MySQL, Redis or `.env`.
- `from app import app` works for gunicorn; `gunicorn --check-config app:app` exits 0.
- `flask db upgrade` on an empty database produces a schema with zero autogenerate diff against the models.
- Then stop for review before step 2.
