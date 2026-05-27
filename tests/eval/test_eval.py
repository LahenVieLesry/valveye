"""Automated evaluation suite for Valveye agent routing and tool calls.

Tests in this module require actual LLM calls and network access.
Run with: RUN_INTEGRATION_TESTS=1 pytest tests/eval/ -v

Without the env var, all tests are skipped.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from langchain_core.messages import HumanMessage, SystemMessage

# Skip all tests in this module unless explicitly enabled
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_INTEGRATION_TESTS"),
    reason="Set RUN_INTEGRATION_TESTS=1 to run integration eval tests",
)

BENCHMARK_PATH = Path(__file__).parent / "benchmark.json"


def _load_benchmark() -> list[dict]:
    return json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))


BENCHMARK = _load_benchmark()


# ── Intent Routing Accuracy ─────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.parametrize("case", BENCHMARK, ids=lambda c: c["id"])
@pytest.mark.asyncio
async def test_intent_routing_accuracy(case: dict):
    """Verify that the supervisor routes to the expected agent."""
    from valveye.agent import build_llm
    from valveye.prompts import SUPERVISOR_PROMPT

    llm = build_llm()
    query = case["query"]

    # Simulate the same context-building logic as route_supervisor
    messages = [HumanMessage(content=query)]
    context_parts = []
    for msg in messages:
        raw = msg.content if hasattr(msg, "content") else str(msg)
        text = raw if isinstance(raw, str) else str(raw)
        if isinstance(msg, HumanMessage):
            context_parts.append(f"[用户]: {text}")
    content = "\n".join(context_parts)

    result = await llm.ainvoke([
        SystemMessage(content=SUPERVISOR_PROMPT),
        HumanMessage(content=content),
    ])
    raw = result.content if isinstance(result.content, str) else str(result.content)

    # Parse routing result
    cleaned = re.sub(r'```(?:json)?\s*', '', raw).strip('` \n')
    task_queue: list[dict] = []
    try:
        json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            for t in data.get("tasks", []):
                agent_name = t.get("agent", "info")
                if agent_name in ("price", "info", "recommend", "subs"):
                    task_queue.append({"agent": agent_name, "query": t.get("query", "")})
    except (ValueError, TypeError):
        pass

    if not task_queue:
        task_queue = [{"agent": "info", "query": content}]

    actual_agent = task_queue[0]["agent"]
    expected = case["expected_agent"]

    assert actual_agent == expected, (
        f"Routing mismatch for '{query}': expected={expected}, got={actual_agent}\n"
        f"Full task queue: {task_queue}"
    )


# ── Response Quality ────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.parametrize(
    "case",
    [c for c in BENCHMARK if c["category"] in ("price_query", "price_comparison")],
    ids=lambda c: c["id"],
)
@pytest.mark.asyncio
async def test_price_response_has_currency(case: dict):
    """Price responses should mention currency symbols or codes."""
    from valveye.agent import build_llm

    llm = build_llm()
    result = await llm.ainvoke([
        SystemMessage(content="You are a price query assistant. Reply concisely."),
        HumanMessage(content=case["query"]),
    ])
    response = result.content if isinstance(result.content, str) else str(result.content)

    # Skip if the response indicates the game wasn't found
    if "未找到" in response or "无法" in response:
        pytest.skip("Game not found, cannot validate currency")

    # Check for currency indicators
    currency_indicators = [
        "$", "€", "£", "¥", "₽", "₩",
        "USD", "CNY", "EUR", "JPY", "GBP", "RUB", "KRW",
        "元", "美元", "日元", "欧元",
    ]
    has_currency = any(ind in response for ind in currency_indicators)
    assert has_currency, f"Price response lacks currency indicator: {response[:200]}"


@pytest.mark.integration
@pytest.mark.parametrize(
    "case",
    [c for c in BENCHMARK if c["category"] == "recommendation"],
    ids=lambda c: c["id"],
)
@pytest.mark.asyncio
async def test_recommendation_response_has_games(case: dict):
    """Recommendation responses should mention at least 2 game names."""
    from valveye.agent import build_llm

    llm = build_llm()
    result = await llm.ainvoke([
        SystemMessage(content="You are a game recommendation assistant. List game names."),
        HumanMessage(content=case["query"]),
    ])
    response = result.content if isinstance(result.content, str) else str(result.content)

    # Count capitalized multi-word sequences that look like game titles
    game_like = re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+', response)
    unique_games = set(game_like)

    assert len(unique_games) >= 2, (
        f"Recommendation should mention ≥2 games, found {len(unique_games)}: {unique_games}\n"
        f"Response: {response[:300]}"
    )
