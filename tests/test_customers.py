import pytest
from app import app, db
from models import Customer

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
