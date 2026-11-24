"""MCP OAuth 2.0 provider backed by Google OAuth + Google Ads API.

Flow:
  1. MCP client → GET /authorize  (code_challenge, redirect_uri, state)
  2. authorize() stores pending state, returns Google OAuth URL
  3. User signs in with Google, Google → GET /oauth/google/callback
  4. Callback exchanges Google code for Google refresh token
  5. Lists accessible Google Ads accounts → account picker if >1
  6. Account selected → MCP auth code created → redirect back to MCP client
  7. MCP client → POST /token  (code + code_verifier)
  8. exchange_authorization_code() mints MCP access+refresh tokens carrying
     encrypted Google refresh token + customer_id
  9. Tools call get_access_token() to retrieve per-user credentials
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from jarrah_ads_mcp import db as _db

log = logging.getLogger(__name__)

_REFRESH_TOKEN_TTL = 60 * 60 * 24 * 90  # 90 days

# Exact hosted callbacks for the MCP clients this server supports. Remote HTTPS
# is otherwise refused: a client could register its own server as the callback
# and receive the user's authorization code.
# Sources checked Nov 2026:
#   Claude — https://claude.com/docs/connectors/building/authentication
#     (claude.com is the same callback on the other product host)
#   Cursor — cursor.com/docs/mcp and the DCR set of three URIs (web, cursor://,
#     and http://localhost:8787/callback, which the loopback rule covers)
#   ChatGPT — https://developers.openai.com/plugins/build/auth
#     (per-connection /connector/oauth/{id}, plus the stable URI used when the
#     server advertises RFC 9207 issuer identification)
#   VS Code — code.visualstudio.com MCP authorization guide and
#     src/vs/base/common/oauth.ts (DCR sends both vscode.dev and insiders)
_ALLOWED_REDIRECT_URIS = frozenset({
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://www.cursor.com/agents/mcp/oauth/callback",
    "cursor://anysphere.cursor-mcp/oauth/callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://vscode.dev/redirect",
    "https://insiders.vscode.dev/redirect",
})
# One path segment. fullmatch rejects queries, fragments, and extra segments.
_CHATGPT_CALLBACK_RE = re.compile(
    r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,128}"
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_allowed_redirect_uri(uri: str) -> bool:
    """Allow loopback http(s) on any port plus an exact allowlist of hosted callbacks.

    Loopback covers Claude Code, Cursor desktop (http://localhost:8787/callback),
    VS Code's 127.0.0.1 listener, and the MCP Inspector (RFC 8252). Arbitrary
    custom schemes (evil-app://, file://, intent://) are refused. Fragments and
    userinfo are refused so a lookalike URI cannot smuggle another target.
    """
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    if parsed.fragment or parsed.username or parsed.password:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme in ("http", "https") and parsed.hostname in _LOOPBACK_HOSTS:
        return True
    return uri in _ALLOWED_REDIRECT_URIS or bool(_CHATGPT_CALLBACK_RE.fullmatch(uri))

# ---------------------------------------------------------------------------
# Fernet encryption helpers
# ---------------------------------------------------------------------------

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get("FERNET_KEY", "")
        if not key:
            key = Fernet.generate_key().decode()
            log.warning(
                "FERNET_KEY not set — using ephemeral key; "
                "tokens will be lost on restart."
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()


# ---------------------------------------------------------------------------
# Extended token models carrying Google Ads credentials
# ---------------------------------------------------------------------------

class GadsAuthCode(AuthorizationCode):
    google_rt_enc: str   # encrypted google refresh token
    customer_id: str
    login_customer_id: str | None = None
    bing_rt_enc: str | None = None  # encrypted microsoft refresh token (optional)


class GadsAccessToken(AccessToken):
    google_refresh_token: str  # decrypted, ready for API calls
    customer_id: str
    login_customer_id: str | None = None
    bing_refresh_token: str | None = None  # decrypted microsoft refresh token (optional)


class GadsRefreshToken(RefreshToken):
    google_rt_enc: str
    customer_id: str
    login_customer_id: str | None = None
    bing_rt_enc: str | None = None  # encrypted microsoft refresh token (optional)


# ---------------------------------------------------------------------------
# OAuth provider
# ---------------------------------------------------------------------------

class GoogleAdsOAuthProvider:
    """Implements OAuthAuthorizationServerProvider for Google Ads via Google OAuth."""

    def __init__(self, server_url: str) -> None:
        # server_url is the base URL of this MCP server (e.g. https://my.railway.app)
        self.server_url = server_url.rstrip("/")
        self._google_client_id = os.environ["GOOGLE_ADS_CLIENT_ID"]
        self._google_client_secret = os.environ["GOOGLE_ADS_CLIENT_SECRET"]
        self._google_redirect_uri = f"{self.server_url}/oauth/google/callback"

    # ------------------------------------------------------------------
    # Client registration (dynamic — MCP clients register themselves)
    # ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await asyncio.to_thread(_db.get_client, client_id)
        if row is None:
            return None
        # Re-check stored registrations. A client saved before the allowlist
        # existed must not keep a callback the server would now refuse.
        raw_uris = row.get("redirect_uris") or []
        allowed = [u for u in raw_uris if _is_allowed_redirect_uri(str(u))]
        if not allowed:
            log.warning("Client %s has no permitted redirect URIs; refusing", client_id)
            return None
        if len(allowed) != len(raw_uris):
            log.warning("Client %s had disallowed redirect URIs removed", client_id)
            row = {**row, "redirect_uris": allowed}
        return OAuthClientInformationFull.model_validate(row)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = list(client_info.redirect_uris or [])
        if not uris:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="At least one redirect URI is required",
            )
        for uri in uris:
            if not _is_allowed_redirect_uri(str(uri)):
                # RegistrationError -> HTTP 400 invalid_redirect_uri.
                # ValueError was unhandled and surfaced as HTTP 500.
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"Redirect URI not permitted: {uri}",
                )
        await asyncio.to_thread(
            _db.save_client,
            client_info.client_id,
            client_info.model_dump(mode="json"),
        )

    # ------------------------------------------------------------------
    # Authorization — redirect to Google OAuth
    # ------------------------------------------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        google_state = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + 600  # 10-minute window

        await asyncio.to_thread(
            _db.save_pending,
            google_state,
            params.state,
            client.client_id,
            str(params.redirect_uri),
            params.code_challenge,
            params.scopes or [],
            expires_at,
        )

        from urllib.parse import urlencode
        google_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": self._google_client_id,
            "redirect_uri": self._google_redirect_uri,
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/adwords",
            "access_type": "offline",
            "prompt": "consent",
            "state": google_state,
        })
        return google_url

    # ------------------------------------------------------------------
    # Authorization code exchange
    # ------------------------------------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> GadsAuthCode | None:
        row = await asyncio.to_thread(_db.get_code, authorization_code)
        if row is None:
            return None
        from pydantic import AnyUrl
        return GadsAuthCode(
            code=row["code"],
            scopes=row["scopes"],
            expires_at=float(row["expires_at"]),
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=row["redirect_uri_explicit"],
            resource=row["resource"],
            google_rt_enc=row["google_rt_enc"],
            customer_id=row["customer_id"],
            login_customer_id=row["login_customer_id"],
            bing_rt_enc=row.get("bing_rt_enc"),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: GadsAuthCode,
    ) -> OAuthToken:
        # Consume the code and store both new tokens in one transaction.
        # A second redemption loses the DELETE and gets no tokens.
        access_token_str = secrets.token_urlsafe(32)
        refresh_token_str = secrets.token_urlsafe(32)
        now = int(time.time())
        redeemed = await asyncio.to_thread(
            _db.redeem_authorization_code,
            authorization_code.code,
            access_token_str,
            refresh_token_str,
            now + 3600,
            now + _REFRESH_TOKEN_TTL,
        )
        if not redeemed:
            log.warning("Authorization code was already redeemed")
            raise TokenError(
                error="invalid_grant",
                error_description="Authorization code already used",
            )

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh_token_str,
        )

    # ------------------------------------------------------------------
    # Token introspection (used by BearerAuthBackend → load_access_token)
    # ------------------------------------------------------------------

    async def load_access_token(self, token: str) -> GadsAccessToken | None:
        row = await asyncio.to_thread(_db.get_access_token_row, token)
        if row is None:
            return None
        return GadsAccessToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            google_refresh_token=decrypt(row["google_rt_enc"]),
            customer_id=row["customer_id"],
            login_customer_id=row["login_customer_id"],
            bing_refresh_token=decrypt(row["bing_rt_enc"]) if row.get("bing_rt_enc") else None,
        )

    # ------------------------------------------------------------------
    # Refresh token exchange
    # ------------------------------------------------------------------

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> GadsRefreshToken | None:
        row = await asyncio.to_thread(_db.get_refresh_token_row, refresh_token)
        if row is None:
            return None
        if row.get("expires_at") and row["expires_at"] < int(time.time()):
            await asyncio.to_thread(_db.delete_refresh_token, refresh_token)
            return None
        return GadsRefreshToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            google_rt_enc=row["google_rt_enc"],
            customer_id=row["customer_id"],
            login_customer_id=row["login_customer_id"],
            bing_rt_enc=row.get("bing_rt_enc"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: GadsRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate the refresh token, revoke its previous access token, and store
        # the new pair in one transaction. A failure rolls the old token back.
        new_access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        use_scopes = scopes or refresh_token.scopes
        now = int(time.time())
        rotated = await asyncio.to_thread(
            _db.rotate_refresh_token,
            refresh_token.token,
            new_access,
            new_refresh,
            use_scopes,
            now + 3600,
            now + _REFRESH_TOKEN_TTL,
        )
        if not rotated:
            log.warning("Refresh token was already used")
            raise TokenError(
                error="invalid_grant",
                error_description="Refresh token already used",
            )

        return OAuthToken(
            access_token=new_access,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(use_scopes),
            refresh_token=new_refresh,
        )

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    async def revoke_token(
        self,
        token: GadsAccessToken | GadsRefreshToken,
    ) -> None:
        if isinstance(token, GadsAccessToken):
            await asyncio.to_thread(_db.delete_access_token, token.token)
        else:
            await asyncio.to_thread(_db.delete_refresh_token, token.token)
