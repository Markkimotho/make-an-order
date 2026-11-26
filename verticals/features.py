"""Which features each vertical gets.

One table decides the nav, the dashboard cards, which page routes exist and
which API blueprints answer for a business. A feature that is off for a
vertical is a 404 on its URLs, not just a hidden link.

Shared requirements (discounts, purchasing, finance, staff, backup) go to
every vertical that sells over a counter; pharmacy-only ones (patients,
prescriptions and the register, recalls) go to pharmacy only.
"""
from functools import wraps

from flask import abort, g, jsonify, request

ALL_FEATURES = frozenset({
    "pos",          # counter sales
    "shift",        # cash drawer shifts and Z-report
    "discounts",    # line and sale discounts at the till
    "inventory",    # items and stock
    "batches",      # batch numbers and expiry at sale
    "recalls",      # batch recall and buyer trace
    "patients",     # patient list and medication history
    "pharmacy",     # prescriptions, controlled-drug register, refills
    "purchasing",   # suppliers and purchase orders
    "pricing",      # markup-based selling prices
    "finance",      # sales, profit and reconciliation reports
    "expenses",     # expenses for net profit
    "staff",        # users, roles, performance
    "activity",     # login and activity log
    "customers",    # customer records
    "dukas",        # distributor: dukas on credit, deni
    "campaigns",    # institution: campaigns and pickup
    "members",      # institution: students or houses
    "orders",       # remote orders from public links
    "backup",       # backup and restore
    "online_shop",  # public shop page and WhatsApp ordering
})

_COUNTER = {"pos", "shift", "discounts", "inventory", "batches", "purchasing", "pricing",
            "finance", "expenses", "staff", "activity", "customers", "orders", "backup",
            "online_shop"}

# Every business type without its own extras runs the general shop set.
GENERAL_TYPES = ("shop", "supermarket", "hardware", "salon", "restaurant", "agrovet",
                 "electronics", "cosmetics")

VERTICAL_FEATURES = {
    "pharmacy": frozenset(_COUNTER | {"recalls", "patients", "pharmacy"}),
    "distributor": frozenset(_COUNTER | {"dukas"}),
    **{name: frozenset(_COUNTER) for name in GENERAL_TYPES},
    "institution": frozenset({"inventory", "purchasing", "pricing", "finance", "expenses",
                              "staff", "activity", "campaigns", "members", "orders", "backup"}),
}
assert all(f <= ALL_FEATURES for f in VERTICAL_FEATURES.values())


def features_for(vertical):
    return VERTICAL_FEATURES.get(vertical, frozenset())


def current_features():
    tenant = g.get("current_tenant")
    return features_for(tenant.vertical) if tenant is not None else frozenset()


def has_feature(name):
    return name in current_features()


def _refuse():
    if request.accept_mimetypes.best == "application/json" or request.is_json \
            or not request.accept_mimetypes.accept_html:
        return jsonify({"error": "Not found"}), 404
    abort(404)


def feature_required(*names):
    """Page or API route decorator: 404 unless the business's vertical has
    every named feature. With no tenant bound it lets the login decorators
    answer (401 or a redirect to login)."""
    for name in names:
        assert name in ALL_FEATURES, name

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if g.get("current_tenant") is not None and not set(names) <= current_features():
                return _refuse()
            return f(*args, **kwargs)
        return decorated
    return decorator


def gate_blueprint(bp, *names):
    """Every route of ``bp`` needs the named features (any one of them)."""
    for name in names:
        assert name in ALL_FEATURES, name

    @bp.before_request
    def _gate():
        if g.get("current_tenant") is not None and not (set(names) & current_features()):
            return jsonify({"error": "Not found"}), 404
        return None
    return bp
