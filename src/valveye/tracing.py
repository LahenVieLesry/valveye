from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
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


class Timer:
    """Context manager that measures wall-clock time in milliseconds."""

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000

    elapsed_ms: float = 0.0
