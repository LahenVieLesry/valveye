from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ToolErrorCode(str, Enum):
    """Structured error codes for tool failures."""

    GAME_NOT_FOUND = "GAME_NOT_FOUND"
    PRICE_SOURCE_UNAVAILABLE = "PRICE_SOURCE_UNAVAILABLE"
    INVALID_INPUT = "INVALID_INPUT"
    RATE_LIMITED = "RATE_LIMITED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN = "UNKNOWN"


class ToolError(Exception):
    """Custom exception for tool failures with structured error codes."""

    def __init__(
        self,
        code: ToolErrorCode,
        message: str,
        suggestion: str = "",
        *args: object,
    ) -> None:
        self.code = code
        self.message = message
        self.suggestion = suggestion
        super().__init__(message, *args)

    def to_dict(self) -> dict:
        return {
            "error": True,
            "code": self.code.value,
            "message": self.message,
            "suggestion": self.suggestion,
        }

    def __str__(self) -> str:
        parts = [f"❌ {self.message}", f"错误码: {self.code.value}"]
        if self.suggestion:
            parts.append(f"建议: {self.suggestion}")
        return "\n".join(parts)


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
