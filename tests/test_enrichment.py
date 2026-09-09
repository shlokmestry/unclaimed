# Unit tests for the pure logic in ingestion/enrichment.py:
# normalize_populations(), normalize_linear(), score_feed(), freshness_score(),
# and compute_readiness_status(). score_feed() operates on raw GTFS zip bytes
# built in-memory with zipfile.ZipFile + io.BytesIO — no network or DB access.
# The module imports db.config/db.models/db.session and ingestion.mobility_db
# at top level, but those only read env vars / build a lazy SQLAlchemy engine
# at import time (no actual connection), so importing is safe offline.

import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from ingestion.enrichment import (
    REQUIRED_FILES,
    compute_readiness_status,
    freshness_score,
    normalize_linear,
    normalize_populations,
    score_feed,
)


def make_gtfs_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    return buf.getvalue()


def csv_rows(header: str, n_data_rows: int, row_template: str) -> str:
    lines = [header] + [row_template.format(i=i) for i in range(n_data_rows)]
    return "\n".join(lines) + "\n"


def days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


class TestNormalizePopulations:
    def test_monotonic_ordering(self):
        populations = [1_000_000, 10_000_000, 1_000_000_000]
        mapping = normalize_populations(populations)
        assert mapping[1_000_000] < mapping[10_000_000] < mapping[1_000_000_000]

    def test_min_maps_to_zero_and_max_to_hundred(self):
        populations = [500_000, 5_000_000, 500_000_000]
        mapping = normalize_populations(populations)
        assert mapping[min(populations)] == 0.0
        assert mapping[max(populations)] == 100.0

    def test_none_population_maps_to_zero(self):
        mapping = normalize_populations([None, 1_000_000, 50_000_000])
        assert mapping[None] == 0.0

    def test_zero_population_maps_to_zero(self):
        mapping = normalize_populations([0, 1_000_000, 50_000_000])
        assert mapping[0] == 0.0

    def test_negative_population_maps_to_zero(self):
        mapping = normalize_populations([-5, 1_000_000])
        assert mapping[-5] == 0.0

    def test_all_equal_values_map_to_hundred(self):
        mapping = normalize_populations([5_000_000, 5_000_000, 5_000_000])
        assert mapping[5_000_000] == 100.0

    def test_all_none_maps_everything_to_zero(self):
        mapping = normalize_populations([None, None])
        assert mapping == {None: 0.0}

    def test_empty_list_returns_empty_mapping(self):
        assert normalize_populations([]) == {}

    def test_single_valid_value_with_none_mixed_in(self):
        mapping = normalize_populations([None, 42_000_000])
        assert mapping[None] == 0.0
        assert mapping[42_000_000] == 100.0


class TestNormalizeLinear:
    def test_monotonic_ordering(self):
        mapping = normalize_linear([2, 20, 200])
        assert mapping[2] < mapping[20] < mapping[200]

    def test_min_maps_to_zero_and_max_to_hundred(self):
        mapping = normalize_linear([1, 50, 500])
        assert mapping[1] == 0.0
        assert mapping[500] == 100.0

    def test_none_maps_to_zero(self):
        mapping = normalize_linear([None, 5, 50])
        assert mapping[None] == 0.0

    def test_all_equal_values_map_to_hundred(self):
        mapping = normalize_linear([10, 10, 10])
        assert mapping[10] == 100.0

    def test_empty_list_returns_empty_mapping(self):
        assert normalize_linear([]) == {}

    def test_linear_not_log_scaled(self):
        # A true midpoint should map to ~50 under linear scaling — this is
        # the behavioral difference from normalize_populations()'s log scale.
        mapping = normalize_linear([0, 50, 100])
        assert mapping[50] == pytest.approx(50.0)


class TestFreshnessScore:
    def test_none_scores_zero(self):
        assert freshness_score(None) == 0.0

    def test_within_30_days_scores_100(self):
        assert freshness_score(days_ago(10)) == 100.0

    def test_within_90_days_scores_75(self):
        assert freshness_score(days_ago(60)) == 75.0

    def test_within_180_days_scores_50(self):
        assert freshness_score(days_ago(150)) == 50.0

    def test_within_365_days_scores_25(self):
        assert freshness_score(days_ago(300)) == 25.0

    def test_older_than_a_year_scores_zero(self):
        assert freshness_score(days_ago(400)) == 0.0

    def test_naive_datetime_is_handled(self):
        # feed_last_updated may come back as a naive datetime from the DB
        # (no tzinfo) — freshness_score() must not raise comparing it against
        # an aware "now".
        naive = datetime.utcnow() - timedelta(days=5)
        assert freshness_score(naive) == 100.0


class TestComputeReadinessStatus:
    def test_unreachable_feed_is_dead(self):
        status = compute_readiness_status(
            quality_score=100, feed_last_updated=days_ago(1), route_count=50, stop_count=100, reachable=False
        )
        assert status == "Dead Feed"

    def test_unknown_last_updated_is_dead(self):
        status = compute_readiness_status(
            quality_score=100, feed_last_updated=None, route_count=50, stop_count=100, reachable=True
        )
        assert status == "Dead Feed"

    def test_stale_over_two_years_is_dead(self):
        status = compute_readiness_status(
            quality_score=100, feed_last_updated=days_ago(800), route_count=50, stop_count=100, reachable=True
        )
        assert status == "Dead Feed"

    def test_meets_all_criteria_is_ready(self):
        status = compute_readiness_status(
            quality_score=85, feed_last_updated=days_ago(10), route_count=10, stop_count=30, reachable=True
        )
        assert status == "Ready"

    def test_low_quality_is_needs_work_not_ready(self):
        status = compute_readiness_status(
            quality_score=50, feed_last_updated=days_ago(10), route_count=10, stop_count=30, reachable=True
        )
        assert status == "Needs Work"

    def test_too_few_routes_is_needs_work(self):
        status = compute_readiness_status(
            quality_score=85, feed_last_updated=days_ago(10), route_count=2, stop_count=30, reachable=True
        )
        assert status == "Needs Work"

    def test_too_few_stops_is_needs_work(self):
        status = compute_readiness_status(
            quality_score=85, feed_last_updated=days_ago(10), route_count=10, stop_count=5, reachable=True
        )
        assert status == "Needs Work"

    def test_stale_beyond_ready_window_but_within_dead_window_is_needs_work(self):
        # >180 days (not Ready) but <=730 days (not Dead) -> Needs Work.
        status = compute_readiness_status(
            quality_score=85, feed_last_updated=days_ago(300), route_count=10, stop_count=30, reachable=True
        )
        assert status == "Needs Work"

    def test_boundary_quality_exactly_70_is_not_ready(self):
        # Spec says "quality_score above 70" — exactly 70 must not qualify.
        status = compute_readiness_status(
            quality_score=70, feed_last_updated=days_ago(10), route_count=10, stop_count=30, reachable=True
        )
        assert status == "Needs Work"


class TestScoreFeed:
    def test_all_files_present_with_row_bonuses_scores_max(self):
        stops = csv_rows("stop_id,stop_name", 12, "s{i},Stop {i}")
        routes = csv_rows("route_id,route_name", 4, "r{i},Route {i}")
        trips = csv_rows("trip_id,route_id", 7, "t{i},r0")
        stop_times = csv_rows("trip_id,stop_id,stop_sequence", 1, "t0,s0,{i}")
        calendar = csv_rows(
            "service_id,monday,start_date,end_date", 1, "svc{i},1,20200101,20991231"
        )

        feed_bytes = make_gtfs_zip(
            {
                "stops.txt": stops,
                "routes.txt": routes,
                "trips.txt": trips,
                "stop_times.txt": stop_times,
                "calendar.txt": calendar,
            }
        )

        result = score_feed(feed_bytes)

        assert result["missing_files"] == []
        # 5 files * 15 (=75) + stops>10 (+10) + routes>3 (+10) + future calendar (+5)
        assert result["quality_score"] == 100.0
        assert result["stop_count"] == 12
        assert result["route_count"] == 4
        assert result["trip_count"] == 7

    def test_missing_files_lowers_score_and_lists_missing(self):
        stops = csv_rows("stop_id,stop_name", 3, "s{i},Stop {i}")
        routes = csv_rows("route_id,route_name", 2, "r{i},Route {i}")

        feed_bytes = make_gtfs_zip({"stops.txt": stops, "routes.txt": routes})

        result = score_feed(feed_bytes)

        assert result["missing_files"] == ["trips.txt", "stop_times.txt", "calendar.txt"]
        # Only file-presence points for stops.txt + routes.txt, no bonuses
        # (3 rows is not > 10, 2 rows is not > 3).
        assert result["quality_score"] == 30.0
        assert result["stop_count"] == 3
        assert result["route_count"] == 2
        assert result["trip_count"] is None

    def test_no_files_present_scores_zero_with_all_missing(self):
        feed_bytes = make_gtfs_zip({"agency.txt": "agency_id,agency_name\n1,Test\n"})

        result = score_feed(feed_bytes)

        assert result["quality_score"] == 0.0
        assert result["missing_files"] == list(REQUIRED_FILES)
        assert result["route_count"] is None
        assert result["stop_count"] is None
        assert result["trip_count"] is None

    def test_corrupt_bytes_scores_zero(self):
        result = score_feed(b"this is definitely not a zip file")

        assert result["quality_score"] == 0.0
        assert result["missing_files"] == list(REQUIRED_FILES)

    def test_empty_bytes_scores_zero(self):
        result = score_feed(b"")

        assert result["quality_score"] == 0.0
        assert result["missing_files"] == list(REQUIRED_FILES)

    def test_calendar_without_future_dates_gets_no_bonus(self):
        stops = csv_rows("stop_id,stop_name", 12, "s{i},Stop {i}")
        routes = csv_rows("route_id,route_name", 4, "r{i},Route {i}")
        trips = csv_rows("trip_id,route_id", 1, "t{i},r0")
        stop_times = csv_rows("trip_id,stop_id,stop_sequence", 1, "t0,s0,{i}")
        # end_date in the past -> no future-dates bonus.
        calendar = csv_rows(
            "service_id,monday,start_date,end_date", 1, "svc{i},1,20000101,20000601"
        )

        feed_bytes = make_gtfs_zip(
            {
                "stops.txt": stops,
                "routes.txt": routes,
                "trips.txt": trips,
                "stop_times.txt": stop_times,
                "calendar.txt": calendar,
            }
        )

        result = score_feed(feed_bytes)

        assert result["missing_files"] == []
        # 75 (presence) + 10 (stops) + 10 (routes) + 0 (no future calendar bonus)
        assert result["quality_score"] == 95.0

    def test_file_present_in_subdirectory_is_found(self):
        # _find_member() matches "…/stops.txt" too, not just an exact name.
        stops = csv_rows("stop_id,stop_name", 1, "s{i},Stop {i}")
        feed_bytes = make_gtfs_zip({"gtfs/stops.txt": stops})

        result = score_feed(feed_bytes)

        assert "stops.txt" not in result["missing_files"]
        assert result["quality_score"] >= 15.0
        assert result["stop_count"] == 1
