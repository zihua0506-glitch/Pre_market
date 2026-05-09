"""
按 database/token.db 中的每个 token，从 Polymarket CLOB 拉 1 分钟价格历史，
写入 database/data.db，用于模型训练 / 回测。

注意：Polymarket 公共 API 不提供历史 orderbook 快照（best bid/ask 深度等）。
     "1 分钟盘口走势" 在公开端点上只能取到 mid/last 概率序列 (0~1)。本脚本
     只落价格时间序列；如果将来要做 L2 回放需要自己长跑 /book 端点采集。

数据模型：
  price_minute(token_id, ts, price)              -- 1 分钟概率时间序列
  token_meta(token_id, side, ...)                -- token 语义 + 拉取范围
  v_minute_full                                  -- view: 价格 + 元信息 join
"""

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

CLOB_API = "https://clob.polymarket.com"
ROOT = Path(__file__).resolve().parent.parent.parent
TOKEN_DB = ROOT / "database" / "token.db"
DATA_DB  = ROOT / "database" / "data.db"

# 单 token HTTP 失败重试参数
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5
SLEEP_BETWEEN = 0.05      # 礼貌限速 (秒)
PAD_SECONDS = 5 * 60      # 拉取窗口在 [start-5min, end+5min] 之间


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_prices_history(token_id, start_ts, end_ts):
    """
    /prices-history 端点：fidelity 单位为分钟。短窗口 + fidelity=1 才能拿到真实 1 min 颗粒。
    返回 [{t: int, p: float}, ...]，t 为 UNIX 秒（分钟边界），p 为概率 [0,1]。
    """
    params = {
        "market": token_id,
        "fidelity": 1,
        "startTs": int(start_ts),
        "endTs": int(end_ts),
    }
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(f"{CLOB_API}/prices-history", params=params, timeout=20)
            r.raise_for_status()
            return r.json().get("history", []) or []
        except requests.exceptions.RequestException as e:
            last_err = e
            time.sleep(RETRY_BACKOFF ** attempt)
    raise RuntimeError(f"prices-history 失败: {last_err}")


def _migrate_token_meta_pk(conn) -> None:
    """老库的 token_meta 没有 PRIMARY KEY，ON CONFLICT(token_id) 会报错。
    检测到这种情况就重建表（保留旧数据，token_id 已验证唯一）。"""
    info = conn.execute("PRAGMA table_info(token_meta)").fetchall()
    if not info:
        return  # 表不存在，让后续 CREATE TABLE 处理
    has_pk = any(col[5] for col in info)  # col[5] is the pk flag
    if has_pk:
        return
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_minute_full;
        CREATE TABLE token_meta__new (
            token_id      TEXT PRIMARY KEY,
            side          TEXT NOT NULL,
            market_id     TEXT,
            event_id      TEXT,
            condition_id  TEXT,
            question      TEXT,
            kind          TEXT,
            threshold_c   INTEGER,
            bound         TEXT,
            day_date      TEXT,
            market_start  TEXT,
            market_end    TEXT,
            n_minutes     INTEGER,
            ts_start      INTEGER,
            ts_end        INTEGER,
            fetched_at    TEXT
        );
        INSERT OR IGNORE INTO token_meta__new
            SELECT token_id, side, market_id, event_id, condition_id, question,
                   kind, threshold_c, bound, day_date, market_start, market_end,
                   n_minutes, ts_start, ts_end, fetched_at
            FROM token_meta;
        DROP TABLE token_meta;
        ALTER TABLE token_meta__new RENAME TO token_meta;
        CREATE INDEX IF NOT EXISTS idx_meta_day   ON token_meta(day_date);
        CREATE INDEX IF NOT EXISTS idx_meta_event ON token_meta(event_id);
        """
    )
    conn.commit()


def init_db(conn):
    """
    Storage layout (compact schema):
      token_map(id INT PK, token_id TEXT UNIQUE)        -- 长串 token_id → 小整数
      price_minute_compact(token_id INT, ts INT, price INT)  -- price 是概率×1000
      price_minute  -- view，对外暴露 (token_id TEXT, ts INT, price REAL[0..1])
    """
    _migrate_token_meta_pk(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS token_map (
            id       INTEGER PRIMARY KEY,
            token_id TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS price_minute_compact (
            token_id INTEGER NOT NULL,
            ts       INTEGER NOT NULL,
            price    INTEGER NOT NULL,
            PRIMARY KEY (token_id, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_price_minute_compact_ts
            ON price_minute_compact(ts);

        CREATE TABLE IF NOT EXISTS token_meta (
            token_id      TEXT PRIMARY KEY,
            side          TEXT NOT NULL,
            market_id     TEXT,
            event_id      TEXT,
            condition_id  TEXT,
            question      TEXT,
            kind          TEXT,
            threshold_c   INTEGER,
            bound         TEXT,
            day_date      TEXT,
            market_start  TEXT,
            market_end    TEXT,
            n_minutes     INTEGER,
            ts_start      INTEGER,
            ts_end        INTEGER,
            fetched_at    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_meta_day ON token_meta(day_date);
        CREATE INDEX IF NOT EXISTS idx_meta_event ON token_meta(event_id);
        """
    )

    # price_minute 在老库里是普通表、在新库里是 view。两种情况都不动它，
    # 只在完全没有的时候新建 view。
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'price_minute'"
    ).fetchone()
    if row is None:
        conn.execute(
            """
            CREATE VIEW price_minute AS
            SELECT m.token_id AS token_id,
                   p.ts       AS ts,
                   p.price / 1000.0 AS price
            FROM   price_minute_compact p
            JOIN   token_map m ON m.id = p.token_id
            """
        )

    # v_minute_full 依赖 price_minute（view 或 table 都能 JOIN），保持可重建
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_minute_full;
        CREATE VIEW v_minute_full AS
        SELECT
            p.ts,
            datetime(p.ts, 'unixepoch') AS ts_iso,
            p.token_id, p.price,
            m.side, m.kind, m.threshold_c, m.bound,
            m.day_date, m.event_id, m.condition_id, m.market_id
        FROM price_minute p
        JOIN token_meta m USING (token_id);
        """
    )
    conn.commit()


def _resolve_token_int_id(conn, token_id_str: str) -> int:
    """token.db 用的长串 token_id → token_map.id（小整数）。不存在则分配。"""
    row = conn.execute(
        "SELECT id FROM token_map WHERE token_id = ?", (token_id_str,)
    ).fetchone()
    if row is not None:
        return row[0]
    next_id = conn.execute(
        "SELECT COALESCE(MAX(id), -1) + 1 FROM token_map"
    ).fetchone()[0]
    conn.execute(
        "INSERT OR IGNORE INTO token_map(id, token_id) VALUES (?, ?)",
        (next_id, token_id_str),
    )
    return conn.execute(
        "SELECT id FROM token_map WHERE token_id = ?", (token_id_str,)
    ).fetchone()[0]


def load_tokens(token_conn):
    """从 token.db 取所有 YES/NO token，连同元信息一起返回。"""
    sql = """
    SELECT m.yes_token_id AS token_id, 'YES' AS side,
           m.id, m.event_id, m.condition_id, m.question,
           m.kind, m.threshold_c, m.bound, e.day_date,
           m.start_date, m.end_date
    FROM markets m JOIN events e ON e.id = m.event_id
    WHERE m.yes_token_id IS NOT NULL
    UNION ALL
    SELECT m.no_token_id AS token_id, 'NO' AS side,
           m.id, m.event_id, m.condition_id, m.question,
           m.kind, m.threshold_c, m.bound, e.day_date,
           m.start_date, m.end_date
    FROM markets m JOIN events e ON e.id = m.event_id
    WHERE m.no_token_id IS NOT NULL
    """
    return token_conn.execute(sql).fetchall()


def fetch_window(market_start, market_end):
    """根据 market 的 start/end 推一个稳妥的拉取窗口。"""
    s = parse_iso(market_start)
    e = parse_iso(market_end)
    now = datetime.now(timezone.utc)
    if not s:
        s = now - timedelta(days=2)
    if not e:
        e = now
    # endTs 取 min(market_end, now)，避免传未来时间
    e = min(e, now)
    s_ts = int((s - timedelta(seconds=PAD_SECONDS)).timestamp())
    e_ts = int((e + timedelta(seconds=PAD_SECONDS)).timestamp())
    return s_ts, e_ts


def upsert_token(dst, token_id, side, market_id, event_id, condition_id, question,
                 kind, threshold_c, bound, day_date, market_start, market_end, history):
    cur = dst.cursor()

    if history:
        int_id = _resolve_token_int_id(dst, token_id)
        cur.executemany(
            "INSERT OR IGNORE INTO price_minute_compact(token_id, ts, price) VALUES(?,?,?)",
            [
                (int_id, int(h["t"]), int(round(float(h["p"]) * 1000)))
                for h in history if "t" in h and "p" in h
            ],
        )
        ts_start = int(history[0]["t"])
        ts_end = int(history[-1]["t"])
        n_minutes = len(history)
    else:
        ts_start = ts_end = None
        n_minutes = 0

    cur.execute(
        """
        INSERT INTO token_meta(token_id, side, market_id, event_id, condition_id, question,
                               kind, threshold_c, bound, day_date,
                               market_start, market_end,
                               n_minutes, ts_start, ts_end, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(token_id) DO UPDATE SET
            side=excluded.side, market_id=excluded.market_id, event_id=excluded.event_id,
            condition_id=excluded.condition_id, question=excluded.question,
            kind=excluded.kind, threshold_c=excluded.threshold_c, bound=excluded.bound,
            day_date=excluded.day_date,
            market_start=excluded.market_start, market_end=excluded.market_end,
            n_minutes=excluded.n_minutes, ts_start=excluded.ts_start, ts_end=excluded.ts_end,
            fetched_at=excluded.fetched_at
        """,
        (
            token_id, side, market_id, event_id, condition_id, question,
            kind, threshold_c, bound, day_date,
            market_start, market_end,
            n_minutes, ts_start, ts_end, datetime.now(timezone.utc).isoformat(),
        ),
    )


def update_recent(days: int = 7) -> None:
    """
    增量更新：只处理 day_date 在最近 days 天内的 token。

    对已有价格数据的 token，从 ts_end（上次拉取截止点）续拉到现在；
    对新 token，则从 market_start 拉完整历史。
    使用 INSERT OR IGNORE，不会产生重复数据。
    """
    if not TOKEN_DB.exists():
        print(f"找不到 {TOKEN_DB}，请先运行 core/fetch_tokens.py")
        return

    now = datetime.now(timezone.utc)
    cutoff_date = (now - timedelta(days=days)).date().isoformat()

    src = sqlite3.connect(TOKEN_DB)
    dst = sqlite3.connect(DATA_DB)
    init_db(dst)

    # 从 token.db 拿最近 days 天的 token 元数据
    tokens = src.execute(
        """
        SELECT m.yes_token_id AS token_id, 'YES' AS side,
               m.id, m.event_id, m.condition_id, m.question,
               m.kind, m.threshold_c, m.bound, e.day_date,
               m.start_date, m.end_date
        FROM markets m JOIN events e ON e.id = m.event_id
        WHERE m.yes_token_id IS NOT NULL
          AND e.day_date >= ?
        UNION ALL
        SELECT m.no_token_id AS token_id, 'NO' AS side,
               m.id, m.event_id, m.condition_id, m.question,
               m.kind, m.threshold_c, m.bound, e.day_date,
               m.start_date, m.end_date
        FROM markets m JOIN events e ON e.id = m.event_id
        WHERE m.no_token_id IS NOT NULL
          AND e.day_date >= ?
        """,
        (cutoff_date, cutoff_date),
    ).fetchall()
    src.close()

    # 查出 data.db 里已有的 ts_end，用于增量起点
    existing = {
        row[0]: row[1]
        for row in dst.execute(
            "SELECT token_id, ts_end FROM token_meta WHERE ts_end IS NOT NULL"
        ).fetchall()
    }

    now_ts = int(now.timestamp())
    print(f"[UpdateMarket] 增量拉取最近 {days} 天 token（{len(tokens)} 个）...")

    n_ok = n_empty = n_fail = n_total_pts = 0
    for i, row in enumerate(tokens, 1):
        (token_id, side, market_id, event_id, condition_id, question,
         kind, threshold_c, bound, day_date, market_start, market_end) = row

        # 增量起点：已有数据则从 ts_end - 5min 续拉，否则全量
        if token_id in existing:
            s_ts = existing[token_id] - PAD_SECONDS
        else:
            s_raw = parse_iso(market_start)
            s_ts  = int((s_raw - timedelta(seconds=PAD_SECONDS)).timestamp()) if s_raw \
                    else int((now - timedelta(days=days)).timestamp())

        e_raw = parse_iso(market_end)
        e_ts  = int(min(e_raw, now).timestamp()) + PAD_SECONDS if e_raw else now_ts

        if s_ts >= e_ts:
            # 已关闭市场且数据完整，无新窗口可拉
            n_empty += 1
            continue

        try:
            history = fetch_prices_history(token_id, s_ts, e_ts)
        except Exception as exc:
            n_fail += 1
            print(f"  [{i:4d}/{len(tokens)}] FAIL {side} {kind} {threshold_c}°C "
                  f"({day_date}): {exc}")
            continue

        upsert_token(dst, token_id, side, market_id, event_id, condition_id, question,
                     kind, threshold_c, bound, day_date, market_start, market_end, history)
        dst.commit()

        if history:
            n_ok += 1
            n_total_pts += len(history)
        else:
            n_empty += 1

        time.sleep(SLEEP_BETWEEN)

    dst.close()
    print(f"[UpdateMarket] 完成: ok={n_ok} empty={n_empty} fail={n_fail} "
          f"新增 {n_total_pts} 条价格记录")


def main():
    if not TOKEN_DB.exists():
        print(f"找不到 {TOKEN_DB}，请先运行 core/fetch_tokens.py")
        sys.exit(1)

    src = sqlite3.connect(TOKEN_DB)
    dst = sqlite3.connect(DATA_DB)
    init_db(dst)

    tokens = load_tokens(src)
    print(f"待拉取 token 数: {len(tokens)}")

    n_total_pts = 0
    n_ok = n_empty = n_fail = 0
    t0 = time.time()

    for i, row in enumerate(tokens, 1):
        (token_id, side, market_id, event_id, condition_id, question,
         kind, threshold_c, bound, day_date, market_start, market_end) = row

        s_ts, e_ts = fetch_window(market_start, market_end)
        try:
            history = fetch_prices_history(token_id, s_ts, e_ts)
        except Exception as e:
            n_fail += 1
            print(f"  [{i:4d}/{len(tokens)}] {side} {kind} {threshold_c}°C ({day_date}) FAIL: {e}")
            continue

        upsert_token(dst, token_id, side, market_id, event_id, condition_id, question,
                     kind, threshold_c, bound, day_date, market_start, market_end, history)
        dst.commit()

        if history:
            n_ok += 1
            n_total_pts += len(history)
        else:
            n_empty += 1

        if i % 50 == 0 or i == len(tokens):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(tokens) - i) / rate if rate > 0 else 0
            print(f"  [{i:4d}/{len(tokens)}] ok={n_ok} empty={n_empty} fail={n_fail} "
                  f"pts={n_total_pts} | {rate:.1f} t/s ETA {eta:.0f}s")

        time.sleep(SLEEP_BETWEEN)

    src.close()
    dst.close()

    print(f"\n完成。ok={n_ok} empty={n_empty} fail={n_fail}, "
          f"写入 {n_total_pts} 条 1-min 记录到 {DATA_DB}")


if __name__ == "__main__":
    main()
