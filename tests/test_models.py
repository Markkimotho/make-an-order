import pytest # type: ignore
from app import app, db
from models import Customer, Order

# This will set up a test client with a temporary database
@pytest.fixture
def client():
    test_app = app  # Using the existing app instance
    
    # Set up the app context
    with test_app.app_context():
        # Configure the app for testing
        test_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'  # Use an in-memory database for tests
        test_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        
        # Create the tables before each test
        db.create_all()

        # Yield the test client for making requests
        yield test_app.test_client()

        # Clean up after tests
        db.session.remove()
        db.drop_all()


# Test customer creation
def test_create_customer(client):
    customer = Customer(name='John Doe', phone_number='1234567890', code='CUST001')
    db.session.add(customer)
    db.session.commit()
    
    # Check if the customer was added correctly
    fetched_customer = db.session.get(Customer, customer.id)  # Use db.session.get instead of query.get
    assert fetched_customer is not None
    assert fetched_customer.name == 'John Doe'
    assert fetched_customer.phone_number == '1234567890'
    assert fetched_customer.code == 'CUST001'

# Test order creation linked to a customer
def test_create_order(client):
    customer = Customer(name='Jane Doe', phone_number='9876543210', code='CUST002')
    db.session.add(customer)
    db.session.commit()
    
    order = Order(customer_id=customer.id, item='Laptop', amount=1000.00)
    db.session.add(order)
    db.session.commit()
    
    # Check if the order was added correctly
    fetched_order = db.session.get(Order, order.id)  # Use db.session.get instead of query.get
    assert fetched_order is not None
    assert fetched_order.item == 'Laptop'
    assert fetched_order.amount == 1000.00
    assert fetched_order.customer_id == customer.id
    assert fetched_order.time is not None  # Ensure the timestamp is set

# Test to_dict method of Customer
def test_customer_to_dict(client):
    customer = Customer(name='Alice', phone_number='1112223333', code='CUST003')
    db.session.add(customer)
    db.session.commit()
    
    customer_dict = customer.to_dict()
    assert customer_dict == {
        "id": customer.id,
        "name": "Alice",
        "phone_number": "1112223333",
        "code": "CUST003"
    }

# Test to_dict method of Order
def test_order_to_dict(client):
    customer = Customer(name='Bob', phone_number='4445556666', code='CUST004')
    db.session.add(customer)
    db.session.commit()
    
    order = Order(customer_id=customer.id, item='Phone', amount=500.00)
    db.session.add(order)
    db.session.commit()
    
    order_dict = order.to_dict()
    assert order_dict == {
        "id": order.id,
        "customer_id": customer.id,
        "item": "Phone",
        "amount": 500.00,
        "time": order.time
    }

# Test that customer and order delete works
def test_delete_customer_and_order(client):
    customer = Customer(name='Charlie', phone_number='7778889999', code='CUST005')
    db.session.add(customer)
    db.session.commit()
    
    order = Order(customer_id=customer.id, item='Tablet', amount=300.00)
    db.session.add(order)
    db.session.commit()
    
    # Deleting the customer should also delete the related order
    db.session.delete(customer)
    db.session.commit()
    
    # Assert customer and order are deleted
    assert db.session.get(Customer, customer.id) is None  # Use db.session.get instead of query.get
    assert db.session.get(Order, order.id) is None  # Use db.session.get instead of query.get
