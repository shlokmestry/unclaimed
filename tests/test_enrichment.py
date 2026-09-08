# Unit tests for the pure logic in ingestion/enrichment.py:
# normalize_populations() and score_feed(). score_feed() operates on raw GTFS
# zip bytes built in-memory with zipfile.ZipFile + io.BytesIO — no network or
# DB access. The module imports db.config/db.models/db.session at top level,
# but those only read env vars / build a lazy SQLAlchemy engine at import
# time (no actual connection), so importing is safe offline.

import io
import zipfile

import pytest

from ingestion.enrichment import REQUIRED_FILES, normalize_populations, score_feed


def make_gtfs_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    return buf.getvalue()


def csv_rows(header: str, n_data_rows: int, row_template: str) -> str:
    lines = [header] + [row_template.format(i=i) for i in range(n_data_rows)]
    return "\n".join(lines) + "\n"


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


class TestScoreFeed:
    def test_all_files_present_with_row_bonuses_scores_max(self):
        stops = csv_rows("stop_id,stop_name", 12, "s{i},Stop {i}")
        routes = csv_rows("route_id,route_name", 4, "r{i},Route {i}")
        trips = csv_rows("trip_id,route_id", 1, "t{i},r0")
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

        score, missing = score_feed(feed_bytes)

        assert missing == []
        # 5 files * 15 (=75) + stops>10 (+10) + routes>3 (+10) + future calendar (+5)
        assert score == 100.0

    def test_missing_files_lowers_score_and_lists_missing(self):
        stops = csv_rows("stop_id,stop_name", 3, "s{i},Stop {i}")
        routes = csv_rows("route_id,route_name", 2, "r{i},Route {i}")

        feed_bytes = make_gtfs_zip({"stops.txt": stops, "routes.txt": routes})

        score, missing = score_feed(feed_bytes)

        assert missing == ["trips.txt", "stop_times.txt", "calendar.txt"]
        # Only file-presence points for stops.txt + routes.txt, no bonuses
        # (3 rows is not > 10, 2 rows is not > 3).
        assert score == 30.0

    def test_no_files_present_scores_zero_with_all_missing(self):
        feed_bytes = make_gtfs_zip({"agency.txt": "agency_id,agency_name\n1,Test\n"})

        score, missing = score_feed(feed_bytes)

        assert score == 0.0
        assert missing == list(REQUIRED_FILES)

    def test_corrupt_bytes_scores_zero(self):
        score, missing = score_feed(b"this is definitely not a zip file")

        assert score == 0.0
        assert missing == list(REQUIRED_FILES)

    def test_empty_bytes_scores_zero(self):
        score, missing = score_feed(b"")

        assert score == 0.0
        assert missing == list(REQUIRED_FILES)

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

        score, missing = score_feed(feed_bytes)

        assert missing == []
        # 75 (presence) + 10 (stops) + 10 (routes) + 0 (no future calendar bonus)
        assert score == 95.0

    def test_file_present_in_subdirectory_is_found(self):
        # _find_member() matches "…/stops.txt" too, not just an exact name.
        stops = csv_rows("stop_id,stop_name", 1, "s{i},Stop {i}")
        feed_bytes = make_gtfs_zip({"gtfs/stops.txt": stops})

        score, missing = score_feed(feed_bytes)

        assert "stops.txt" not in missing
        assert score >= 15.0
