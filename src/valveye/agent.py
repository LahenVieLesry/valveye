from __future__ import annotations

from typing import Any, AsyncIterator

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver

from valveye.config import settings

SYSTEM_PROMPT = (
    "你是 Valveye，一个专业的 Steam 游戏价格助手。你的职责是帮助玩家：\n"
    "\n"
    "1. **查询游戏价格**：查找游戏的当前售价和历史最低价，支持 Steam、IsThereAnyDeal、CheapShark 等多个数据源。"
    "2. **推荐相似游戏**：基于标签、评价和相似产品，为玩家推荐可能感兴趣的游戏。"
    "3. **订阅价格提醒**：帮助玩家设置价格监控，当游戏价格达到史低时通过邮件、Telegram、Discord 等渠道发送通知。"
    "4. **管理订阅**：查看当前已有的价格提醒订阅。"
    "\n"
    "使用规则：\n"
    "- 当玩家提到一个游戏名称时，优先使用查询工具获取实时价格信息。\n"
    "- 价格查询默认使用中国区（CN）和人民币（CNY），除非玩家指定其他地区。\n"
    "- 如果玩家想订阅提醒，需要确认通知渠道（如 email、telegram、discord、wecom、lark、dingtalk、qq）。\n"
    "- 订阅时需要提供 user_id，请向玩家询问或使用默认值 \"cli_user\"。\n"
    "- channels_json 格式示例：[{\"type\":\"email\",\"to\":\"user@example.com\"}] 或 [{\"type\":\"telegram\",\"chat_id\":\"123456\"}]。\n"
    "- 用简洁、友好的中文回答。如果工具返回了数据，在此基础上给出购买建议。\n"
    "- 如果工具调用失败，告知玩家原因并建议稍后重试。"
)


def build_llm() -> ChatOpenAI:
    kwargs: dict = {
        "model": settings.openai_model,
        "api_key": settings.openai_api_key,
        "temperature": 0.7,
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return ChatOpenAI(**kwargs)


def build_agent_executor(tools: list):
    llm = build_llm()
    checkpointer = MemorySaver()
    return create_agent(
        model=llm,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )


async def run_single_turn(agent, message: str, thread_id: str) -> str:
    config = {"configurable": {"thread_id": thread_id}}
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=message)]},
        config=config,
    )
    ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
    if ai_messages:
        content: Any = ai_messages[-1].content
        return str(content) if not isinstance(content, str) else content
    return "(no response)"


async def stream_turn(agent, message: str, thread_id: str) -> AsyncIterator[str]:
    config = {"configurable": {"thread_id": thread_id}}
    async for event in agent.astream(
        {"messages": [HumanMessage(content=message)]},
        config=config,
        stream_mode="messages",
    ):
        msg, _metadata = event  # noqa: F841
        if isinstance(msg, AIMessage) and msg.content:
            content: Any = msg.content
            yield content if isinstance(content, str) else str(content)
        elif hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                yield f"\n[调用工具: {tc['name']}({tc['args']})]\n"
