import pytest # type: ignore
from app import app, db
from models import Customer # Import Customer model

@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    # This secret key is needed for session handling in tests, especially with login_required
    app.config['SECRET_KEY'] = 'test_secret_key'
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

def test_register_customer(logged_in_client):
    data = {"name": "John Doe", "phone_number": "+25756098389", "code": "ABC123"}
    response = logged_in_client.post("/customers/register", json=data)
    assert response.status_code == 201
    assert response.get_json()["message"] == "Customer registered successfully"

def test_register_customer_unauthorized(client):
    data = {"name": "John Doe", "phone_number": "+25756098389", "code": "ABC123"}
    response = client.post("/customers/register", json=data)
    assert response.status_code == 302 # Redirect to login

def test_register_duplicate_customer(logged_in_client):
    data = {"name": "John Doe", "phone_number": "+25756098389", "code": "ABC123"}
    logged_in_client.post("/customers/register", json=data)
    response = logged_in_client.post("/customers/register", json=data)
    assert response.status_code == 400
    assert response.get_json()["error"] == "Phone number or code already exists"

def test_view_customers(logged_in_client):
    logged_in_client.post("/customers/register", json={"name": "Jane Doe", "phone_number": "+25756098388", "code": "DEF456"})
    response = logged_in_client.get("/customers/view_customers")
    assert response.status_code == 200
    assert len(response.get_json()) == 1

def test_view_customers_unauthorized(client):
    response = client.get("/customers/view_customers")
    assert response.status_code == 302 # Redirect to login

def test_view_single_customer(logged_in_client):
    response = logged_in_client.post("/customers/register", json={"name": "Jane Doe", "phone_number": "+25756098388", "code": "DEF456"})
    customer_id = response.get_json()["customer_id"]
    response = logged_in_client.get(f"/customers/view_customers/{customer_id}")
    assert response.status_code == 200
    assert response.get_json()["name"] == "Jane Doe"

def test_view_nonexistent_customer(logged_in_client):
    response = logged_in_client.get("/customers/view_customers/999")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"

def test_update_customer(logged_in_client):
    response = logged_in_client.post("/customers/register", json={"name": "Jane Doe", "phone_number": "+25756098388", "code": "DEF456"})
    customer_id = response.get_json()["customer_id"]
    update_data = {"name": "Jane Smith"}
    response = logged_in_client.put(f"/customers/update_customers/{customer_id}", json=update_data)
    assert response.status_code == 200
    assert response.get_json()["message"] == "Customer updated successfully"

def test_update_nonexistent_customer(logged_in_client):
    update_data = {"name": "Jane Smith"}
    response = logged_in_client.put("/customers/update_customers/999", json=update_data)
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"

def test_delete_customer(logged_in_client):
    response = logged_in_client.post("/customers/register", json={"name": "Jane Doe", "phone_number": "+25756098388", "code": "DEF456"})
    customer_id = response.get_json()["customer_id"]
    response = logged_in_client.delete(f"/customers/delete_customers/{customer_id}")
    assert response.status_code == 200
    assert response.get_json()["message"] == "Customer deleted successfully"

def test_delete_nonexistent_customer(logged_in_client):
    response = logged_in_client.delete("/customers/delete_customers/999")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"