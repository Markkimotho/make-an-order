"""Each vertical gets its own screens: nav, page routes, API blueprints and
public links all follow verticals/features.py."""
import pytest
from flask import url_for

from tests.conftest import set_vertical
from verticals.features import ALL_FEATURES, VERTICAL_FEATURES, features_for
from verticals.nav import NAV, Group, nav_for

PAGES = {  # page endpoint -> feature it needs
    "pos_page": "pos", "shift_page": "shift", "pharmacy_page": "pharmacy",
    "purchase_orders_page": "purchasing", "inventory_page": "inventory",
    "customers_page": "customers", "orders_page": "orders", "reports_page": "finance",
    "reports_dashboard_page": "finance",
}

APIS = {  # an API URL per gated blueprint -> feature
    "/pos/shift/current": "pos",
    "/pharmacy/dashboard": "pharmacy",
    "/distributor/dashboard": "dukas",
    "/institution/campaigns": "campaigns",
    "/reports/sales_summary": "finance",
    "/customers/view_customers": "customers",
    "/orders/view_orders": "orders",
    "/inventory/items": "inventory",
}


def test_matrix_is_complete_and_pharmacy_only_features_stay_pharmacy():
    assert set(VERTICAL_FEATURES) == {"pharmacy", "distributor", "institution"}
    for feature in ("patients", "pharmacy", "recalls"):
        assert [v for v, f in VERTICAL_FEATURES.items() if feature in f] == ["pharmacy"]
    shared = {"discounts", "purchasing", "pricing", "finance", "expenses", "staff", "activity",
              "backup"}
    assert shared <= features_for("pharmacy") and shared <= features_for("distributor")
    assert "pos" not in features_for("institution")
    assert features_for("nonsense") == frozenset()
    assert all(f <= ALL_FEATURES for f in VERTICAL_FEATURES.values())


@pytest.mark.parametrize("vertical", ["pharmacy", "distributor", "institution"])
def test_page_routes_follow_the_vertical(app, login_as, vertical):
    set_vertical(app.acme_id, vertical)
    c = login_as("superuser")
    for endpoint, feature in PAGES.items():
        with app.test_request_context():
            url = url_for(endpoint)
        status = c.get(url).status_code
        expected = 200 if feature in features_for(vertical) else 404
        assert status == expected, (vertical, endpoint, status)


@pytest.mark.parametrize("vertical", ["pharmacy", "distributor", "institution"])
def test_api_blueprints_follow_the_vertical(app, login_as, vertical):
    set_vertical(app.acme_id, vertical)
    c = login_as("superuser")
    for url, feature in APIS.items():
        status = c.get(url).status_code
        if feature in features_for(vertical):
            assert status != 404, (vertical, url)
        else:
            assert status == 404, (vertical, url, status)
            assert c.get(url).get_json() == {"error": "Not found"}


def test_gated_api_still_asks_for_login_first(client):
    assert client.get("/pos/shift/current").status_code == 401


def test_public_links_only_for_their_vertical(app, client):
    # acme is a pharmacy: a duka or campaign link under its slug is a 404,
    # whatever the token, before any lookup happens.
    assert client.get("/o/acme/d/anything").status_code == 404
    assert client.get("/o/acme/c/anything").status_code == 404
    set_vertical(app.acme_id, "distributor")
    assert client.get("/o/acme/r/anything").status_code == 404


def _flatten(entries):
    for entry in entries:
        yield from entry.get("links", [entry])


@pytest.mark.parametrize("vertical", ["pharmacy", "distributor", "institution"])
@pytest.mark.parametrize("role", ["superuser", "admin", "pharmacist", "cashier", "stock_clerk",
                                  "accountant"])
def test_every_nav_link_opens(app, login_as, vertical, role):
    """No vertical or role is shown a link that 404s or bounces to the dashboard."""
    set_vertical(app.acme_id, vertical)
    c = login_as(role)
    with app.test_request_context():
        links = list(_flatten(nav_for(vertical, role)))
    assert links, (vertical, role)
    for link in links:
        r = c.get(link["url"])
        assert r.status_code == 200, (vertical, role, link["url"], r.status_code)


def test_nav_differs_per_vertical(app):
    with app.test_request_context():
        def labels(vertical, role="superuser"):
            return {link["label"] for link in _flatten(nav_for(vertical, role))}
        pharmacy, distributor, institution = (labels(v) for v in
                                              ("pharmacy", "distributor", "institution"))
    assert "Prescriptions & refills" in pharmacy and "Point of sale" in pharmacy
    assert "Dukas" in distributor and "Prescriptions & refills" not in distributor
    assert "Point of sale" not in institution and "Dukas" not in institution
    with app.test_request_context():
        cashier = {link["label"] for link in _flatten(nav_for("pharmacy", "cashier"))}
    assert "Point of sale" in cashier
    assert not cashier & {"Users & roles", "Reports", "Settings", "Online orders"}


def test_nav_entries_name_real_features():
    for entry in NAV:
        for link in (entry.links if isinstance(entry, Group) else (entry,)):
            assert link.feature is None or link.feature in ALL_FEATURES, link


def test_signup_and_landing_show_what_each_vertical_includes(client):
    register = client.get("/register").get_data(as_text=True)
    landing = client.get("/").get_data(as_text=True)
    for text in (register, landing):
        assert "Batch, expiry and recall tracking" in text
        assert "Dukas on credit with limits and deni aging" in text
    assert "Salon" not in landing and "Restaurant" not in landing


def test_staff_stock_lookup_has_no_order_cart(app, login_as):
    html = login_as("cashier").get("/inventory-ui").get_data(as_text=True)
    assert "Stock lookup" in html and 'id="placeOrderBtn"' not in html
