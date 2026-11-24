"""Customer API. Register/view need admin; update/delete need superuser."""
import pytest

JANE = {"name": "Jane Doe", "phone_number": "+25756098388", "code": "DEF456"}


@pytest.fixture
def admin(login_as):
    return login_as("admin")


@pytest.fixture
def superuser(login_as):
    return login_as("superuser")


def _register(c, data=JANE):
    response = c.post("/customers/register", json=data)
    assert response.status_code == 201
    return response.get_json()["customer_id"]


def test_register_customer(admin):
    data = {"name": "John Doe", "phone_number": "+25756098389", "code": "ABC123"}
    response = admin.post("/customers/register", json=data)
    assert response.status_code == 201
    assert response.get_json()["message"] == "Customer registered successfully"


def test_register_customer_unauthorized(client):
    response = client.post("/customers/register", json=JANE)
    assert response.status_code == 401


def test_register_customer_forbidden_for_cashier(login_as):
    response = login_as("cashier").post("/customers/register", json=JANE)
    assert response.status_code == 403


def test_register_customer_missing_fields(admin):
    response = admin.post("/customers/register", json={"name": "No Phone"})
    assert response.status_code == 400


def test_register_duplicate_customer(admin):
    _register(admin)
    response = admin.post("/customers/register", json=JANE)
    assert response.status_code == 400
    assert response.get_json()["error"] == "Phone number or code already exists"


def test_view_customers(admin):
    _register(admin)
    response = admin.get("/customers/view_customers")
    assert response.status_code == 200
    body = response.get_json()
    assert len(body["customers"]) == 1
    assert body["pagination"]["total"] == 1


def test_view_customers_unauthorized(client):
    response = client.get("/customers/view_customers")
    assert response.status_code == 401


def test_view_single_customer(admin):
    customer_id = _register(admin)
    response = admin.get(f"/customers/view_customers/{customer_id}")
    assert response.status_code == 200
    body = response.get_json()
    assert body["customer"]["name"] == "Jane Doe"
    assert body["stats"]["total_orders"] == 0


def test_view_nonexistent_customer(admin):
    response = admin.get("/customers/view_customers/999")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"


def test_update_customer(admin, superuser):
    customer_id = _register(admin)
    response = superuser.put(f"/customers/update_customers/{customer_id}",
                             json={"name": "Jane Smith"})
    assert response.status_code == 200
    assert response.get_json()["message"] == "Customer updated successfully"


def test_update_customer_forbidden_for_admin(admin):
    customer_id = _register(admin)
    response = admin.put(f"/customers/update_customers/{customer_id}",
                         json={"name": "Jane Smith"})
    assert response.status_code == 403


def test_update_nonexistent_customer(superuser):
    response = superuser.put("/customers/update_customers/999", json={"name": "Jane Smith"})
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"


def test_delete_customer(admin, superuser):
    customer_id = _register(admin)
    response = superuser.delete(f"/customers/delete_customers/{customer_id}")
    assert response.status_code == 200
    assert response.get_json()["message"] == "Customer deleted successfully"


def test_delete_nonexistent_customer(superuser):
    response = superuser.delete("/customers/delete_customers/999")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Customer not found"
