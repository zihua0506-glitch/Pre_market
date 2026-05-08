from collector.discovery import classify_slug


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
