"""The staff nav, built from the vertical's features and the user's role.

One list for every vertical; each entry names the feature it needs and the
roles that see it. ``nav_for`` drops entries whose feature is off, whose
role does not match, or whose endpoint does not exist (yet), so the nav can
never link to a page that would 404.
"""
from dataclasses import dataclass, field

from flask import current_app, url_for

from verticals.features import features_for

ADMIN = ("admin", "superuser")
FINANCE = ("admin", "superuser", "accountant")
POS = ("cashier", "pharmacist", "admin", "superuser")
PHARMACY = ("pharmacist", "admin", "superuser")
STOCK = ("stock_clerk", "pharmacist", "admin", "superuser")
EVERYONE = None


@dataclass(frozen=True)
class Link:
    label: str
    endpoint: str
    icon: str
    feature: str | None = None
    roles: tuple | None = EVERYONE
    # Per-vertical label, e.g. customers are "Patients" in a pharmacy.
    labels: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Group:
    label: str
    icon: str
    links: tuple


NAV = (
    Link("Dashboard", "dashboard", "fa-house"),
    Group("Sell", "fa-cash-register", (
        Link("Point of sale", "pos_page", "fa-cash-register", "pos", POS),
        Link("Shift & cash drawer", "shift_page", "fa-business-time", "shift", POS),
        Link("Online orders", "orders_page", "fa-receipt", "orders", POS),
        Link("WhatsApp chats", "whatsapp_chats_page", "fa-comments", "online_shop", POS),
    )),
    Group("Patients", "fa-user-injured", (
        Link("Patients", "patients_page", "fa-user-injured", "patients", PHARMACY),
        Link("Prescriptions & refills", "pharmacy_page", "fa-prescription-bottle-medical",
             "pharmacy", PHARMACY),
    )),
    Link("Customers", "customers_page", "fa-users", "customers", ADMIN,
         labels={"distributor": "Dukas", "pharmacy": "Customers & credit"}),
    Group("School", "fa-school", (
        Link("Campaigns", "campaigns_page", "fa-bullhorn", "campaigns", ADMIN),
        Link("Members", "members_page", "fa-id-card", "members", ADMIN),
    )),
    Group("Stock", "fa-boxes-stacked", (
        Link("Items", "inventory_page", "fa-store", "inventory", STOCK + ("cashier",)),
        Link("Batches & expiry", "batches_page", "fa-calendar-xmark", "batches", STOCK),
        Link("Recalls", "recalls_page", "fa-triangle-exclamation", "recalls", STOCK),
        Link("Write-offs", "writeoffs_page", "fa-trash-can", "inventory", STOCK),
    )),
    Group("Purchasing", "fa-truck", (
        Link("Purchase orders", "purchase_orders_page", "fa-truck", "purchasing", STOCK),
        Link("Pricing", "pricing_page", "fa-tags", "pricing", ADMIN),
    )),
    Group("Money", "fa-coins", (
        Link("Daily report", "reports_dashboard_page", "fa-chart-line", "finance", FINANCE),
        Link("Reports", "reports_page", "fa-file-lines", "finance", FINANCE),
        Link("Reconciliation", "reconciliation_page", "fa-scale-balanced", "finance", FINANCE),
        Link("Expenses", "expenses_page", "fa-money-bill-wave", "expenses", FINANCE),
        Link("Payments to verify", "verify_payments_page", "fa-circle-check", "finance", FINANCE),
    )),
    Group("Staff", "fa-user-shield", (
        Link("Users & roles", "users_page", "fa-shield-halved", "staff", ADMIN),
        Link("Roles & permissions", "permissions_page", "fa-table-cells", "staff", ADMIN),
        Link("Performance", "performance_page", "fa-ranking-star", "staff", FINANCE),
        Link("Activity log", "activity_page", "fa-list-check", "activity", ADMIN),
    )),
    Link("Settings", "settings.page", "fa-gear", None, ("superuser",)),
)


def _visible(link, vertical, features, role):
    if link.feature is not None and link.feature not in features:
        return False
    if link.roles is not None and role not in link.roles:
        return False
    return link.endpoint in current_app.view_functions


def _item(link, vertical):
    return {"label": link.labels.get(vertical, link.label), "endpoint": link.endpoint,
            "url": url_for(link.endpoint), "icon": link.icon}


def nav_for(vertical, role):
    """Nav entries for this vertical and role: dicts with ``label``, ``icon``
    and either ``url``/``endpoint`` or ``links`` (a dropdown). A group with
    one visible link is shown as that link."""
    features = features_for(vertical)
    out = []
    for entry in NAV:
        if isinstance(entry, Link):
            if _visible(entry, vertical, features, role):
                out.append(_item(entry, vertical))
            continue
        links = [_item(link, vertical) for link in entry.links
                 if _visible(link, vertical, features, role)]
        if len(links) == 1:
            out.append(links[0])
        elif links:
            out.append({"label": entry.label, "icon": entry.icon, "links": links,
                        "endpoints": [link["endpoint"] for link in links]})
    return out
