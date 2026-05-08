"""Tests for clients.data.polymarket_prices — sqlite migration + I/O.

Focus is the schema-migration / view-vs-table bug we hit in production:
older databases have `price_minute` as a plain table; newer ones have it as
a view over a compact (int-id, int-price) representation. The init_db helper
must:
  - Detect a missing PK on token_meta and rebuild it
  - Not collide with an existing view named price_minute
  - Maintain the v_minute_full view (which depends on token_meta)
"""

import sqlite3

from clients.data.polymarket_prices import (
    init_db, _migrate_token_meta_pk, _resolve_token_int_id,
)


class TestInitDbFreshDB:
    def test_creates_all_tables_and_view(self, tmp_path):
        db = tmp_path / "fresh.db"
        conn = sqlite3.connect(db)
        init_db(conn)

        names = {row[0]: row[1] for row in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE name IN "
            "('token_map','price_minute_compact','token_meta','price_minute','v_minute_full')"
        )}
        assert names["token_map"]              == "table"
        assert names["price_minute_compact"]   == "table"
        assert names["token_meta"]             == "table"
        # price_minute view is created on a fresh DB
        assert names.get("price_minute")       == "view"
        assert names.get("v_minute_full")      == "view"
        conn.close()

    def test_token_meta_has_pk_on_fresh_db(self, tmp_path):
        db = tmp_path / "fresh.db"
        conn = sqlite3.connect(db)
        init_db(conn)
        info = conn.execute("PRAGMA table_info(token_meta)").fetchall()
        # col[5] is the pk flag
        assert any(col[5] for col in info), "token_meta should have a PK on fresh DB"
        conn.close()


class TestInitDbLegacyDB:
    """Simulate the production legacy DB: token_meta without PK + price_minute as TABLE."""

    def test_migrates_token_meta_to_have_pk(self, tmp_path):
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(db)
        # Old-style token_meta without PK
        conn.executescript(
            """
            CREATE TABLE token_meta (token_id, side, market_id, event_id, condition_id,
                                     question, kind, threshold_c, bound, day_date,
                                     market_start, market_end, n_minutes, ts_start, ts_end,
                                     fetched_at);
            INSERT INTO token_meta (token_id, side) VALUES ('tok1', 'YES'), ('tok2', 'NO');
            """
        )
        conn.commit()
        # Sanity: legacy schema
        info = conn.execute("PRAGMA table_info(token_meta)").fetchall()
        assert not any(col[5] for col in info)

        # init_db should migrate
        init_db(conn)

        info = conn.execute("PRAGMA table_info(token_meta)").fetchall()
        assert any(col[5] for col in info), "token_meta should have a PK after migration"

        rows = conn.execute("SELECT token_id, side FROM token_meta ORDER BY token_id").fetchall()
        assert rows == [("tok1", "YES"), ("tok2", "NO")], "data must survive migration"
        conn.close()

    def test_idempotent_on_already_migrated_db(self, tmp_path):
        db = tmp_path / "twice.db"
        conn = sqlite3.connect(db)
        init_db(conn)
        # Run init_db twice; nothing should explode.
        init_db(conn)
        init_db(conn)
        conn.close()

    def test_does_not_break_when_price_minute_is_a_view(self, tmp_path):
        """Older databases pre-compact had a TABLE; some have a VIEW. Both must work."""
        db = tmp_path / "view-style.db"
        conn = sqlite3.connect(db)
        # Create the compact tables + a price_minute VIEW first (mirrors current prod)
        conn.executescript(
            """
            CREATE TABLE token_map (id INTEGER PRIMARY KEY, token_id TEXT UNIQUE NOT NULL);
            CREATE TABLE price_minute_compact (
                token_id INTEGER NOT NULL, ts INTEGER NOT NULL, price INTEGER NOT NULL,
                PRIMARY KEY (token_id, ts)
            );
            CREATE VIEW price_minute AS
                SELECT m.token_id AS token_id, p.ts AS ts, p.price/1000.0 AS price
                FROM   price_minute_compact p
                JOIN   token_map m ON m.id = p.token_id;
            """
        )
        conn.commit()
        # init_db should not crash on the existing view
        init_db(conn)
        # Existing view should be preserved (i.e., still a view, not redefined as table)
        row = conn.execute(
            "SELECT type FROM sqlite_master WHERE name='price_minute'"
        ).fetchone()
        assert row == ("view",)
        conn.close()


class TestResolveTokenIntId:
    def test_assigns_sequential_ids(self, tmp_path):
        db = tmp_path / "ids.db"
        conn = sqlite3.connect(db)
        init_db(conn)
        a = _resolve_token_int_id(conn, "long-token-id-A")
        b = _resolve_token_int_id(conn, "long-token-id-B")
        c = _resolve_token_int_id(conn, "long-token-id-C")
        assert a == 0
        assert b == 1
        assert c == 2

    def test_returns_existing_id(self, tmp_path):
        db = tmp_path / "ids.db"
        conn = sqlite3.connect(db)
        init_db(conn)
        a1 = _resolve_token_int_id(conn, "tokA")
        a2 = _resolve_token_int_id(conn, "tokA")
        assert a1 == a2
