from __future__ import annotations

from pydantic import BaseModel, Field


class RoutingTask(BaseModel):
    """A single task in the supervisor's routing plan."""

    agent: str = Field(
        description="Target agent: price, info, recommend, or subs",
        pattern=r"^(price|info|recommend|subs)$",
    )
    query: str = Field(description="Refined query for the agent to execute")


class SupervisorRouting(BaseModel):
    """Structured output from the supervisor routing LLM."""

    reasoning: str = Field(description="简要分析用户意图")
    tasks: list[RoutingTask] = Field(
        min_length=0,
        description="任务列表，直接回复时为空数组",
    )
    direct_response: str | None = Field(
        default=None,
        description="直接回复内容，非空时表示无需转发给任何 agent",
    )
