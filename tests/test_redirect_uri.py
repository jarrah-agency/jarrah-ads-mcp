"""Redirect URI allowlist: hosted clients, loopback, and rejected schemes."""
from __future__ import annotations

import pytest

from jarrah_ads_mcp.auth_provider import (
    RegistrationError,
    _is_allowed_redirect_uri,
)
from tests.helpers import oauth_client

CURSOR_REDIRECTS = [
    "cursor://anysphere.cursor-mcp/oauth/callback",
    "https://www.cursor.com/agents/mcp/oauth/callback",
    "http://localhost:8787/callback",
]

VSCODE_REDIRECTS = [
    "https://insiders.vscode.dev/redirect",
    "https://vscode.dev/redirect",
    "http://127.0.0.1/",
    "http://127.0.0.1:33418/",
]

_REGISTRATION = {
    "token_endpoint_auth_method": "none",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "client_name": "Test",
}


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost/callback",
        "http://localhost:3118/callback",
        "https://localhost:8443/cb",
        "http://127.0.0.1/callback",
        "http://127.0.0.1:8787/callback",
        "http://127.0.0.1:33418/",
        "http://[::1]/callback",
        "http://[::1]:54321/callback",
        "https://[::1]:8443/cb",
        "HTTP://LOCALHOST:9/cb",
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
        "https://www.cursor.com/agents/mcp/oauth/callback",
        "cursor://anysphere.cursor-mcp/oauth/callback",
        "https://chatgpt.com/connector/oauth/abc_DEF-123",
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://vscode.dev/redirect",
        "https://insiders.vscode.dev/redirect",
    ],
)
def test_allows_supported_callbacks(uri):
    assert _is_allowed_redirect_uri(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/callback",
        "https://claude.ai.evil.com/api/mcp/auth_callback",
        "https://claude.ai/api/mcp/auth_callback/extra",
        "https://claude.ai/api/mcp/auth_callback?next=https://evil.example",
        "https://claude.ai/api/mcp/auth_callback#frag",
        "https://user:pass@claude.ai/api/mcp/auth_callback",
        "http://user@localhost/callback",
        "http://localhost/callback#x",
        "http://[::1]:1/cb#x",
        "evil-app://callback",
        "file:///etc/passwd",
        "intent://evil",
        "javascript:alert(1)",
        "data:text/html,hi",
        "cursor://evil/oauth/callback",
        "http://0.0.0.0:80/callback",
        "http://localhost.evil.com/callback",
        "http://127.0.0.1.evil.com/callback",
        "https://chatgpt.com/connector/oauth/",
        "https://chatgpt.com/connector/oauth/abc/extra",
        "https://chatgpt.com/connector/oauth/abc?x=1",
        "",
        "http://",
    ],
)
def test_rejects_everything_else(uri):
    assert not _is_allowed_redirect_uri(uri)


async def test_register_client_raises_registration_error_not_value_error(provider):
    client = oauth_client(redirect="evil-app://callback")
    with pytest.raises(RegistrationError) as caught:
        await provider.register_client(client)
    assert caught.value.error == "invalid_redirect_uri"
    assert type(caught.value) is RegistrationError
    assert not isinstance(caught.value, ValueError)


def test_register_http_status_is_400_not_500(http):
    rejected = [
        ["evil-app://callback"],
        ["file:///tmp/x"],
        ["intent://scan"],
        ["https://evil.example/callback"],
        ["https://claude.ai/api/mcp/auth_callback#x"],
        [
            "https://www.cursor.com/agents/mcp/oauth/callback",
            "https://evil.example/callback",
            "http://localhost:8787/callback",
        ],
    ]
    for uris in rejected:
        response = http.post("/register", json={**_REGISTRATION, "redirect_uris": uris})
        assert response.status_code == 400, response.text
        body = response.json()
        assert body["error"] == "invalid_redirect_uri"
        assert response.status_code != 500


def test_cursor_three_uri_registration(http, db):
    response = http.post(
        "/register",
        json={**_REGISTRATION, "client_name": "Cursor", "redirect_uris": CURSOR_REDIRECTS},
    )
    assert response.status_code == 201, response.text
    stored = set(response.json()["redirect_uris"])
    assert stored == set(CURSOR_REDIRECTS)
    saved = db.get_client(response.json()["client_id"])
    assert set(saved["redirect_uris"]) == set(CURSOR_REDIRECTS)


def test_vscode_and_loopback_ipv6_registration(http):
    response = http.post(
        "/register",
        json={**_REGISTRATION, "redirect_uris": VSCODE_REDIRECTS},
    )
    assert response.status_code == 201, response.text

    ipv6 = http.post(
        "/register",
        json={**_REGISTRATION, "redirect_uris": ["http://[::1]:54321/callback"]},
    )
    assert ipv6.status_code == 201, ipv6.text


def test_rejected_registration_is_not_stored(http, db):
    from tests.helpers import count_rows

    response = http.post(
        "/register",
        json={**_REGISTRATION, "redirect_uris": ["https://evil.example/callback"]},
    )
    assert response.status_code == 400, response.text
    assert count_rows(db, "oauth_clients") == 0


async def test_get_client_refuses_stored_bad_redirects(provider, db):
    client = oauth_client()
    await provider.register_client(client)
    row = db.get_client("client-1")
    row["redirect_uris"] = ["https://evil.example/steal"]
    db.save_client("client-1", row)

    assert await provider.get_client("client-1") is None


async def test_get_client_filters_disallowed_uris(provider, db):
    client = oauth_client()
    await provider.register_client(client)
    row = db.get_client("client-1")
    row["redirect_uris"] = [
        "https://evil.example/steal",
        "http://127.0.0.1:8787/callback",
    ]
    db.save_client("client-1", row)

    loaded = await provider.get_client("client-1")
    assert loaded is not None
    assert [str(uri) for uri in loaded.redirect_uris] == ["http://127.0.0.1:8787/callback"]


def test_authorize_refuses_stored_bad_client(http, db):
    db.save_client(
        "legacy-evil",
        {
            "client_id": "legacy-evil",
            "client_name": "Legacy",
            "redirect_uris": ["https://evil.example/steal"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    response = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "legacy-evil",
            "redirect_uri": "https://evil.example/steal",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
            "state": "s",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400, response.text
    assert "not found" in response.text
    location = response.headers.get("location", "")
    assert "evil.example" not in location
