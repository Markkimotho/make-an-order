import pytest
from app import app, db
from models import Customer, Order

@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
        yield client
        with app.app_context():
            db.drop_all()

def setup_customer(client):
    response = client.post("/customers/register", json={"name": "Alice", "phone_number": "+25756098389", "code": "XYZ789"})
    customer_id = response.get_json()["customer_id"]
    return customer_id

def test_place_order(client):
    customer_id = setup_customer(client)
    data = {"customer_id": customer_id, "item": "Laptop", "amount": 1500.0}
    response = client.post("/orders/place_order", json=data)
    assert response.status_code == 201
    assert response.get_json()["message"] == "Order placed successfully!"

def test_place_order_invalid_customer(client):
    data = {"customer_id": 999, "item": "Laptop", "amount": 1500.0}
    response = client.post("/orders/place_order", json=data)
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"

def test_view_orders(client):
    customer_id = setup_customer(client)
    client.post("/orders/place_order", json={"customer_id": customer_id, "item": "Laptop", "amount": 1500.0})
    response = client.get(f"/orders/view_orders/{customer_id}")
    assert response.status_code == 200
    assert len(response.get_json()) == 1

def test_view_orders_invalid_customer(client):
    response = client.get("/orders/view_orders/999")
    assert response.status_code == 404
    assert response.get_json()["message"] == "No orders found for this customer."

def test_update_order(client):
    customer_id = setup_customer(client)
    response = client.post("/orders/place_order", json={"customer_id": customer_id, "item": "Laptop", "amount": 1500.0})
    order_id = response.get_json()["id"]
    update_data = {"item": "Desktop PC"}
    response = client.put(f"/orders/update_orders/{order_id}", json=update_data)
    assert response.status_code == 200
    assert response.get_json()["message"] == "Order updated successfully"

def test_update_nonexistent_order(client):
    update_data = {"item": "Desktop PC"}
    response = client.put("/orders/update_orders/999", json=update_data)
    assert response.status_code == 404
    assert response.get_json()["error"] == "Order not found"

def test_delete_order(client):
    customer_id = setup_customer(client)
    response = client.post("/orders/place_order", json={"customer_id": customer_id, "item": "Laptop", "amount": 1500.0})
    order_id = response.get_json()["id"]
    response = client.delete(f"/orders/delete_orders/{order_id}")
    assert response.status_code == 200
    assert response.get_json()["message"] == "Order deleted successfully"

def test_delete_nonexistent_order(client):
    response = client.delete("/orders/delete_orders/999")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Order not found"
