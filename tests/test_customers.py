import pytest # type: ignore
from app import app, db

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

def test_register_customer(client):
    data = {"name": "John Doe", "phone_number": "+25756098389", "code": "ABC123"}
    response = client.post("/customers/register", json=data)
    assert response.status_code == 201
    assert response.get_json()["message"] == "Customer registered successfully"

def test_register_duplicate_customer(client):
    data = {"name": "John Doe", "phone_number": "+25756098389", "code": "ABC123"}
    client.post("/customers/register", json=data)
    response = client.post("/customers/register", json=data)
    assert response.status_code == 400
    assert response.get_json()["error"] == "Phone number or code already exists"

def test_view_customers(client):
    client.post("/customers/register", json={"name": "Jane Doe", "phone_number": "+25756098388", "code": "DEF456"})
    response = client.get("/customers/view_customers")
    assert response.status_code == 200
    assert len(response.get_json()) == 1

def test_view_single_customer(client):
    response = client.post("/customers/register", json={"name": "Jane Doe", "phone_number": "+25756098388", "code": "DEF456"})
    customer_id = response.get_json()["customer_id"]
    response = client.get(f"/customers/view_customers/{customer_id}")
    assert response.status_code == 200
    assert response.get_json()["name"] == "Jane Doe"

def test_view_nonexistent_customer(client):
    response = client.get("/customers/view_customers/999")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"

def test_update_customer(client):
    response = client.post("/customers/register", json={"name": "Jane Doe", "phone_number": "+25756098388", "code": "DEF456"})
    customer_id = response.get_json()["customer_id"]
    update_data = {"name": "Jane Smith"}
    response = client.put(f"/customers/update_customers/{customer_id}", json=update_data)
    assert response.status_code == 200
    assert response.get_json()["message"] == "Customer updated successfully"

def test_update_nonexistent_customer(client):
    update_data = {"name": "Jane Smith"}
    response = client.put("/customers/update_customers/999", json=update_data)
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"

def test_delete_customer(client):
    response = client.post("/customers/register", json={"name": "Jane Doe", "phone_number": "+25756098388", "code": "DEF456"})
    customer_id = response.get_json()["customer_id"]
    response = client.delete(f"/customers/delete_customers/{customer_id}")
    assert response.status_code == 200
    assert response.get_json()["message"] == "Customer deleted successfully"

def test_delete_nonexistent_customer(client):
    response = client.delete("/customers/delete_customers/999")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"
