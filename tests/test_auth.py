import pytest # type: ignore
from app import app

@pytest.fixture
def client():
    with app.test_client() as client:
        yield client

def test_login(client):
    # Test to simulate the login route
    response = client.get('/login')
    assert response.status_code == 302  # Expecting a redirect

def test_hello_world_logged_in(client):
    with client.session_transaction() as sess:
        sess['profile'] = {'email': 'testuser@example.com'}
    
    response = client.get('/')
    assert response.status_code == 200
    assert 'Hello' in response.get_data(as_text=True)

def test_logout(client):
    # Firstly, set a fake session
    with client.session_transaction() as sess:
        sess['profile'] = {'email': 'testuser@example.com'}
    
    # Now testing the logout route
    response = client.get('/logout')
    assert response.status_code == 200
    assert 'You have logged out successfully!' in response.get_data(as_text=True)
