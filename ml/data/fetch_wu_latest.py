"""
Fetch the latest WU ZSPD half-hourly observation and insert into standard.db.

Data source: reuses core/fetch_zspd_history.py (same API key + schema).
Manual fallback: tkinter dialog when API is unavailable or returns stale data.

Flow:
  1. Call core.fetch_zspd_history.fetch_day(today) → insert all of today's obs.
  2. Read the target 30-min slot from standard.db.
  3. If slot is missing or > 90 min old → open manual input dialog.
  4. Manual input: returns raw (un-rounded) values which are then inserted.

Manual input fields (all raw, un-rounded):
  temp_c        required   e.g.  27.8    degC  (-10 ~ 45)
  dew_c         optional   e.g.  22.1    degC
  wind_kmh      optional   e.g.  15.2    km/h
  wind_dir_deg  optional   e.g.  180     deg
  pressure_hpa  optional   e.g.  1012.5  hPa
  visibility_km optional   e.g.  9.9     km

Run:
    python -m ml.data.fetch_wu_latest            # auto (API then dialog fallback)
    python -m ml.data.fetch_wu_latest --manual   # force manual dialog
    python -m ml.data.fetch_wu_latest --show     # print latest slot, no insert
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from ml.config import WU_ZSPD_DB

SHANGHAI_OFFSET = timedelta(hours=8)

_MANUAL_INSERT = """
INSERT OR REPLACE INTO zspd_observations (
    valid_time_gmt, ts_utc, ts_local, obs_date_local, station,
    temp_c, dew_point_c, wind_speed_kmh, wind_dir_deg,
    pressure_hpa, visibility_km
) VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""


# ── Time helpers ───────────────────────────────────────────────────────────

def _nearest_past_30min(dt_utc: datetime) -> datetime:
    epoch   = int(dt_utc.timestamp())
    floored = (epoch // 1800) * 1800
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _slot_strings(slot_utc: datetime) -> tuple[str, str, str]:
    local    = slot_utc + SHANGHAI_OFFSET
    ts_utc   = slot_utc.strftime("%Y-%m-%dT%H:%M:00Z")
    ts_local = local.strftime("%Y-%m-%dT%H:%M:00+08:00")
    obs_date = local.strftime("%Y-%m-%d")
    return ts_utc, ts_local, obs_date


# ── WU fetch via core.fetch_zspd_history ───────────────────────────────────

def refresh_today_wu() -> int:
    """
    Fetch ALL of today's WU ZSPD observations and upsert into standard.db.
    Delegates entirely to core.fetch_zspd_history (same API key, same schema).
    Returns the number of rows inserted/refreshed (0 = API failed).
    """
    from core.fetch_zspd_history import fetch_day, init_db, to_row, INSERT_SQL

    today   = (datetime.now(timezone.utc) + SHANGHAI_OFFSET).date()
    session = requests.Session()

    print(f"  Fetching WU ZSPD for {today} via weather.com API ...", end=" ", flush=True)
    try:
        obs_list = fetch_day(today, session)
    except Exception as e:
        print(f"FAILED ({e})")
        return 0

    if not obs_list:
        print("0 obs")
        return 0

    conn = init_db(WU_ZSPD_DB)
    rows = [to_row(o) for o in obs_list]
    conn.executemany(INSERT_SQL, rows)
    conn.commit()
    conn.close()
    print(f"{len(rows)} obs")
    return len(rows)


def _read_slot_from_db(slot_utc: datetime) -> Optional[dict]:
    """
    Return the observation for slot_utc from standard.db, or None if
    the slot is absent or older than 90 minutes (considered stale).
    Accepts an obs within ±15 min of the target epoch.
    """
    if not WU_ZSPD_DB.exists():
        return None
    epoch = int(slot_utc.timestamp())
    conn  = sqlite3.connect(WU_ZSPD_DB)
    row   = conn.execute(
        """SELECT valid_time_gmt, temp_c, dew_point_c, wind_speed_kmh,
                  wind_dir_deg, pressure_hpa, visibility_km
           FROM zspd_observations
           WHERE valid_time_gmt BETWEEN ? AND ?
             AND temp_c IS NOT NULL
           ORDER BY ABS(valid_time_gmt - ?) ASC
           LIMIT 1""",
        (epoch - 900, epoch + 900, epoch),
    ).fetchone()
    conn.close()

    if row is None:
        return None

    vt = row[0]
    age_min = (datetime.now(timezone.utc).timestamp() - vt) / 60.0
    if age_min > 90:
        return None  # stale — force manual

    local_str = (datetime.fromtimestamp(vt, tz=timezone.utc)
                 + SHANGHAI_OFFSET).strftime("%H:%M CST")
    print(f"  DB hit: {local_str}  temp={row[1]}°C  age={age_min:.0f}min")

    return {
        "slot_utc":      slot_utc,
        "temp_c":        row[1],
        "dew_c":         row[2],
        "wind_kmh":      row[3],
        "wind_dir_deg":  row[4],
        "pressure_hpa":  row[5],
        "visibility_km": row[6],
        "source":        "wu_db",
    }


# ── Manual input dialog ────────────────────────────────────────────────────

def _manual_dialog(slot_utc: datetime) -> Optional[dict]:
    """tkinter dialog for manual observation entry. Falls back to CLI."""
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        return _cli_fallback(slot_utc)

    local_dt   = slot_utc + SHANGHAI_OFFSET
    slot_label = local_dt.strftime("%Y-%m-%d  %H:%M  CST")
    result: dict = {}

    root = tk.Tk()
    root.title("ZSPD 气象观测 — 手动输入")
    root.resizable(False, False)

    FL = ("Microsoft YaHei", 10)
    FE = ("Consolas", 11)
    FH = ("Microsoft YaHei", 11, "bold")
    P  = {"padx": 10, "pady": 4}

    tk.Label(root, text="ZSPD 半小时气象观测", font=FH).grid(
        row=0, column=0, columnspan=3, pady=(12, 0))
    tk.Label(root, text=f"时间槽: {slot_label}", font=FL, fg="#555").grid(
        row=1, column=0, columnspan=3, pady=(0, 8))

    for col, txt in enumerate(["字段", "值 (原始，未取整)", "单位"]):
        tk.Label(root, text=txt, font=FL, fg="#888").grid(
            row=2, column=col, sticky="w", **P)
    tk.Frame(root, height=1, bg="#ccc").grid(
        row=3, column=0, columnspan=3, sticky="ew", padx=10)

    fields = [
        ("temp_c",        "气温 *",   "°C  (-10~45)",   True),
        ("dew_c",         "露点",     "°C",             False),
        ("wind_kmh",      "风速",     "km/h",           False),
        ("wind_dir_deg",  "风向",     "deg (0=N,180=S)", False),
        ("pressure_hpa",  "气压 QNH", "hPa",            False),
        ("visibility_km", "能见度",   "km",             False),
    ]
    entries: dict[str, tk.Entry] = {}
    for i, (key, label, unit, req) in enumerate(fields):
        r = i + 4
        tk.Label(root, text=label, font=FL, fg="#000" if req else "#333").grid(
            row=r, column=0, sticky="w", **P)
        e = tk.Entry(root, font=FE, width=14)
        e.grid(row=r, column=1, sticky="w", **P)
        entries[key] = e
        tk.Label(root, text=unit, font=FL, fg="#777").grid(
            row=r, column=2, sticky="w", **P)

    tk.Label(root, text="* 必填", font=("Microsoft YaHei", 9), fg="#e44").grid(
        row=10, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))

    def _submit():
        raw = entries["temp_c"].get().strip()
        if not raw:
            messagebox.showerror("错误", "气温 (temp_c) 为必填项"); return
        try:
            t = float(raw)
        except ValueError:
            messagebox.showerror("错误", f"无法解析气温: '{raw}'"); return
        if not (-10.0 <= t <= 45.0):
            messagebox.showerror(
                "错误",
                f"气温 {t}°C 超出 ZSPD 合理范围 (-10~45°C)\n"
                "请输入原始值（非取整后的数值）")
            return

        def _f(k):
            v = entries[k].get().strip()
            try: return float(v) if v else None
            except ValueError: return None

        result.update({
            "slot_utc": slot_utc, "temp_c": t,
            "dew_c": _f("dew_c"), "wind_kmh": _f("wind_kmh"),
            "wind_dir_deg": _f("wind_dir_deg"), "pressure_hpa": _f("pressure_hpa"),
            "visibility_km": _f("visibility_km"), "source": "manual",
        })
        root.destroy()

    def _cancel(): root.destroy()

    bf = tk.Frame(root)
    bf.grid(row=11, column=0, columnspan=3, pady=12)
    tk.Button(bf, text="提交", font=FL, width=10,
              bg="#4CAF50", fg="white", command=_submit).pack(side="left", padx=8)
    tk.Button(bf, text="取消", font=FL, width=10,
              command=_cancel).pack(side="left", padx=8)

    root.update_idletasks()
    w, h   = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")
    root.mainloop()
    return result if result else None


def _cli_fallback(slot_utc: datetime) -> Optional[dict]:
    local = slot_utc + SHANGHAI_OFFSET
    print(f"\n手动输入 ZSPD 观测 [{local.strftime('%Y-%m-%d %H:%M CST')}]")
    print("(回车跳过可选字段，q 取消)\n")

    def _ask(prompt: str, required: bool = False) -> Optional[float]:
        while True:
            v = input(f"  {prompt}: ").strip()
            if v.lower() == "q": return None
            if not v:
                if required: print("  必填，请重新输入"); continue
                return None
            try: return float(v)
            except ValueError: print(f"  无法解析 '{v}'")

    t = _ask("气温 temp_c (°C, 必填, 如 27.8)", required=True)
    if t is None:
        print("取消"); return None

    return {
        "slot_utc": slot_utc, "temp_c": t,
        "dew_c":         _ask("露点 dew_c (°C)"),
        "wind_kmh":      _ask("风速 wind_kmh (km/h)"),
        "wind_dir_deg":  _ask("风向 wind_dir_deg (deg)"),
        "pressure_hpa":  _ask("气压 pressure_hpa (hPa)"),
        "visibility_km": _ask("能见度 visibility_km (km)"),
        "source": "manual",
    }


# ── DB insert for manual data ──────────────────────────────────────────────

def insert_observation(obs: dict) -> bool:
    """
    Insert a manually-supplied observation into standard.db.
    Uses the same table as core/fetch_zspd_history (compatible schema).
    Returns True if a new/updated row was written.
    """
    from core.fetch_zspd_history import init_db

    slot_utc = obs["slot_utc"]
    epoch    = int(slot_utc.timestamp())
    ts_utc, ts_local, obs_date = _slot_strings(slot_utc)

    conn = init_db(WU_ZSPD_DB)   # ensures correct table schema

    existing = conn.execute(
        "SELECT temp_c FROM zspd_observations WHERE valid_time_gmt = ?", (epoch,)
    ).fetchone()
    if existing and existing[0] == obs["temp_c"]:
        conn.close()
        return False

    conn.execute(_MANUAL_INSERT, (
        epoch, ts_utc, ts_local, obs_date, "ZSPD",
        obs.get("temp_c"),
        obs.get("dew_c"),
        obs.get("wind_kmh"),
        obs.get("wind_dir_deg"),
        obs.get("pressure_hpa"),
        obs.get("visibility_km"),
    ))
    conn.commit()
    conn.close()
    return True


# ── Main entry point ───────────────────────────────────────────────────────

def get_latest_observation(force_manual: bool = False) -> Optional[dict]:
    """
    Get the observation for the most recent 30-min slot.
    Strategy:
      1. Refresh today's WU data from API (core/fetch_zspd_history).
      2. Read the current slot from standard.db.
      3. If missing or stale → open manual input dialog.
    """
    slot_utc = _nearest_past_30min(datetime.now(timezone.utc))
    local    = slot_utc + SHANGHAI_OFFSET
    print(f"Target slot: {local.strftime('%Y-%m-%d %H:%M CST')}")

    if not force_manual:
        n = refresh_today_wu()
        if n > 0:
            obs = _read_slot_from_db(slot_utc)
            if obs:
                return obs
        print("  Slot missing or stale — opening manual dialog")

    return _manual_dialog(slot_utc)


def main(argv: list[str]) -> None:
    force_manual = "--manual" in argv
    show_only    = "--show"   in argv

    obs = get_latest_observation(force_manual=force_manual)
    if obs is None:
        print("No observation obtained."); return

    print(f"\nObservation [source={obs.get('source')}]:")
    for k, v in obs.items():
        if k != "slot_utc" and v is not None:
            print(f"  {k:<20} {v}")

    if show_only:
        return

    if obs.get("source") == "manual":
        inserted = insert_observation(obs)
        status   = "inserted" if inserted else "already up-to-date"
        print(f"\n[{status}] -> {WU_ZSPD_DB}")
    else:
        print(f"\n(WU API rows already written to {WU_ZSPD_DB} during refresh)")


if __name__ == "__main__":
    main(sys.argv)
