"""
Warstwa SQLite - cała "pamięć" bota.

Po restarcie bot musi wiedzieć:
  - jakie eventy/rynki monitorował (żeby się ponownie zasubskrybować)
  - kiedy ostatnio wysłał jaki alert (żeby nie złamać cooldownu)
  - jaki był ostatni order book per token (żeby wykrywać delty)

Wszystko trzymamy w jednym pliku `bot_state.db` (SQLite). Nie używamy ORM-a
typu SQLAlchemy, żeby kod był prosty i nie miał ukrytych "magii" - czysty
sqlite3 ze standardowej biblioteki Pythona w zupełności wystarczy.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
-- Eventy aktualnie monitorowane przez bota.
-- 'source' mówi czy to z auto_monitor_series, czy dodane ręcznie.
CREATE TABLE IF NOT EXISTS events (
    slug          TEXT PRIMARY KEY,
    event_id      TEXT,
    title         TEXT,
    end_date      TEXT,
    source        TEXT NOT NULL,         -- 'auto' / 'manual'
    series_prefix TEXT,                  -- np. 'bitcoin-above' (gdy auto)
    added_at      INTEGER NOT NULL
);

-- Rynki w obrębie eventów. Jeden event ma N rynków.
-- token_yes_id / token_no_id - identyfikatory tokenów do subskrypcji WebSocket.
CREATE TABLE IF NOT EXISTS markets (
    condition_id  TEXT PRIMARY KEY,
    event_slug    TEXT NOT NULL,
    question      TEXT,
    token_yes_id  TEXT,
    token_no_id   TEXT,
    end_date      TEXT,
    is_monitored  INTEGER NOT NULL DEFAULT 0,  -- 1 = blisko 99.9¢, subskrybujemy
    monitored_side TEXT,                       -- 'YES' / 'NO' / NULL
    added_at      INTEGER NOT NULL,
    FOREIGN KEY (event_slug) REFERENCES events(slug) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_slug);
CREATE INDEX IF NOT EXISTS idx_markets_monitored ON markets(is_monitored);

-- Historia wysłanych alertów - do cooldownu i deduplikacji.
-- Klucz: (alert_type, token_id) - jeden cooldown per typ per token.
CREATE TABLE IF NOT EXISTS sent_alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type   TEXT NOT NULL,             -- 'A' / 'B' / 'C' / 'D'
    token_id     TEXT NOT NULL,
    condition_id TEXT,
    sent_at      INTEGER NOT NULL,          -- unix timestamp w sekundach
    payload      TEXT                       -- JSON z danymi alertu (debug)
);

CREATE INDEX IF NOT EXISTS idx_alerts_lookup
    ON sent_alerts(alert_type, token_id, sent_at DESC);

-- Ostatni znany stan order booka per token (snapshot).
-- Trzymamy bids/asks jako JSON - pełny order book, nie tylko nasze poziomy 99.x¢.
CREATE TABLE IF NOT EXISTS order_book_snapshots (
    token_id     TEXT PRIMARY KEY,
    bids_json    TEXT NOT NULL,              -- JSON: [{"price":"0.998","size":"1234"}, ...]
    asks_json    TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);

-- Globalne statystyki / stan bota (klucz-wartość).
CREATE TABLE IF NOT EXISTS kv_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# Domyślnie alerty A nie commitują info o "ostatniej sumie" do osobnej tabeli -
# trzymamy ją w pamięci procesu (Detector). Po restarcie zaczynamy od zera dla
# A - to OK, bo cooldown w sent_alerts i tak działa.


class Database:
    """Cienka otoczka na sqlite3 - zarządza połączeniem i schematem."""

    def __init__(self, path: str | Path = "bot_state.db"):
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL = Write-Ahead-Log: lepsze dla równoczesnych zapisów/odczytów
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._tx() as cur:
            cur.executescript(SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        """Mała transakcja z auto-commit / rollback przy błędzie."""
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    # -------------------------------------------------------------------------
    # Eventy
    # -------------------------------------------------------------------------

    def upsert_event(
        self,
        slug: str,
        event_id: str | None,
        title: str | None,
        end_date: str | None,
        source: str,
        series_prefix: str | None = None,
    ) -> None:
        """Dodaje lub aktualizuje event (np. zmiana end_date)."""
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO events (slug, event_id, title, end_date, source,
                                    series_prefix, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    event_id = excluded.event_id,
                    title    = excluded.title,
                    end_date = excluded.end_date,
                    source   = excluded.source,
                    series_prefix = excluded.series_prefix
                """,
                (slug, event_id, title, end_date, source, series_prefix, int(time.time())),
            )

    def remove_event(self, slug: str) -> int:
        """Usuwa event i jego rynki (kaskadowo). Zwraca ile usunął."""
        with self._tx() as cur:
            cur.execute("DELETE FROM events WHERE slug = ?", (slug,))
            return cur.rowcount

    def list_events(self) -> list[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM events ORDER BY added_at DESC")
        return cur.fetchall()

    def get_event(self, slug: str) -> sqlite3.Row | None:
        cur = self._conn.execute("SELECT * FROM events WHERE slug = ?", (slug,))
        return cur.fetchone()

    # -------------------------------------------------------------------------
    # Rynki
    # -------------------------------------------------------------------------

    def upsert_market(
        self,
        condition_id: str,
        event_slug: str,
        question: str | None,
        token_yes_id: str | None,
        token_no_id: str | None,
        end_date: str | None,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO markets (condition_id, event_slug, question,
                                     token_yes_id, token_no_id, end_date,
                                     added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_id) DO UPDATE SET
                    event_slug   = excluded.event_slug,
                    question     = excluded.question,
                    token_yes_id = excluded.token_yes_id,
                    token_no_id  = excluded.token_no_id,
                    end_date     = excluded.end_date
                """,
                (
                    condition_id, event_slug, question,
                    token_yes_id, token_no_id, end_date, int(time.time()),
                ),
            )

    def set_market_monitored(
        self, condition_id: str, monitored: bool, side: str | None
    ) -> None:
        """Oznacza rynek jako monitorowany (blisko 99.9¢) i zapisuje stronę."""
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE markets
                SET is_monitored = ?, monitored_side = ?
                WHERE condition_id = ?
                """,
                (1 if monitored else 0, side, condition_id),
            )

    def list_markets_for_event(self, event_slug: str) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM markets WHERE event_slug = ?", (event_slug,)
        )
        return cur.fetchall()

    def list_monitored_markets(self) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM markets WHERE is_monitored = 1"
        )
        return cur.fetchall()

    def list_all_markets(self) -> list[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM markets")
        return cur.fetchall()

    def get_market_by_token(self, token_id: str) -> sqlite3.Row | None:
        cur = self._conn.execute(
            """
            SELECT * FROM markets
            WHERE token_yes_id = ? OR token_no_id = ?
            LIMIT 1
            """,
            (token_id, token_id),
        )
        return cur.fetchone()

    # -------------------------------------------------------------------------
    # Alerty (cooldown)
    # -------------------------------------------------------------------------

    def record_alert(
        self,
        alert_type: str,
        token_id: str,
        condition_id: str | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO sent_alerts (alert_type, token_id, condition_id,
                                         sent_at, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    alert_type, token_id, condition_id, int(time.time()),
                    json.dumps(payload) if payload else None,
                ),
            )

    def last_alert_at(self, alert_type: str, token_id: str) -> int | None:
        """Unix timestamp ostatniego alertu danego typu dla tokena, albo None."""
        cur = self._conn.execute(
            """
            SELECT sent_at FROM sent_alerts
            WHERE alert_type = ? AND token_id = ?
            ORDER BY sent_at DESC LIMIT 1
            """,
            (alert_type, token_id),
        )
        row = cur.fetchone()
        return row["sent_at"] if row else None

    def count_alerts_since(self, since_ts: int) -> dict[str, int]:
        """Zwraca słownik {alert_type: count} od podanego timestampu."""
        cur = self._conn.execute(
            """
            SELECT alert_type, COUNT(*) AS c FROM sent_alerts
            WHERE sent_at >= ?
            GROUP BY alert_type
            """,
            (since_ts,),
        )
        return {row["alert_type"]: row["c"] for row in cur.fetchall()}

    def total_alerts_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM sent_alerts")
        return cur.fetchone()["c"]

    # -------------------------------------------------------------------------
    # Order book snapshots
    # -------------------------------------------------------------------------

    def save_order_book(
        self,
        token_id: str,
        bids: list[dict[str, str]],
        asks: list[dict[str, str]],
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO order_book_snapshots (token_id, bids_json, asks_json,
                                                   updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(token_id) DO UPDATE SET
                    bids_json  = excluded.bids_json,
                    asks_json  = excluded.asks_json,
                    updated_at = excluded.updated_at
                """,
                (token_id, json.dumps(bids), json.dumps(asks), int(time.time())),
            )

    def load_order_book(self, token_id: str) -> tuple[list[dict], list[dict]] | None:
        cur = self._conn.execute(
            "SELECT bids_json, asks_json FROM order_book_snapshots WHERE token_id = ?",
            (token_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return json.loads(row["bids_json"]), json.loads(row["asks_json"])

    # -------------------------------------------------------------------------
    # Stan ogólny (kv)
    # -------------------------------------------------------------------------

    def kv_get(self, key: str) -> str | None:
        cur = self._conn.execute("SELECT value FROM kv_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO kv_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
