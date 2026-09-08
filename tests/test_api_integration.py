# Integration/smoke tests for the live Unclaimed API.
#
# These tests exercise a *running* instance of api/main.py as a black box —
# real HTTP requests over the network, nothing mocked or imported directly
# from the app. That means they need the API to actually be up and reachable
# before you run them.
#
# Usage:
#   docker-compose up --build          # (in another terminal, from repo root)
#   python3 -m pip install --user pytest httpx
#   python3 -m pytest tests/test_api_integration.py -v
#
# To point at a different deployment (e.g. Railway) instead of localhost:
#   API_BASE_URL=https://your-app.up.railway.app python3 -m pytest tests/test_api_integration.py -v

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")

# Columns the API whitelists for `sort_by` (kept in sync with api/main.py's
# SORTABLE_COLUMNS so test 4 asserts against the real whitelist wording).
SORTABLE_COLUMNS = ["country", "municipality", "name", "opportunity_score", "population", "quality_score"]


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
        yield c


@pytest.fixture(scope="module")
def default_agencies(client: httpx.Client) -> list[dict[str, Any]]:
    """The full /agencies list with default params, fetched once and reused."""
    resp = client.get("/agencies")
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture(scope="module")
def stats(client: httpx.Client) -> dict[str, Any]:
    resp = client.get("/stats")
    assert resp.status_code == 200
    return resp.json()


def _is_monotonic(values: list, descending: bool) -> bool:
    """True if `values` is non-increasing (descending) / non-decreasing
    (ascending). None values are excluded from the comparison since NULL
    ordering is a DB-default concern outside the scope of these tests, and
    the current dataset doesn't have any for the numeric score columns."""
    filtered = [v for v in values if v is not None]
    for a, b in zip(filtered, filtered[1:]):
        if descending and a < b:
            return False
        if not descending and a > b:
            return False
    return True


# --- 1. /health -------------------------------------------------------------


def test_health(client: httpx.Client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- 2. /agencies default ordering ------------------------------------------


def test_agencies_default_returns_list(default_agencies: list[dict[str, Any]]):
    assert isinstance(default_agencies, list)
    assert len(default_agencies) > 0
    # Sanity-check the shape of one row against AgencyOut.
    row = default_agencies[0]
    for field in (
        "id",
        "mobility_agency_id",
        "name",
        "country",
        "municipality",
        "feed_url",
        "population",
        "quality_score",
        "opportunity_score",
        "missing_files",
    ):
        assert field in row


def test_agencies_default_sorted_by_opportunity_score_desc(default_agencies: list[dict[str, Any]]):
    scores = [row["opportunity_score"] for row in default_agencies]
    assert _is_monotonic(scores, descending=True), "default /agencies is not sorted by opportunity_score desc"


# --- 3. sort_by=quality_score&order=asc changes ordering ---------------------


def test_agencies_sort_by_quality_score_asc(client: httpx.Client, default_agencies: list[dict[str, Any]]):
    resp = client.get("/agencies", params={"sort_by": "quality_score", "order": "asc"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == len(default_agencies)

    quality_scores = [row["quality_score"] for row in rows]
    assert _is_monotonic(quality_scores, descending=False), "quality_score asc ordering is wrong"

    # Confirm this genuinely differs from the default ordering (same set of
    # rows, different order) rather than the API silently ignoring sort_by.
    default_ids = [row["id"] for row in default_agencies]
    asc_ids = [row["id"] for row in rows]
    assert default_ids != asc_ids


# --- 4. invalid sort_by --------------------------------------------------


def test_agencies_invalid_sort_by_returns_400(client: httpx.Client):
    resp = client.get("/agencies", params={"sort_by": "not_a_real_column"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "not_a_real_column" in detail
    # The whitelist should be surfaced to the caller so they know what's valid.
    for column in SORTABLE_COLUMNS:
        assert column in detail


# --- 5. country filter --------------------------------------------------


def test_agencies_country_filter(client: httpx.Client, stats: dict[str, Any]):
    # Pick a real country straight out of the live /stats response instead of
    # hardcoding a guess.
    assert stats["top_5_countries"], "expected /stats to report at least one country"
    country = stats["top_5_countries"][0]["country"]
    assert country

    resp = client.get("/agencies", params={"country": country})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) > 0
    assert all(row["country"] == country for row in rows)


def test_agencies_country_filter_no_match(client: httpx.Client):
    resp = client.get("/agencies", params={"country": "Nowhereland-Not-A-Real-Country"})
    assert resp.status_code == 200
    assert resp.json() == []


# --- 6. min_quality filter --------------------------------------------------


def test_agencies_min_quality_filter(client: httpx.Client):
    resp = client.get("/agencies", params={"min_quality": 90})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) > 0
    assert all(row["quality_score"] is not None and row["quality_score"] >= 90 for row in rows)


# --- 7. /agencies/{id} --------------------------------------------------


def test_agency_by_id_found(client: httpx.Client, default_agencies: list[dict[str, Any]]):
    real_row = default_agencies[0]
    resp = client.get(f"/agencies/{real_row['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == real_row["id"]
    assert body["mobility_agency_id"] == real_row["mobility_agency_id"]
    assert body["name"] == real_row["name"]
    assert body["country"] == real_row["country"]


def test_agency_by_id_not_found(client: httpx.Client):
    resp = client.get("/agencies/999999999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agency not found"


# --- 8. /stats --------------------------------------------------


def test_stats_shape_and_sanity(stats: dict[str, Any]):
    for field in ("total_uncovered", "countries_count", "avg_quality", "top_5_countries"):
        assert field in stats

    assert isinstance(stats["total_uncovered"], int)
    assert stats["total_uncovered"] > 0

    assert isinstance(stats["countries_count"], int)
    assert stats["countries_count"] > 0

    assert isinstance(stats["top_5_countries"], list)
    assert len(stats["top_5_countries"]) <= 5
    for entry in stats["top_5_countries"]:
        assert "country" in entry
        assert "count" in entry
        assert isinstance(entry["count"], int)


def test_stats_matches_agencies_count(stats: dict[str, Any], default_agencies: list[dict[str, Any]]):
    # /stats and /agencies both read the same table with no filters, so their
    # counts should agree.
    assert stats["total_uncovered"] == len(default_agencies)


# --- 9. CORS --------------------------------------------------


def test_cors_allow_origin_header_present(client: httpx.Client):
    resp = client.get("/health", headers={"Origin": "https://example.com"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"
