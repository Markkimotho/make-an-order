import pytest # type: ignore
from app import app, db

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
    assert response.status_code == 302 # Now redirects to dashboard
    assert response.headers['Location'] == '/dashboard' # Verify redirect to dashboard


def test_logout(client):
    # Firstly, set a fake session
    with client.session_transaction() as sess:
        sess['profile'] = {'email': 'testuser@example.com'}

        # Now testing the logout route
    response = client.get('/logout')
    assert response.status_code == 302 # Now redirects to index
    assert response.headers['Location'].startswith('/') # Verify redirect to index
    assert 'You have logged out successfully!' in response.headers['Location'] # Check message in redirect URL