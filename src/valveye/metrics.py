from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field


@dataclass(slots=True)
class TurnMetrics:
    trace_id: str
    query: str
    routed_agents: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    tool_latencies_ms: dict[str, list[float]] = field(default_factory=lambda: {})
    total_latency_ms: float = 0.0
    success: bool = True
    error: str | None = None
    _start_time: float = field(default_factory=time.perf_counter, repr=False)

    def finish(self) -> None:
        self.total_latency_ms = (time.perf_counter() - self._start_time) * 1000


class MetricsCollector:
    def __init__(self) -> None:
        self._turns: list[TurnMetrics] = []

    def start_turn(self, trace_id: str, query: str) -> TurnMetrics:
        m = TurnMetrics(trace_id=trace_id, query=query)
        return m

    def record_routing(self, metrics: TurnMetrics, agent: str) -> None:
        metrics.routed_agents.append(agent)

    def record_tool_call(self, metrics: TurnMetrics, tool: str, latency_ms: float) -> None:
        metrics.tools_called.append(tool)
        metrics.tool_latencies_ms.setdefault(tool, []).append(latency_ms)

    def end_turn(self, metrics: TurnMetrics) -> None:
        metrics.finish()
        self._turns.append(metrics)

    @property
    def turns(self) -> list[TurnMetrics]:
        return self._turns

    def summary(self) -> dict:
        if not self._turns:
            return {"total_turns": 0}

        total = len(self._turns)
        latencies = [t.total_latency_ms for t in self._turns]
        agent_counts: Counter[str] = Counter()
        tool_counts: Counter[str] = Counter()
        errors = 0

        for t in self._turns:
            for a in t.routed_agents:
                agent_counts[a] += 1
            for tool in t.tools_called:
                tool_counts[tool] += 1
            if not t.success:
                errors += 1

        return {
            "total_turns": total,
            "avg_latency_ms": sum(latencies) / total,
            "p50_latency_ms": sorted(latencies)[total // 2],
            "error_count": errors,
            "error_rate": errors / total,
            "routing_distribution": dict(agent_counts),
            "tool_usage": dict(tool_counts),
        }
