"""Microsoft Advertising (Bing Ads) API client wrapper."""
from __future__ import annotations

import hashlib
import io
import logging
import os
import tempfile
import time
import urllib.request
import zipfile
from datetime import date

import pandas as pd

log = logging.getLogger(__name__)

DEVELOPER_TOKEN = os.environ.get("BING_DEVELOPER_TOKEN", "")
CLIENT_ID       = os.environ.get("BING_CLIENT_ID", "")
CLIENT_SECRET   = os.environ.get("BING_CLIENT_SECRET", "")

# Env token is the bootstrap value (Fly secret). Microsoft often rotates the
# refresh token and invalidates the previous one. A rotated token is persisted
# in SQLite, keyed to the fingerprint of the env token that started the chain,
# so a restart keeps the live token and a new Fly secret starts a new chain.
_env_refresh_token = os.environ.get("BING_REFRESH_TOKEN", "")
_refresh_token: dict[str, str] = {"value": ""}
_refresh_token_loaded = False

# SearchAccounts has returned the same full page forever. Stop on a repeated
# page, and never walk more than this many pages.
_ACCOUNT_PAGE_SIZE = 100
_MAX_ACCOUNT_PAGES = 25
# urllib and the customer-management SOAP client. The bingads SDK otherwise
# blocks for up to 300 seconds inside its own token refresh.
_REQUEST_TIMEOUT_SECONDS = 20


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _ensure_refresh_token() -> str:
    """Return the live refresh token, preferring a persisted rotation of the current env token."""
    global _refresh_token_loaded
    if _refresh_token_loaded and _refresh_token["value"]:
        return _refresh_token["value"]

    token = _env_refresh_token
    if _env_refresh_token:
        try:
            from jarrah_ads_mcp import db as _db
            from jarrah_ads_mcp.auth_provider import decrypt

            _db.init_db()
            stored = _db.kv_get("bing_refresh_token")
            source = _db.kv_get("bing_refresh_token_source")
            if stored and source == _token_fingerprint(_env_refresh_token):
                token = decrypt(stored)
                log.info("Using persisted Bing Ads refresh token")
        except Exception:
            log.warning("Could not load persisted Bing refresh token; using env value", exc_info=True)

    _refresh_token["value"] = token
    _refresh_token_loaded = True
    return token


def _persist_refresh_token(new_rt: str) -> None:
    """Store a rotated refresh token so it survives process restarts."""
    if not _env_refresh_token:
        return
    try:
        from jarrah_ads_mcp import db as _db
        from jarrah_ads_mcp.auth_provider import encrypt

        _db.init_db()
        _db.kv_set("bing_refresh_token", encrypt(new_rt))
        _db.kv_set("bing_refresh_token_source", _token_fingerprint(_env_refresh_token))
        log.info("Persisted rotated Bing Ads refresh token")
    except Exception:
        log.warning("Failed to persist rotated Bing refresh token", exc_info=True)


def _bing_signin_expired_error() -> RuntimeError:
    """Microsoft rejected the refresh token. Reconnecting fixes this; a new developer token does not."""
    return RuntimeError(
        "Bing Ads sign-in expired or was rejected. "
        "Reconnect Microsoft from the MCP client. "
        "A new developer token is not required."
    )


def _persist_user_refresh_token(old_rt: str, new_rt: str) -> None:
    """Write a rotated per-user refresh token onto the session rows that still hold the old one.

    Microsoft invalidates the previous refresh token. The next load of those
    rows must present the new token, or the customer list fails again.
    """
    try:
        from jarrah_ads_mcp import db as _db

        _db.init_db()
        updated = _db.replace_bing_refresh_token(old_rt, new_rt)
        log.info("Persisted rotated Bing Ads refresh token on %s session rows", updated)
    except Exception:
        log.warning("Failed to persist rotated per-user Bing refresh token", exc_info=True)
    _refresh_live_session_token(old_rt, new_rt)


def _refresh_live_session_token(old_rt: str, new_rt: str) -> None:
    """Update the token already loaded for this MCP session.

    FastMCP calls load_access_token once per session, so a database update
    alone leaves the in-memory credential pointing at the invalidated token.
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
    except Exception:
        return
    if getattr(token, "bing_refresh_token", None) != old_rt:
        return
    try:
        token.bing_refresh_token = new_rt
    except Exception:
        log.warning("Could not update in-memory Bing refresh token", exc_info=True)


def _exchange_refresh_token(bing_refresh_token: str | None = None) -> tuple[str, str]:
    """Exchange a refresh token for an access token using urllib directly.

    Bypasses the bingads SDK's internal OAuth flow so we control the exact
    scope string and avoid any SDK version quirks.

    Returns ``(access_token, refresh_token)``. Microsoft may rotate the refresh
    token. A per-user rotation is written onto the session rows. The shared
    env token is written to the kv table instead.

    The Microsoft call times out after 20 seconds. ``invalid_grant`` means the
    sign-in was rotated or rejected, and the caller must reconnect.
    """
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    rt = bing_refresh_token if bing_refresh_token else _ensure_refresh_token()

    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": rt,
        "scope": "https://ads.microsoft.com/msads.manage offline_access",
    }).encode()

    req = urllib.request.Request(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as r:
            tokens = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        error_code = ""
        try:
            error_code = json.loads(body).get("error") or ""
        except json.JSONDecodeError:
            error_code = ""
        if error_code == "invalid_grant" or "invalid_grant" in body:
            raise _bing_signin_expired_error() from exc
        raise RuntimeError(f"Bing Ads token exchange failed ({exc.code}): {body}") from exc
    except TimeoutError as exc:
        raise RuntimeError(
            f"Bing Ads token exchange timed out after {_REQUEST_TIMEOUT_SECONDS} seconds"
        ) from exc

    if "access_token" not in tokens:
        raise RuntimeError(f"Bing Ads token exchange returned no access_token: {tokens}")

    new_rt = tokens.get("refresh_token") or rt
    if new_rt != rt:
        if bing_refresh_token is None:
            log.info("Bing Ads shared refresh token rotated")
            _refresh_token["value"] = new_rt
            _persist_refresh_token(new_rt)
        else:
            log.info("Bing Ads per-user refresh token rotated")
            _persist_user_refresh_token(rt, new_rt)

    return tokens["access_token"], new_rt


def _patch_service_headers() -> None:
    """Omit empty CustomerId headers.

    The SDK stringifies a missing id to "None", and Microsoft rejects that as
    invalid client data. Account-discovery calls have no customer id yet.
    """
    from bingads.manifest import USER_AGENT
    from bingads.service_client import ServiceClient

    if getattr(ServiceClient, "_jarrah_headers", False):
        return

    @staticmethod
    def _ensemble_header(authorization_data, **kwargs):
        if "soapheaders" in kwargs:
            raise Exception("cannot pass in kwargs contains soapheaders")
        if authorization_data is None:
            return kwargs
        headers = {"DeveloperToken": authorization_data.developer_token}
        if authorization_data.customer_id not in (None, ""):
            headers["CustomerId"] = str(authorization_data.customer_id)
        if authorization_data.account_id not in (None, ""):
            headers["CustomerAccountId"] = str(authorization_data.account_id)
        authorization_data.authentication.enrich_headers(headers)
        kwargs["soapheaders"] = headers
        kwargs["headers"] = {"User-Agent": USER_AGENT}
        return kwargs

    ServiceClient._ensemble_header = _ensemble_header
    ServiceClient._jarrah_headers = True


def _developer_token() -> str:
    token = os.environ.get("BING_DEVELOPER_TOKEN", "") or DEVELOPER_TOKEN
    if not token:
        raise RuntimeError("BING_DEVELOPER_TOKEN is not configured")
    return token


def _auth(
    account_id: int | None = None,
    customer_id: int | None = None,
    bing_refresh_token: str | None = None,
):
    """Build an AuthorizationData object with a pre-fetched access token.

    Fetches the access token ourselves (bypassing bingads SDK OAuth) so the
    scope and token format are guaranteed to match what Microsoft expects.

    When bing_refresh_token is provided, it is used directly (per-user OAuth).
    Otherwise the shared env token is used as a fallback.
    """
    from bingads import AuthorizationData, OAuthWebAuthCodeGrant
    from bingads.authorization import OAuthTokens

    _patch_service_headers()
    access_token, rt_for_sdk = _exchange_refresh_token(bing_refresh_token)

    # oauth_tokens is a read-only property; pass the tokens to the constructor.
    # rt_for_sdk is the token Microsoft just issued, including a rotation.
    oauth = OAuthWebAuthCodeGrant(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirection_uri="http://localhost:8080",
        oauth_tokens=OAuthTokens(
            access_token=access_token,
            access_token_expires_in_seconds=3600,
            refresh_token=rt_for_sdk,
        ),
        env="production",
    )

    return AuthorizationData(
        developer_token=_developer_token(),
        authentication=oauth,
        account_id=account_id,
        customer_id=customer_id,
    )


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _customer_management_client(authorization_data):
    """Customer-management SOAP client that fails fast when Microsoft rejects the token.

    ``refresh_oauth_tokens_automatically`` makes the SDK call Microsoft itself
    and wait up to 300 seconds. This server already exchanged the refresh token.
    """
    from bingads import ServiceClient

    svc = ServiceClient(
        "CustomerManagementService",
        version=13,
        authorization_data=authorization_data,
        environment="production",
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    svc.refresh_oauth_tokens_automatically = False
    return svc


def list_accounts(bing_refresh_token: str | None = None) -> list[dict]:
    """Return {id, name, number, customer_id, status} for all accessible accounts."""
    svc = _customer_management_client(_auth(bing_refresh_token=bing_refresh_token))
    user = svc.GetUser(UserId=None).User
    predicates = {
        "Predicate": [
            {"Field": "UserId", "Operator": "Equals", "Value": user.Id},
        ]
    }

    found: list = []
    seen_ids: set[str] = set()
    page_size = _ACCOUNT_PAGE_SIZE
    stopped_early = False
    for page_index in range(_MAX_ACCOUNT_PAGES):
        resp = svc.SearchAccounts(
            PageInfo={"Index": page_index, "Size": page_size},
            Predicates=predicates,
        )
        batch = _as_list(getattr(resp, "AdvertiserAccount", None))
        page_ids = [str(a.Id) for a in batch]
        # A full page of ids we already have means SearchAccounts is repeating
        # itself. Another request would never finish.
        if page_ids and all(account_id in seen_ids for account_id in page_ids):
            log.warning(
                "Bing Ads SearchAccounts repeated account ids on page %s; stopping",
                page_index,
            )
            stopped_early = True
            break
        for account, account_id in zip(batch, page_ids):
            if account_id in seen_ids:
                continue
            seen_ids.add(account_id)
            found.append(account)
        if len(batch) < page_size:
            stopped_early = True
            break
    if not stopped_early:
        log.warning("Bing Ads SearchAccounts stopped after %s pages", _MAX_ACCOUNT_PAGES)

    accounts = [
        {
            "id": str(a.Id),
            "name": a.Name,
            "number": a.Number,
            "customer_id": str(a.ParentCustomerId),
            "status": str(a.AccountLifeCycleStatus),
        }
        for a in found
    ]
    _customers_by_account.update({a["id"]: int(a["customer_id"]) for a in accounts})
    return accounts


_customers_by_account: dict[str, int] = {}


def _customer_id_for(account_id: int, bing_refresh_token: str | None = None) -> int:
    key = str(account_id)
    if key not in _customers_by_account:
        list_accounts(bing_refresh_token)
    if key not in _customers_by_account:
        raise RuntimeError(
            f"Bing Ads account {account_id} is not accessible to the signed-in Microsoft user"
        )
    return _customers_by_account[key]


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def _reporting_svc(account_id: int, bing_refresh_token: str | None = None):
    from bingads import ServiceClient
    return ServiceClient(
        "ReportingService",
        version=13,
        authorization_data=_auth(
            account_id=account_id,
            customer_id=_customer_id_for(account_id, bing_refresh_token),
            bing_refresh_token=bing_refresh_token,
        ),
        environment="production",
    )


def _make_time(svc, start: date, end: date):
    t = svc.factory.create("ReportTime")
    cs = svc.factory.create("Date")
    cs.Day, cs.Month, cs.Year = start.day, start.month, start.year
    t.CustomDateRangeStart = cs
    ce = svc.factory.create("Date")
    ce.Day, ce.Month, ce.Year = end.day, end.month, end.year
    t.CustomDateRangeEnd = ce
    return t


def _acct_ids(account_id: int):
    # ArrayOflong lives in the serialization namespace, which suds will not
    # create under the bare name. A long-array dict is the shape the SDK sends.
    return {"long": [account_id]}


def _clear_empty_enums(report_request) -> None:
    """Drop unset suds enums so Microsoft does not receive an empty value."""
    report_time = getattr(report_request, "Time", None)
    if report_time is None:
        return
    for name in ("PredefinedTime", "ReportTimeZone"):
        value = getattr(report_time, name, None)
        if value is not None and getattr(value, "value", None) is None:
            setattr(report_time, name, None)


def _submit_and_download(svc, report_request) -> pd.DataFrame:
    """Submit report, poll until ready, download zip, return parsed DataFrame."""
    _clear_empty_enums(report_request)
    report_id = svc.SubmitGenerateReport(ReportRequest=report_request)

    deadline = time.time() + 120
    download_url: str | None = None
    while time.time() < deadline:
        poll = svc.PollGenerateReport(ReportRequestId=report_id)
        status_obj = getattr(poll, "ReportRequestStatus", poll)
        status = status_obj.Status
        if status == "Success":
            download_url = status_obj.ReportDownloadUrl
            break
        if status in ("Error", "Failed"):
            raise RuntimeError(f"Bing Ads report failed with status: {status}")
        time.sleep(3)

    if not download_url:
        raise TimeoutError("Bing Ads report polling timed out after 120 seconds")

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        urllib.request.urlretrieve(download_url, tmp_path)
        with zipfile.ZipFile(tmp_path) as zf:
            csv_bytes = zf.read(zf.namelist()[0])
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return _parse_report_csv(csv_bytes.decode("utf-8-sig"))


def _parse_report_csv(content: str) -> pd.DataFrame:
    """Parse a Bing Ads report CSV (ExcludeReportHeader/Footer=True format).

    With ExcludeReportHeader and ExcludeReportFooter set to True the file is:
      - line 0: column headers
      - lines 1..n-1: data rows
      - last line: "Total" aggregation row (filtered out)
    """
    if not content.strip():
        return pd.DataFrame()

    df = pd.read_csv(io.StringIO(content))

    # Drop the totals row emitted by Bing (first column starts with "Total")
    if len(df.columns):
        first = df.columns[0]
        df = df[~df[first].astype(str).str.startswith("Total")]

    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Public fetch functions
# ---------------------------------------------------------------------------

def fetch_campaign_metrics(
    account_id: str,
    start_date: date,
    end_date: date,
    bing_refresh_token: str | None = None,
) -> pd.DataFrame:
    """Campaign performance: daily spend, impressions, clicks, CTR, avg CPC."""
    aid = int(account_id.replace("-", ""))
    svc = _reporting_svc(aid, bing_refresh_token)

    rq = svc.factory.create("CampaignPerformanceReportRequest")
    rq.Format = "Csv"
    rq.ReportName = "CampaignPerformance"
    rq.ReturnOnlyCompleteData = False
    rq.Aggregation = "Daily"
    rq.ExcludeReportHeader = True
    rq.ExcludeReportFooter = True

    cols = svc.factory.create("ArrayOfCampaignPerformanceReportColumn")
    cols.CampaignPerformanceReportColumn = [
        "TimePeriod", "CampaignName", "CampaignId",
        "Impressions", "Clicks", "Spend", "AverageCpc", "Ctr",
        "ConversionsQualified", "AllConversionsQualified", "Revenue", "AllRevenue",
    ]
    rq.Columns = cols

    scope = svc.factory.create("AccountThroughCampaignReportScope")
    scope.AccountIds = _acct_ids(aid)
    rq.Scope = scope

    rq.Time = _make_time(svc, start_date, end_date)
    return _submit_and_download(svc, rq)


def fetch_keyword_performance(
    account_id: str,
    start_date: date,
    end_date: date,
    bing_refresh_token: str | None = None,
) -> pd.DataFrame:
    """Keyword performance: clicks, spend, avg CPC, CTR, quality score."""
    aid = int(account_id.replace("-", ""))
    svc = _reporting_svc(aid, bing_refresh_token)

    rq = svc.factory.create("KeywordPerformanceReportRequest")
    rq.Format = "Csv"
    rq.ReportName = "KeywordPerformance"
    rq.ReturnOnlyCompleteData = False
    rq.Aggregation = "Summary"
    rq.ExcludeReportHeader = True
    rq.ExcludeReportFooter = True

    cols = svc.factory.create("ArrayOfKeywordPerformanceReportColumn")
    cols.KeywordPerformanceReportColumn = [
        "CampaignName", "AdGroupName", "Keyword", "BidMatchType",
        "Impressions", "Clicks", "Spend", "AverageCpc", "Ctr", "QualityScore",
        "ConversionsQualified", "AllConversionsQualified", "Revenue", "AllRevenue",
    ]
    rq.Columns = cols

    scope = svc.factory.create("AccountThroughAdGroupReportScope")
    scope.AccountIds = _acct_ids(aid)
    rq.Scope = scope

    rq.Time = _make_time(svc, start_date, end_date)
    return _submit_and_download(svc, rq)


# ---------------------------------------------------------------------------
# Flexible report builder
# ---------------------------------------------------------------------------

_VALID_REPORT_TYPES: dict[str, dict[str, str]] = {
    "CampaignPerformanceReportRequest": {
        "scope_type": "AccountThroughCampaignReportScope",
        "col_array_type": "ArrayOfCampaignPerformanceReportColumn",
        "col_element": "CampaignPerformanceReportColumn",
    },
    "AdGroupPerformanceReportRequest": {
        "scope_type": "AccountThroughAdGroupReportScope",
        "col_array_type": "ArrayOfAdGroupPerformanceReportColumn",
        "col_element": "AdGroupPerformanceReportColumn",
    },
    "KeywordPerformanceReportRequest": {
        "scope_type": "AccountThroughAdGroupReportScope",
        "col_array_type": "ArrayOfKeywordPerformanceReportColumn",
        "col_element": "KeywordPerformanceReportColumn",
    },
    "AccountPerformanceReportRequest": {
        "scope_type": "AccountReportScope",
        "col_array_type": "ArrayOfAccountPerformanceReportColumn",
        "col_element": "AccountPerformanceReportColumn",
    },
    "ConversionPerformanceReportRequest": {
        "scope_type": "AccountThroughAdGroupReportScope",
        "col_array_type": "ArrayOfConversionPerformanceReportColumn",
        "col_element": "ConversionPerformanceReportColumn",
    },
}


def fetch_flexible_report(
    account_id: str,
    report_type: str,
    columns: list[str],
    start_date: date,
    end_date: date,
    aggregation: str = "Daily",
    bing_refresh_token: str | None = None,
) -> dict:
    """Submit an arbitrary Bing Ads report request and return up to 200 rows.

    Returns a dict with keys:
      - 'records': list of row dicts (max 200)
      - 'truncated': True if more than 200 rows were returned
      - 'total_rows': actual row count before truncation
      or on error:
      - 'error': Microsoft fault or validation message
    """
    if report_type not in _VALID_REPORT_TYPES:
        return {
            "error": (
                f"Unknown report_type '{report_type}'. "
                f"Allowed: {list(_VALID_REPORT_TYPES)}"
            )
        }

    if aggregation not in ("Daily", "Weekly", "Monthly", "Summary"):
        return {
            "error": (
                f"Unknown aggregation '{aggregation}'. "
                "Allowed: Daily, Weekly, Monthly, Summary"
            )
        }

    today = date.today()
    if end_date > today:
        return {"error": f"end_date {end_date} is after today ({today})"}
    if end_date < start_date:
        return {"error": f"end_date {end_date} is before start_date {start_date}"}

    cols = list(columns)
    if aggregation == "Summary":
        if "TimePeriod" in cols:
            return {"error": "TimePeriod cannot be used with Summary aggregation"}
    else:
        if "TimePeriod" not in cols:
            cols = ["TimePeriod"] + cols

    aid = int(account_id.replace("-", ""))
    meta = _VALID_REPORT_TYPES[report_type]

    try:
        svc = _reporting_svc(aid, bing_refresh_token)

        rq = svc.factory.create(report_type)
        rq.Format = "Csv"
        rq.ReportName = "FlexibleReport"
        rq.ReturnOnlyCompleteData = False
        rq.Aggregation = aggregation
        rq.ExcludeReportHeader = True
        rq.ExcludeReportFooter = True

        col_array = svc.factory.create(meta["col_array_type"])
        setattr(col_array, meta["col_element"], cols)
        rq.Columns = col_array

        scope = svc.factory.create(meta["scope_type"])
        scope.AccountIds = _acct_ids(aid)
        rq.Scope = scope

        rq.Time = _make_time(svc, start_date, end_date)

        df = _submit_and_download(svc, rq)
    except Exception as exc:
        return {"error": str(exc)}

    records = df.to_dict(orient="records")
    total = len(records)
    truncated = total > 200
    return {
        "records": records[:200],
        "truncated": truncated,
        "total_rows": total,
    }


def fetch_ad_creative(account_id: str, bing_refresh_token: str | None = None) -> list[dict]:
    """Fetch RSA and ETA creative via Campaign Management service."""
    from bingads import ServiceClient

    aid = int(account_id.replace("-", ""))
    svc = ServiceClient(
        "CampaignManagementService",
        version=13,
        authorization_data=_auth(
            account_id=aid,
            customer_id=_customer_id_for(aid, bing_refresh_token),
            bing_refresh_token=bing_refresh_token,
        ),
        environment="production",
    )

    campaigns_resp = svc.GetCampaignsByAccountId(
        AccountId=aid,
        CampaignType="Search",
        ReturnAdditionalFields=None,
    )
    campaigns = (
        getattr(getattr(campaigns_resp, "Campaigns", None), "Campaign", None) or []
    )

    results = []
    for campaign in campaigns:
        ag_resp = svc.GetAdGroupsByCampaignId(CampaignId=campaign.Id)
        ad_groups = (
            getattr(getattr(ag_resp, "AdGroups", None), "AdGroup", None) or []
        )
        for ag in ad_groups:
            ads_resp = svc.GetAdsByAdGroupId(
                AdGroupId=ag.Id,
                AdTypes={"AdType": ["ResponsiveSearch", "ExpandedText"]},
                ReturnAdditionalFields=None,
            )
            ads = getattr(getattr(ads_resp, "Ads", None), "Ad", None) or []
            for ad in ads:
                entry: dict = {
                    "campaign_id": str(campaign.Id),
                    "campaign_name": campaign.Name,
                    "ad_group_id": str(ag.Id),
                    "ad_group_name": ag.Name,
                    "ad_id": str(ad.Id),
                    "ad_type": type(ad).__name__,
                    "status": str(ad.Status),
                }
                # Responsive Search Ad
                if hasattr(ad, "Headlines") and ad.Headlines:
                    links = getattr(ad.Headlines, "AssetLink", None) or []
                    entry["headlines"] = [
                        getattr(getattr(l, "Asset", None), "Text", None)
                        for l in links
                        if getattr(getattr(l, "Asset", None), "Text", None)
                    ]
                if hasattr(ad, "Descriptions") and ad.Descriptions:
                    links = getattr(ad.Descriptions, "AssetLink", None) or []
                    entry["descriptions"] = [
                        getattr(getattr(l, "Asset", None), "Text", None)
                        for l in links
                        if getattr(getattr(l, "Asset", None), "Text", None)
                    ]
                # Expanded Text Ad
                for field in ("TitlePart1", "TitlePart2", "TitlePart3", "Text", "TextPart2"):
                    val = getattr(ad, field, None)
                    if val:
                        entry[field.lower()] = val
                # Final URLs
                urls = getattr(getattr(ad, "FinalUrls", None), "string", None) or []
                if urls:
                    entry["final_urls"] = list(urls)

                results.append(entry)

    return results
