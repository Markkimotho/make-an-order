import pytest # type: ignore
from app import app, db
from models import Customer # Import Customer model for setup

@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config['SECRET_KEY'] = 'test_secret_key' # Needed for session handling
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
        yield client
        with app.app_context():
            db.drop_all()

# Helper to simulate a logged-in session for protected routes
@pytest.fixture
def logged_in_client(client):
    with client.session_transaction() as sess:
        sess['profile'] = {'email': 'testuser@example.com'}
        sess['access_token'] = 'fake-token'
    return client

def setup_customer(logged_in_client):
    response = logged_in_client.post("/customers/register", json={"name": "Alice", "phone_number": "+25756098389", "code": "XYZ789"})
    assert response.status_code == 201 # Ensure customer registration is successful
    customer_id = response.get_json()["customer_id"]
    return customer_id

def test_place_order(logged_in_client):
    customer_id = setup_customer(logged_in_client)
    data = {"customer_id": customer_id, "item": "Laptop", "amount": 1500.0}
    response = logged_in_client.post("/orders/place_order", json=data)
    assert response.status_code == 201
    assert response.get_json()["message"] == "Order placed successfully!"
    assert "sms_status" in response.get_json()

def test_place_order_unauthorized(client):
    # This test will attempt to place an order without logging in
    # It should redirect to login page (302) or return 401
    # Given the middleware, it will redirect for UI flow.
    # If this was a pure API, it would be 401.
    data = {"customer_id": 1, "item": "Laptop", "amount": 1500.0}
    response = client.post("/orders/place_order", json=data)
    assert response.status_code == 302 # Redirect to login

def test_place_order_invalid_customer(logged_in_client):
    data = {"customer_id": 999, "item": "Laptop", "amount": 1500.0}
    response = logged_in_client.post("/orders/place_order", json=data)
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"

def test_place_order_missing_details(logged_in_client):
    customer_id = setup_customer(logged_in_client)
    data = {"customer_id": customer_id, "item": "Laptop"} # Missing amount
    response = logged_in_client.post("/orders/place_order", json=data)
    assert response.status_code == 400
    assert response.get_json()["error"] == "Missing order details (customer_id, item, amount are required)"


def test_view_orders(logged_in_client):
    customer_id = setup_customer(logged_in_client)
    logged_in_client.post("/orders/place_order", json={"customer_id": customer_id, "item": "Laptop", "amount": 1500.0})
    response = logged_in_client.get(f"/orders/view_orders/{customer_id}")
    assert response.status_code == 200
    assert len(response.get_json()) == 1
    assert response.get_json()[0]["item"] == "Laptop"

def test_view_all_orders(logged_in_client):
    customer_id1 = setup_customer(logged_in_client)
    logged_in_client.post("/customers/register", json={"name": "Bob", "phone_number": "+254712345678", "code": "CUST2"})
    customer_id2 = logged_in_client.get_json()["customer_id"] # Retrieve customer ID from the response of previous post
    logged_in_client.post("/orders/place_order", json={"customer_id": customer_id1, "item": "Laptop", "amount": 1500.0})
    logged_in_client.post("/orders/place_order", json={"customer_id": customer_id2, "item": "Keyboard", "amount": 150.0})
    response = logged_in_client.get("/orders/view_orders")
    assert response.status_code == 200
    assert len(response.get_json()) == 2

def test_view_orders_unauthorized(client):
    response = client.get("/orders/view_orders/1")
    assert response.status_code == 302 # Redirect to login

def test_view_orders_invalid_customer(logged_in_client):
    response = logged_in_client.get("/orders/view_orders/999")
    assert response.status_code == 404
    assert response.get_json()["message"] == "No orders found for customer with ID 999."

def test_update_order(logged_in_client):
    customer_id = setup_customer(logged_in_client)
    response = logged_in_client.post("/orders/place_order", json={"customer_id": customer_id, "item": "Laptop", "amount": 1500.0})
    order_id = response.get_json()["id"]
    update_data = {"item": "Desktop PC"}
    response = logged_in_client.put(f"/orders/update_orders/{order_id}", json=update_data)
    assert response.status_code == 200
    assert response.get_json()["message"] == "Order updated successfully"

def test_update_nonexistent_order(logged_in_client):
    update_data = {"item": "Desktop PC"}
    response = logged_in_client.put("/orders/update_orders/999", json=update_data)
    assert response.status_code == 404
    assert response.get_json()["error"] == "Order not found"

def test_update_order_missing_details(logged_in_client):
    customer_id = setup_customer(logged_in_client)
    response = logged_in_client.post("/orders/place_order", json={"customer_id": customer_id, "item": "Laptop", "amount": 1500.0})
    order_id = response.get_json()["id"]
    update_data = {} # Empty data
    response = logged_in_client.put(f"/orders/update_orders/{order_id}", json=update_data)
    assert response.status_code == 400
    assert response.get_json()["error"] == "No update data provided"


def test_delete_order(logged_in_client):
    customer_id = setup_customer(logged_in_client)
    response = logged_in_client.post("/orders/place_order", json={"customer_id": customer_id, "item": "Laptop", "amount": 1500.0})
    order_id = response.get_json()["id"]
    response = logged_in_client.delete(f"/orders/delete_orders/{order_id}")
    assert response.status_code == 200
    assert response.get_json()["message"] == "Order deleted successfully"

def test_delete_nonexistent_order(logged_in_client):
    response = logged_in_client.delete("/orders/delete_orders/999")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Order not found"