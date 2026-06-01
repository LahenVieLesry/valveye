from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


@dataclass(slots=True)
class TraceEvent:
    trace_id: str
    node: str
    event: str
    data: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0


class StructuredLogger:
    def __init__(self, logger_name: str = "valveye") -> None:
        self._logger = logging.getLogger(logger_name)

    def emit(self, event: TraceEvent) -> None:
        self._logger.info(
            "trace_id=%s node=%s event=%s latency_ms=%.1f data=%s",
            event.trace_id,
            event.node,
            event.event,
            event.latency_ms,
            event.data,
        )

    def emit_warning(self, event: TraceEvent) -> None:
        self._logger.warning(
            "trace_id=%s node=%s event=%s latency_ms=%.1f data=%s",
            event.trace_id,
            event.node,
            event.event,
            event.latency_ms,
            event.data,
        )


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


class AuditLogger:
    """Persistent audit log for tool calls and results.

    Reuses the SQLite + WAL + Lock pattern from SubscriptionRepository.
    """

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = Path.home() / ".valveye" / "audit.db"
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
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trace_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        inputs_json TEXT,
                        output_json TEXT,
                        latency_ms REAL,
                        user_id TEXT,
                        thread_id TEXT,
                        error_msg TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_log(trace_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool_name)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp)"
                )
                conn.commit()
            finally:
                conn.close()

    def log(
        self,
        trace_id: str,
        tool_name: str,
        inputs: dict[str, Any],
        output: str,
        latency_ms: float = 0.0,
        user_id: str = "",
        thread_id: str = "",
        error_msg: str = "",
    ) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    INSERT INTO audit_log
                    (trace_id, timestamp, tool_name, inputs_json, output_json, latency_ms, user_id, thread_id, error_msg)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trace_id,
                        datetime.now(tz=timezone.utc).isoformat(),
                        tool_name,
                        json.dumps(inputs, ensure_ascii=False) if inputs else None,
                        output[:4000] if output else None,
                        latency_ms,
                        user_id or "",
                        thread_id or "",
                        error_msg[:2000] if error_msg else None,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def query(
        self,
        trace_id: str | None = None,
        tool_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                conditions: list[str] = []
                params: list[Any] = []
                if trace_id:
                    conditions.append("trace_id = ?")
                    params.append(trace_id)
                if tool_name:
                    conditions.append("tool_name = ?")
                    params.append(tool_name)
                where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
                sql = f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                cursor = conn.execute(sql, params)
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            try:
                total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
                errors = conn.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE error_msg != ''"
                ).fetchone()[0]
                top_tools = conn.execute(
                    """
                    SELECT tool_name, COUNT(*) as cnt FROM audit_log
                    GROUP BY tool_name ORDER BY cnt DESC LIMIT 5
                    """
                ).fetchall()
                return {
                    "total_records": total,
                    "error_count": errors,
                    "top_tools": [dict(r) for r in top_tools],
                }
            finally:
                conn.close()


class Timer:
    """Context manager that measures wall-clock time in milliseconds."""

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000

    elapsed_ms: float = 0.0
