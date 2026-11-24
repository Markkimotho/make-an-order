"""Static guard: application code cannot step around the tenant filter.

The filter in auth/tenancy.py sees ORM statements only. Raw SQL, Core
table statements, direct engine/connection use and bulk mapping helpers
would silently read or write across tenants, so they are banned outside
models/ and migrations/. unscoped() is allowed only at the call sites
listed below; adding one means adding it here, on purpose, in review.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = [ROOT / "app.py", *(ROOT / d for d in ("api", "auth", "services"))]

BANNED_CALLS = {"text", "insert", "bulk_insert_mappings", "bulk_update_mappings",
                "bulk_save_objects", "connection", "exec_driver_sql", "merge",
                "from_statement"}
BANNED_STRINGS = {"tenancy_internal"}   # the hooks' private execution option
BANNED_ATTRIBUTES = {"__table__", "engine"}

# (file, enclosing function) for every permitted unscoped() call.
UNSCOPED_ALLOWLIST = {
    ("app.py", "bind_request_tenant"),          # load the session's user and tenant
    ("app.py", "create_platform_admin_command"),
    ("app.py", "grant_platform_admin_command"),
    ("api/payments.py", "_owning_tenant"),      # webhooks: which tenant owns this reference
    ("api/daraja.py", "_tenant_for"),           # webhook URL slug + token -> tenant
    ("app.py", "payments_sweep_command"),       # which businesses have silent STK pushes
    ("app.py", "import_env_credentials"),       # CLI: copy MPESA_* into a named business
    ("app.py", "register_c2b"),                 # CLI: register a named business's URLs
    ("app.py", "seed_demo_command"),            # resolve slug -> tenant id
    ("api/platform.py", "tenants"),             # platform admins list all tenants
    ("api/platform.py", "act_as"),
    ("auth/auth_routes.py", "_login_user"),     # emails are global; find the user's tenant
    ("auth/auth_routes.py", "login_local"),
    ("auth/auth_routes.py", "register"),        # create tenant + owner
}


def python_files():
    for target in SCANNED:
        if target.is_file():
            yield target
        else:
            yield from sorted(target.rglob("*.py"))


def call_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class Finder(ast.NodeVisitor):
    def __init__(self, rel):
        self.rel = rel
        self.stack = []
        self.banned = []
        self.unscoped = []

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        name = call_name(node)
        if name in BANNED_CALLS:
            self.banned.append(f"{self.rel}:{node.lineno} {name}()")
        if name == "unscoped":
            self.unscoped.append((self.rel, self.stack[-1] if self.stack else "<module>",
                                  node.lineno))
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and node.value in BANNED_STRINGS:
            self.banned.append(f"{self.rel}:{node.lineno} '{node.value}'")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr in BANNED_ATTRIBUTES and not (
                node.attr == "engine" and not _is_db(node.value)):
            self.banned.append(f"{self.rel}:{node.lineno} .{node.attr}")
        if (node.attr == "tables" and isinstance(node.value, ast.Attribute)
                and node.value.attr == "metadata"):
            self.banned.append(f"{self.rel}:{node.lineno} metadata.tables")
        self.generic_visit(node)


def _is_db(node):
    return isinstance(node, ast.Name) and node.id == "db"


def scan():
    banned, unscoped = [], []
    for path in python_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel == "auth/tenancy.py":
            continue   # defines the filter itself
        finder = Finder(rel)
        finder.visit(ast.parse(path.read_text(), filename=rel))
        banned += finder.banned
        unscoped += finder.unscoped
    return banned, unscoped


def test_no_filter_bypass_in_application_code():
    banned, _ = scan()
    assert banned == [], ("these bypass the tenant filter; use ORM queries, or move "
                          "platform-wide work behind unscoped() and the allowlist:\n  "
                          + "\n  ".join(banned))


def test_unscoped_only_at_allowlisted_call_sites():
    _, unscoped = scan()
    found = {(rel, func) for rel, func, _ in unscoped}
    unexpected = sorted(f"{rel}:{line} in {func}()" for rel, func, line in unscoped
                        if (rel, func) not in UNSCOPED_ALLOWLIST)
    assert unexpected == [], ("new unscoped() call sites; review them, then add to "
                              "UNSCOPED_ALLOWLIST:\n  " + "\n  ".join(unexpected))
    stale = UNSCOPED_ALLOWLIST - found
    assert stale == set(), f"allowlist entries with no unscoped() call: {sorted(stale)}"


def test_the_scanner_catches_what_it_should(tmp_path):
    """The guard itself: a file using each banned form is flagged."""
    sample = (
        "from sqlalchemy import text, insert\n"
        "def leak():\n"
        "    db.session.execute(text('select * from customers'))\n"
        "    db.session.execute(insert(Customer).values(name='x'))\n"
        "    Customer.__table__.select()\n"
        "    db.engine.connect()\n"
        "    db.session.connection()\n"
        "    db.session.bulk_insert_mappings(Customer, [])\n"
        "    db.metadata.tables['customers']\n"
        "    db.session.merge(obj, load=False)\n"
        "    select(Customer).from_statement(q)\n"
        "    q.execution_options(tenancy_internal=True)\n"
        "    q.execution_options(**{'tenancy_internal': True})\n"
        "    with unscoped():\n"
        "        pass\n"
    )
    finder = Finder("sample.py")
    finder.visit(ast.parse(sample))
    flagged = " ".join(finder.banned)
    for needle in ("text()", "insert()", ".__table__", ".engine", "connection()",
                   "bulk_insert_mappings()", "metadata.tables"):
        assert needle in flagged, needle
    for needle in ("merge()", "from_statement()", "'tenancy_internal'"):
        assert needle in flagged, needle
    assert finder.unscoped == [("sample.py", "leak", 14)]
