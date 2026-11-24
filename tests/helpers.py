"""Shared fixtures for OAuth tests. No network, no real credentials."""
from __future__ import annotations

import time

from mcp.shared.auth import OAuthClientInformationFull

from jarrah_ads_mcp.auth_provider import encrypt

REDIRECT = "http://127.0.0.1:8787/callback"


def oauth_client(
    client_id: str = "client-1",
    redirect: str = REDIRECT,
) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[redirect],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        client_name="Test",
    )


def seed_code(db, code: str = "auth-code-1", client_id: str = "client-1") -> None:
    db.save_code(
        code,
        client_id,
        REDIRECT,
        True,
        "challenge",
        ["google-ads"],
        encrypt("google-refresh-token"),
        "1234567890",
        None,
        None,
        int(time.time()) + 300,
        None,
    )


def seed_pending(
    db,
    *,
    google_state: str = "google-state",
    client_id: str = "client-1",
    redirect_uri: str = "http://127.0.0.1:9/callback",
    with_google_token: bool = True,
) -> str:
    db.save_pending(
        google_state,
        "mcp-state",
        client_id,
        redirect_uri,
        "challenge",
        ["google-ads"],
        int(time.time()) + 600,
    )
    if with_google_token:
        db.update_pending_google_rt(google_state, encrypt("google-refresh-token"))
    db.save_client(
        client_id,
        {
            "client_id": client_id,
            "client_name": "Test Client",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    return google_state


def count_rows(db, table: str) -> int:
    return db._get_conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
