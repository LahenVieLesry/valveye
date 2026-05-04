from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from valveye.domain import Subscription


class SubscriptionRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    game_query TEXT NOT NULL,
                    window TEXT NOT NULL DEFAULT 'all',
                    region TEXT NOT NULL DEFAULT 'US',
                    currency TEXT NOT NULL DEFAULT 'USD',
                    channels_json TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_notified_low REAL,
                    last_notified_at TEXT
                )
                """
            )
            conn.commit()

    def add(
        self,
        user_id: str,
        game_query: str,
        window: str,
        region: str,
        currency: str,
        channels: list[dict],
    ) -> tuple[int, bool]:
        with self._lock:
            existing = self._find_active_duplicate(
                user_id=user_id,
                game_query=game_query,
                window=window,
                region=region,
                currency=currency,
                channels=channels,
            )
            if existing is not None:
                return existing.id, False

            conn = self._get_conn()
            cur = conn.execute(
                """
                INSERT INTO subscriptions (user_id, game_query, window, region, currency, channels_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    game_query,
                    window,
                    region,
                    currency,
                    self._channels_to_json(channels),
                ),
            )
            conn.commit()
            return int(cur.lastrowid), True

    def _find_active_duplicate(
        self,
        user_id: str,
        game_query: str,
        window: str,
        region: str,
        currency: str,
        channels: list[dict],
    ) -> Subscription | None:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE active=1 AND user_id=? AND game_query=? AND window=? AND region=? AND currency=?
            ORDER BY id DESC
            """,
            (user_id, game_query, window, region, currency),
        ).fetchall()

        normalized_channels = self._channels_to_json(channels)
        for row in rows:
            if self._channels_to_json(json.loads(row["channels_json"])) == normalized_channels:
                return self._to_sub(row)
        return None

    def list_active(self) -> list[Subscription]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute("SELECT * FROM subscriptions WHERE active=1 ORDER BY id DESC").fetchall()
        return [self._to_sub(row) for row in rows]

    def deactivate(self, sub_id: int) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute("UPDATE subscriptions SET active=0 WHERE id=?", (sub_id,))
            conn.commit()

    def mark_notified(self, sub_id: int, low_price: float) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE subscriptions SET last_notified_low=?, last_notified_at=? WHERE id=?",
                (low_price, now, sub_id),
            )
            conn.commit()

    @staticmethod
    def _channels_to_json(channels: list[dict]) -> str:
        return json.dumps(channels, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _to_sub(row: sqlite3.Row) -> Subscription:
        raw_ts = row["last_notified_at"]
        parsed_ts = datetime.fromisoformat(raw_ts) if raw_ts else None
        return Subscription(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            game_query=str(row["game_query"]),
            window=str(row["window"]),
            region=str(row["region"]),
            currency=str(row["currency"]),
            channels=json.loads(row["channels_json"]),
            active=bool(row["active"]),
            last_notified_low=(float(row["last_notified_low"]) if row["last_notified_low"] is not None else None),
            last_notified_at=parsed_ts,
        )
