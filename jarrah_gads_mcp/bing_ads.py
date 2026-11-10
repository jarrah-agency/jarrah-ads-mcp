"""Microsoft Advertising (Bing Ads) API client wrapper."""
from __future__ import annotations

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
REFRESH_TOKEN   = os.environ.get("BING_REFRESH_TOKEN", "")


def _auth(account_id: int | None = None, customer_id: int | None = None):
    """Build an AuthorizationData object using the shared refresh token."""
    from bingads import AuthorizationData, OAuthWebAuthCodeGrant

    oauth = OAuthWebAuthCodeGrant(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirection_uri="http://localhost:8080",
        env="production",
    )
    oauth.request_oauth_tokens_by_refresh_token(REFRESH_TOKEN)

    auth_data = AuthorizationData(
        developer_token=DEVELOPER_TOKEN,
        authentication=oauth,
    )
    if account_id is not None:
        auth_data.account_id = account_id
    if customer_id is not None:
        auth_data.customer_id = customer_id
    return auth_data


def list_accounts() -> list[dict]:
    """Return {id, name, number, customer_id, status} for all accessible accounts."""
    from bingads import ServiceClient

    svc = ServiceClient(
        "CustomerManagementService",
        version=13,
        authorization_data=_auth(),
        environment="production",
    )
    resp = svc.GetAccountsInfo()
    raw = (
        getattr(getattr(resp, "AccountInfoWithCustomerData", None),
                "AccountInfoWithCustomerData", None)
        or []
    )
    return [
        {
            "id": str(a.AccountId),
            "name": a.AccountName,
            "number": a.AccountNumber,
            "customer_id": str(a.CustomerId),
            "status": str(a.AccountLifeCycleStatus),
        }
        for a in raw
    ]


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def _reporting_svc(account_id: int):
    from bingads import ServiceClient
    return ServiceClient(
        "ReportingService",
        version=13,
        authorization_data=_auth(account_id=account_id),
        environment="production",
    )


def _make_time(svc, start: date, end: date):
    t = svc.factory.create("ReportTime")
    cs = svc.factory.create("CustomDateRangeStart")
    cs.Day, cs.Month, cs.Year = start.day, start.month, start.year
    t.CustomDateRangeStart = cs
    ce = svc.factory.create("CustomDateRangeEnd")
    ce.Day, ce.Month, ce.Year = end.day, end.month, end.year
    t.CustomDateRangeEnd = ce
    return t


def _acct_ids(svc, account_id: int):
    arr = svc.factory.create("ArrayOflong")
    arr.long = [account_id]
    return arr


def _submit_and_download(svc, report_request) -> pd.DataFrame:
    """Submit report, poll until ready, download zip, return parsed DataFrame."""
    submit_resp = svc.SubmitGenerateReport(ReportRequest=report_request)
    report_id = submit_resp.ReportRequestId

    deadline = time.time() + 120
    download_url: str | None = None
    while time.time() < deadline:
        poll = svc.PollGenerateReport(ReportRequestId=report_id)
        status = poll.ReportRequestStatus.Status
        if status == "Success":
            download_url = poll.ReportRequestStatus.ReportDownloadUrl
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
) -> pd.DataFrame:
    """Campaign performance: daily spend, impressions, clicks, CTR, avg CPC."""
    aid = int(account_id.replace("-", ""))
    svc = _reporting_svc(aid)

    rq = svc.factory.create("CampaignPerformanceReportRequest")
    rq.Format = "Csv"
    rq.Language = "English"
    rq.ReportName = "CampaignPerformance"
    rq.ReturnOnlyCompleteData = False
    rq.Aggregation = "Daily"
    rq.ExcludeReportHeader = True
    rq.ExcludeReportFooter = True

    cols = svc.factory.create("ArrayOfCampaignPerformanceReportColumn")
    cols.CampaignPerformanceReportColumn = [
        "TimePeriod", "CampaignName", "CampaignId",
        "Impressions", "Clicks", "Spend", "AverageCpc", "Ctr",
    ]
    rq.Columns = cols

    scope = svc.factory.create("AccountThroughCampaignReportScope")
    scope.AccountIds = _acct_ids(svc, aid)
    rq.Scope = scope

    rq.Time = _make_time(svc, start_date, end_date)
    return _submit_and_download(svc, rq)


def fetch_keyword_performance(
    account_id: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Keyword performance: clicks, spend, avg CPC, CTR, quality score."""
    aid = int(account_id.replace("-", ""))
    svc = _reporting_svc(aid)

    rq = svc.factory.create("KeywordPerformanceReportRequest")
    rq.Format = "Csv"
    rq.Language = "English"
    rq.ReportName = "KeywordPerformance"
    rq.ReturnOnlyCompleteData = False
    rq.Aggregation = "Summary"
    rq.MaxOrMinBidOption = "MaxBid"
    rq.ExcludeReportHeader = True
    rq.ExcludeReportFooter = True

    cols = svc.factory.create("ArrayOfKeywordPerformanceReportColumn")
    cols.KeywordPerformanceReportColumn = [
        "CampaignName", "AdGroupName", "Keyword", "MatchType",
        "Impressions", "Clicks", "Spend", "AverageCpc", "Ctr", "QualityScore",
    ]
    rq.Columns = cols

    scope = svc.factory.create("AccountThroughAdGroupReportScope")
    scope.AccountIds = _acct_ids(svc, aid)
    rq.Scope = scope

    rq.Time = _make_time(svc, start_date, end_date)
    return _submit_and_download(svc, rq)


def fetch_ad_creative(account_id: str) -> list[dict]:
    """Fetch RSA and ETA creative via Campaign Management service."""
    from bingads import ServiceClient

    aid = int(account_id.replace("-", ""))
    svc = ServiceClient(
        "CampaignManagementService",
        version=13,
        authorization_data=_auth(account_id=aid),
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
