"""Tests for parallel task aggregation logic."""

from typing import cast

from valveye.agent import SupervisorState, aggregate_results_node


def test_aggregate_empty_results():
    state = cast(SupervisorState, {"parallel_results": []})
    result = aggregate_results_node(state)
    assert result["active_agent"] == "finish"
    assert result["execution_mode"] == "aggregated"
    assert "未能获取任何结果" in result["messages"][0].content


def test_aggregate_single_result():
    state = cast(
        SupervisorState,
        {
            "parallel_results": [
                {"task_id": 0, "agent": "price", "query": "Game A", "response": "A: 100 CNY"},
            ]
        },
    )
    result = aggregate_results_node(state)
    content = result["messages"][0].content
    assert "**价格查询**" in content
    assert "A: 100 CNY" in content


def test_aggregate_same_agent_multiple_tasks():
    """Two tasks targeting the same agent should NOT overwrite each other."""
    state = cast(
        SupervisorState,
        {
            "parallel_results": [
                {"task_id": 0, "agent": "price", "query": "Game A", "response": "A: 100 CNY"},
                {"task_id": 1, "agent": "price", "query": "Game B", "response": "B: 200 CNY"},
                {"task_id": 2, "agent": "info", "query": "Game A", "response": "A is an RPG"},
            ]
        },
    )
    result = aggregate_results_node(state)
    content = result["messages"][0].content

    # All three responses must be present
    assert "A: 100 CNY" in content
    assert "B: 200 CNY" in content
    assert "A is an RPG" in content

    # Same-agent results should be numbered
    assert "价格查询 (1)" in content
    assert "价格查询 (2)" in content

    # Single-agent result should not be numbered
    assert "游戏信息 (1)" not in content
    assert "**游戏信息**" in content

    assert result["active_agent"] == "finish"


def test_aggregate_preserves_agent_order():
    """Results should be ordered price → info → recommend → subs regardless of task_id."""
    state = cast(
        SupervisorState,
        {
            "parallel_results": [
                {"task_id": 2, "agent": "subs", "query": "sub", "response": "sub ok"},
                {"task_id": 1, "agent": "info", "query": "info", "response": "info ok"},
                {"task_id": 0, "agent": "price", "query": "price", "response": "price ok"},
            ]
        },
    )
    result = aggregate_results_node(state)
    content = result["messages"][0].content

    parts = content.split("\n\n---\n\n")
    assert "价格查询" in parts[0]
    assert "游戏信息" in parts[1]
    assert "订阅管理" in parts[2]


def test_aggregate_sorts_same_agent_by_task_id():
    """Within the same agent group, results should be ordered by task_id."""
    state = cast(
        SupervisorState,
        {
            "parallel_results": [
                {"task_id": 2, "agent": "price", "query": "Z", "response": "Z result"},
                {"task_id": 0, "agent": "price", "query": "X", "response": "X result"},
                {"task_id": 1, "agent": "price", "query": "Y", "response": "Y result"},
            ]
        },
    )
    result = aggregate_results_node(state)
    content = result["messages"][0].content

    # Order in output should be X, Y, Z (by task_id 0, 1, 2)
    x_pos = content.index("X result")
    y_pos = content.index("Y result")
    z_pos = content.index("Z result")
    assert x_pos < y_pos < z_pos


def test_aggregate_single_no_query_hint():
    """Single result should not include query hint in the label."""
    state = cast(
        SupervisorState,
        {
            "parallel_results": [
                {
                    "task_id": 0,
                    "agent": "price",
                    "query": "Elden Ring 当前多少钱",
                    "response": "120 CNY",
                },
            ]
        },
    )
    result = aggregate_results_node(state)
    content = result["messages"][0].content

    # Single result: no numbering, no query hint
    assert "价格查询 (1)" not in content
    assert "Elden Ring" not in content
    assert "120 CNY" in content


def test_aggregate_query_hint_truncated():
    """Long queries should be truncated to 40 chars in the sub-label."""
    long_query = "a" * 50
    state = cast(
        SupervisorState,
        {
            "parallel_results": [
                {"task_id": 0, "agent": "price", "query": long_query, "response": "ok"},
                {"task_id": 1, "agent": "price", "query": long_query, "response": "ok2"},
            ]
        },
    )
    result = aggregate_results_node(state)
    content = result["messages"][0].content

    assert "价格查询 (1)" in content
    assert "价格查询 (2)" in content
    # The truncated query hint should not appear in full
    assert long_query not in content
