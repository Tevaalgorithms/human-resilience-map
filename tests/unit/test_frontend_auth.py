from flask import Flask, abort, request

from resilience_map.core.config import Settings
from resilience_map.frontend.auth import install_auth


def _client():
    server = Flask(__name__)

    @server.before_request  # like Dash's first-request setup: registered before install_auth
    def first_request_setup():
        if request.path == "/":
            abort(401)

    settings = Settings(
        _env_file=None,
        auth_mode="cognito",
        cognito_domain="https://login.example.test",
        cognito_client_id="client",
        public_base_url="https://app.example.test",
    )
    install_auth(server, settings)

    @server.get("/")
    def home():  # stands in for the Dash app
        return "app"

    @server.get("/private")
    def private():
        return "secret"

    return server.test_client()


def test_signed_out_home_is_public_landing_page():
    r = _client().get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Human Resilience Map" in body
    assert 'href="/login"' in body
    assert "secret" not in body


def test_signed_out_other_paths_redirect_to_login():
    r = _client().get("/private")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/login")


def test_login_redirects_to_cognito():
    r = _client().get("/login")
    assert r.status_code == 302
    assert r.headers["Location"].startswith("https://login.example.test/oauth2/authorize")
