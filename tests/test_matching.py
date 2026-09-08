# Unit tests for the pure logic in ingestion/matching.py: normalize() and
# best_match(). These do not touch the DB — matching.py imports db.session /
# db.config at module level, but db.config only reads env vars (with
# defaults) and db.session's create_engine() doesn't actually connect until
# a query is run, so importing the module is safe offline.

from ingestion.matching import STOPWORDS, best_match, normalize


class TestNormalize:
    def test_lowercases(self):
        assert normalize("CHICAGO") == "chicago"

    def test_strips_punctuation(self):
        assert normalize("St. Louis, Missouri!") == "st louis missouri"

    def test_removes_stopwords(self):
        assert normalize("Chicago Transit Authority") == "chicago"

    def test_removes_multiple_stopwords(self):
        assert normalize("Metropolitan Transportation Authority") == ""

    def test_new_york_city_drops_city_stopword(self):
        assert normalize("New York City") == "new york"

    def test_collapses_whitespace(self):
        assert normalize("  New   York  ") == "new york"

    def test_none_returns_empty_string(self):
        assert normalize(None) == ""

    def test_empty_string_returns_empty_string(self):
        assert normalize("") == ""

    def test_only_stopwords_and_punct_returns_empty(self):
        assert normalize("Regional District Authority!!") == ""

    def test_stopwords_set_contents(self):
        expected = {
            "city",
            "metro",
            "metropolitan",
            "transit",
            "transportation",
            "authority",
            "district",
            "municipality",
            "municipal",
            "county",
            "region",
            "regional",
            "township",
            "area",
        }
        assert STOPWORDS == expected

    def test_stopword_substrings_not_removed(self):
        # "cityscape" contains "city" but is a distinct token — must survive.
        assert normalize("Cityscape") == "cityscape"


class TestBestMatch:
    def test_empty_candidates_returns_none_and_zero(self):
        assert best_match("chicago", []) == (None, 0.0)

    def test_empty_normalized_municipality_returns_none_and_zero(self):
        candidates = [("chicago", "Chicago")]
        assert best_match("", candidates) == (None, 0.0)

    def test_exact_match_scores_100(self):
        candidates = [("chicago", "Chicago")]
        name, score = best_match("chicago", candidates)
        assert name == "Chicago"
        assert score == 100.0

    def test_picks_best_among_multiple_candidates(self):
        candidates = [
            ("los angeles", "Los Angeles"),
            ("chicago", "Chicago"),
            ("new york", "New York"),
        ]
        name, score = best_match("chicago", candidates)
        assert name == "Chicago"
        assert score == 100.0

    def test_partial_match_scores_lower_than_exact(self):
        candidates = [("chicago heights", "Chicago Heights")]
        name, score = best_match("chicago", candidates)
        assert name == "Chicago Heights"
        assert 0.0 < score < 100.0

    def test_no_similarity_scores_low(self):
        candidates = [("tokyo", "Tokyo")]
        name, score = best_match("chicago", candidates)
        # WRatio between wholly dissimilar short strings should stay well
        # under a typical match-confidence threshold.
        assert score < 60.0
