"""Fetch half-hourly observation history for Shanghai/Pudong (ZSPD) from
Weather Underground's underlying weather.com API and store it in
database/standard.db.

Usage:
    python -m core.fetch_zspd_history             # 2026-03-13 -> today
    python -m core.fetch_zspd_history 20260313 20260428
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

STATION = "ZSPD"
LOCATION_ID = f"{STATION}:9:CN"
API_URL = "https://api.weather.com/v1/location/{loc}/observations/historical.json"
API_KEY = "6532d6454b8aa370768e63d6ba5a832e"  # public key used by wunderground.com frontend

SHANGHAI_TZ = timezone(timedelta(hours=8))
DB_PATH = Path(__file__).resolve().parent.parent.parent / "database" / "standard.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.wunderground.com",
    "Referer": "https://www.wunderground.com/",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS zspd_observations (
    valid_time_gmt   INTEGER PRIMARY KEY,   -- unix epoch seconds (UTC)
    ts_utc           TEXT NOT NULL,         -- ISO 8601 UTC
    ts_local         TEXT NOT NULL,         -- ISO 8601 Asia/Shanghai (+08:00)
    obs_date_local   TEXT NOT NULL,         -- YYYY-MM-DD in Shanghai local time
    station          TEXT NOT NULL,
    temp_c           REAL,                  -- temperature, Celsius
    feels_like_c     REAL,
    dew_point_c      REAL,
    humidity_pct     REAL,
    pressure_hpa     REAL,
    wind_speed_kmh   REAL,
    wind_dir_deg     REAL,
    wind_dir_card    TEXT,
    wind_gust_kmh    REAL,
    visibility_km    REAL,
    precip_hourly_mm REAL,
    wx_phrase        TEXT,
    day_ind          TEXT
);
CREATE INDEX IF NOT EXISTS idx_zspd_obs_date ON zspd_observations(obs_date_local);
"""


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def fetch_day(d: date, session: requests.Session) -> list[dict]:
    params = {
        "apiKey": API_KEY,
        "units": "m",
        "startDate": d.strftime("%Y%m%d"),
    }
    url = API_URL.format(loc=LOCATION_ID)
    resp = session.get(url, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("observations") or []


def to_row(o: dict) -> tuple:
    ts = int(o["valid_time_gmt"])
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    dt_local = dt_utc.astimezone(SHANGHAI_TZ)
    return (
        ts,
        dt_utc.isoformat(),
        dt_local.isoformat(),
        dt_local.date().isoformat(),
        o.get("obs_id") or STATION,
        o.get("temp"),
        o.get("feels_like"),
        o.get("dewPt"),
        o.get("rh"),
        o.get("pressure"),
        o.get("wspd"),
        o.get("wdir"),
        o.get("wdir_cardinal"),
        o.get("gust"),
        o.get("vis"),
        o.get("precip_hrly"),
        o.get("wx_phrase"),
        o.get("day_ind"),
    )


INSERT_SQL = """
INSERT OR REPLACE INTO zspd_observations (
    valid_time_gmt, ts_utc, ts_local, obs_date_local, station,
    temp_c, feels_like_c, dew_point_c, humidity_pct, pressure_hpa,
    wind_speed_kmh, wind_dir_deg, wind_dir_card, wind_gust_kmh,
    visibility_km, precip_hourly_mm, wx_phrase, day_ind
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def daterange(start: date, end_inclusive: date):
    cur = start
    while cur <= end_inclusive:
        yield cur
        cur += timedelta(days=1)


def parse_args(argv: list[str]) -> tuple[date, date]:
    if len(argv) >= 3:
        start = datetime.strptime(argv[1], "%Y%m%d").date()
        end = datetime.strptime(argv[2], "%Y%m%d").date()
    else:
        start = date(2026, 3, 13)
        end = datetime.now(SHANGHAI_TZ).date()
    return start, end


def main(argv: list[str]) -> int:
    start, end = parse_args(argv)
    print(f"Fetching ZSPD observations {start} -> {end} into {DB_PATH}")
    conn = init_db(DB_PATH)
    session = requests.Session()
    total_rows = 0
    failed: list[date] = []

    for d in daterange(start, end):
        try:
            obs = fetch_day(d, session)
        except Exception as e:
            print(f"  {d}: ERROR {e}")
            failed.append(d)
            continue
        rows = [to_row(o) for o in obs]
        if rows:
            conn.executemany(INSERT_SQL, rows)
            conn.commit()
        total_rows += len(rows)
        print(f"  {d}: {len(rows):3d} obs")
        time.sleep(0.4)  # be polite

    cur = conn.execute("SELECT COUNT(*), MIN(ts_local), MAX(ts_local) FROM zspd_observations")
    n, tmin, tmax = cur.fetchone()
    print(f"\nDone. inserted/refreshed {total_rows} rows this run.")
    print(f"Table now has {n} rows; range: {tmin} .. {tmax}")
    if failed:
        print(f"Failed days ({len(failed)}): {failed}")
    conn.close()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
