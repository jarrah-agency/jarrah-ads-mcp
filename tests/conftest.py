"""Isolated env for OAuth tests. Runs before the package is imported."""
from __future__ import annotations

import os
import tempfile

from cryptography.fernet import Fernet

os.environ["FERNET_KEY"] = Fernet.generate_key().decode()
os.environ["GOOGLE_ADS_CLIENT_ID"] = "test-google-client-id"
os.environ["GOOGLE_ADS_CLIENT_SECRET"] = "test-google-client-secret"
os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"] = "test-developer-token"
os.environ["SERVER_URL"] = "http://127.0.0.1:8000"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="jarrah-ads-mcp-"), "oauth.db")
for _bing_var in (
    "BING_CLIENT_ID",
    "BING_CLIENT_SECRET",
    "BING_DEVELOPER_TOKEN",
    "BING_REFRESH_TOKEN",
):
    os.environ.pop(_bing_var, None)

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    import jarrah_ads_mcp.db as database

    if database._conn is not None:
        database._conn.close()
        database._conn = None
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "oauth.db"))
    database.init_db()
    yield database
    if database._conn is not None:
        database._conn.close()
        database._conn = None


@pytest.fixture
def provider(db):
    from jarrah_ads_mcp.auth_provider import GoogleAdsOAuthProvider

    return GoogleAdsOAuthProvider("http://127.0.0.1:8000")


@pytest.fixture
def http(db):
    """ASGI client for the OAuth routes. Server exceptions stay HTTP responses."""
    from starlette.testclient import TestClient

    from jarrah_ads_mcp.server import _McpRootRewrite, mcp

    # streamable_http_app() binds one session manager, and its lifespan can
    # start only once. A fresh manager per test keeps the real ASGI app.
    mcp._session_manager = None
    app = _McpRootRewrite(mcp.streamable_http_app())
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
