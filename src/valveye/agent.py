from __future__ import annotations

import asyncio
import json as _json
import operator
import re
import warnings
from collections.abc import AsyncIterator
from typing import Annotated, Any, TypedDict

import aiosqlite
from langchain.agents import create_agent
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


from valveye.config import settings
from valveye.guardrails import ResponseValidator
from valveye.memory import VikingMemory
from valveye.metrics import MetricsCollector, TurnMetrics
from valveye.prompts import (
    INFO_AGENT_PROMPT,
    PRICE_AGENT_PROMPT,
    RECOMMEND_AGENT_PROMPT,
    SUBS_AGENT_PROMPT,
    SUPERVISOR_PROMPT,
)
from valveye.schemas import SupervisorRouting
from valveye.tracing import AuditLogger, StructuredLogger, Timer, TraceEvent, new_trace_id

_logger = StructuredLogger("valveye.agent")


# ── State schema ───────────────────────────────────────────────────────────

class SupervisorState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    active_agent: str
    task_queue: list[dict]
    current_task_index: int
    accumulated_context: dict[str, str]
    iteration_count: int
    original_query: str
    handoff_pending: bool
    trace_id: str
    supervisor_response: str
    context_summary: str
    # 功能2新增字段
    parallel_results: Annotated[list[dict], operator.add]
    execution_mode: str


# ── Kimi-compatible LLM wrapper ───────────────────────────────────────────

class KimiCompatibleChatOpenAI(ChatOpenAI):
    """ChatOpenAI wrapper that injects missing reasoning_content for Kimi.

    Kimi K2.5/K2.6 with thinking enabled requires ``reasoning_content`` on
    every assistant message that contains ``tool_calls``. LangChain's
    ``create_agent`` drops this field when replaying history, causing a 400
    error on multi-turn tool calls. This wrapper adds an empty string when
    the field is absent.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        for msg in payload.get("messages", []):
            if (
                msg.get("role") == "assistant"
                and msg.get("tool_calls")
                and "reasoning_content" not in msg
            ):
                msg["reasoning_content"] = ""
        return payload


# ── LLM builder ─────────────────────────────────────────────────────────--

def build_llm() -> ChatOpenAI:
    kwargs: dict = {
        "model": settings.openai_model,
        "api_key": settings.openai_api_key,
        "temperature": 0.7,
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    if settings.openai_user_agent:
        kwargs["default_headers"] = {"User-Agent": settings.openai_user_agent}

    is_kimi = "kimi" in settings.openai_model.lower() or (
        settings.openai_base_url
        and ("kimi" in settings.openai_base_url.lower() or "moonshot" in settings.openai_base_url.lower())
    )
    cls = KimiCompatibleChatOpenAI if is_kimi else ChatOpenAI
    return cls(**kwargs)


def aggregate_results_node(state: SupervisorState) -> dict:
    """合并所有并行分支的结果为单一 AIMessage。"""
    from collections import defaultdict

    results = state.get("parallel_results", [])
    if not results:
        return {
            "messages": [AIMessage(content="未能获取任何结果。")],
            "execution_mode": "aggregated",
            "active_agent": "finish",
        }

    # 按 agent 分组，组内按 task_id 排序
    agent_results: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        agent_results[r["agent"]].append(r)
    for agent_key in agent_results:
        agent_results[agent_key].sort(key=lambda x: x.get("task_id", -1))

    parts = []
    agent_order = ["price", "info", "recommend", "subs"]
    display_names = {
        "price": "价格查询",
        "info": "游戏信息",
        "recommend": "游戏推荐",
        "subs": "订阅管理",
    }

    for agent_key in agent_order:
        if agent_key not in agent_results:
            continue
        results_for_agent = agent_results[agent_key]
        label = display_names.get(agent_key, agent_key)

        if len(results_for_agent) == 1:
            parts.append(f"**{label}**\n{results_for_agent[0]['response']}")
        else:
            for i, r in enumerate(results_for_agent, 1):
                sub_label = f"{label} ({i})"
                query_hint = r.get("query", "")
                if query_hint:
                    sub_label += f" — {query_hint[:40]}"
                parts.append(f"**{sub_label}**\n{r['response']}")

    combined = "\n\n---\n\n".join(parts)
    return {
        "messages": [AIMessage(content=combined)],
        "execution_mode": "aggregated",
        "active_agent": "finish",
    }


# ── Multi-agent graph builder ─────────────────────────────────────────────

async def build_multi_agent(
    tool_groups: dict[str, list],
    get_game_details_fn,
    memory: VikingMemory | None = None,
    context_seed: str = "",
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

    def _keyword_fallback(query: str) -> str:
        """Keyword-based fallback when structured routing fails."""
        q = query.lower()
        if any(w in q for w in ("价格", "多少钱", "便宜", "price", "cost", "史低", "打折")):
            return "price"
        if any(w in q for w in ("推荐", "类似", "像", "recommend", "similar", "好玩")):
            return "recommend"
        if any(w in q for w in ("订阅", "提醒", "subscribe", "alert", "notify")):
            return "subs"
        return "info"

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

        # Try structured output first (Pydantic schema validation)
        task_queue: list[dict] = []
        direct_response: str | None = None
        if settings.use_structured_routing:
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message=".*Pydantic.*", category=UserWarning)
                    structured_router = router_llm.with_structured_output(SupervisorRouting)
                    routing: SupervisorRouting = await structured_router.ainvoke([
                        SystemMessage(content=SUPERVISOR_PROMPT),
                        HumanMessage(content=content),
                    ])
                # Check for direct response
                if routing.direct_response:
                    direct_response = routing.direct_response
                for i, t in enumerate(routing.tasks):
                    if t.agent in ("price", "info", "recommend", "subs"):
                        task_queue.append({
                            "agent": t.agent,
                            "query": t.query or content,
                            "depends_on": t.depends_on or [],
                            "task_id": i,
                        })
            except Exception:
                # Structured output not supported by model, fall through
                pass

        # Defence: some models (e.g. Kimi K2.6) may populate both tasks and
        # direct_response in structured output.  When tasks exist the intent
        # has been decomposed, so prefer specialist agents over direct reply.
        if task_queue and direct_response:
            direct_response = None

        # Fallback: regex-based JSON parsing
        if not task_queue and not direct_response:
            result = await router_llm.ainvoke([
                SystemMessage(content=SUPERVISOR_PROMPT),
                HumanMessage(content=content),
            ])
            raw = result.content if isinstance(result.content, str) else str(result.content)

            cleaned = re.sub(r'```(?:json)?\s*', '', raw).strip('` \n')
            try:
                json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
                if json_match:
                    data = _json.loads(json_match.group())
                    tasks = data.get("tasks", [])
                    for i, t in enumerate(tasks):
                        agent_name = t.get("agent", "info")
                        if agent_name in ("price", "info", "recommend", "subs"):
                            task_queue.append({
                                "agent": agent_name,
                                "query": t.get("query", content),
                                "depends_on": t.get("depends_on", []) or [],
                                "task_id": i,
                            })
            except (ValueError, TypeError):
                pass

        # Fallback: keyword-based routing
        if not task_queue and not direct_response:
            fallback_agent = _keyword_fallback(content)
            task_queue = [{"agent": fallback_agent, "query": content, "task_id": 0}]

        trace_id = state.get("trace_id", new_trace_id())
        _logger.emit(TraceEvent(
            trace_id=trace_id,
            node="route_supervisor",
            event="routing_decision",
            data={
                "query": content[:100],
                "tasks": [(t["agent"], t["query"][:50]) for t in task_queue],
                "direct": bool(direct_response),
            },
        ))

        # Direct response — no specialist agent needed
        if direct_response:
            return {
                "active_agent": "direct",
                "task_queue": [],
                "current_task_index": 0,
                "original_query": content,
                "trace_id": trace_id,
                "supervisor_response": direct_response,
            }

        # 分析并行可行性
        execution_mode = "serial"
        if len(task_queue) > 1:
            has_deps = any(len(t.get("depends_on", [])) > 0 for t in task_queue)
            if not has_deps:
                execution_mode = "parallel"

        return {
            "active_agent": task_queue[0]["agent"] if task_queue else "finish",
            "task_queue": task_queue,
            "current_task_index": 0,
            "original_query": content,
            "trace_id": trace_id,
            "execution_mode": execution_mode,
        }

    def route_to_agent(state: SupervisorState) -> str:
        """Read current task from queue, return agent node name."""
        active = state.get("active_agent", "info")
        if active == "direct":
            return "direct_respond"
        queue = state.get("task_queue", [])
        idx = state.get("current_task_index", 0)
        if queue and idx < len(queue):
            agent = queue[idx]["agent"]
        else:
            agent = active
        return f"{agent}_agent"

    async def pre_process_node(state: SupervisorState) -> dict:
        """Inject current task query as a HumanMessage for the agent, with optional memory recall."""
        queue = state.get("task_queue", [])
        idx = state.get("current_task_index", 0)
        if not queue or idx >= len(queue):
            return {}

        task = queue[idx]

        # After handoff, game details are already in messages — skip re-injection
        if state.get("handoff_pending"):
            return {"active_agent": task["agent"], "handoff_pending": False}

        # Memory recall — only for specialist agent paths
        query = task["query"]
        if memory:
            try:
                ctx = await memory.recall(query=query, session_id=state.get("trace_id", ""))
                if ctx:
                    query = f"[相关记忆]\n{ctx}\n\n[用户消息]\n{query}"
            except Exception:
                pass  # memory recall failure is non-fatal

        # Cross-session context seed — only for recommend/info agents
        # price/subs agents should not be distracted by previous session context
        agent_type = task.get("agent", "info")
        if context_seed and agent_type in ("recommend", "info"):
            query = f"[来自上一轮的上下文] {context_seed}\n\n{query}"

        return {
            "messages": [HumanMessage(content=query)],
            "active_agent": task["agent"],
        }

    def direct_respond_node(state: SupervisorState) -> dict:
        """Emit supervisor's direct response as an AIMessage."""
        response = state.get("supervisor_response", "你好！有什么可以帮你的吗？")
        return {"messages": [AIMessage(content=response)]}

    async def post_process_node(state: SupervisorState) -> dict:
        """Detect handoff requests, advance task queue, or finish."""
        messages = state["messages"]
        accumulated = dict(state.get("accumulated_context", {}))
        iteration = state.get("iteration_count", 0) + 1
        queue = state.get("task_queue", [])
        idx = state.get("current_task_index", 0)
        trace_id = state.get("trace_id", "")

        # Detect handoff by checking AIMessage.tool_calls for request_game_details
        for msg in reversed(messages[-5:]):
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.get("name") == "request_game_details":
                        games_str = tc.get("args", {}).get("games", "")
                        games = [g.strip() for g in games_str.split(",") if g.strip()]
                        if not games:
                            break

                        with Timer() as t:
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

                        _logger.emit(TraceEvent(
                            trace_id=trace_id,
                            node="post_process",
                            event="handoff",
                            data={"games": games, "iteration": iteration},
                            latency_ms=t.elapsed_ms,
                        ))

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
            _logger.emit(TraceEvent(
                trace_id=trace_id,
                node="post_process",
                event="task_advance",
                data={"from_idx": idx, "to_idx": next_idx, "next_agent": queue[next_idx]["agent"]},
            ))
            return {
                "current_task_index": next_idx,
                "active_agent": queue[next_idx]["agent"],
                "accumulated_context": accumulated,
                "iteration_count": iteration,
            }

        # All tasks done
        _logger.emit(TraceEvent(
            trace_id=trace_id,
            node="post_process",
            event="turn_complete",
            data={"iterations": iteration, "tasks_completed": len(queue)},
        ))
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

    # ── Parallel execution nodes ──────────────────────────────────────────────

    def route_from_supervisor(state: SupervisorState) -> str:
        """Decide serial or parallel execution after supervisor routing."""
        if state.get("execution_mode") == "parallel":
            return "parallel_dispatch"

        active = state.get("active_agent", "info")
        if active == "direct":
            return "direct_respond"
        return "pre_process"

    async def parallel_dispatch_node(state: SupervisorState) -> dict:
        """并行调度所有 specialist agent，使用 asyncio.gather 在单节点内完成。

        避免 LangGraph Send API 的 fan-in 不确定性：所有 agent 在节点内部并行执行，
        结果一次性收集到 parallel_results，然后由 aggregate_results 合并。
        """
        tasks = state.get("task_queue", [])
        # 只提取 SystemMessage（上下文摘要等系统注入信息），排除原始用户输入
        system_msgs = [m for m in state["messages"] if isinstance(m, SystemMessage)]

        agent_map = {
            "price": price_agent,
            "info": info_agent,
            "recommend": recommend_agent,
            "subs": subs_agent,
        }

        async def _run_one(task: dict) -> dict:
            agent_name = task.get("agent", "info")
            query = task.get("query", "")
            target_agent = agent_map.get(agent_name, info_agent)

            # 只传递系统上下文 + supervisor 专属指令，不传原始多意图用户输入
            input_messages: list[BaseMessage] = list(system_msgs)
            input_messages.append(HumanMessage(content=query))

            try:
                result = await target_agent.ainvoke(
                    {"messages": input_messages},
                    config={"recursion_limit": 15},
                )
                result_messages = result.get("messages", [])
                ai_msgs = [m for m in result_messages if isinstance(m, AIMessage)]
                response = ai_msgs[-1].content if ai_msgs else ""
                if isinstance(response, list) and response:
                    response = str(response[0])
            except Exception as e:
                response = f"执行出错: {e}"

            return {
                "task_id": task.get("task_id", -1),
                "agent": agent_name,
                "query": query,
                "response": response,
            }

        results = await asyncio.gather(*[_run_one(t) for t in tasks])

        return {
            "parallel_results": results,
        }


    async def summarize_node(state: SupervisorState) -> dict:
        """自动压缩过长的消息历史。

        触发条件：消息数 > 20 或预估 token > 100000。
        安全规则：不截断未完成的 tool_call 对；保留最近 6 条消息。
        """
        messages = state["messages"]
        if len(messages) <= 20:
            est_tokens = sum(
                len(m.content) // 3 for m in messages if isinstance(m.content, str)
            )
            if est_tokens <= 100000:
                return {}

        # 保留最近 6 条作为"热上下文"
        keep_recent = 6
        safe_split = max(len(messages) // 2, keep_recent)

        # 安全边界：确保 split 点不在 tool_call 中间
        while safe_split < len(messages) - keep_recent:
            msg_at_split = messages[safe_split - 1]
            msg_after = messages[safe_split]
            if (
                isinstance(msg_at_split, AIMessage)
                and getattr(msg_at_split, "tool_calls", None)
                and isinstance(msg_after, ToolMessage)
            ):
                safe_split += 1 + len(msg_at_split.tool_calls)
                continue
            break

        early = messages[:safe_split]

        # 生成摘要
        parts = []
        for m in early:
            role = (
                "用户"
                if isinstance(m, HumanMessage)
                else ("助手" if isinstance(m, AIMessage) else "系统")
            )
            content = m.content if isinstance(m.content, str) else str(m.content)
            parts.append(f"[{role}] {content[:300]}")
        summary_input = "\n".join(parts)

        try:
            summary_llm = build_llm()
            prompt = (
                "请用 300 字以内摘要以下对话的核心内容，保留用户明确表达过的偏好、"
                "已经确认过的游戏信息、以及未完成的请求。只输出摘要内容，不要加任何前缀。\n\n"
                f"{summary_input}"
            )
            result = await summary_llm.ainvoke([SystemMessage(content=prompt)])
            summary_text = (
                result.content.strip()
                if isinstance(result.content, str)
                else str(result.content)
            )
        except Exception:
            summary_text = summary_input[:500] + "…（早期对话摘要）"

        # 使用 RemoveMessage 删除早期消息，追加摘要 SystemMessage
        removes = [RemoveMessage(id=m.id) for m in early if m.id]
        summary_msg = SystemMessage(content=f"[历史摘要] {summary_text}")

        return {
            "messages": removes + [summary_msg],
            "context_summary": summary_text,
        }

    # --- Build graph ---
    builder = StateGraph(SupervisorState)

    # 功能1: 上下文压缩节点
    builder.add_node("summarize", summarize_node)

    # Supervisor 路由
    builder.add_node("route_supervisor", route_supervisor)

    # 串行路径节点
    builder.add_node("pre_process", pre_process_node)
    builder.add_node("price_agent", price_agent)
    builder.add_node("info_agent", info_agent)
    builder.add_node("recommend_agent", recommend_agent)
    builder.add_node("subs_agent", subs_agent)
    builder.add_node("post_process", post_process_node)
    builder.add_node("direct_respond", direct_respond_node)

    # 功能2: 并行包装和聚合
    builder.add_node("parallel_dispatch", parallel_dispatch_node)
    builder.add_node("aggregate_results", aggregate_results_node)

    # --- Edges ---
    # 功能1: START → summarize → route_supervisor
    builder.add_edge(START, "summarize")
    builder.add_edge("summarize", "route_supervisor")

    # 功能2: route_supervisor 后选择串行或并行
    builder.add_conditional_edges("route_supervisor", route_from_supervisor, {
        "parallel_dispatch": "parallel_dispatch",
        "direct_respond": "direct_respond",
        "pre_process": "pre_process",
    })
    builder.add_edge("direct_respond", END)

    # 串行路径
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

    # After post_process → 继续串行或结束
    builder.add_conditional_edges("post_process", route_after_post_process, {
        "price_agent": "pre_process",
        "info_agent": "pre_process",
        "recommend_agent": "pre_process",
        "subs_agent": "pre_process",
        "__end__": END,
    })

    # 并行路径: parallel_dispatch → aggregate_results → END
    builder.add_edge("parallel_dispatch", "aggregate_results")
    builder.add_edge("aggregate_results", END)

    # --- Compile with checkpointer ---
    conn = await aiosqlite.connect(settings.chat_db_path)
    checkpointer = AsyncSqliteSaver(conn)
    return builder.compile(checkpointer=checkpointer), conn


# ── Streaming turn (structured events) ────────────────────────────────────

_AGENT_NAMES = {"price_agent", "info_agent", "recommend_agent", "subs_agent"}


async def stream_turn(
    agent,
    message: str,
    thread_id: str,
    memory: VikingMemory | None = None,
    metrics_collector: MetricsCollector | None = None,
    audit_logger: AuditLogger | None = None,
    chat_store = None,
) -> AsyncIterator[dict]:
    """Execute one turn, yielding structured event dicts.

    Event types:
      {"type": "agent_start", "agent": "price_agent"}
      {"type": "handoff", "from": "recommend_agent", "to": "info_agent"}
      {"type": "token", "content": "..."}
      {"type": "tool_start", "name": "...", "agent": "...", "inputs": {...}}
      {"type": "tool_end", "name": "...", "output": "..."}
      {"type": "agent_end", "agent": "price_agent"}
      {"type": "trace_id", "trace_id": "..."}
    """
    trace_id = new_trace_id()
    config = {"configurable": {"thread_id": thread_id, "trace_id": trace_id}, "recursion_limit": 20}

    # # Build messages list with optional context seed for new threads
    # messages: list[BaseMessage] = [HumanMessage(content=message)]
    # if chat_store is not None:
    #     thread = chat_store.get_thread(thread_id)
    #     if thread and thread.get("context_seed") and len(thread.get("messages", [])) == 0:
    #         # Only inject context seed on the very first message of a new thread
    #         seed = thread["context_seed"]
    #         messages.insert(0, SystemMessage(content=f"[来自上一轮的上下文] {seed}"))

    current_agent = ""
    collected: list[str] = []
    turn_metrics: TurnMetrics | None = None
    tool_outputs: list[str] = []
    in_supervisor = False
    supervisor_tokens: list[str] = []
    agent_count = 0

    if metrics_collector:
        turn_metrics = metrics_collector.start_turn(trace_id, message)

    # Emit trace_id so CLI can display it
    yield {"type": "trace_id", "trace_id": trace_id}

    # Emit initial context status (will be updated as turn progresses)
    yield {
        "type": "context_status",
        "message_count": 1,
        "estimated_tokens": len(message) // 3,
    }

    async for event in agent.astream_events(
        {"messages": [HumanMessage(content=message)]},
        config=config,
        version="v2",
    ):
        kind = event.get("event")
        name = event.get("name", "")

        # Track supervisor phase
        if kind == "on_chain_start" and name == "route_supervisor":
            in_supervisor = True
            continue

        # Direct response node — emit buffered supervisor tokens
        if kind == "on_chain_start" and name == "direct_respond":
            for t in supervisor_tokens:
                collected.append(t)
                yield {"type": "token", "content": t}
            continue

        # Detect specialist agent transitions (serial path)
        if kind == "on_chain_start" and name in _AGENT_NAMES:
            if current_agent and current_agent != name:
                yield {"type": "handoff", "from": current_agent, "to": name}
            current_agent = name
            agent_count += 1
            if turn_metrics and metrics_collector is not None:
                metrics_collector.record_routing(turn_metrics, name)
            yield {"type": "agent_start", "agent": name, "agent_count": agent_count}
            yield {"type": "progress", "current": name, "agent_count": agent_count, "status": "running"}

        elif kind == "on_chain_start" and name == "aggregate_results":
            yield {"type": "parallel_complete", "status": "aggregating"}

        elif kind == "on_chat_model_stream":
            chunk = event["data"]["chunk"]
            if chunk.content:
                text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                if in_supervisor:
                    # Buffer supervisor tokens — only emit if direct response
                    supervisor_tokens.append(text)
                elif current_agent:
                    # Specialist agent tokens
                    collected.append(text)
                    yield {"type": "token", "content": text}

        elif kind == "on_tool_start" and current_agent:
            tool_name = event.get("name", "unknown")
            inputs = event.get("data", {}).get("input", {})
            from valveye.agent_tools import SENSITIVE_TOOLS
            if tool_name in SENSITIVE_TOOLS:
                current_task = asyncio.current_task()
                yield {
                    "type": "permission_request",
                    "tool": tool_name,
                    "agent": current_agent,
                    "inputs": inputs,
                    "task_id": id(current_task) if current_task else None,
                    "options": [
                        {"key": "a", "label": "✅ 同意执行"},
                        {"key": "d", "label": "❌ 拒绝"},
                        {"key": "o", "label": "📝 其他（自定义备注）"},
                    ],
                }
            else:
                yield {"type": "tool_start", "name": tool_name, "agent": current_agent, "inputs": inputs}

        elif kind == "on_tool_end" and current_agent:
            output_obj = event.get("data", {}).get("output", "")
            if hasattr(output_obj, "content"):
                output = str(output_obj.content)
            else:
                output = str(output_obj)
            if len(output) > 500:
                output = output[:500] + "…"
            if turn_metrics and metrics_collector is not None:
                metrics_collector.record_tool_call(turn_metrics, event.get("name", ""), 0.0)
            tool_outputs.append(output)
            yield {"type": "tool_end", "name": event.get("name", ""), "output": output}
            # Audit log
            if audit_logger is not None:
                audit_logger.log(
                    trace_id=trace_id,
                    tool_name=event.get("name", ""),
                    inputs=event.get("data", {}).get("input", {}),
                    output=output,
                    thread_id=thread_id,
                )

        elif kind == "on_tool_error" and current_agent:
            error = str(event.get("data", {}).get("error", "unknown error"))
            yield {"type": "tool_end", "name": event.get("name", ""), "output": f"错误: {error}"}
            if audit_logger is not None:
                audit_logger.log(
                    trace_id=trace_id,
                    tool_name=event.get("name", ""),
                    inputs=event.get("data", {}).get("input", {}),
                    output="",
                    error_msg=error,
                    thread_id=thread_id,
                )

        elif kind == "on_chain_end":
            if name == "route_supervisor":
                in_supervisor = False
            elif name == "summarize":
                state_data = event.get("data", {}).get("output", {})
                summary = state_data.get("context_summary", "")
                if summary:
                    yield {"type": "context_compressed", "summary": summary}
            elif name in _AGENT_NAMES:
                yield {"type": "agent_end", "agent": name}
                current_agent = ""
            elif name == "aggregate_results":
                yield {"type": "parallel_complete", "status": "done"}
                # 提取合并后的回复文本并 yield 为 token，确保 response_parts 被填充
                output = event.get("data", {}).get("output", {})
                for m in output.get("messages", []):
                    if isinstance(m, AIMessage) and m.content:
                        yield {"type": "token", "content": m.content}
                        break

    # Auto-Capture and validation
    if memory and collected:
        full_response = "".join(collected)
        await memory.capture(thread_id, message, full_response)

    # Response validation (hallucination guardrail)
    if collected and tool_outputs:
        validator = ResponseValidator()
        full_response = "".join(collected)
        warnings = validator.validate(full_response, tool_outputs, current_agent)
        if warnings:
            yield {"type": "validation_warning", "warnings": warnings}

    if turn_metrics and metrics_collector is not None:
        metrics_collector.end_turn(turn_metrics)


# ── Non-streaming turn ────────────────────────────────────────────────────

async def run_single_turn(
    agent, message: str, thread_id: str, memory: VikingMemory | None = None,
) -> str:
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 20}

    # Memory recall is now handled inside the graph's pre_process_node
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=message)]},
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
