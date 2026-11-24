# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A remote HTTP MCP server exposing Google Ads and Microsoft Advertising (Bing Ads) tools. Users
authenticate with their own Google and Microsoft accounts via OAuth 2.0, and the server issues MCP
bearer tokens that carry both sets of credentials encrypted. Published to PyPI as `jarrah-ads-mcp`.

Derived from the Parabola AI app (`jarrah-agency/parabola-ai`) — `google_ads.py` and `google_ads_tools.py`
are taken from that codebase.

## Commands

```bash
# Install dependencies (use pyproject.toml — requirements.txt is missing bingads)
pip install -e .

# Run locally
SERVER_URL=http://localhost:8000 python3 -m jarrah_ads_mcp.server

# Build PyPI package
python3 -m build

# Generate a new Fernet key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Regenerate Bing refresh token (safe — does not invalidate the production Fly token)
BING_CLIENT_ID=... BING_CLIENT_SECRET=... python3 get_bing_refresh_token.py
```

There is no test suite.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | Yes | From Google Ads API Centre |
| `GOOGLE_ADS_CLIENT_ID` | Yes | **Web application** OAuth client in Google Cloud Console |
| `GOOGLE_ADS_CLIENT_SECRET` | Yes | Same Web application OAuth client |
| `SERVER_URL` | Yes | Deployed base URL, e.g. `https://jarrah-gads-mcp.fly.dev` — no trailing slash |
| `FERNET_KEY` | Recommended | Base64 Fernet key for encrypting Google refresh tokens in SQLite. Auto-generates one if absent (tokens lost on restart). |
| `BING_DEVELOPER_TOKEN` | Yes (Bing) | Microsoft Advertising developer token |
| `BING_CLIENT_ID` | Yes (Bing) | Azure AD app client ID |
| `BING_CLIENT_SECRET` | Yes (Bing) | Azure AD app client secret |
| `BING_REFRESH_TOKEN` | Yes (Bing) | Fallback refresh token for sessions created before per-user OAuth, and for self-hosters who skip the Microsoft login step |
| `PORT` | No | HTTP port (default 8000) |
| `DB_PATH` | No | Path to SQLite DB (default: `gads_mcp.db` beside project root) |

`GOOGLE_ADS_REFRESH_TOKEN`, `GOOGLE_ADS_CUSTOMER_ID`, and `GOOGLE_ADS_LOGIN_CUSTOMER_ID` are **no longer
needed** — credentials are obtained per-user via the OAuth flow.

## Architecture

```
jarrah_ads_mcp/
  db.py            — SQLite CRUD (sync functions, called via asyncio.to_thread)
  auth_provider.py — GoogleAdsOAuthProvider implementing OAuthAuthorizationServerProvider
  server.py        — FastMCP server with auth, tools, and custom routes
  google_ads.py    — GoogleAdsClient wrapper + fetch_* functions returning DataFrames
  google_ads_tools.py — run_gaql() async wrapper with JSON output
  bing_ads.py      — Bing Ads SOAP client wrapper (per-user OAuth; falls back to env token)
```

Root-level `server.py`, `google_ads.py`, and `google_ads_tools.py` are the original stdio-based
versions kept for reference only. `jarrah_ads_mcp/server.py` is the authoritative remote version
used by the PyPI entry point. `AGENTS.md` is a stale duplicate of this file (pre-Bing Ads).

### Tool development pattern

Every MCP tool follows the same two-layer pattern:

1. **Sync function** in `google_ads.py` or `bing_ads.py` — does the actual API work, returns a
   DataFrame or dict/list.
2. **Async wrapper** in `server.py` decorated with `@mcp.tool()` — parses string args, calls the
   sync function via `asyncio.to_thread(_sync)`, and returns JSON-serialisable output.

```python
# bing_ads.py — always accept bing_refresh_token=None as last parameter
def fetch_something(account_id: str, start_date: date, end_date: date,
                    bing_refresh_token: str | None = None) -> pd.DataFrame:
    ...

# server.py — call _bing_rt() and pass it through
@mcp.tool()
async def bing_something(account_id: str, start_date: str, end_date: str) -> list[dict]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    bing_refresh_token = _bing_rt()
    def _sync():
        from jarrah_ads_mcp.bing_ads import fetch_something
        return fetch_something(account_id, start, end, bing_refresh_token).to_dict(orient="records")
    return await asyncio.to_thread(_sync)
```

Tools that change session state (e.g. `switch_account`) must write to the `_account_overrides`
dict in `server.py` directly — DB updates alone are invisible to the already-loaded token in the
session contextvar.

### OAuth flow

```
MCP client → GET /authorize
  → authorize() stores pending state → redirects to Google OAuth
Google → GET /oauth/google/callback
  → exchanges Google code for Google refresh token
  → lists accessible Google Ads accounts
  → single account: auto-selects; multiple: shows HTML account picker
  → stores account selection + bing_state in pending row
  → redirects to Microsoft OAuth
Microsoft → GET /oauth/microsoft/callback
  → exchanges Microsoft code for Bing refresh token
  → creates MCP auth code → redirects back to MCP client redirect_uri
  (user may skip via GET /oauth/microsoft/skip → MCP code with no Bing token)
MCP client → POST /token
  → exchange_authorization_code() mints MCP access+refresh tokens
  → tokens carry (encrypted Google RT, customer_id, login_customer_id, encrypted Bing RT)
MCP tool call → bearer token verified via load_access_token()
  → GadsAccessToken contains decrypted google_refresh_token + bing_refresh_token
  → Google tools call _creds(); Bing tools call _bing_rt()
```

### Key types

- `GadsAccessToken(AccessToken)` — extends with `google_refresh_token`, `customer_id`, `login_customer_id`, `bing_refresh_token`
- `GadsAuthCode(AuthorizationCode)` — extends with `google_rt_enc`, `customer_id`, `login_customer_id`, `bing_rt_enc`
- `GadsRefreshToken(RefreshToken)` — extends with `google_rt_enc`, `customer_id`, `login_customer_id`, `bing_rt_enc`

FastMCP doesn't render these types in the user response, so it's safe to add fields to subclasses.

### Access token context in tools

```python
from mcp.server.auth.middleware.auth_context import get_access_token
token = get_access_token()  # returns GadsAccessToken for authenticated requests
```

`AuthContextMiddleware` stores the authenticated user in a contextvar.

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
- `GET /oauth/microsoft/callback` — receives Microsoft OAuth callback; exchanges code for Bing refresh token
- `GET /oauth/microsoft/skip` — completes the flow without a Bing token (Bing tools fall back to env)
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
| `bing_list_accounts` | List all Microsoft Advertising accounts accessible to the authenticated user |
| `bing_get_campaign_metrics` | Bing campaign daily spend, impressions, clicks, CTR, avg CPC, conversions, and revenue |
| `bing_get_keyword_performance` | Bing keyword clicks, spend, avg CPC, CTR, quality score, conversions, and revenue |
| `bing_get_ad_creative` | Bing RSA headlines/descriptions and ETA title/text |
| `bing_run_report` | Flexible Bing Ads report with caller-supplied report type, columns, date range, and aggregation; supports ConversionPerformanceReportRequest for goal breakdowns |

### Bing Ads notes

- Uses the `bingads` Python SDK (SOAP). Each user authenticates via Microsoft OAuth during the MCP connect flow; the per-user Bing refresh token is stored encrypted in SQLite alongside the Google token. Sessions that pre-date per-user OAuth, or users who skip the Microsoft step, fall back to `BING_REFRESH_TOKEN`.
- Required env vars: `BING_DEVELOPER_TOKEN`, `BING_CLIENT_ID`, `BING_CLIENT_SECRET`, `BING_REFRESH_TOKEN`. All four must be set — a missing `BING_DEVELOPER_TOKEN` causes "Invalid client data" errors even if OAuth succeeds.
- The Azure AD app must have `{SERVER_URL}/oauth/microsoft/callback` as an authorised redirect URI.
- **Token rotation**: Microsoft rotates the refresh token on each use. `_exchange_refresh_token()` in `bing_ads.py` fetches the access token directly via `urllib` (bypassing the SDK's OAuth flow to control the exact scope). Rotated tokens are persisted in the SQLite `kv` table (keys `bing_refresh_token` / `bing_refresh_token_source`) so they survive process restarts. The `kv` table is keyed to a SHA-256 fingerprint of the env token, so a new Fly secret starts a fresh chain. **Do not run local token-exchange tests against the production `BING_REFRESH_TOKEN` — each use invalidates the previous token and can break the entire token family.**
- **Account lookup**: `list_accounts` uses `CustomerManagementService.SearchAccounts` with a user-ID predicate (not `GetAccountsInfo`). Customer IDs are cached in `_customers_by_account` and injected into reporting/management service calls.
- **SDK SOAP header patch**: `_patch_service_headers()` overrides the SDK's header builder to omit `CustomerId`/`CustomerAccountId` fields when they are `None` — the SDK otherwise serialises them as the string `"None"` which Microsoft rejects.
- **Report requests**: `CustomDateRangeStart/End` use the `Date` SOAP type (not named types). `AccountIds` is passed as `{"long": [account_id]}` (not `svc.factory.create("ArrayOflong")`). Keyword reports use `BidMatchType` not `MatchType`. `Language` and `MaxOrMinBidOption` fields are omitted.
- Reporting tools submit a report, poll up to 120 seconds, download the zip, parse the CSV. `ExcludeReportHeader=True` / `ExcludeReportFooter=True` produce clean CSVs; `_parse_report_csv` drops the trailing Total row.
- `bing_list_accounts` returns account `id` values required by the other Bing tools.
- **Conversion columns**: `Conversions` and `AllConversions` are deprecated and return 0. Use `ConversionsQualified` and `AllConversionsQualified`. Revenue columns are `Revenue` and `AllRevenue`. Do not add `Goal`/`GoalType` to `CampaignPerformanceReportRequest` — they conflict with impression-share columns. Use `ConversionPerformanceReportRequest` via `bing_run_report` for goal breakdowns.
- **`bing_run_report` scope types**: `CampaignPerformanceReportRequest` → `AccountThroughCampaignReportScope`; keyword/ad group/conversion reports → `AccountThroughAdGroupReportScope`; account reports → `AccountReportScope`.
- To regenerate the refresh token: `python3 get_bing_refresh_token.py` (uses authorization_code flow, safe to run without invalidating Fly's token).

### Keyword Planner notes

**Removed.** `generate_keyword_ideas` and `get_keyword_historical_metrics` were deleted. `KeywordPlanIdeaService` requires Standard Access; Google rejected the upgrade application in Nov 2026 (insufficient traffic). To restore keyword research, implement via the **DataForSEO Keywords Data API** — no Google Ads Standard Access required.

## Deploying to Fly.io

The app is deployed at `https://jarrah-gads-mcp.fly.dev` (region: `syd`, 512 MB RAM, persistent
volume at `/data`). The Fly app name is `jarrah-gads-mcp` (Fly names are permanent; `fly.toml`
matches this). The PyPI package and Python package directory are `jarrah-ads-mcp` / `jarrah_ads_mcp`.

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
5. In Azure portal: add `https://<app>.fly.dev/oauth/microsoft/callback` as a redirect URI on the Azure AD app.

## PyPI package

To release a new version:
1. Bump `version` in `pyproject.toml`
2. `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -m build`
3. **Before uploading**, verify the sdist contains no credentials:
   `tar tzf dist/*.tar.gz | grep -E '\.env|\.db|secret|token|key'`
   The only expected match is the package source files. If `.env` appears, stop — do not upload.
4. `PYPI_TOKEN=$(grep pypi_token .env | awk '{print $2}') && twine upload dist/* -u __token__ -p "$PYPI_TOKEN"`

The PyPI token is stored as `pypi_token` in `.env`. If upload returns 403, the token has expired — generate a new one at pypi.org/manage/account/token/ scoped to `jarrah-ads-mcp`.

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
- `requirements.txt` does not include `bingads` — use `pip install -e .` (from `pyproject.toml`) for a complete install.
