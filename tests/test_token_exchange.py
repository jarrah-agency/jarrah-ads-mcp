"""Authorization-code and refresh-token redemption is single-use and atomic."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from jarrah_ads_mcp.auth_provider import TokenError, encrypt
from tests.helpers import count_rows, oauth_client, seed_code


async def _issued(provider):
    seed_client = oauth_client()
    loaded = await provider.load_authorization_code(seed_client, "auth-code-1")
    assert loaded is not None
    return await provider.exchange_authorization_code(seed_client, loaded), seed_client, loaded


async def test_code_is_single_use(provider, db):
    seed_code(db)
    token, client, loaded = await _issued(provider)

    with pytest.raises(TokenError) as caught:
        await provider.exchange_authorization_code(client, loaded)
    assert caught.value.error == "invalid_grant"

    assert db.get_code("auth-code-1") is None
    assert count_rows(db, "oauth_access_tokens") == 1
    assert count_rows(db, "oauth_refresh_tokens") == 1
    refreshed = db.get_refresh_token_row(token.refresh_token)
    assert refreshed["access_token"] == token.access_token

    access = await provider.load_access_token(token.access_token)
    assert access is not None
    assert access.customer_id == "1234567890"
    assert access.google_refresh_token == "google-refresh-token"


async def test_concurrent_code_redemption_issues_one_pair(provider, db):
    seed_code(db)
    client = oauth_client()
    loaded = await provider.load_authorization_code(client, "auth-code-1")

    async def once():
        try:
            return await provider.exchange_authorization_code(client, loaded)
        except TokenError:
            return None

    results = await asyncio.gather(once(), once())
    assert sum(result is not None for result in results) == 1
    assert count_rows(db, "oauth_access_tokens") == 1
    assert count_rows(db, "oauth_refresh_tokens") == 1
    assert db.get_code("auth-code-1") is None


async def test_code_exchange_rolls_back_when_insert_fails(provider, db, monkeypatch):
    import jarrah_ads_mcp.auth_provider as auth_provider

    seed_code(db)
    db.save_access_token(
        "colliding-access",
        "other-client",
        ["google-ads"],
        encrypt("other-refresh"),
        "999",
        None,
        2_000_000_000,
    )
    issued = iter(["colliding-access", "brand-new-refresh"])
    monkeypatch.setattr(auth_provider.secrets, "token_urlsafe", lambda n=32: next(issued))

    client = oauth_client()
    loaded = await provider.load_authorization_code(client, "auth-code-1")
    with pytest.raises(sqlite3.IntegrityError):
        await provider.exchange_authorization_code(client, loaded)

    assert db.get_code("auth-code-1") is not None
    assert db.get_refresh_token_row("brand-new-refresh") is None
    assert db.get_access_token_row("colliding-access") is not None
    assert count_rows(db, "oauth_refresh_tokens") == 0


async def test_refresh_rotates_and_revokes_previous_access_token(provider, db):
    seed_code(db)
    first, client, _loaded = await _issued(provider)
    refresh = await provider.load_refresh_token(client, first.refresh_token)
    assert refresh is not None

    second = await provider.exchange_refresh_token(client, refresh, ["google-ads"])

    assert db.get_refresh_token_row(first.refresh_token) is None
    assert await provider.load_access_token(first.access_token) is None
    assert db.get_access_token_row(second.access_token) is not None
    assert db.get_refresh_token_row(second.refresh_token)["access_token"] == second.access_token
    current = await provider.load_access_token(second.access_token)
    assert current is not None
    assert current.google_refresh_token == "google-refresh-token"
    assert current.customer_id == "1234567890"


async def test_refresh_rollback_keeps_old_tokens(provider, db, monkeypatch):
    import jarrah_ads_mcp.auth_provider as auth_provider

    seed_code(db)
    first, client, _loaded = await _issued(provider)
    db.save_access_token(
        "colliding-access",
        "other-client",
        ["google-ads"],
        encrypt("other-refresh"),
        "999",
        None,
        2_000_000_000,
    )
    issued = iter(["colliding-access", "brand-new-refresh"])
    original_token = auth_provider.secrets.token_urlsafe
    monkeypatch.setattr(auth_provider.secrets, "token_urlsafe", lambda n=32: next(issued))

    refresh = await provider.load_refresh_token(client, first.refresh_token)
    with pytest.raises(sqlite3.IntegrityError):
        await provider.exchange_refresh_token(client, refresh, ["google-ads"])

    assert db.get_refresh_token_row(first.refresh_token) is not None
    assert db.get_access_token_row(first.access_token) is not None
    assert db.get_refresh_token_row("brand-new-refresh") is None
    assert await provider.load_access_token(first.access_token) is not None
    # The old refresh token can still be rotated after the failed attempt.
    monkeypatch.setattr(auth_provider.secrets, "token_urlsafe", original_token)
    again = await provider.load_refresh_token(client, first.refresh_token)
    recovered = await provider.exchange_refresh_token(client, again, ["google-ads"])
    assert recovered.access_token
    assert db.get_refresh_token_row(first.refresh_token) is None


async def test_second_refresh_is_rejected(provider, db):
    seed_code(db)
    first, client, _loaded = await _issued(provider)
    refresh = await provider.load_refresh_token(client, first.refresh_token)
    await provider.exchange_refresh_token(client, refresh, ["google-ads"])
    with pytest.raises(TokenError) as caught:
        await provider.exchange_refresh_token(client, refresh, ["google-ads"])
    assert caught.value.error == "invalid_grant"


def test_existing_database_gains_access_token_column(db):
    db._get_conn().execute("ALTER TABLE oauth_refresh_tokens DROP COLUMN access_token")
    db.init_db()
    columns = [
        row["name"]
        for row in db._get_conn().execute("PRAGMA table_info(oauth_refresh_tokens)")
    ]
    assert "access_token" in columns
