"""Selling prices from cost: markup, rounding and re-pricing on delivery.

``selling_price`` is cost x (1 + markup/100), rounded UP to the next
multiple of the business's rounding (1, 5 or 10 shillings), so a price is
never below the markup the owner set. An exact multiple is not bumped.

An item's markup is its own ``markup_pct`` or, when that is empty, the
business's ``default_markup_pct``. Items in 'auto' price mode take the new
selling price whenever stock arrives at a new unit cost; 'manual' items
keep their price and only get a suggestion. Every price change is audited.

Money is Decimal throughout; floats are converted through ``str`` so a
JSON 30.1 becomes Decimal('30.1'), not its binary approximation.
"""
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

from models import Tenant, db
from services import audit_service

ROUNDINGS = (1, 5, 10)
PRICE_MODES = ("auto", "manual")
MAX_MARKUP = Decimal("1000")
LOW_MARGIN_PCT = Decimal("10")
CENT = Decimal("0.01")


def to_decimal(value, name="value"):
    """Decimal from a Decimal, int, numeric string or float. Raises
    ValueError for None, bools, blanks, NaN and infinities."""
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        out = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not out.is_finite():
        raise ValueError(f"{name} must be a number")
    return out


def selling_price(cost, markup_pct, rounding):
    """Selling price for ``cost`` at ``markup_pct`` percent, rounded up to a
    multiple of ``rounding`` shillings, quantised to 2 dp."""
    cost = to_decimal(cost, "cost")
    markup = to_decimal(markup_pct, "markup")
    if cost < 0:
        raise ValueError("cost cannot be negative")
    if markup < 0:
        raise ValueError("markup cannot be negative")
    if isinstance(rounding, bool) or rounding not in ROUNDINGS:
        raise ValueError(f"rounding must be one of {ROUNDINGS}")
    raw = cost * (1 + markup / 100)
    step = Decimal(rounding)
    return ((raw / step).to_integral_value(rounding=ROUND_CEILING) * step).quantize(CENT)


def margin_pct(cost, price):
    """Gross margin as a percent of the selling price, 2 dp. None when
    either is missing or the price is not above zero."""
    if cost is None or price is None:
        return None
    cost, price = to_decimal(cost, "cost"), to_decimal(price, "price")
    if price <= 0:
        return None
    return ((price - cost) * 100 / price).quantize(CENT, rounding=ROUND_HALF_UP)


def effective_markup(item, tenant):
    """The item's own markup, else the business default."""
    if item.markup_pct is not None:
        return to_decimal(item.markup_pct, "markup")
    return to_decimal(tenant.default_markup_pct, "default markup")


def _tenant_of(item):
    return db.session.get(Tenant, item.tenant_id)


def suggested_price(item, tenant=None):
    """What the item would sell for at its markup, or None without a cost
    (or with business settings that cannot price, which never raise here)."""
    if item.cost_price is None:
        return None
    tenant = tenant or _tenant_of(item)
    if tenant is None:
        return None
    try:
        return selling_price(item.cost_price, effective_markup(item, tenant),
                             tenant.price_rounding)
    except ValueError:
        return None


def set_price(item, new_price, user_id, action, extra=None):
    """Set ``item.price`` and audit it, unless it is unchanged. True if changed."""
    new_price = to_decimal(new_price, "price").quantize(CENT)
    old = item.price
    if old is not None and to_decimal(old, "price") == new_price:
        return False
    item.price = new_price
    audit_service.log(user_id, action, "InventoryItem", item.id,
                      {"price": str(old) if old is not None else None},
                      {"price": str(new_price), **(extra or {})})
    return True


def reprice_on_receipt(item, unit_cost, user_id):
    """Stock arrived at ``unit_cost``: it becomes the item's cost, and an
    'auto' item's price follows it. Returns the suggested price (None when
    the item has no cost).

    A unit cost of None (not given) or 0 (bonus goods from the supplier)
    leaves the cost and price as they are.
    """
    tenant = _tenant_of(item)
    old_cost = item.cost_price
    if unit_cost is not None:
        cost = to_decimal(unit_cost, "unit cost")
        if cost < 0:
            raise ValueError("unit cost cannot be negative")
        if cost > 0:
            item.cost_price = cost.quantize(CENT)
    suggested = suggested_price(item, tenant)
    if suggested is not None and item.price_mode == "auto":
        set_price(item, suggested, user_id, "pricing.auto", {
            "cost_price": str(item.cost_price),
            "previous_cost_price": str(old_cost) if old_cost is not None else None,
            "markup_pct": str(effective_markup(item, tenant)),
            "rounding": tenant.price_rounding,
        })
    return suggested


# --- input validation for the items and pricing APIs -----------------------

def parse_markup(value):
    """None (use the business default) or a percent in 0..1000."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    markup = to_decimal(value, "markup_pct")
    if markup < 0 or markup > MAX_MARKUP:
        raise ValueError("markup_pct must be between 0 and 1000")
    return markup.quantize(CENT)


def parse_price_mode(value):
    if value not in PRICE_MODES:
        raise ValueError("price_mode must be 'auto' or 'manual'")
    return value


def parse_reorder_quantity(value):
    """None (order up to twice the reorder level) or a whole number above 0."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("reorder_quantity must be a whole number above 0")
    try:
        qty = to_decimal(value, "reorder_quantity")
    except ValueError:
        raise ValueError("reorder_quantity must be a whole number above 0") from None
    if qty != qty.to_integral_value() or qty <= 0:
        raise ValueError("reorder_quantity must be a whole number above 0")
    return int(qty)


def parse_rounding(value):
    try:
        rounding = to_decimal(value, "price_rounding")
    except ValueError:
        rounding = None
    if rounding is None or rounding not in ROUNDINGS:
        raise ValueError("price_rounding must be 1, 5 or 10")
    return int(rounding)


def pricing_fields(data):
    """Validated pricing fields present in an item payload, as a dict.
    Raises ValueError with a message for the client."""
    out = {}
    if "markup_pct" in data:
        out["markup_pct"] = parse_markup(data.get("markup_pct"))
    if "price_mode" in data:
        out["price_mode"] = parse_price_mode(data.get("price_mode"))
    if "reorder_quantity" in data:
        out["reorder_quantity"] = parse_reorder_quantity(data.get("reorder_quantity"))
    return out


def row(item, tenant):
    """One line of the pricing table."""
    suggested = suggested_price(item, tenant)
    margin = margin_pct(item.cost_price, item.price)
    below_cost = (item.cost_price is not None
                  and to_decimal(item.price, "price") < to_decimal(item.cost_price, "cost"))
    low_margin = margin is not None and margin < LOW_MARGIN_PCT
    return {
        "id": item.id,
        "name": item.name,
        "sku": item.sku,
        "stock": item.stock,
        "cost_price": float(item.cost_price) if item.cost_price is not None else None,
        "price": float(item.price),
        "markup_pct": float(item.markup_pct) if item.markup_pct is not None else None,
        "markup_used": float(effective_markup(item, tenant)),
        "margin_pct": float(margin) if margin is not None else None,
        "suggested_price": float(suggested) if suggested is not None else None,
        "price_mode": item.price_mode,
        "below_cost": below_cost,
        "low_margin": low_margin,
        "flagged": below_cost or low_margin,
        "differs": suggested is not None and suggested != to_decimal(item.price, "price"),
    }
