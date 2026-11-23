# jarrah-gads-mcp

A remote MCP server for Google Ads. Each person signs in with their own Google account, chooses an Ads account, and the server issues an MCP token for that session.

The hosted server is [https://jarrah-gads-mcp.fly.dev](https://jarrah-gads-mcp.fly.dev).

## Connect

Add this URL as a remote MCP server in Claude or any client that supports streamable HTTP and OAuth:

```text
https://jarrah-gads-mcp.fly.dev
```

The MCP endpoint is `/mcp`. Clients that POST to the site root are forwarded there.

On first use the client opens Google sign-in. One accessible Ads account is selected automatically. Several accounts show a picker. `switch_account` changes the account for the rest of the session, including accounts under an MCC.

## Tools

| Tool | Description |
|---|---|
| `run_gaql` | Run a GAQL query against the signed-in account |
| `list_accounts` | List Ads accounts the signed-in Google user can access |
| `list_client_accounts` | List leaf accounts under an MCC manager |
| `switch_account` | Point this session at a different account, including an MCC child |
| `get_campaign_metrics` | Spend, impressions, clicks, and impression share by date, device, and channel |
| `get_conversion_actions` | Enabled conversions with 90-day volume |
| `get_quality_scores` | Keyword quality score (1–10) with ad relevance, landing page, and expected CTR |
| `get_ad_creative` | RSA headlines, descriptions, and final URLs for active ads |
| `bing_list_accounts` | List Microsoft Advertising accounts accessible with the configured credentials |
| `bing_get_campaign_metrics` | Bing campaign daily spend, impressions, clicks, CTR, avg CPC, conversions, and revenue |
| `bing_get_keyword_performance` | Bing keyword clicks, spend, avg CPC, CTR, quality score, conversions, and revenue |
| `bing_get_ad_creative` | Bing RSA headlines/descriptions and ETA title/text |
| `bing_run_report` | Flexible Bing Ads report with caller-supplied report type, columns, date range, and aggregation — use `ConversionPerformanceReportRequest` for goal breakdowns |

## Query notes

- `run_gaql` uses `search()` and is meant for ad-hoc queries of up to 100 rows. Include a `LIMIT`.
- Include a date filter. The Google Ads API rejects most queries without one, for example `segments.date DURING LAST_30_DAYS`.
- Customer ID dashes are stripped before the API call.

## Bing Ads note

The hosted server connects Bing Ads tools to a single configured Microsoft Advertising account. If you need Bing tools pointed at your own account, run your own server and supply your own `BING_*` credentials.

## Run your own server

The hosted app is the way to connect. To run a copy yourself:

```bash
pip install -e .
cp .env.example .env
SERVER_URL=http://localhost:8000 python3 -m jarrah_gads_mcp.server
```

| Variable | Required | Notes |
|---|---|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | Yes | From the Google Ads API Centre |
| `GOOGLE_ADS_CLIENT_ID` | Yes | **Web application** OAuth client in Google Cloud Console |
| `GOOGLE_ADS_CLIENT_SECRET` | Yes | Same OAuth client |
| `SERVER_URL` | Yes | Public base URL, no trailing slash, e.g. `https://your-app.fly.dev` |
| `FERNET_KEY` | Recommended | Encrypts Google refresh tokens in SQLite. Without it, a new key is generated on startup and stored tokens are lost on restart |
| `PORT` | No | HTTP port (default `8000`) |
| `DB_PATH` | No | SQLite path (default `gads_mcp.db`) |

Google refresh tokens and customer IDs are collected per user during OAuth. They are not server environment variables.

In Google Cloud Console, add `{SERVER_URL}/oauth/google/callback` as an authorized redirect URI on that web OAuth client.

A production deploy needs a persistent disk for the SQLite database (`DB_PATH=/data/gads_mcp.db`) and at least 512 MB of RAM. The running server for this repo is on Fly.io in `syd`: [jarrah-gads-mcp.fly.dev](https://jarrah-gads-mcp.fly.dev).

## Source

[github.com/jarrah-agency/google-ads-mcp](https://github.com/jarrah-agency/google-ads-mcp)
