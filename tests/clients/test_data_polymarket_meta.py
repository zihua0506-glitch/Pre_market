"""Pure-logic tests for clients.data.polymarket_meta — no network.

Covers the question/title parser and observation-date extractor which are the
non-trivial bits of the gamma-api fetcher. The HTTP layer + sqlite I/O are
left for integration tests.
"""

import pytest

from clients.data.polymarket_meta import (
    parse_question, parse_observation_date, extract_tokens, is_daily_temp,
)


class TestParseQuestion:
    # The regex requires 'C' after the threshold (with or without °).
    # See clients/data/polymarket_meta.py:QUESTION_RE.

    def test_highest_or_below(self):
        q = "Will the highest temperature in Shanghai be 25°C or below on April 28?"
        out = parse_question(q)
        assert out == {"kind": "highest", "threshold_c": 25, "bound": "le"}

    def test_lowest_or_higher(self):
        q = "Will the lowest temperature in Shanghai be 10°C or higher on March 15?"
        out = parse_question(q)
        assert out == {"kind": "lowest", "threshold_c": 10, "bound": "ge"}

    def test_eq_no_bound_phrase(self):
        q = "Will the highest temperature in Shanghai be 28°C on July 1?"
        out = parse_question(q)
        assert out == {"kind": "highest", "threshold_c": 28, "bound": "eq"}

    def test_negative_threshold(self):
        q = "Will the lowest temperature in Shanghai be -5°C or below on January 12?"
        out = parse_question(q)
        assert out == {"kind": "lowest", "threshold_c": -5, "bound": "le"}

    def test_handles_space_before_C(self):
        q = "Will the highest temperature in Shanghai be 30 °C on May 1?"
        out = parse_question(q)
        assert out == {"kind": "highest", "threshold_c": 30, "bound": "eq"}

    def test_no_match_returns_none(self):
        assert parse_question("totally unrelated question") is None
        assert parse_question("") is None
        assert parse_question(None) is None

    def test_missing_C_unit_returns_none(self):
        # Regex requires 'C' — without it we don't match
        assert parse_question(
            "Will the highest temperature in Shanghai be 25 on April 28?"
        ) is None


class TestParseObservationDate:
    def test_basic_extraction(self):
        title = "Highest temperature in Shanghai on March 15?"
        end_date = "2026-03-16T00:00:00Z"
        assert parse_observation_date(title, end_date) == "2026-03-15"

    def test_year_inferred_from_enddate(self):
        # When endDate is in 2025, the inferred year should be 2025
        title = "... on October 7?"
        end_date = "2025-10-08T00:00:00Z"
        assert parse_observation_date(title, end_date) == "2025-10-07"

    def test_january_endate_december_title(self):
        # Edge: title says December but endDate fell into next-year January
        title = "... on December 30?"
        end_date = "2026-01-01T00:00:00Z"
        assert parse_observation_date(title, end_date) == "2025-12-30"

    def test_december_endate_january_title(self):
        # Mirror: title says January but endDate fell into prior-year December
        title = "... on January 1?"
        end_date = "2025-12-31T00:00:00Z"
        assert parse_observation_date(title, end_date) == "2026-01-01"

    def test_no_date_in_title(self):
        assert parse_observation_date("totally unrelated", "2026-01-01T00:00:00Z") is None
        assert parse_observation_date("", "2026-01-01T00:00:00Z") is None

    def test_invalid_month(self):
        # The regex would match but the month name is bogus
        assert parse_observation_date("on Foobary 1?", "2026-01-01T00:00:00Z") is None

    def test_invalid_day(self):
        # Feb 30 — date() will reject; function should return None
        assert parse_observation_date("on February 30?", "2026-02-28T00:00:00Z") is None


class TestExtractTokens:
    def test_yes_then_no(self):
        market = {"clobTokenIds": '["yes-token-id-here", "no-token-id-here"]'}
        yes, no = extract_tokens(market)
        assert yes == "yes-token-id-here"
        assert no == "no-token-id-here"

    def test_dict_input(self):
        market = {"clobTokenIds": ["yes123", "no456"]}
        yes, no = extract_tokens(market)
        assert yes == "yes123"
        assert no == "no456"

    def test_missing_field_returns_none_pair(self):
        assert extract_tokens({}) == (None, None)

    def test_malformed_json(self):
        assert extract_tokens({"clobTokenIds": "not json"}) == (None, None)

    def test_only_one_token(self):
        market = {"clobTokenIds": '["only"]'}
        yes, no = extract_tokens(market)
        assert yes == "only"
        assert no is None


class TestIsDailyTemp:
    def test_match(self):
        ev = {"tags": [{"slug": "shanghai"}, {"slug": "daily-temperature"}]}
        assert is_daily_temp(ev) is True

    def test_no_match(self):
        ev = {"tags": [{"slug": "shanghai"}, {"slug": "weather"}]}
        assert is_daily_temp(ev) is False

    def test_no_tags(self):
        assert is_daily_temp({"tags": []}) is False
        assert is_daily_temp({}) is False
