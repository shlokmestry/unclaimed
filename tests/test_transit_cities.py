# Unit tests for the pure helpers in ingestion/transit_cities.py:
# _decode_flag() and _clean_name(). The module imports httpx/bs4/db.config at
# top level, but none of those perform network/DB I/O at import time, so a
# plain import is safe offline.

from ingestion.transit_cities import _clean_name, _decode_flag

US_FLAG = "\U0001F1FA\U0001F1F8"  # 🇺🇸
CANADA_FLAG = "\U0001F1E8\U0001F1E6"  # 🇨🇦
FRANCE_FLAG = "\U0001F1EB\U0001F1F7"  # 🇫🇷


class TestDecodeFlag:
    def test_decodes_us_flag(self):
        assert _decode_flag(f"United States{US_FLAG}") == "US"

    def test_decodes_canada_flag(self):
        assert _decode_flag(f"Canada{CANADA_FLAG}") == "CA"

    def test_decodes_france_flag(self):
        assert _decode_flag(f"France{FRANCE_FLAG}") == "FR"

    def test_flag_only_no_text(self):
        assert _decode_flag(US_FLAG) == "US"

    def test_no_flag_returns_none(self):
        assert _decode_flag("Alabama\U0001F98B") is None  # butterfly emoji, no flag

    def test_plain_text_returns_none(self):
        assert _decode_flag("Just plain text") is None

    def test_empty_string_returns_none(self):
        assert _decode_flag("") is None


class TestCleanName:
    def test_strips_flag_emoji(self):
        assert _clean_name(f"United States{US_FLAG}") == "United States"

    def test_strips_theme_emoji_preserves_accents(self):
        # "Québec" preceded/followed by an emoji (per LOG.md's example of a
        # region heading like "Alabama🦋") must keep its accented character.
        assert _clean_name("Québec\U0001F98B") == "Québec"

    def test_strips_multiple_emoji_and_whitespace(self):
        assert _clean_name("  Paris \U0001F1EB\U0001F1F7 \U0001FA82  ") == "Paris"

    def test_no_emoji_leaves_name_unchanged(self):
        assert _clean_name("Montréal") == "Montréal"

    def test_empty_string(self):
        assert _clean_name("") == ""
