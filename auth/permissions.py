"""What each role can do, read from the role tuples the decorators use.

The permission page and the users page show this. ``matrix()`` reads the
tuples in auth/auth_middleware.py when it is called, so the table cannot
drift from the code: change POS_ROLES there and the page changes with it.
"""
from auth import auth_middleware
from models import ALL_ROLES

SUPERUSER_ONLY = ('superuser',)

# (capability shown to the owner, name of the tuple in auth_middleware, or
#  SUPERUSER_ONLY for what only the owner may do).
CAPABILITIES = (
    ("Sell at the till, open and close a shift, take payments", "POS_ROLES"),
    ("Void or refund a sale (a completed sale needs a manager code)", "POS_ROLES"),
    ("Prescriptions, patients, refills and the controlled-drug register", "PHARMACY_ROLES"),
    ("Receive stock, purchase orders, batches, recalls and write-offs", "STOCK_ROLES"),
    ("Reports, reconciliation, expenses and payments to verify", "FINANCE_ROLES"),
    ("Staff performance (sales, discounts, voids, cash variances)", "FINANCE_ROLES"),
    ("Approve or reject new staff, unlock locked accounts", "ADMIN_ROLES"),
    ("Activity log, roles and permissions, online orders and customers", "ADMIN_ROLES"),
    ("Add, change and remove staff and their roles", SUPERUSER_ONLY),
    ("Business settings, discount cap, M-Pesa and bank details", SUPERUSER_ONLY),
    ("Delete customers and items", SUPERUSER_ONLY),
)

ROLE_LABELS = {
    'superuser':   'Owner',
    'admin':       'Manager',
    'accountant':  'Accountant',
    'pharmacist':  'Pharmacist',
    'cashier':     'Cashier',
    'stock_clerk': 'Stock clerk',
    'user':        'Staff (view only)',
}

ROLE_DESCRIPTIONS = {
    'superuser':   'The owner. Everything, including staff, roles and business settings.',
    'admin':       'Runs the shop: approves staff, unlocks accounts, sees reports and the activity log.',
    'accountant':  'Reads money: reports, reconciliation, expenses, payment checks, staff performance. Cannot sell or change stock.',
    'pharmacist':  'Sells, dispenses prescriptions, keeps the controlled-drug register and receives stock.',
    'cashier':     'Sells at the till, runs a shift and takes payments.',
    'stock_clerk': 'Receives stock, purchase orders, batches and write-offs.',
    'user':        'Signs in and sees the dashboard and item list only.',
}

# Column order on the page: most rights first.
ROLE_ORDER = ('superuser', 'admin', 'accountant', 'pharmacist', 'cashier', 'stock_clerk', 'user')
assert set(ROLE_ORDER) == set(ALL_ROLES) == set(ROLE_DESCRIPTIONS) == set(ROLE_LABELS)


def roles_for(source):
    """The roles allowed by ``source``: a tuple name in auth_middleware, or a tuple."""
    if isinstance(source, str):
        return tuple(getattr(auth_middleware, source))
    return tuple(source)


def matrix():
    """{'roles': [...], 'rows': [{'capability', 'source', 'allowed': {role: bool}}]}."""
    rows = []
    for capability, source in CAPABILITIES:
        allowed = set(roles_for(source))
        rows.append({
            'capability': capability,
            'source': source if isinstance(source, str) else 'owner only',
            'allowed': {role: role in allowed for role in ROLE_ORDER},
        })
    return {
        'roles': [{'key': r, 'label': ROLE_LABELS[r], 'description': ROLE_DESCRIPTIONS[r]}
                  for r in ROLE_ORDER],
        'rows': rows,
    }


def role_choices():
    """[{key, label, description}] for role pickers."""
    return matrix()['roles']
