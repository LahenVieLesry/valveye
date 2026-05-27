from __future__ import annotations

import json as _json
import operator
import re
from typing import Annotated, Any, AsyncIterator, TypedDict

import aiosqlite
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from valveye.config import settings
from valveye.memory import VikingMemory
from valveye.prompts import (
    INFO_AGENT_PROMPT,
    PRICE_AGENT_PROMPT,
    RECOMMEND_AGENT_PROMPT,
    SUBS_AGENT_PROMPT,
    SUPERVISOR_PROMPT,
)


# ── State schema ───────────────────────────────────────────────────────────

class SupervisorState(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    active_agent: str
    task_queue: list[dict]
    current_task_index: int
    accumulated_context: dict[str, str]
    iteration_count: int
    original_query: str
    handoff_pending: bool


# ── LLM builder ───────────────────────────────────────────────────────────

def build_llm() -> ChatOpenAI:
    kwargs: dict = {
        "model": settings.openai_model,
        "api_key": settings.openai_api_key,
        "temperature": 0.7,
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return ChatOpenAI(**kwargs)


# ── Multi-agent graph builder ─────────────────────────────────────────────

async def build_multi_agent(
    tool_groups: dict[str, list],
    get_game_details_fn,
) -> Any:
    """Build the Supervisor + Specialist multi-agent graph."""
    llm = build_llm()

    # --- Build specialist agents as compiled subgraphs (share one LLM) ---
    price_agent = create_agent(
        model=llm,
        tools=tool_groups["price"],
        system_prompt=PRICE_AGENT_PROMPT,
        name="price_agent",
    )
    info_agent = create_agent(
        model=llm,
        tools=tool_groups["info"],
        system_prompt=INFO_AGENT_PROMPT,
        name="info_agent",
    )
    recommend_agent = create_agent(
        model=llm,
        tools=tool_groups["recommend"],
        system_prompt=RECOMMEND_AGENT_PROMPT,
        name="recommend_agent",
    )
    subs_agent = create_agent(
        model=llm,
        tools=tool_groups["subs"],
        system_prompt=SUBS_AGENT_PROMPT,
        name="subs_agent",
    )

    # --- Supervisor routing LLM (plain, no structured output) ---
    router_llm = llm

    # --- Graph nodes ---

    async def route_supervisor(state: SupervisorState) -> dict:
        """LLM-based intent decomposition → ordered task queue."""
        messages = state["messages"]
        if not messages:
            return {"active_agent": "info", "task_queue": [{"agent": "info", "query": ""}], "current_task_index": 0}

        # Build context from last 3 messages for follow-up awareness
        context_parts = []
        for msg in messages[-3:]:
            raw = msg.content if hasattr(msg, "content") else str(msg)
            text = raw if isinstance(raw, str) else str(raw)
            if isinstance(msg, HumanMessage):
                context_parts.append(f"[用户]: {text}")
            elif isinstance(msg, AIMessage):
                # Only include short preview to avoid token bloat
                preview = text[:200] + ("…" if len(text) > 200 else "")
                context_parts.append(f"[助手]: {preview}")
        content = "\n".join(context_parts) if context_parts else str(messages[-1].content)

        result = await router_llm.ainvoke([
            SystemMessage(content=SUPERVISOR_PROMPT),
            HumanMessage(content=content),
        ])
        raw = result.content if isinstance(result.content, str) else str(result.content)

        # Parse JSON: {"tasks": [{"agent": "price", "query": "..."}, ...]}
        # Strip markdown code fences if present
        cleaned = re.sub(r'```(?:json)?\s*', '', raw).strip('` \n')
        task_queue: list[dict] = []
        try:
            json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if json_match:
                data = _json.loads(json_match.group())
                tasks = data.get("tasks", [])
                for t in tasks:
                    agent_name = t.get("agent", "info")
                    if agent_name in ("price", "info", "recommend", "subs"):
                        task_queue.append({"agent": agent_name, "query": t.get("query", content)})
        except (ValueError, TypeError):
            pass

        # Fallback: single info task
        if not task_queue:
            task_queue = [{"agent": "info", "query": content}]

        import logging
        logging.getLogger(__name__).info(
            "route_supervisor: query=%r → tasks=%s", content[:50],
            [(t["agent"], t["query"][:30]) for t in task_queue],
        )

        return {
            "active_agent": task_queue[0]["agent"],
            "task_queue": task_queue,
            "current_task_index": 0,
            "original_query": content,
        }

    def route_to_agent(state: SupervisorState) -> str:
        """Read current task from queue, return agent node name."""
        queue = state.get("task_queue", [])
        idx = state.get("current_task_index", 0)
        if queue and idx < len(queue):
            agent = queue[idx]["agent"]
        else:
            agent = state.get("active_agent", "info")
        return f"{agent}_agent"

    def pre_process_node(state: SupervisorState) -> dict:
        """Inject current task query as a HumanMessage for the agent."""
        queue = state.get("task_queue", [])
        idx = state.get("current_task_index", 0)
        if queue and idx < len(queue):
            task = queue[idx]
            # After handoff, game details are already in messages — skip re-injection
            if state.get("handoff_pending"):
                return {"active_agent": task["agent"], "handoff_pending": False}
            return {
                "messages": [HumanMessage(content=task["query"])],
                "active_agent": task["agent"],
            }
        return {}

    async def post_process_node(state: SupervisorState) -> dict:
        """Detect handoff requests, advance task queue, or finish."""
        messages = state["messages"]
        accumulated = dict(state.get("accumulated_context", {}))
        iteration = state.get("iteration_count", 0) + 1
        queue = state.get("task_queue", [])
        idx = state.get("current_task_index", 0)

        # Detect handoff by checking AIMessage.tool_calls for request_game_details
        for msg in reversed(messages[-5:]):
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.get("name") == "request_game_details":
                        games_str = tc.get("args", {}).get("games", "")
                        games = [g.strip() for g in games_str.split(",") if g.strip()]
                        if not games:
                            break

                        details_parts = []
                        for game in games:
                            cache_key = f"game_details:{game}"
                            error_key = f"error:game_details:{game}"
                            # Return cached success, but retry on cached errors
                            if cache_key in accumulated and error_key not in accumulated:
                                details_parts.append(f"--- {game} ---\n{accumulated[cache_key]}")
                            else:
                                try:
                                    result = await get_game_details_fn.ainvoke({"game": game})
                                    accumulated[cache_key] = result
                                    accumulated.pop(error_key, None)
                                except Exception as e:
                                    result = f"获取失败: {e}"
                                    accumulated[error_key] = result
                                details_parts.append(f"--- {game} ---\n{result}")

                        context_msg = AIMessage(
                            content="[系统注入的游戏详情]\n\n" + "\n\n".join(details_parts),
                        )
                        return {
                            "messages": [context_msg],
                            "accumulated_context": accumulated,
                            "active_agent": "recommend",
                            "iteration_count": iteration,
                            "handoff_pending": True,
                        }
                break

        # No handoff — advance to next task in queue
        next_idx = idx + 1
        if next_idx < len(queue):
            return {
                "current_task_index": next_idx,
                "active_agent": queue[next_idx]["agent"],
                "accumulated_context": accumulated,
                "iteration_count": iteration,
            }

        # All tasks done
        return {
            "iteration_count": iteration,
            "active_agent": "finish",
            "accumulated_context": accumulated,
        }

    def route_after_post_process(state: SupervisorState) -> str:
        """Decide next step after post-processing."""
        if state.get("iteration_count", 0) >= 20:
            return "__end__"

        active = state.get("active_agent", "finish")
        if active == "finish":
            return "__end__"

        # Handoff: route to handoff target (e.g. recommend → info for details)
        return f"{active}_agent"

    # --- Build graph ---
    builder = StateGraph(SupervisorState)

    # Routing node
    builder.add_node("route_supervisor", route_supervisor)
    builder.add_node("pre_process", pre_process_node)

    # Specialist agent nodes (compiled subgraphs)
    builder.add_node("price_agent", price_agent)
    builder.add_node("info_agent", info_agent)
    builder.add_node("recommend_agent", recommend_agent)
    builder.add_node("subs_agent", subs_agent)

    # Post-processing
    builder.add_node("post_process", post_process_node)

    # --- Edges ---
    builder.add_edge(START, "route_supervisor")
    builder.add_conditional_edges("route_supervisor", route_to_agent, {
        "price_agent": "pre_process",
        "info_agent": "pre_process",
        "recommend_agent": "pre_process",
        "subs_agent": "pre_process",
    })

    # After pre_process, route to the correct agent
    builder.add_conditional_edges("pre_process", route_to_agent, {
        "price_agent": "price_agent",
        "info_agent": "info_agent",
        "recommend_agent": "recommend_agent",
        "subs_agent": "subs_agent",
    })

    # After each agent → post_process
    builder.add_edge("price_agent", "post_process")
    builder.add_edge("info_agent", "post_process")
    builder.add_edge("recommend_agent", "post_process")
    builder.add_edge("subs_agent", "post_process")

    # After post_process → route or end
    builder.add_conditional_edges("post_process", route_after_post_process, {
        "price_agent": "pre_process",
        "info_agent": "pre_process",
        "recommend_agent": "pre_process",
        "subs_agent": "pre_process",
        "__end__": END,
    })

    # --- Compile with checkpointer ---
    conn = await aiosqlite.connect(settings.chat_db_path)
    checkpointer = AsyncSqliteSaver(conn)
    return builder.compile(checkpointer=checkpointer), conn


# ── Streaming turn (structured events) ────────────────────────────────────

_AGENT_NAMES = {"price_agent", "info_agent", "recommend_agent", "subs_agent"}


async def _enhance_with_memory(message: str, thread_id: str, memory: VikingMemory | None) -> str:
    """Enhance message with memory recall context."""
    if not memory:
        return message
    ctx = await memory.recall(query=message, session_id=thread_id)
    if ctx:
        return f"[相关记忆]\n{ctx}\n\n[用户消息]\n{message}"
    return message


async def stream_turn(
    agent, message: str, thread_id: str, memory: VikingMemory | None = None,
) -> AsyncIterator[dict]:
    """Execute one turn, yielding structured event dicts.

    Event types:
      {"type": "agent_start", "agent": "price_agent"}
      {"type": "handoff", "from": "recommend_agent", "to": "info_agent"}
      {"type": "token", "content": "..."}
      {"type": "tool_start", "name": "...", "agent": "...", "inputs": {...}}
      {"type": "tool_end", "name": "...", "output": "..."}
      {"type": "agent_end", "agent": "price_agent"}
    """
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 20}

    enhanced = await _enhance_with_memory(message, thread_id, memory)

    current_agent = ""
    collected: list[str] = []

    async for event in agent.astream_events(
        {"messages": [HumanMessage(content=enhanced)]},
        config=config,
        version="v2",
    ):
        kind = event.get("event")
        name = event.get("name", "")

        # Detect agent transitions
        if kind == "on_chain_start" and name in _AGENT_NAMES:
            if current_agent and current_agent != name:
                yield {"type": "handoff", "from": current_agent, "to": name}
            current_agent = name
            yield {"type": "agent_start", "agent": name}

        elif kind == "on_chat_model_stream" and current_agent:
            # Only capture tokens from specialist agent subgraphs,
            # not from the supervisor's internal routing LLM call
            chunk = event["data"]["chunk"]
            if chunk.content:
                text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                collected.append(text)
                yield {"type": "token", "content": text}

        elif kind == "on_tool_start" and current_agent:
            tool_name = event.get("name", "unknown")
            inputs = event.get("data", {}).get("input", {})
            yield {"type": "tool_start", "name": tool_name, "agent": current_agent, "inputs": inputs}

        elif kind == "on_tool_end" and current_agent:
            output_obj = event.get("data", {}).get("output", "")
            if hasattr(output_obj, "content"):
                output = str(output_obj.content)
            else:
                output = str(output_obj)
            if len(output) > 500:
                output = output[:500] + "…"
            yield {"type": "tool_end", "name": event.get("name", ""), "output": output}

        elif kind == "on_tool_error" and current_agent:
            error = str(event.get("data", {}).get("error", "unknown error"))
            yield {"type": "tool_end", "name": event.get("name", ""), "output": f"错误: {error}"}

        elif kind == "on_chain_end" and name in _AGENT_NAMES:
            yield {"type": "agent_end", "agent": name}
            current_agent = ""

    # Auto-Capture
    if memory and collected:
        full_response = "".join(collected)
        await memory.capture(thread_id, message, full_response)


# ── Non-streaming turn ────────────────────────────────────────────────────

async def run_single_turn(
    agent, message: str, thread_id: str, memory: VikingMemory | None = None,
) -> str:
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 20}

    enhanced = await _enhance_with_memory(message, thread_id, memory)

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=enhanced)]},
        config=config,
    )
    ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
    response = "(no response)"
    if ai_messages:
        content: Any = ai_messages[-1].content
        response = str(content) if not isinstance(content, str) else content

    if memory:
        await memory.capture(thread_id, message, response)

    return response
