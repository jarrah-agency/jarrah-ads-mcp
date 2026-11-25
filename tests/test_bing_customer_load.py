"""Bing customer loading must finish when SearchAccounts repeats a page or Microsoft rejects the refresh token."""
from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from jarrah_ads_mcp.auth_provider import decrypt, encrypt
from jarrah_ads_mcp.bing_ads import (
    _ACCOUNT_PAGE_SIZE,
    _MAX_ACCOUNT_PAGES,
    _REQUEST_TIMEOUT_SECONDS,
    _exchange_refresh_token,
)


class _Account:
    def __init__(self, account_id: int):
        self.Id = account_id
        self.Name = f"Account {account_id}"
        self.Number = f"N{account_id}"
        self.ParentCustomerId = 100
        self.AccountLifeCycleStatus = "Active"


class _Response:
    def __init__(self, accounts):
        self.AdvertiserAccount = accounts


def _page(start: int, size: int = _ACCOUNT_PAGE_SIZE):
    return [_Account(i) for i in range(start, start + size)]


class _Service:
    def __init__(self, pages):
        self.pages = pages
        self.calls: list[int] = []
        self.refresh_oauth_tokens_automatically = True
        self.timeout = None

    def GetUser(self, UserId=None):
        return SimpleNamespace(User=SimpleNamespace(Id=7))

    def SearchAccounts(self, PageInfo, Predicates):
        index = PageInfo["Index"]
        self.calls.append(index)
        if len(self.calls) > _MAX_ACCOUNT_PAGES:
            raise AssertionError("SearchAccounts paging did not stop")
        accounts = self.pages(index)
        return _Response(accounts)


def _install_service(monkeypatch, pages):
    from jarrah_ads_mcp import bing_ads

    service = _Service(pages)
    monkeypatch.setattr(bing_ads, "_auth", lambda **kwargs: object())
    bing_ads._customers_by_account.clear()

    def _client(authorization_data):
        service.refresh_oauth_tokens_automatically = False
        return service

    monkeypatch.setattr(bing_ads, "_customer_management_client", _client)
    return service


def test_repeated_account_page_stops(monkeypatch):
    from jarrah_ads_mcp.bing_ads import list_accounts

    service = _install_service(monkeypatch, lambda index: _page(0))
    accounts = list_accounts("user-refresh-token")
    assert service.calls == [0, 1]
    assert len(accounts) == _ACCOUNT_PAGE_SIZE
    assert accounts[0]["id"] == "0"
    assert accounts[-1]["customer_id"] == "100"


def test_search_pages_stop_at_twenty_five(monkeypatch):
    from jarrah_ads_mcp.bing_ads import list_accounts

    service = _install_service(
        monkeypatch,
        lambda index: _page(index * _ACCOUNT_PAGE_SIZE),
    )
    accounts = list_accounts("user-refresh-token")
    assert service.calls == list(range(_MAX_ACCOUNT_PAGES))
    assert len(accounts) == _ACCOUNT_PAGE_SIZE * _MAX_ACCOUNT_PAGES


def test_short_page_ends_the_search(monkeypatch):
    from jarrah_ads_mcp.bing_ads import list_accounts

    service = _install_service(monkeypatch, lambda index: _page(0, size=2))
    accounts = list_accounts("user-refresh-token")
    assert service.calls == [0]
    assert [row["id"] for row in accounts] == ["0", "1"]


def test_customer_client_disables_sdk_refresh_and_sets_timeout(monkeypatch):
    import bingads

    from jarrah_ads_mcp import bing_ads

    captured = {}

    class _Client:
        def __init__(self, service, version=None, authorization_data=None, environment=None, **kwargs):
            captured["service"] = service
            captured["timeout"] = kwargs.get("timeout")
            self.refresh_oauth_tokens_automatically = True

    monkeypatch.setattr(bingads, "ServiceClient", _Client)
    svc = bing_ads._customer_management_client(object())
    assert captured == {
        "service": "CustomerManagementService",
        "timeout": _REQUEST_TIMEOUT_SECONDS,
    }
    assert svc.refresh_oauth_tokens_automatically is False


class _TokenResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patch_urlopen(monkeypatch, handler):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", handler)


def test_microsoft_token_request_times_out_at_twenty_seconds(monkeypatch):
    seen = {}

    def _urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _TokenResponse(b'{"access_token":"access","refresh_token":"same-rt"}')

    _patch_urlopen(monkeypatch, _urlopen)
    access, refresh = _exchange_refresh_token("same-rt")
    assert seen["timeout"] == _REQUEST_TIMEOUT_SECONDS
    assert access == "access"
    assert refresh == "same-rt"


def test_invalid_grant_tells_the_user_to_reconnect(monkeypatch):
    def _urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "Bad Request",
            None,
            io.BytesIO(b'{"error":"invalid_grant","error_description":"AADSTS70000"}'),
        )

    _patch_urlopen(monkeypatch, _urlopen)
    with pytest.raises(RuntimeError) as caught:
        _exchange_refresh_token("dead-refresh-token")
    message = str(caught.value)
    assert "sign-in expired" in message
    assert "Reconnect Microsoft from the MCP client" in message
    assert "A new developer token is not required." in message
    assert "BING_DEVELOPER_TOKEN" not in message


def test_other_token_errors_keep_the_microsoft_body(monkeypatch):
    def _urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            None,
            io.BytesIO(b'{"error":"invalid_client"}'),
        )

    _patch_urlopen(monkeypatch, _urlopen)
    with pytest.raises(RuntimeError) as caught:
        _exchange_refresh_token("refresh-token")
    message = str(caught.value)
    assert "401" in message
    assert "invalid_client" in message
    assert "sign-in expired" not in message


def test_token_timeout_is_an_error(monkeypatch):
    def _urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    _patch_urlopen(monkeypatch, _urlopen)
    with pytest.raises(RuntimeError) as caught:
        _exchange_refresh_token("refresh-token")
    assert "timed out after 20 seconds" in str(caught.value)


def test_rotated_user_token_is_stored_on_session_rows(db, monkeypatch):
    from jarrah_ads_mcp.auth_provider import GadsAccessToken
    from mcp.server.auth.middleware import auth_context

    old = "old-user-refresh"
    new = "new-user-refresh"
    other = "someone-else"
    old_enc = encrypt(old)
    other_enc = encrypt(other)
    db.save_access_token(
        "access-1", "client", ["google-ads"], encrypt("google"), "1", None, None, old_enc,
    )
    db.save_refresh_token(
        "refresh-1", "client", ["google-ads"], encrypt("google"), "1", None, None, old_enc,
    )
    db.save_access_token(
        "access-2", "client", ["google-ads"], encrypt("google"), "2", None, None, other_enc,
    )
    live = GadsAccessToken(
        token="access-1",
        client_id="client",
        scopes=["google-ads"],
        google_refresh_token="google",
        customer_id="1",
        bing_refresh_token=old,
    )
    monkeypatch.setattr(auth_context, "get_access_token", lambda: live)

    def _urlopen(req, timeout=None):
        assert timeout == _REQUEST_TIMEOUT_SECONDS
        body = json.dumps({"access_token": "access", "refresh_token": new}).encode()
        return _TokenResponse(body)

    _patch_urlopen(monkeypatch, _urlopen)
    access, refresh = _exchange_refresh_token(old)
    assert access == "access"
    assert refresh == new
    assert decrypt(db.get_access_token_row("access-1")["bing_rt_enc"]) == new
    assert decrypt(db.get_refresh_token_row("refresh-1")["bing_rt_enc"]) == new
    assert decrypt(db.get_access_token_row("access-2")["bing_rt_enc"]) == other
    assert db.kv_get("bing_refresh_token") is None
    assert live.bing_refresh_token == new
