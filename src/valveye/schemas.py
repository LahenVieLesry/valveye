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

    reasoning: str = Field(description="Brief analysis of user intent")
    tasks: list[RoutingTask] = Field(
        min_length=1,
        description="Ordered list of tasks to execute",
    )
