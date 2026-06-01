from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class UserProfileStore:
    """SQLite-backed store for user feedback on recommendations.

    Records like/dislike/dismiss actions and maintains per-tag weights
    for personalized ranking.
    """

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = Path.home() / ".valveye" / "user_profile.db"
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_feedback (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        game_title TEXT NOT NULL,
                        app_id INTEGER,
                        action TEXT NOT NULL,
                        tags_json TEXT,
                        timestamp TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_fb_user ON user_feedback(user_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_fb_game ON user_feedback(app_id)"
                )
                conn.commit()
            finally:
                conn.close()

    def record_feedback(
        self,
        user_id: str,
        game_title: str,
        app_id: int,
        action: str,  # "like", "dislike", "dismiss"
        tags: list[str],
    ) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                from datetime import datetime, timezone
                conn.execute(
                    """
                    INSERT INTO user_feedback
                    (user_id, game_title, app_id, action, tags_json, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        game_title,
                        app_id,
                        action,
                        json.dumps(tags, ensure_ascii=False),
                        datetime.now(tz=timezone.utc).isoformat(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def get_tag_weights(self, user_id: str) -> dict[str, float]:
        """Return accumulated tag weights for a user."""
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    "SELECT action, tags_json FROM user_feedback WHERE user_id = ?",
                    (user_id,),
                )
                rows = cursor.fetchall()
            finally:
                conn.close()

        weights: dict[str, float] = {}
        delta_map = {"like": 0.1, "dislike": -0.1, "dismiss": -0.05}
        for row in rows:
            action = row["action"]
            delta = delta_map.get(action, 0.0)
            try:
                tags = json.loads(row["tags_json"] or "[]")
            except (ValueError, TypeError):
                tags = []
            for tag in tags:
                weights[tag] = weights.get(tag, 0.0) + delta
        return weights

    def summary(self, user_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    """
                    SELECT action, COUNT(*) as cnt
                    FROM user_feedback
                    WHERE user_id = ?
                    GROUP BY action
                    """,
                    (user_id,),
                )
                counts = {row["action"]: row["cnt"] for row in cursor.fetchall()}
            finally:
                conn.close()
        weights = self.get_tag_weights(user_id)
        top_tags = sorted(weights.items(), key=lambda x: abs(x[1]), reverse=True)[:10]
        return {
            "feedback_count": sum(counts.values()),
            "counts": counts,
            "top_tags": top_tags,
        }
