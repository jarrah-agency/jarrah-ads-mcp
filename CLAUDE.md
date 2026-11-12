# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A remote HTTP MCP server exposing Google Ads and Microsoft Advertising (Bing Ads) tools. Users
authenticate with their own Google accounts via OAuth 2.0, select a Google Ads account, and the
server issues MCP bearer tokens that carry their credentials. Bing Ads tools use a single shared
refresh token stored in env vars (no per-user OAuth). Published to PyPI as `jarrah-gads-mcp`.

Derived from the Parabola AI app (`jarrah-agency/parabola-ai`) — `google_ads.py` and `google_ads_tools.py`
are taken from that codebase.

## Running locally for development

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in SERVER_URL, GOOGLE_ADS_* credentials, FERNET_KEY
SERVER_URL=http://localhost:8000 python3 -m jarrah_gads_mcp.server
```

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | Yes | From Google Ads API Centre |
| `GOOGLE_ADS_CLIENT_ID` | Yes | **Web application** OAuth client in Google Cloud Console |
| `GOOGLE_ADS_CLIENT_SECRET` | Yes | Same Web application OAuth client |
| `SERVER_URL` | Yes | Deployed base URL, e.g. `https://jarrah-gads-mcp.fly.dev` — no trailing slash |
| `FERNET_KEY` | Recommended | Base64 Fernet key for encrypting Google refresh tokens in SQLite. Auto-generates one if absent (tokens lost on restart). Generate: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `PORT` | No | HTTP port (default 8000) |
| `DB_PATH` | No | Path to SQLite DB (default: `gads_mcp.db` beside project root) |

`GOOGLE_ADS_REFRESH_TOKEN`, `GOOGLE_ADS_CUSTOMER_ID`, and `GOOGLE_ADS_LOGIN_CUSTOMER_ID` are **no longer
needed** — credentials are obtained per-user via the OAuth flow.

## Architecture

```
jarrah_gads_mcp/
  db.py            — SQLite CRUD (sync functions, called via asyncio.to_thread)
  auth_provider.py — GoogleAdsOAuthProvider implementing OAuthAuthorizationServerProvider
  server.py        — FastMCP server with auth, tools, and custom routes
  google_ads.py    — GoogleAdsClient wrapper + fetch_* functions returning DataFrames
  google_ads_tools.py — run_gaql() async wrapper with JSON output
  bing_ads.py      — Bing Ads SOAP client wrapper (shared credentials, no per-user OAuth)
```

Root-level `server.py` is the original stdio version kept for reference; `jarrah_gads_mcp/server.py`
is the authoritative remote version used by the PyPI entry point.

### OAuth flow

```
MCP client → GET /authorize
  → authorize() stores pending state → redirects to Google OAuth
Google → GET /oauth/google/callback
  → exchanges Google code for Google refresh token
  → lists accessible Google Ads accounts
  → single account: auto-selects; multiple: shows HTML account picker
  → creates MCP auth code → redirects back to MCP client redirect_uri
MCP client → POST /token
  → exchange_authorization_code() mints MCP access+refresh tokens
  → tokens carry (encrypted Google refresh token, customer_id, login_customer_id)
MCP tool call → bearer token verified via load_access_token()
  → GadsAccessToken contains decrypted google_refresh_token
  → tools call _creds() → get_access_token() to retrieve credentials
```

### Key types

- `GadsAccessToken(AccessToken)` — extends with `google_refresh_token`, `customer_id`, `login_customer_id`
- `GadsAuthCode(AuthorizationCode)` — extends with `google_rt_enc`, `customer_id`, `login_customer_id`
- `GadsRefreshToken(RefreshToken)` — extends with `google_rt_enc`, `customer_id`, `login_customer_id`

Note from provider.py: "FastMCP doesn't render any of these types in the user response, so it's OK to
add fields to subclasses which should not be exposed externally."

### Access token context in tools

Tools retrieve per-user credentials via:
```python
from mcp.server.auth.middleware.auth_context import get_access_token
token = get_access_token()  # returns GadsAccessToken for authenticated requests
```

This works because `AuthContextMiddleware` stores the authenticated user in a contextvar.

### MCP endpoint path

FastMCP mounts the streamable-HTTP MCP endpoint at `/mcp`, not `/`. Claude Desktop and other
clients that POST to the root URL are handled by `_McpRootRewrite` in `server.py`, an ASGI
middleware that rewrites `scope["path"]` from `/` to `/mcp` before the request reaches the app.

`resource_server_url` in `AuthSettings` is set to `f"{SERVER_URL}/mcp"` so the
`/.well-known/oauth-protected-resource` metadata advertises the correct endpoint.

### Custom routes

`@mcp.custom_route()` adds routes that bypass MCP auth middleware:
- `GET /oauth/google/callback` — receives Google OAuth callback
- `POST /oauth/select-account` — handles account picker form
- `GET /health` — health check

### Tools

| Tool | Function |
|---|---|
| `run_gaql` | Execute arbitrary GAQL against the authenticated account |
| `list_accounts` | List all accounts accessible with the authenticated user's Google token |
| `list_client_accounts` | List leaf accounts under an MCC manager |
| `switch_account` | Switch the session to a different account (direct or MCC child) |
| `get_campaign_metrics` | Spend/impressions/clicks/IS by date, device, channel |
| `get_conversion_actions` | Enabled conversions with 90-day volume |
| `get_quality_scores` | Keyword QS (1–10) with ad relevance, landing page, expected CTR |
| `get_ad_creative` | RSA headlines, descriptions, final URLs for all active ads |
| `generate_keyword_ideas` | **Non-functional under Basic API access (quota exhausted). Pending removal — see TODO in server.py.** |
| `get_keyword_historical_metrics` | **Non-functional under Basic API access (quota exhausted). Pending removal — see TODO in server.py.** |
| `bing_list_accounts` | List all Microsoft Advertising accounts accessible with the shared credentials |
| `bing_get_campaign_metrics` | Bing campaign daily spend, impressions, clicks, CTR, avg CPC, conversions, and revenue |
| `bing_get_keyword_performance` | Bing keyword clicks, spend, avg CPC, CTR, quality score, conversions, and revenue |
| `bing_get_ad_creative` | Bing RSA headlines/descriptions and ETA title/text |
| `bing_run_report` | Flexible Bing Ads report with caller-supplied report type, columns, date range, and aggregation; supports ConversionPerformanceReportRequest for goal breakdowns |

### Bing Ads notes

- Uses the `bingads` Python SDK (SOAP). Auth uses a single shared refresh token from `BING_REFRESH_TOKEN` env var — no per-user OAuth.
- Required env vars: `BING_DEVELOPER_TOKEN`, `BING_CLIENT_ID`, `BING_CLIENT_SECRET`, `BING_REFRESH_TOKEN`. All four must be set — a missing `BING_DEVELOPER_TOKEN` causes "Invalid client data" errors even if OAuth succeeds.
- **Token rotation**: Microsoft rotates the refresh token on each use. `_exchange_refresh_token()` in `bing_ads.py` fetches the access token directly via `urllib` (bypassing the SDK's OAuth flow to control the exact scope). Rotated tokens are persisted in the SQLite `kv` table (keys `bing_refresh_token` / `bing_refresh_token_source`) so they survive process restarts. The `kv` table is keyed to a SHA-256 fingerprint of the env token, so a new Fly secret starts a fresh chain. **Do not run local token-exchange tests against the production `BING_REFRESH_TOKEN` — each use invalidates the previous token and can break the entire token family.**
- **Account lookup**: `list_accounts` uses `CustomerManagementService.SearchAccounts` with a user-ID predicate (not `GetAccountsInfo`). Customer IDs are cached in `_customers_by_account` and injected into reporting/management service calls.
- **SDK SOAP header patch**: `_patch_service_headers()` overrides the SDK's header builder to omit `CustomerId`/`CustomerAccountId` fields when they are `None` — the SDK otherwise serialises them as the string `"None"` which Microsoft rejects.
- **Report requests**: `CustomDateRangeStart/End` use the `Date` SOAP type (not named types). `AccountIds` is passed as `{"long": [account_id]}` (not `svc.factory.create("ArrayOflong")`). Keyword reports use `BidMatchType` not `MatchType`. `Language` and `MaxOrMinBidOption` fields are omitted.
- Reporting tools submit a report, poll up to 120 seconds, download the zip, parse the CSV. `ExcludeReportHeader=True` / `ExcludeReportFooter=True` produce clean CSVs; `_parse_report_csv` drops the trailing Total row.
- `bing_list_accounts` returns account `id` values required by the other Bing tools.
- To regenerate the refresh token: `python3 get_bing_refresh_token.py` (uses authorization_code flow, safe to run without invalidating Fly's token).

### Keyword Planner notes

**These tools are non-functional under Basic API access and are pending removal.** `KeywordPlanIdeaService` has a separate, very tight quota that is exhausted immediately regardless of the general 15,000 ops/day Basic limit. Google rejected a Standard Access upgrade application in Nov 2026 (insufficient traffic). Reapply once other tool usage grows, or replace with a third-party keyword API (DataForSEO, SemRush). See TODO comment above `generate_keyword_ideas` in `server.py`.

- Uses `KeywordPlanIdeaService.GenerateKeywordIdeas` — **not** `search` or `search_stream`.
- Geo targeting uses a hardcoded `_COUNTRY_GEO_ID` dict in `google_ads.py` (ISO 3166-1 alpha-2 →
  criteria ID, e.g. `AU=2036`, `US=2840`). Do **not** use `SuggestGeoTargetConstants` — it requires
  `location_names` or `geo_targets` in the request body and errors if only `country_code` is set.
- Seed objects (`KeywordSeed`, `UrlSeed`, `KeywordAndUrlSeed`) use proto-plus and do **not** have
  `CopyFrom()`. Set fields directly on `request.keyword_seed`, `request.url_seed`, etc.

## Deploying to Fly.io

The app is deployed at `https://jarrah-gads-mcp.fly.dev` (region: `syd`, 512 MB RAM, persistent
volume at `/data`).

```bash
fly deploy                          # build and deploy
fly logs --app jarrah-gads-mcp      # stream logs
fly machine restart <id>            # restart a crashed machine
fly machine update <id> --vm-memory 512 --yes   # resize RAM
fly secrets set KEY=value           # set env vars (triggers redeploy)
```

To set up from scratch:
1. `fly launch` — creates app and `fly.toml`
2. `fly volumes create gads_mcp_data --region syd --size 1`
3. `fly secrets set GOOGLE_ADS_DEVELOPER_TOKEN=... GOOGLE_ADS_CLIENT_ID=... GOOGLE_ADS_CLIENT_SECRET=... SERVER_URL=https://<app>.fly.dev FERNET_KEY=... DB_PATH=/data/gads_mcp.db`
4. In Google Cloud Console: add `https://<app>.fly.dev/oauth/google/callback` as an authorized redirect URI on the **Web application** OAuth client.

## PyPI package

To release a new version:
1. Bump `version` in `pyproject.toml`
2. `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -m build`
3. **Before uploading**, verify the sdist contains no credentials:
   `tar tzf dist/*.tar.gz | grep -E '\.env|\.db|secret|token|key'`
   The only expected match is the package source files. If `.env` appears, stop — do not upload.
4. `PYPI_TOKEN=$(grep pypi_token .env | awk '{print $2}') && twine upload dist/* -u __token__ -p "$PYPI_TOKEN"`

The PyPI token is stored as `pypi_token` in `.env`. If upload returns 403, the token has expired — generate a new one at pypi.org/manage/account/token/ scoped to `jarrah-gads-mcp`.

Note: Fly.io deploys install from local source via Docker (`pip install .`), so a PyPI release is not required to deploy.

## GAQL notes

- `run_gaql` uses `ga_service.search()` (not `search_stream`) — suitable for ad-hoc queries up to 100 rows.
- Always include a date filter; Google Ads API rejects most queries without one.
- Customer ID dashes are stripped inside `run_gaql` before the API call.
- The `_compact()` helper in `google_ads_tools.py` recursively drops keys with empty dict, empty list,
  `None`, `0`, `""`, or `False` values to keep LLM context concise.
- `mcp` is pinned to `<2` — the 2.x release renamed `FastMCP` and broke the server.

## Known issues / gotchas

- `db.py` uses `threading.RLock()` (not `Lock()`) — `init_db()` acquires the lock then calls
  `_get_conn()` which also acquires it; a non-reentrant lock would deadlock on startup.
- `db.py` has a general-purpose `kv` table (keys: `bing_refresh_token`, `bing_refresh_token_source`) used to persist rotated Bing refresh tokens across restarts. Access via `kv_get(key)` / `kv_set(key, value)`.
- The Fly.io free tier machine (256 MB) is too small — the Google Ads gRPC library spikes memory
  during tool calls. Run at 512 MB minimum.
- Health check timeout is 5s; long-running Google Ads API calls can cause a single check to fail.
  This is harmless unless the machine is genuinely OOM-crashing.
- FastMCP holds a persistent session connection and calls `load_access_token` only once per session,
  not per tool call. Any code that needs to change credentials mid-session (e.g. `switch_account`)
  must write to the `_account_overrides` dict in `server.py` — DB updates alone are invisible to the
  running session.
