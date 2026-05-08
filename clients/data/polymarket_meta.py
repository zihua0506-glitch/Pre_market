"""
拉取 Polymarket 上海每日温度合约（daily-temperature × shanghai），
把每一个温度阈值市场的 YES / NO clobTokenIds 落到 SQLite (token.db)。

数据源： gamma-api.polymarket.com /events?tag_slug=shanghai
        过滤 tags 必须包含 'daily-temperature'，end_date 在过去 5 个月内。
"""

import calendar
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

GAMMA_API = "https://gamma-api.polymarket.com"
DB_PATH = Path(__file__).resolve().parent.parent / "database" / "token.db"
LOOKBACK_DAYS = 150  # ~ 5 months

QUESTION_RE = re.compile(
    r"Will the (?P<kind>highest|lowest) temperature in Shanghai be "
    r"(?P<threshold>-?\d+)\s*°?C(?P<bound>\s*or below|\s*or higher)?\s*on\s*(?P<day>[A-Za-z]+\s+\d+)",
    re.IGNORECASE,
)

# 标题里 "... on April 28?" / "... on April 28" 的观测日抓取
TITLE_DATE_RE = re.compile(r"on\s+(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})", re.IGNORECASE)
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}


def fetch_shanghai_events():
    """拉取所有 tag=shanghai 的 events（active + closed），按 endDate 倒序分页。"""
    url = f"{GAMMA_API}/events"
    seen = set()
    all_events = []

    for closed_flag in ("true", "false"):
        offset = 0
        while True:
            params = {
                "limit": 100,
                "offset": offset,
                "tag_slug": "shanghai",
                "order": "endDate",
                "ascending": "false",
                "closed": closed_flag,
            }
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for ev in batch:
                if ev["id"] in seen:
                    continue
                seen.add(ev["id"])
                all_events.append(ev)
            if len(batch) < 100:
                break
            offset += 100
            if offset > 5000:
                break
    return all_events


def is_daily_temp(event):
    return any(t.get("slug") == "daily-temperature" for t in event.get("tags", []))


def parse_question(question):
    """从题面解析 kind (highest/lowest)、threshold (摄氏度整数)、bound (eq/le/ge)。"""
    if not question:
        return None
    m = QUESTION_RE.search(question)
    if not m:
        return None
    bound_raw = (m.group("bound") or "").strip().lower()
    if "below" in bound_raw:
        bound = "le"
    elif "higher" in bound_raw:
        bound = "ge"
    else:
        bound = "eq"
    return {
        "kind": m.group("kind").lower(),
        "threshold_c": int(m.group("threshold")),
        "bound": bound,
    }


def parse_observation_date(title_or_question, end_date_str):
    """从 title / question 中解析"观测日" (YYYY-MM-DD)。

    Polymarket 把 lowest 系列的 endDate 设到次日 UTC 0:00，直接截 endDate
    会偏移一天；改成解析题面的 'on Month D'，年份取自 endDate（跨年时回退一年）。
    """
    if not title_or_question:
        return None
    m = TITLE_DATE_RE.search(title_or_question)
    if not m:
        return None
    month = _MONTHS.get(m.group("month").lower())
    day = int(m.group("day"))
    if not month or not (1 <= day <= 31):
        return None

    try:
        end_dt = datetime.fromisoformat((end_date_str or "").replace("Z", "+00:00"))
        year = end_dt.year
    except (ValueError, AttributeError):
        year = datetime.now(timezone.utc).year

    # 跨年边界：endDate 落在 1 月而题面写的是 12 月（或反过来）
    if month == 12 and end_dt and end_dt.month == 1:
        year -= 1
    elif month == 1 and end_dt and end_dt.month == 12:
        year += 1

    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def extract_tokens(market):
    raw = market.get("clobTokenIds") or "[]"
    try:
        tokens = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        tokens = []
    yes = tokens[0] if len(tokens) > 0 else None
    no = tokens[1] if len(tokens) > 1 else None
    return yes, no


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id           TEXT PRIMARY KEY,
            slug         TEXT,
            title        TEXT,
            day_date     TEXT,        -- 合约对应的那一天 (YYYY-MM-DD, UTC)
            start_date   TEXT,
            end_date     TEXT,
            closed       INTEGER,
            active       INTEGER,
            updated_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS markets (
            id              TEXT PRIMARY KEY,    -- market id
            event_id        TEXT NOT NULL,
            condition_id    TEXT,
            question        TEXT,
            kind            TEXT,                -- highest / lowest
            threshold_c     INTEGER,             -- 温度阈值 (摄氏度)
            bound           TEXT,                -- eq / le / ge
            yes_token_id    TEXT,
            no_token_id     TEXT,
            start_date      TEXT,
            end_date        TEXT,
            closed          INTEGER,
            active          INTEGER,
            updated_at      TEXT,
            FOREIGN KEY (event_id) REFERENCES events(id)
        );

        CREATE INDEX IF NOT EXISTS idx_markets_event   ON markets(event_id);
        CREATE INDEX IF NOT EXISTS idx_markets_day     ON markets(end_date);
        CREATE INDEX IF NOT EXISTS idx_markets_kind_th ON markets(kind, threshold_c);
        CREATE INDEX IF NOT EXISTS idx_events_day      ON events(day_date);
        """
    )
    conn.commit()


def upsert(conn, events_within):
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    n_ev = n_mk = 0

    for ev in events_within:
        end_date = ev.get("endDate")
        # 优先从 title 解析观测日，解析失败再回退到 endDate 截断
        day_date = parse_observation_date(ev.get("title"), end_date) or (
            (end_date or "")[:10] or None
        )

        cur.execute(
            """
            INSERT INTO events(id, slug, title, day_date, start_date, end_date, closed, active, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                slug=excluded.slug, title=excluded.title, day_date=excluded.day_date,
                start_date=excluded.start_date, end_date=excluded.end_date,
                closed=excluded.closed, active=excluded.active, updated_at=excluded.updated_at
            """,
            (
                str(ev["id"]),
                ev.get("slug"),
                ev.get("title"),
                day_date,
                ev.get("startDate"),
                ev.get("endDate"),
                int(bool(ev.get("closed"))),
                int(bool(ev.get("active"))),
                now_iso,
            ),
        )
        n_ev += 1

        for m in ev.get("markets", []) or []:
            yes, no = extract_tokens(m)
            parsed = parse_question(m.get("question")) or {}
            cur.execute(
                """
                INSERT INTO markets(id, event_id, condition_id, question, kind, threshold_c, bound,
                                    yes_token_id, no_token_id, start_date, end_date, closed, active, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    event_id=excluded.event_id, condition_id=excluded.condition_id, question=excluded.question,
                    kind=excluded.kind, threshold_c=excluded.threshold_c, bound=excluded.bound,
                    yes_token_id=excluded.yes_token_id, no_token_id=excluded.no_token_id,
                    start_date=excluded.start_date, end_date=excluded.end_date,
                    closed=excluded.closed, active=excluded.active, updated_at=excluded.updated_at
                """,
                (
                    str(m["id"]),
                    str(ev["id"]),
                    m.get("conditionId"),
                    m.get("question"),
                    parsed.get("kind"),
                    parsed.get("threshold_c"),
                    parsed.get("bound"),
                    yes,
                    no,
                    m.get("startDate"),
                    m.get("endDate"),
                    int(bool(m.get("closed"))),
                    int(bool(m.get("active"))),
                    now_iso,
                ),
            )
            n_mk += 1

    conn.commit()
    return n_ev, n_mk


def check_highest_coverage(conn):
    """检查每一天是否都有 highest 事件，列出缺口。"""
    rows = conn.execute(
        """
        SELECT day_date FROM events
        WHERE day_date IS NOT NULL
          AND title LIKE 'Highest%'
        """
    ).fetchall()
    days = sorted({r[0] for r in rows})
    if not days:
        print("\n[完整性检查] 库内没有 highest 事件，跳过。")
        return

    start = date.fromisoformat(days[0])
    end = date.fromisoformat(days[-1])
    have = set(days)
    missing = []
    d = start
    while d <= end:
        if d.isoformat() not in have:
            missing.append(d.isoformat())
        d += timedelta(days=1)

    print(f"\n[完整性检查] highest 覆盖 {start} -> {end}（共 {(end-start).days+1} 天）")
    if not missing:
        print("  OK: 区间内每天都有 highest 事件。")
    else:
        print(f"  缺失 {len(missing)} 天:")
        for m in missing:
            print(f"   - {m}")


def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    print(f"拉取 Polymarket 上海每日温度合约 (cutoff = {cutoff.date()})...")

    events = fetch_shanghai_events()
    print(f"  shanghai 标签事件总数: {len(events)}")

    daily = [e for e in events if is_daily_temp(e)]
    print(f"  daily-temperature 事件: {len(daily)}")

    def end_dt(ev):
        try:
            return datetime.fromisoformat((ev.get("endDate") or "").replace("Z", "+00:00"))
        except ValueError:
            return None

    within = [e for e in daily if (dt := end_dt(e)) and dt >= cutoff]
    print(f"  过去 {LOOKBACK_DAYS} 天内的 events: {len(within)}")

    if within:
        days = sorted({(e.get("endDate") or "")[:10] for e in within})
        print(f"  覆盖日期: {days[0]} -> {days[-1]} ({len(days)} 天)")

    with sqlite3.connect(DB_PATH) as conn:
        init_db(conn)
        n_ev, n_mk = upsert(conn, within)
        print(f"  写入 events={n_ev}, markets(token 对)={n_mk}")
        c = conn.execute("SELECT COUNT(*) FROM markets WHERE yes_token_id IS NOT NULL")
        print(f"  token.db 现存 YES token 数: {c.fetchone()[0]}")
        print(f"  数据库路径: {DB_PATH}")

        check_highest_coverage(conn)


if __name__ == "__main__":
    main()
