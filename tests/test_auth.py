def test_login_page_renders_when_logged_out(client):
    response = client.get('/login')
    assert response.status_code == 200


def test_login_page_redirects_when_logged_in(login_as):
    response = login_as('cashier').get('/login')
    assert response.status_code == 302
    assert response.headers['Location'] == '/dashboard'


def test_index_redirects_logged_in_user_to_dashboard(login_as):
    response = login_as('cashier').get('/')
    assert response.status_code == 302
    assert response.headers['Location'] == '/dashboard'


def test_index_shows_landing_when_logged_out(client):
    response = client.get('/')
    assert response.status_code == 200


def test_logout_clears_session_and_redirects_home(login_as):
    client = login_as('admin')
    response = client.get('/logout')
    assert response.status_code == 302
    assert response.headers['Location'] == '/'
    with client.session_transaction() as sess:
        assert 'profile' not in sess


def test_dashboard_requires_login(client):
    response = client.get('/dashboard')
    # The password sign-in page, not Google: a shop without Google keys
    # would otherwise land on Google's 'Access blocked' error.
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login')


def test_sign_in_page_points_to_real_places(client):
    """Staff are added by the owner; /register makes a new business, and the
    old role-select page is gone."""
    html = client.get("/login").get_data(as_text=True)
    assert "Request access" not in html and "role-select" not in html
    assert 'href="/register"' in html and "Create an account" in html
