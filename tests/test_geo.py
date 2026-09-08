# Unit tests for ingestion/geo.py's country_name_to_code(). This module has
# no DB imports at all (just pycountry + logging), so it's safe to import
# directly with no environment setup.

from ingestion.geo import country_name_to_code


class TestCountryNameToCode:
    def test_override_united_states(self):
        assert country_name_to_code("united states") == "US"

    def test_override_is_case_insensitive_and_strips_whitespace(self):
        assert country_name_to_code("  United States  ") == "US"

    def test_override_usa_abbreviation(self):
        assert country_name_to_code("USA") == "US"

    def test_override_uk(self):
        assert country_name_to_code("UK") == "GB"

    def test_override_south_korea(self):
        assert country_name_to_code("South Korea") == "KR"

    def test_override_czechia(self):
        assert country_name_to_code("Czechia") == "CZ"

    def test_override_ivory_coast(self):
        assert country_name_to_code("Ivory Coast") == "CI"

    def test_pycountry_lookup_france(self):
        assert country_name_to_code("France") == "FR"

    def test_pycountry_lookup_germany(self):
        assert country_name_to_code("Germany") == "DE"

    def test_pycountry_lookup_japan(self):
        assert country_name_to_code("Japan") == "JP"

    def test_unresolvable_name_returns_none(self):
        assert country_name_to_code("Definitely Not A Real Country") is None

    def test_none_input_returns_none(self):
        assert country_name_to_code(None) is None

    def test_empty_string_returns_none(self):
        assert country_name_to_code("") is None

    def test_result_is_cached(self):
        # Call twice; second call should hit the cache and return the same
        # value (behavior, not implementation, is what we can assert here).
        first = country_name_to_code("Brazil")
        second = country_name_to_code("Brazil")
        assert first == second == "BR"
