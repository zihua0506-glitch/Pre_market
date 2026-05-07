"""
core/fetch_weather.py

Fetch cloud cover, wind, precipitation and atmospheric data for ZSPD
from Open-Meteo (free, no API key required).

Two endpoints:
  Archive API  — ERA5 reanalysis, 1-hour rows, covers 1940-present with ~7-day lag
  Forecast API — GFS/ICON blend,  1-hour + 15-min rows, past 7 days + 7 days ahead

Tables written to database/weather_om.db:
  om_hourly  — 1-hour rows  (archive preferred, forecast fills recent gap)
  om_15min   — 15-min rows  (forecast API only)

Incremental by default: reads the last stored timestamp and only fetches newer data.

Run:
  python -m core.fetch_weather                        # incremental update
  python -m core.fetch_weather --backfill 2023-01-01  # full historical load
  python -m core.fetch_weather --forecast-only        # recent + forecast only
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

ROOT       = Path(__file__).resolve().parent.parent
WEATHER_DB = ROOT / "database" / "weather_om.db"

LAT, LON = 31.1443, 121.8083   # ZSPD — Shanghai Pudong Airport
TZ       = "Asia/Shanghai"
CST      = timezone(timedelta(hours=8))

ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

ERA5_LAG_DAYS = 7   # conservative: ERA5 typically available after ~5 days


# ── Variable lists ──────────────────────────────────────────────────────────
# Verified against Open-Meteo ERA5 archive + GFS/ICON forecast models

HOURLY_VARS = [
    # Temperature & moisture
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "apparent_temperature",
    # Precipitation
    "precipitation",
    "rain",
    "snowfall",
    # Cloud
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    # Wind (10 m and 80 m levels)
    "wind_speed_10m",
    "wind_speed_80m",
    "wind_direction_10m",
    "wind_gusts_10m",
    # Atmospheric
    "surface_pressure",
    "weather_code",
    "visibility",
    # Radiation (useful proxy for cloud thickness)
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
]

MIN15_VARS = [
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "precipitation",
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "cloud_cover",
    "surface_pressure",
    "weather_code",
    "visibility",
    "shortwave_radiation",
    "direct_radiation",
]


# ── DB setup ────────────────────────────────────────────────────────────────

_DDL_HOURLY = """
CREATE TABLE IF NOT EXISTS om_hourly (
    ts               INTEGER PRIMARY KEY,   -- UTC epoch (seconds)
    obs_date         TEXT    NOT NULL,       -- YYYY-MM-DD (CST)
    obs_hour         INTEGER NOT NULL,       -- 0-23 (CST)
    dt               TEXT    NOT NULL,       -- ISO datetime string (CST)
    -- Temperature & moisture
    temp_c           REAL,
    apparent_temp_c  REAL,
    humidity_pct     REAL,
    dew_point_c      REAL,
    -- Precipitation
    precip_mm        REAL,
    rain_mm          REAL,
    snowfall_cm      REAL,
    -- Cloud cover
    cloud_pct        REAL,
    cloud_low_pct    REAL,
    cloud_mid_pct    REAL,
    cloud_high_pct   REAL,
    -- Wind
    wind_speed_10m   REAL,
    wind_speed_80m   REAL,
    wind_dir_10m     REAL,
    wind_gust_10m    REAL,
    -- Atmospheric
    pressure_hpa     REAL,
    visibility_m     REAL,
    weather_code     INTEGER,
    -- Radiation
    sw_radiation     REAL,
    direct_rad       REAL,
    diffuse_rad      REAL,
    -- Metadata
    source           TEXT    NOT NULL,       -- 'archive' | 'forecast'
    fetched_at       INTEGER NOT NULL
)
"""

_DDL_15MIN = """
CREATE TABLE IF NOT EXISTS om_15min (
    ts               INTEGER PRIMARY KEY,   -- UTC epoch (seconds)
    obs_date         TEXT    NOT NULL,       -- YYYY-MM-DD (CST)
    obs_hour_frac    REAL    NOT NULL,       -- 0.00 / 0.25 / 0.50 / 0.75 / 1.00 ...
    dt               TEXT    NOT NULL,       -- ISO datetime string (CST)
    -- Temperature & moisture
    temp_c           REAL,
    humidity_pct     REAL,
    dew_point_c      REAL,
    -- Precipitation
    precip_mm        REAL,
    -- Wind
    wind_speed_10m   REAL,
    wind_gust_10m    REAL,
    wind_dir_10m     REAL,
    -- Atmospheric
    cloud_pct        REAL,
    pressure_hpa     REAL,
    visibility_m     REAL,
    weather_code     INTEGER,
    -- Radiation
    sw_radiation     REAL,
    direct_rad       REAL,
    -- Metadata
    source           TEXT    NOT NULL,       -- always 'forecast'
    fetched_at       INTEGER NOT NULL
)
"""

_IDX_HOURLY = [
    "CREATE INDEX IF NOT EXISTS idx_om_hourly_date ON om_hourly(obs_date)",
]
_IDX_15MIN = [
    "CREATE INDEX IF NOT EXISTS idx_om_15min_date ON om_15min(obs_date)",
]


def _open_db() -> sqlite3.Connection:
    WEATHER_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(WEATHER_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_DDL_HOURLY)
    conn.execute(_DDL_15MIN)
    for s in _IDX_HOURLY + _IDX_15MIN:
        conn.execute(s)
    conn.commit()
    return conn


def _last_ts(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT MAX(ts) FROM {table}").fetchone()
    return int(row[0]) if row and row[0] else 0


# ── Time helpers ─────────────────────────────────────────────────────────────

def _iso_to_ts(dt_str: str) -> int:
    """'2023-06-01T14:00' (CST, no suffix) → UTC epoch."""
    return int(datetime.fromisoformat(dt_str).replace(tzinfo=CST).timestamp())


def _iso_to_date(dt_str: str) -> str:
    return dt_str[:10]


def _iso_to_hour(dt_str: str) -> int:
    return int(dt_str[11:13])


def _iso_to_hour_frac(dt_str: str) -> float:
    h = int(dt_str[11:13])
    m = int(dt_str[14:16])
    return round(h + m / 60.0, 4)


def _date_str(d: date) -> str:
    return d.isoformat()


# ── API calls ────────────────────────────────────────────────────────────────

def _get(url: str, params: dict, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            print(f"  [retry {attempt+1}] {e}")
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def _fetch_archive_chunk(start: str, end: str) -> list[dict]:
    """
    Fetch hourly ERA5 data for [start, end] (inclusive date strings).
    Returns list of row dicts ready for om_hourly.
    """
    data = _get(ARCHIVE_URL, {
        "latitude":        LAT,
        "longitude":       LON,
        "start_date":      start,
        "end_date":        end,
        "hourly":          ",".join(HOURLY_VARS),
        "timezone":        TZ,
        "wind_speed_unit": "kmh",
    })
    return _parse_hourly(data["hourly"], source="archive")


def _fetch_forecast(past_days: int = 7, forecast_days: int = 7
                    ) -> tuple[list[dict], list[dict]]:
    """
    Fetch hourly + 15-min forecast data.
    Returns (hourly_rows, min15_rows).
    """
    data = _get(FORECAST_URL, {
        "latitude":        LAT,
        "longitude":       LON,
        "hourly":          ",".join(HOURLY_VARS),
        "minutely_15":     ",".join(MIN15_VARS),
        "past_days":       past_days,
        "forecast_days":   forecast_days,
        "timezone":        TZ,
        "wind_speed_unit": "kmh",
    })
    hourly_rows = _parse_hourly(data["hourly"],     source="forecast")
    min15_rows  = _parse_15min(data["minutely_15"], source="forecast")
    return hourly_rows, min15_rows


# ── Parsers ──────────────────────────────────────────────────────────────────

def _v(block: dict, key: str, idx: int) -> Any:
    """Safe value accessor — returns None if key missing or value is null."""
    vals = block.get(key)
    if vals is None:
        return None
    v = vals[idx]
    return None if v is None else v


def _parse_hourly(block: dict, source: str) -> list[dict]:
    now = int(datetime.now(timezone.utc).timestamp())
    times = block["time"]
    rows = []
    for i, t in enumerate(times):
        rows.append({
            "ts":              _iso_to_ts(t),
            "obs_date":        _iso_to_date(t),
            "obs_hour":        _iso_to_hour(t),
            "dt":              t,
            "temp_c":          _v(block, "temperature_2m",    i),
            "apparent_temp_c": _v(block, "apparent_temperature", i),
            "humidity_pct":    _v(block, "relative_humidity_2m", i),
            "dew_point_c":     _v(block, "dew_point_2m",      i),
            "precip_mm":       _v(block, "precipitation",     i),
            "rain_mm":         _v(block, "rain",              i),
            "snowfall_cm":     _v(block, "snowfall",          i),
            "cloud_pct":       _v(block, "cloud_cover",       i),
            "cloud_low_pct":   _v(block, "cloud_cover_low",   i),
            "cloud_mid_pct":   _v(block, "cloud_cover_mid",   i),
            "cloud_high_pct":  _v(block, "cloud_cover_high",  i),
            "wind_speed_10m":  _v(block, "wind_speed_10m",    i),
            "wind_speed_80m":  _v(block, "wind_speed_80m",    i),
            "wind_dir_10m":    _v(block, "wind_direction_10m",i),
            "wind_gust_10m":   _v(block, "wind_gusts_10m",    i),
            "pressure_hpa":    _v(block, "surface_pressure",  i),
            "visibility_m":    _v(block, "visibility",        i),
            "weather_code":    _v(block, "weather_code",      i),
            "sw_radiation":    _v(block, "shortwave_radiation",i),
            "direct_rad":      _v(block, "direct_radiation",  i),
            "diffuse_rad":     _v(block, "diffuse_radiation", i),
            "source":          source,
            "fetched_at":      now,
        })
    return rows


def _parse_15min(block: dict, source: str) -> list[dict]:
    now = int(datetime.now(timezone.utc).timestamp())
    times = block["time"]
    rows = []
    for i, t in enumerate(times):
        rows.append({
            "ts":            _iso_to_ts(t),
            "obs_date":      _iso_to_date(t),
            "obs_hour_frac": _iso_to_hour_frac(t),
            "dt":            t,
            "temp_c":        _v(block, "temperature_2m",     i),
            "humidity_pct":  _v(block, "relative_humidity_2m",i),
            "dew_point_c":   _v(block, "dew_point_2m",       i),
            "precip_mm":     _v(block, "precipitation",      i),
            "wind_speed_10m":_v(block, "wind_speed_10m",     i),
            "wind_gust_10m": _v(block, "wind_gusts_10m",     i),
            "wind_dir_10m":  _v(block, "wind_direction_10m", i),
            "cloud_pct":     _v(block, "cloud_cover",        i),
            "pressure_hpa":  _v(block, "surface_pressure",   i),
            "visibility_m":  _v(block, "visibility",         i),
            "weather_code":  _v(block, "weather_code",       i),
            "sw_radiation":  _v(block, "shortwave_radiation",i),
            "direct_rad":    _v(block, "direct_radiation",   i),
            "source":        source,
            "fetched_at":    now,
        })
    return rows


# ── DB writers ───────────────────────────────────────────────────────────────

_HOURLY_COLS = [
    "ts", "obs_date", "obs_hour", "dt",
    "temp_c", "apparent_temp_c", "humidity_pct", "dew_point_c",
    "precip_mm", "rain_mm", "snowfall_cm",
    "cloud_pct", "cloud_low_pct", "cloud_mid_pct", "cloud_high_pct",
    "wind_speed_10m", "wind_speed_80m", "wind_dir_10m", "wind_gust_10m",
    "pressure_hpa", "visibility_m", "weather_code",
    "sw_radiation", "direct_rad", "diffuse_rad",
    "source", "fetched_at",
]

_MIN15_COLS = [
    "ts", "obs_date", "obs_hour_frac", "dt",
    "temp_c", "humidity_pct", "dew_point_c",
    "precip_mm",
    "wind_speed_10m", "wind_gust_10m", "wind_dir_10m",
    "cloud_pct", "pressure_hpa", "visibility_m", "weather_code",
    "sw_radiation", "direct_rad",
    "source", "fetched_at",
]


def _upsert(conn: sqlite3.Connection, table: str,
            cols: list[str], rows: list[dict]) -> int:
    if not rows:
        return 0
    ph  = ",".join("?" * len(cols))
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({ph})"
    vals = [[r.get(c) for c in cols] for r in rows]
    conn.executemany(sql, vals)
    conn.commit()
    return len(rows)


# ── Public API ───────────────────────────────────────────────────────────────

def update(forecast_only: bool = False) -> None:
    """
    Incremental update:
      1. ERA5 archive from last stored timestamp to (today - ERA5_LAG_DAYS)
      2. Forecast API for recent + upcoming (hourly + 15-min)
    """
    conn = _open_db()

    archive_cutoff = date.today() - timedelta(days=ERA5_LAG_DAYS)
    last_hourly_ts = _last_ts(conn, "om_hourly")
    last_hourly_dt = (datetime.fromtimestamp(last_hourly_ts, tz=CST).date()
                      if last_hourly_ts else None)

    # ── 1. Archive fill ──────────────────────────────────────────────────
    if not forecast_only and (last_hourly_dt is None or
                              last_hourly_dt < archive_cutoff):
        arch_start = (last_hourly_dt + timedelta(days=1)
                      if last_hourly_dt else date(2023, 1, 1))
        arch_end   = archive_cutoff

        print(f"Archive: {arch_start} → {arch_end}")
        for chunk_start, chunk_end in _date_chunks(arch_start, arch_end, days=180):
            print(f"  chunk {chunk_start} → {chunk_end} …", end="", flush=True)
            rows = _fetch_archive_chunk(_date_str(chunk_start), _date_str(chunk_end))
            n = _upsert(conn, "om_hourly", _HOURLY_COLS, rows)
            print(f" {n} rows")
            time.sleep(0.4)   # be polite to the free API

    # ── 2. Forecast (recent + ahead) ─────────────────────────────────────
    print("Forecast: past 7 days + next 7 days …", end="", flush=True)
    h_rows, m_rows = _fetch_forecast(past_days=7, forecast_days=7)
    nh = _upsert(conn, "om_hourly", _HOURLY_COLS, h_rows)
    nm = _upsert(conn, "om_15min",  _MIN15_COLS,  m_rows)
    print(f" {nh} hourly  {nm} 15-min")

    conn.close()
    print("Done.")


def backfill(start_date: date, end_date: Optional[date] = None) -> None:
    """
    Full historical load from start_date.
    Archive covers up to (today - ERA5_LAG_DAYS); the rest comes from forecast.
    """
    conn       = _open_db()
    arch_end   = date.today() - timedelta(days=ERA5_LAG_DAYS)
    if end_date:
        arch_end = min(arch_end, end_date)

    total = 0
    if start_date <= arch_end:
        print(f"Backfill archive: {start_date} → {arch_end}")
        for chunk_start, chunk_end in _date_chunks(start_date, arch_end, days=180):
            print(f"  chunk {chunk_start} → {chunk_end} …", end="", flush=True)
            rows = _fetch_archive_chunk(_date_str(chunk_start), _date_str(chunk_end))
            n = _upsert(conn, "om_hourly", _HOURLY_COLS, rows)
            total += n
            print(f" {n} rows")
            time.sleep(0.4)

    # Fill recent gap with forecast API
    print("Forecast layer (recent + ahead) …", end="", flush=True)
    h_rows, m_rows = _fetch_forecast(past_days=7, forecast_days=7)
    nh = _upsert(conn, "om_hourly", _HOURLY_COLS, h_rows)
    nm = _upsert(conn, "om_15min",  _MIN15_COLS,  m_rows)
    print(f" {nh} hourly  {nm} 15-min")

    conn.close()
    print(f"Backfill complete: {total + nh} hourly rows, {nm} 15-min rows.")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _date_chunks(start: date, end: date, days: int = 180):
    """Yield (chunk_start, chunk_end) pairs of up to `days` calendar days."""
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=days - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def print_summary() -> None:
    """Print row counts and date ranges for both tables."""
    conn = _open_db()
    for tbl in ("om_hourly", "om_15min"):
        row = conn.execute(
            f"SELECT COUNT(*), MIN(obs_date), MAX(obs_date) FROM {tbl}"
        ).fetchone()
        print(f"  {tbl}: {row[0]:,} rows  {row[1]} → {row[2]}")
    conn.close()


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(description="Fetch Open-Meteo weather data for ZSPD")
    p.add_argument("--backfill",      metavar="DATE",
                   help="Full historical load from YYYY-MM-DD")
    p.add_argument("--forecast-only", action="store_true",
                   help="Skip archive, only fetch recent forecast + 15-min")
    p.add_argument("--summary",       action="store_true",
                   help="Print DB row counts and exit")
    args = p.parse_args(argv[1:])

    if args.summary:
        print_summary()
    elif args.backfill:
        backfill(date.fromisoformat(args.backfill))
    else:
        update(forecast_only=args.forecast_only)


if __name__ == "__main__":
    main(sys.argv)
