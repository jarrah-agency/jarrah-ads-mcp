"""Google Ads GAQL tool — async function that executes arbitrary GAQL queries."""

from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger(__name__)

# The tool returns at most this many rows. One extra row is fetched so the
# response can say the result was truncated instead of looking complete.
MAX_GAQL_ROWS = 100


def _scan_keywords(query: str) -> list[tuple[str, int, int]]:
    """Find LIMIT and PARAMETERS keywords that are not inside string literals."""
    found: list[tuple[str, int, int]] = []
    i = 0
    n = len(query)
    in_string = False
    while i < n:
        ch = query[i]
        if in_string:
            if ch == "'":
                if i + 1 < n and query[i + 1] == "'":
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            i += 1
            continue
        if ch.isalpha():
            j = i + 1
            while j < n and query[j].isalpha():
                j += 1
            word = query[i:j].upper()
            prev_ok = i == 0 or not (query[i - 1].isalnum() or query[i - 1] == "_")
            next_ok = j == n or not (query[j].isalnum() or query[j] == "_")
            if prev_ok and next_ok and word in ("LIMIT", "PARAMETERS"):
                found.append((word, i, j))
            i = j
            continue
        i += 1
    return found


def _bounded_gaql(query: str, cap: int = MAX_GAQL_ROWS) -> str:
    """Ask Google for at most cap+1 rows when that rewrite is safe.

    A trailing or mid-query LIMIT larger than the cap is lowered. A LIMIT at
    or below the cap is left alone. When the query has no LIMIT, one is
    inserted before PARAMETERS, or appended. LIMIT inside a string literal
    is not treated as a clause.
    """
    fetch = cap + 1
    keywords = _scan_keywords(query)
    limit_hits = [item for item in keywords if item[0] == "LIMIT"]
    if limit_hits:
        _word, _start, end = limit_hits[-1]
        j = end
        while j < len(query) and query[j].isspace():
            j += 1
        k = j
        while k < len(query) and query[k].isdigit():
            k += 1
        if k == j:
            return query
        if int(query[j:k]) <= cap:
            return query
        return f"{query[:j]}{fetch}{query[k:]}"

    insertion = f" LIMIT {fetch} "
    param_hits = [item for item in keywords if item[0] == "PARAMETERS"]
    if param_hits:
        param_at = param_hits[0][1]
        return f"{query[:param_at].rstrip()}{insertion}{query[param_at:]}"
    stripped = query.strip().rstrip(";").rstrip()
    return f"{stripped}\nLIMIT {fetch}"


async def run_gaql(
    query: str,
    customer_id: str,
    refresh_token: str,
    login_customer_id: str | None = None,
) -> str:
    """Execute a GAQL query and return results as a JSON string.

    At most MAX_GAQL_ROWS rows are returned. When more rows match, the JSON
    is followed by a truncation note. The search stops as soon as that extra
    row is seen, and the query sent to Google is capped with LIMIT when safe.
    """

    def _sync() -> list[dict] | str | tuple[list[dict], bool]:
        from google.ads.googleads.errors import GoogleAdsException
        from jarrah_ads_mcp.google_ads import build_client

        client = build_client(refresh_token, login_customer_id)
        ga_service = client.get_service("GoogleAdsService")
        cid = customer_id.replace("-", "")
        bounded = _bounded_gaql(query)

        try:
            response = ga_service.search(customer_id=cid, query=bounded)
            rows: list[dict] = []
            truncated = False
            for row in response:
                if len(rows) >= MAX_GAQL_ROWS:
                    truncated = True
                    break
                row_dict = type(row).to_dict(row)
                rows.append(_compact(row_dict))
            return rows, truncated
        except GoogleAdsException as exc:
            errors = [e.message for e in exc.failure.errors]
            return f"Google Ads API error: {'; '.join(errors)}"
        except Exception as exc:
            return f"Error: {exc}"

    result = await asyncio.to_thread(_sync)

    if isinstance(result, str):
        log.warning("GAQL error for customer %s: %s", customer_id, result)
        return result

    rows, truncated = result
    if not rows:
        return "Query returned no results."

    output = json.dumps(rows, indent=2, default=str)
    if truncated:
        output += f"\n\n[Results truncated to {MAX_GAQL_ROWS} rows]"
    log.info(
        "GAQL returned %d rows for customer %s%s",
        len(rows),
        customer_id,
        " (truncated)" if truncated else "",
    )
    return output


def _compact(d: dict) -> dict:
    """Drop None and containers that are empty after compaction.

    Zero, 0.0, False, and "" are kept. A metrics object of all zeros stays
    a metrics object instead of disappearing.
    """
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            v = _compact(v)
            if v:
                out[k] = v
        elif isinstance(v, list):
            if v:
                out[k] = v
        elif v is not None:
            out[k] = v
    return out
