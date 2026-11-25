"""Conversion-window accuracy and GAQL row capping."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from jarrah_ads_mcp.google_ads import (
    VOLUME_WINDOW_DAYS,
    conversion_volume_window,
    list_conversion_actions,
)
from jarrah_ads_mcp.google_ads_tools import MAX_GAQL_ROWS, _bounded_gaql, _compact, run_gaql


def test_window_is_trailing_90_calendar_days():
    assert VOLUME_WINDOW_DAYS == 90
    start, end = conversion_volume_window(date(2026, 11, 25))
    assert (start, end) == (date(2026, 8, 28), date(2026, 11, 25))
    assert (end - start).days + 1 == 90

    # Month-start arithmetic used to land on the 1st three months back (~115 days).
    assert start != date(2026, 8, 1)

    leap_start, leap_end = conversion_volume_window(date(2024, 3, 1))
    assert (leap_start, leap_end) == (date(2023, 12, 3), date(2024, 3, 1))
    assert (leap_end - leap_start).days + 1 == 90
    assert conversion_volume_window(date(2026, 3, 1)) == (
        date(2025, 12, 2),
        date(2026, 3, 1),
    )


class _Batch:
    def __init__(self, results):
        self.results = results


def _action(action_id, name, conversions_type="WEBPAGE", category="PURCHASE"):
    resource = f"customers/123/conversionActions/{action_id}"
    return SimpleNamespace(
        conversion_action=SimpleNamespace(
            id=action_id,
            name=name,
            resource_name=resource,
            type=SimpleNamespace(name=conversions_type),
            category=SimpleNamespace(name=category),
        )
    )


def _volume(action_id, amount):
    return SimpleNamespace(
        segments=SimpleNamespace(
            conversion_action=f"customers/123/conversionActions/{action_id}"
        ),
        metrics=SimpleNamespace(all_conversions=amount),
    )


def _install_ads(monkeypatch, action_rows, volume_rows, volume_error=None):
    import jarrah_ads_mcp.google_ads as ads

    queries = []

    def search_stream(customer_id, query):
        queries.append(query)
        if "FROM conversion_action" in query:
            return [_Batch(action_rows)]
        if "metrics.all_conversions" in query:
            if volume_error is not None:
                raise volume_error
            return [_Batch(volume_rows)]
        raise AssertionError(query)

    service = SimpleNamespace(search_stream=search_stream)
    monkeypatch.setattr(
        ads,
        "build_client",
        lambda *args, **kwargs: SimpleNamespace(get_service=lambda _name: service),
    )
    monkeypatch.setattr(ads, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 11, 25))}))
    return queries


def test_duplicate_action_names_stay_separate(monkeypatch):
    queries = _install_ads(
        monkeypatch,
        [_action(1, "Purchase"), _action(2, "Purchase")],
        [_volume(1, 10), _volume(2, 3), _volume(1, 1.5)],
    )
    rows = list_conversion_actions("refresh", "123")
    purchases = [row for row in rows if row["name"] == "Purchase"]
    assert len(purchases) == 2
    by_id = {row["id"]: row for row in purchases}
    assert set(by_id) == {"1", "2"}
    assert by_id["1"]["volume_90d"] == 11.5
    assert by_id["2"]["volume_90d"] == 3
    assert by_id["1"]["resource_name"] != by_id["2"]["resource_name"]
    assert by_id["1"]["volume_status"] == "ok"
    assert by_id["1"]["volume_start"] == "2026-08-28"
    assert by_id["1"]["volume_end"] == "2026-11-25"
    assert by_id["1"]["volume_days"] == 90
    volume_query = queries[-1]
    assert "segments.conversion_action" in volume_query
    assert "2026-08-28" in volume_query
    assert "2026-11-25" in volume_query
    assert "2026-08-01" not in volume_query


def test_volume_query_failure_is_unknown_not_zero(monkeypatch):
    _install_ads(
        monkeypatch,
        [_action(1, "Purchase"), _action(2, "Purchase")],
        [],
        volume_error=RuntimeError("volume down"),
    )
    rows = list_conversion_actions("refresh", "123")
    assert len(rows) == 2
    assert {row["id"] for row in rows} == {"1", "2"}
    for row in rows:
        assert row["volume_90d"] is None
        assert row["volume_90d"] != 0
        assert row["volume_status"] == "unknown"
        assert "volume down" in row["volume_error"]
        assert row["volume_start"] == "2026-08-28"
        assert row["name"] == "Purchase"


def test_compact_keeps_zero_false_and_empty_string():
    compacted = _compact(
        {
            "metrics": {"clicks": 0, "impressions": 0.0, "cost": 1.5},
            "enabled": False,
            "label": "",
            "missing": None,
            "empty": {},
            "empty_list": [],
            "nested": {"zero": 0, "gone": None},
        }
    )
    assert compacted == {
        "metrics": {"clicks": 0, "impressions": 0.0, "cost": 1.5},
        "enabled": False,
        "label": "",
        "nested": {"zero": 0},
    }
    assert _compact({"metrics": {"clicks": 0, "impressions": 0, "cost_micros": 0}}) == {
        "metrics": {"clicks": 0, "impressions": 0, "cost_micros": 0}
    }


def test_bounded_gaql_rewrites_only_real_limit_clauses():
    assert _bounded_gaql("SELECT campaign.id FROM campaign").endswith("LIMIT 101")
    assert _bounded_gaql("SELECT campaign.id FROM campaign LIMIT 20").endswith("LIMIT 20")
    rewritten = _bounded_gaql("SELECT campaign.id FROM campaign ORDER BY campaign.id LIMIT 500")
    assert rewritten.endswith("LIMIT 101")
    assert "LIMIT 500" not in rewritten
    literal = "SELECT campaign.id FROM campaign WHERE campaign.name = 'LIMIT 5'"
    bounded_literal = _bounded_gaql(literal)
    assert "LIMIT 5" in bounded_literal
    assert bounded_literal.rstrip().endswith("LIMIT 101")
    with_params = "SELECT campaign.id FROM campaign LIMIT 1000 PARAMETERS include_drafts=true"
    assert _bounded_gaql(with_params) == (
        "SELECT campaign.id FROM campaign LIMIT 101 PARAMETERS include_drafts=true"
    )


class _Row:
    def __init__(self, payload):
        self._payload = payload

    @classmethod
    def to_dict(cls, row):
        return row._payload


class _Pager:
    def __init__(self, pages):
        self._pages = pages
        self.pages_fetched = 0

    def __iter__(self):
        for page in self._pages:
            self.pages_fetched += 1
            yield from page


def _install_search(monkeypatch, pages):
    import jarrah_ads_mcp.google_ads as ads

    pager = _Pager(pages)
    seen = {}

    def search(customer_id, query):
        seen["customer_id"] = customer_id
        seen["query"] = query
        return pager

    service = SimpleNamespace(search=search)
    monkeypatch.setattr(
        ads,
        "build_client",
        lambda *args, **kwargs: SimpleNamespace(get_service=lambda _name: service),
    )
    return pager, seen


@pytest.mark.asyncio
async def test_gaql_stops_paging_and_flags_truncation(monkeypatch):
    pages = [
        [_Row({"metrics": {"clicks": i}}) for i in range(start, start + 40)]
        for start in range(0, 200, 40)
    ]
    pager, seen = _install_search(monkeypatch, pages)
    output = await run_gaql(
        "SELECT metrics.clicks FROM campaign",
        "123-456",
        "refresh",
    )
    body, _, note = output.partition("\n\n")
    rows = json.loads(body)
    assert len(rows) == MAX_GAQL_ROWS
    assert note == "[Results truncated to 100 rows]"
    assert pager.pages_fetched == 3
    assert seen["customer_id"] == "123456"
    assert seen["query"].rstrip().endswith("LIMIT 101")
    assert rows[0]["metrics"]["clicks"] == 0
    assert rows[-1]["metrics"]["clicks"] == 99


@pytest.mark.asyncio
async def test_gaql_under_the_cap_is_not_marked_truncated(monkeypatch):
    pages = [[_Row({"metrics": {"clicks": 0}, "enabled": False})]]
    _pager, seen = _install_search(monkeypatch, pages)
    output = await run_gaql(
        "SELECT metrics.clicks FROM campaign LIMIT 20",
        "99",
        "refresh",
    )
    assert "truncated" not in output
    rows = json.loads(output)
    assert rows == [{"metrics": {"clicks": 0}, "enabled": False}]
    assert seen["query"].endswith("LIMIT 20")
