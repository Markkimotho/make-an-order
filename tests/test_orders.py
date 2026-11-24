"""Legacy manual-order API (admin only)."""
import pytest


@pytest.fixture
def admin(login_as):
    return login_as("admin")


def _customer(c, phone="+25756098389", code="XYZ789", name="Alice"):
    response = c.post("/customers/register",
                      json={"name": name, "phone_number": phone, "code": code})
    assert response.status_code == 201
    return response.get_json()["customer_id"]


def _order(c, customer_id, item="Laptop", amount=1500.0):
    response = c.post("/orders/place_order",
                      json={"customer_id": customer_id, "item": item, "amount": amount})
    assert response.status_code == 201
    return response.get_json()["id"]


def test_place_order(admin):
    customer_id = _customer(admin)
    response = admin.post("/orders/place_order",
                          json={"customer_id": customer_id, "item": "Laptop", "amount": 1500.0})
    assert response.status_code == 201
    body = response.get_json()
    assert body["message"] == "Order placed successfully!"
    # Africa's Talking is not configured in tests: the order still succeeds.
    assert body["sms_status"] == "SMS sending failed"


def test_place_order_unauthorized(client):
    response = client.post("/orders/place_order",
                           json={"customer_id": 1, "item": "Laptop", "amount": 1500.0})
    assert response.status_code == 401


def test_place_order_invalid_customer(admin):
    response = admin.post("/orders/place_order",
                          json={"customer_id": 999, "item": "Laptop", "amount": 1500.0})
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"


def test_place_order_missing_details(admin):
    customer_id = _customer(admin)
    response = admin.post("/orders/place_order", json={"customer_id": customer_id, "item": "Laptop"})
    assert response.status_code == 400
    assert response.get_json()["error"] == \
        "Missing order details (customer_id, item, amount are required)"


def test_view_orders(admin):
    customer_id = _customer(admin)
    _order(admin, customer_id)
    response = admin.get(f"/orders/view_orders/{customer_id}")
    assert response.status_code == 200
    orders = response.get_json()["orders"]
    assert len(orders) == 1
    assert orders[0]["item"] == "Laptop"
    assert orders[0]["customer_name"] == "Alice"


def test_view_all_orders(admin):
    first = _customer(admin)
    second = _customer(admin, phone="+254712345678", code="CUST2", name="Bob")
    _order(admin, first)
    _order(admin, second, item="Keyboard", amount=150.0)
    response = admin.get("/orders/view_orders")
    assert response.status_code == 200
    assert response.get_json()["pagination"]["total"] == 2


def test_view_orders_unauthorized(client):
    response = client.get("/orders/view_orders/1")
    assert response.status_code == 401


def test_view_orders_unknown_customer_is_empty(admin):
    response = admin.get("/orders/view_orders/999")
    assert response.status_code == 200
    assert response.get_json()["orders"] == []


def test_update_order(admin):
    order_id = _order(admin, _customer(admin))
    response = admin.put(f"/orders/update_orders/{order_id}", json={"item": "Desktop PC"})
    assert response.status_code == 200
    assert response.get_json()["message"] == "Order updated successfully"


def test_update_nonexistent_order(admin):
    response = admin.put("/orders/update_orders/999", json={"item": "Desktop PC"})
    assert response.status_code == 404
    assert response.get_json()["error"] == "Order not found"


def test_update_order_missing_details(admin):
    order_id = _order(admin, _customer(admin))
    response = admin.put(f"/orders/update_orders/{order_id}", json={})
    assert response.status_code == 400
    assert response.get_json()["error"] == "No update data provided"


def test_delete_order(admin):
    order_id = _order(admin, _customer(admin))
    response = admin.delete(f"/orders/delete_orders/{order_id}")
    assert response.status_code == 200
    assert response.get_json()["message"] == "Order deleted successfully"


def test_delete_nonexistent_order(admin):
    response = admin.delete("/orders/delete_orders/999")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Order not found"
