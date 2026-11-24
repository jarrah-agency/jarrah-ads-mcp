"""Account picker rejects unknown ids, and Google-only sign-in skips Bing."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from tests.helpers import count_rows, seed_pending

ACCOUNTS = [
    {"id": "1234567890", "name": "Main", "is_manager": False},
    {"id": "555", "name": "Manager", "is_manager": True},
]


class _GoogleTokenResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"refresh_token": "google-refresh-token"}


class _GoogleHttp:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, data=None, **kwargs):
        assert "oauth2.googleapis.com" in url
        return _GoogleTokenResponse()


def _select(http, state, customer_id):
    return http.post(
        "/oauth/select-account",
        data={"google_state": state, "customer_id": customer_id},
        follow_redirects=False,
    )


def test_unmatched_account_is_rejected(http, db, monkeypatch):
    import jarrah_ads_mcp.server as server

    monkeypatch.setattr(server, "_list_google_accounts", lambda refresh_token: ACCOUNTS)
    state = seed_pending(db)

    response = _select(http, state, "9999999999")

    assert response.status_code == 400, response.text
    assert "is not available" in response.text
    assert "1234567890" in response.text
    assert 'name="customer_id"' in response.text
    pending = db.get_pending(state)
    assert pending is not None
    assert pending["customer_id"] is None
    assert count_rows(db, "oauth_codes") == 0


def test_dashed_known_account_is_accepted_without_bing(http, db, monkeypatch):
    import jarrah_ads_mcp.server as server

    monkeypatch.delenv("BING_CLIENT_ID", raising=False)
    monkeypatch.setattr(server, "_list_google_accounts", lambda refresh_token: ACCOUNTS)
    state = seed_pending(db)

    response = _select(http, state, "123-456-7890")

    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("http://127.0.0.1:9/callback?")
    assert "login.microsoftonline.com" not in location
    code = parse_qs(urlparse(location).query)["code"][0]
    row = db.get_code(code)
    assert row["customer_id"] == "1234567890"
    assert row["bing_rt_enc"] is None
    assert row["login_customer_id"] == "555"
    assert db.get_pending(state) is None


def test_discovery_failure_on_select_does_not_save_id(http, db, monkeypatch):
    import jarrah_ads_mcp.server as server

    def _boom(refresh_token):
        raise RuntimeError("ads api down")

    monkeypatch.setattr(server, "_list_google_accounts", _boom)
    state = seed_pending(db)

    response = _select(http, state, "9999999999")

    assert response.status_code == 503, response.text
    assert "Could not load your Google Ads accounts" in response.text
    assert "Go back and try again" in response.text
    pending = db.get_pending(state)
    assert pending is not None
    assert pending["customer_id"] is None
    assert count_rows(db, "oauth_codes") == 0


def test_discovery_failure_on_callback_is_retryable(http, db, monkeypatch):
    import jarrah_ads_mcp.server as server

    def _boom(refresh_token):
        raise RuntimeError("ads api down")

    monkeypatch.setattr(server.httpx, "AsyncClient", _GoogleHttp)
    monkeypatch.setattr(server, "_list_google_accounts", _boom)
    state = seed_pending(db, with_google_token=False)

    response = http.get(
        "/oauth/google/callback",
        params={"state": state, "code": "google-auth-code"},
        follow_redirects=False,
    )

    assert response.status_code == 503, response.text
    assert "Start the connection again" in response.text
    assert db.get_pending(state) is None
    assert count_rows(db, "oauth_codes") == 0
    assert "login.microsoftonline.com" not in response.headers.get("location", "")


def test_google_only_single_account_callback_skips_microsoft(http, db, monkeypatch):
    import jarrah_ads_mcp.server as server

    monkeypatch.delenv("BING_CLIENT_ID", raising=False)
    monkeypatch.setattr(server.httpx, "AsyncClient", _GoogleHttp)
    monkeypatch.setattr(
        server,
        "_list_google_accounts",
        lambda refresh_token: [{"id": "1234567890", "name": "Only", "is_manager": False}],
    )
    state = seed_pending(db, with_google_token=False)

    response = http.get(
        "/oauth/google/callback",
        params={"state": state, "code": "google-auth-code"},
        follow_redirects=False,
    )

    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("http://127.0.0.1:9/callback?")
    assert "login.microsoftonline.com" not in location
    code = parse_qs(urlparse(location).query)["code"][0]
    row = db.get_code(code)
    assert row["customer_id"] == "1234567890"
    assert row["bing_rt_enc"] is None


def test_bing_configured_still_redirects_to_microsoft(http, db, monkeypatch):
    import jarrah_ads_mcp.server as server

    monkeypatch.setenv("BING_CLIENT_ID", "test-bing-client")
    monkeypatch.setattr(
        server,
        "_list_google_accounts",
        lambda refresh_token: [{"id": "1234567890", "name": "Only", "is_manager": False}],
    )
    state = seed_pending(db)

    response = _select(http, state, "1234567890")

    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    assert "client_id=test-bing-client" in location
    assert db.get_pending(state)["customer_id"] == "1234567890"
    assert count_rows(db, "oauth_codes") == 0


def test_empty_account_list_does_not_accept_submitted_id(http, db, monkeypatch):
    import jarrah_ads_mcp.server as server

    monkeypatch.setattr(server, "_list_google_accounts", lambda refresh_token: [])
    state = seed_pending(db)

    response = _select(http, state, "9999999999")

    assert response.status_code == 400, response.text
    assert "No Google Ads accounts found" in response.text
    assert db.get_pending(state) is None
    assert count_rows(db, "oauth_codes") == 0


def test_disallowed_client_redirect_is_not_followed(http, db, monkeypatch):
    import jarrah_ads_mcp.server as server

    monkeypatch.delenv("BING_CLIENT_ID", raising=False)
    monkeypatch.setattr(
        server,
        "_list_google_accounts",
        lambda refresh_token: [{"id": "1234567890", "name": "Only", "is_manager": False}],
    )
    state = seed_pending(db, redirect_uri="https://evil.example/steal")

    response = _select(http, state, "1234567890")

    assert response.status_code == 400, response.text
    assert "not permitted" in response.text
    assert "evil.example" not in response.headers.get("location", "")
    assert count_rows(db, "oauth_codes") == 0
    assert db.get_pending(state) is None
