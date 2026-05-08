"""Pure-logic tests for collector.discovery — no network."""
from collector.discovery import (
    classify_event_market,
    classify_slug,
    city_code_for,
)


def _ev(tag_slugs: list[str]) -> dict:
    return {"tags": [{"slug": s} for s in tag_slugs]}


# --- classify_event_market (real gamma shape) ---


class TestClassifyEventMarket:
    def test_shanghai_high_eq(self):
        ev = _ev(["weather", "daily-temperature", "shanghai", "highest-temperature"])
        mk = {"slug": "highest-temperature-in-shanghai-on-may-8-2026-25c"}
        out = classify_event_market(ev, mk)
        assert out == {"city": "ZSPD", "contract_type": "high_eq", "threshold_c": 25.0}

    def test_london_low_le(self):
        ev = _ev(["weather", "daily-temperature", "london", "lowest-temperature"])
        mk = {"slug": "lowest-temperature-in-london-on-may-8-2026-4corbelow"}
        out = classify_event_market(ev, mk)
        assert out == {"city": "LON", "contract_type": "low_le", "threshold_c": 4.0}

    def test_nyc_high_ge(self):
        ev = _ev(["weather", "daily-temperature", "new-york-city", "highest-temperature"])
        mk = {"slug": "highest-temperature-in-new-york-city-on-may-8-2026-22corabove"}
        out = classify_event_market(ev, mk)
        assert out == {"city": "NYC", "contract_type": "high_ge", "threshold_c": 22.0}

    def test_negative_threshold(self):
        ev = _ev(["weather", "daily-temperature", "moscow", "lowest-temperature"])
        mk = {"slug": "lowest-temperature-in-moscow-on-jan-3-2026--5corbelow"}
        out = classify_event_market(ev, mk)
        assert out == {"city": "MOW", "contract_type": "low_le", "threshold_c": -5.0}

    def test_unknown_city_falls_back_to_uppercased_slug(self):
        ev = _ev(["weather", "daily-temperature", "atlantis", "highest-temperature"])
        mk = {"slug": "highest-temperature-in-atlantis-on-may-8-2026-30c"}
        out = classify_event_market(ev, mk)
        assert out == {"city": "ATLANTIS", "contract_type": "high_eq", "threshold_c": 30.0}

    def test_missing_daily_temperature_tag_returns_none(self):
        ev = _ev(["weather", "shanghai", "highest-temperature"])
        mk = {"slug": "highest-temperature-in-shanghai-on-may-8-2026-25c"}
        assert classify_event_market(ev, mk) is None

    def test_missing_kind_tag_returns_none(self):
        ev = _ev(["weather", "daily-temperature", "shanghai"])
        mk = {"slug": "anything-25c"}
        assert classify_event_market(ev, mk) is None

    def test_unparseable_threshold_returns_none(self):
        ev = _ev(["weather", "daily-temperature", "shanghai", "highest-temperature"])
        mk = {"slug": "highest-temperature-in-shanghai-no-threshold"}
        assert classify_event_market(ev, mk) is None


# --- city_code_for ---


def test_city_code_known_slug():
    assert city_code_for(["weather", "shanghai", "daily-temperature"]) == "ZSPD"


def test_city_code_unknown_slug_falls_back():
    assert city_code_for(["weather", "daily-temperature", "antarctica"]) == "ANTARCTICA"


def test_city_code_only_controlled_returns_none():
    assert city_code_for(["weather", "daily-temperature", "highest-temperature"]) is None


# --- classify_slug (legacy, synthetic airport-prefixed slugs) ---


def test_classify_zspd_high_ge():
    out = classify_slug("zspd-shanghai-high-temperature-may-7-2026-25c-or-higher")
    assert out["city"] == "ZSPD"
    assert out["contract_type"] == "high_ge"
    assert out["threshold_c"] == 25.0


def test_classify_jfk_low_le():
    out = classify_slug("kjfk-new-york-low-temperature-may-7-2026-15c-or-lower")
    assert out["city"] == "KJFK"
    assert out["contract_type"] == "low_le"
    assert out["threshold_c"] == 15.0


def test_classify_eq_threshold():
    out = classify_slug("klga-laguardia-high-may-7-2026-eq-22c")
    assert out["city"] == "KLGA"
    assert out["contract_type"] == "high_eq"
    assert out["threshold_c"] == 22.0


def test_classify_non_weather_returns_none():
    assert classify_slug("will-trump-win-the-2028-election") is None
