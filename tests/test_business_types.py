"""Business types table (verticals/business_types.py) and the pages built on it.

The table is the single source for what each type is called, where it is
grouped and what it claims to include. These tests keep those claims honest
against verticals/features.py, keep every starter catalogue seedable, and
check the landing, signup and onboarding pages show every type and stay
usable on a 360px phone.
"""
import re

import pytest

from auth.tenancy import tenant_scope, unscoped
from models import VERTICALS, InventoryItem, db
from services.seed_service import _SEEDS, seed_inventory
from verticals.business_types import BUSINESS_CONFIGS, GROUPS, grouped, specialised
from verticals.features import GENERAL_TYPES, VERTICAL_FEATURES

from tests.conftest import create_tenant

REQUIRED_KEYS = {"label", "icon", "color", "group", "tagline", "pitch", "examples",
                 "description", "includes", "seed", "specialised", "features"}

# A phrase in a type's text (lower-cased) -> features the type must have.
CLAIMS = {
    "patient": {"patients"},
    "controlled-drug": {"pharmacy"},
    "ppb": {"pharmacy"},
    "recall": {"recalls"},
    "duka": {"dukas"},
    "deni": {"dukas"},
    "campaign": {"campaigns"},
    "pickup": {"campaigns"},
    "admission": {"members"},
    "whatsapp": {"online_shop"},
    "online shop": {"online_shop"},
    "expiry": {"batches"},
    "batch": {"batches"},
    "point of sale": {"pos"},
    "till": {"pos"},
    "barcode": {"pos"},
    "purchase order": {"purchasing"},
    "markup": {"pricing"},
    "profit": {"finance"},
    "reconciliation": {"finance"},
    "credit": {"customers"},
    "customer records": {"customers"},
    "expenses": {"expenses"},
    "staff roles": {"staff"},
    "sales by staff": {"staff"},
    "activity log": {"activity"},
}


def _text(cfg):
    return " ".join([cfg["tagline"], cfg["pitch"], cfg["description"], *cfg["includes"]]).lower()


# --- the table --------------------------------------------------------------------


def test_every_vertical_has_a_complete_config_in_table_order_by_group():
    assert set(BUSINESS_CONFIGS) == set(VERTICALS)
    for slug, cfg in BUSINESS_CONFIGS.items():
        assert REQUIRED_KEYS <= set(cfg), slug
        for key in ("label", "icon", "tagline", "pitch", "examples", "description"):
            assert isinstance(cfg[key], str) and cfg[key].strip(), (slug, key)
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", cfg["color"]), slug
        assert cfg["icon"].startswith("fa-"), slug
        assert cfg["includes"] and all(isinstance(x, str) for x in cfg["includes"]), slug
        assert cfg["features"] == VERTICAL_FEATURES[slug], slug
    # Grouped order is the dict order, so templates can loop either way.
    flat = [slug for _, members in grouped() for slug, _ in members]
    assert flat == list(BUSINESS_CONFIGS)


def test_groups_cover_every_type_and_only_known_groups():
    assert [g for g, _ in grouped()] == list(GROUPS)
    seen = [slug for _, members in grouped() for slug, _ in members]
    assert sorted(seen) == sorted(VERTICALS) and len(seen) == len(set(seen))
    assert BUSINESS_CONFIGS["pharmacy"]["group"] == "Health"
    assert BUSINESS_CONFIGS["shop"]["group"] == "Retail"
    assert BUSINESS_CONFIGS["institution"]["group"] == "Trade & institutions"


def test_specialised_types_are_exactly_those_with_extra_features():
    general = VERTICAL_FEATURES["shop"]
    expected = {s for s, f in VERTICAL_FEATURES.items() if f - general}
    assert set(specialised()) == expected == {"pharmacy", "distributor", "institution"}
    assert not set(specialised()) & set(GENERAL_TYPES)


def test_includes_never_claim_features_the_type_lacks():
    for slug, cfg in BUSINESS_CONFIGS.items():
        text = _text(cfg)
        for phrase, needs in CLAIMS.items():
            if re.search(r"\b" + re.escape(phrase), text):
                assert needs <= cfg["features"], f"{slug} claims {phrase!r} without {needs}"


@pytest.mark.parametrize("slug,phrase", [
    ("pharmacy", "patient"), ("distributor", "dukas"), ("institution", "campaign"),
    ("restaurant", "menu"), ("salon", "services"), ("agrovet", "expiry"),
    ("supermarket", "expiry"), ("hardware", "credit"),
])
def test_each_type_lists_something_specific_to_it(slug, phrase):
    assert phrase in " ".join(BUSINESS_CONFIGS[slug]["includes"]).lower()


def test_whatsapp_never_claimed_for_institution_and_patients_only_for_pharmacy():
    assert "whatsapp" not in _text(BUSINESS_CONFIGS["institution"])
    for slug, cfg in BUSINESS_CONFIGS.items():
        if slug != "pharmacy":
            assert "patient" not in _text(cfg), slug
        if slug != "distributor":
            assert "duka" not in " ".join(cfg["includes"]).lower(), slug


def test_general_types_have_their_own_pitch_and_examples():
    pitches = [BUSINESS_CONFIGS[s]["pitch"] for s in GENERAL_TYPES]
    examples = [BUSINESS_CONFIGS[s]["examples"] for s in GENERAL_TYPES]
    assert len(set(pitches)) == len(pitches) and len(set(examples)) == len(examples)


# --- starter catalogues -----------------------------------------------------------


def test_every_seed_names_an_existing_catalogue():
    for slug, cfg in BUSINESS_CONFIGS.items():
        assert cfg["seed"] in _SEEDS, slug
    for slug in ("agrovet", "electronics", "cosmetics"):
        assert BUSINESS_CONFIGS[slug]["seed"] == slug


@pytest.mark.parametrize("name", ["agrovet", "electronics", "cosmetics"])
def test_new_catalogues_are_realistic(name):
    items = _SEEDS[name]
    assert 10 <= len(items) <= 15
    for item in items:
        assert item["price"] > 0 and item["cost_price"] is not None, item["name"]
        assert item["cost_price"] <= item["price"], item["name"]
        assert item["category"] and item["unit"] and item["icon"].startswith("fa-"), item["name"]
        assert len(item["unit"]) <= 20
    assert len({i["name"] for i in items}) == len(items)
    assert len({i["category"] for i in items}) >= 3


def test_seeding_each_general_type_creates_its_items(app):
    for slug in GENERAL_TYPES:
        tenant_id = create_tenant(f"Seed {slug}", f"seed-{slug}", slug)
        catalogue = _SEEDS[BUSINESS_CONFIGS[slug]["seed"]]
        with tenant_scope(tenant_id):
            seed_inventory(BUSINESS_CONFIGS[slug]["seed"])
            names = {i.name for i in InventoryItem.query.all()}
            assert names == {c["name"] for c in catalogue}, slug
            stocked = InventoryItem.query.filter(InventoryItem.stock > 0).count()
            assert stocked == sum(1 for c in catalogue if c.get("stock")), slug
    with unscoped():
        db.session.remove()


# --- pages ------------------------------------------------------------------------


def _inline_widths(html):
    """Pixel widths set in inline style attributes (width, max-width, min-width)."""
    out = []
    for style in re.findall(r'style="([^"]*)"', html):
        out += [int(n) for n in re.findall(r"width\s*:\s*(\d+)px", style)]
    return out


@pytest.mark.parametrize("path", ["/", "/register"])
def test_public_pages_render_every_type_label(client, path):
    html = client.get(path).get_data(as_text=True)
    for cfg in BUSINESS_CONFIGS.values():
        assert cfg["label"].replace("&", "&amp;") in html or cfg["label"] in html, cfg["label"]


def test_onboarding_renders_every_type_grouped(login_as):
    html = login_as("superuser").get("/onboarding").get_data(as_text=True)
    for cfg in BUSINESS_CONFIGS.values():
        assert cfg["label"] in html
    for group in GROUPS:
        assert f'<h2 class="biz-group-title">{group.replace("&", "&amp;")}</h2>' in html
    assert html.count("Extra tools</span>") == len(specialised())
    assert 'name="viewport"' in html
    assert all(w <= 360 for w in _inline_widths(html))


def test_landing_is_mobile_safe_grouped_and_honest(client):
    html = client.get("/").get_data(as_text=True)
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0">' in html
    assert all(w <= 360 for w in _inline_widths(html)), _inline_widths(html)
    for group in GROUPS:
        assert f'<h3 class="group-title">{group.replace("&", "&amp;")}</h3>' in html
    assert html.count('class="badge-extra"') == len(specialised())
    assert "Don't see your business?" in html and "Any other shop" in html
    assert "KES 3,500 to 5,000 a month after a 14-day trial" in html
    assert 'href="/register"' in html and 'href="/login"' in html
    lowered = html.lower()
    for banned in ("—", "delve", "robust", "seamless", "comprehensive",
                   "cutting-edge", "leverage", "testimonial", "kra certified",
                   "kra-certified", "trusted by"):
        assert banned not in lowered, banned


def test_landing_type_cards_use_a_responsive_equal_height_grid(client):
    html = client.get("/").get_data(as_text=True)
    css = html[html.index("<style>"):html.index("</style>")]
    assert ".types { display: grid; grid-template-columns: 1fr;" in css           # 360px: 1
    assert "(min-width: 576px) { .types { grid-template-columns: repeat(2, 1fr)" in css
    assert "(min-width: 992px) { .types { grid-template-columns: repeat(3, 1fr)" in css
    assert "(min-width: 1200px) { .types { grid-template-columns: repeat(4, 1fr)" in css
    assert "height: 100%" in css and "overflow-x: hidden" in css


def test_register_select_is_grouped_with_includes_per_type(client):
    html = client.get("/register").get_data(as_text=True)
    select = html[html.index('<select name="vertical"'):html.index("</select>")]
    assert select.count("<optgroup") == len(GROUPS)
    for group in GROUPS:
        assert f'<optgroup label="{group.replace("&", "&amp;")}">' in select
    for slug in BUSINESS_CONFIGS:
        assert f'value="{slug}"' in select
        assert f'class="vertical-includes" data-vertical="{slug}"' in html
    # The default choice is Any other shop, and only its list is visible.
    assert re.search(r'<option value="shop"\s+selected', select)
    assert re.search(r'data-vertical="shop"\s*>', html)


def test_landing_does_not_promise_institutions_a_till(client):
    """Codex P2: 'Every type gets the till' was false for schools, estates
    and chamas, which have no POS."""
    from verticals.features import features_for
    assert "pos" not in features_for("institution")
    html = client.get("/").get_data(as_text=True)
    assert "Every type gets the till" not in html
    assert "collect money by campaign" in html
