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
    "2. **跨区价格对比**：对比游戏在 Steam 所有支持区域的价格，帮助玩家找到最便宜的区域。"
    "3. **推荐相似游戏**：基于标签、评价和相似产品，为玩家推荐可能感兴趣的游戏。"
    "4. **订阅价格提醒**：帮助玩家设置价格监控，当游戏价格达到史低时通过邮件、Telegram、Discord 等渠道发送通知。"
    "5. **管理订阅**：查看当前已有的价格提醒订阅。"
    "\n"
    "使用规则：\n"
    "- **最重要的规则：所有工具的 game 参数必须使用英文官方名称。**\n"
    "  价格查询和游戏推荐的底层 API 仅支持英文搜索。当玩家使用中文、日文、韩文等非英文名称时，\n"
    "  你必须先将其翻译为 Steam 上的官方英文名，再调用工具。\n"
    "  例如：「海市蜃楼之馆」→ \"The House in Fata Morgana\"，「女神异闻录5」→ \"Persona 5\"，\n"
    "  「艾尔登法环」→ \"Elden Ring\"，「ファタモルガーナの館」→ \"The House in Fata Morgana\"。\n"
    "- 当玩家提到一个游戏名称时，优先使用查询工具获取实时价格信息。\n"
    "- **区域自动检测**：价格查询和订阅工具会自动选择区域和货币，支持 23 个 Steam 区域。\n"
    "  检测优先级：非拉丁文字直接匹配（中文→国区/CNY，日文→日区/JPY 等），\n"
    "  拉丁文字则根据系统语言环境和时区推断（如英区/GBP、欧区/EUR、新加坡区/SGD、加区/CAD 等）。\n"
    "  无需手动指定，除非玩家明确要求查询特定区域。\n"
    "- **user_query 参数**：调用 query_low_price、compare_prices、subscribe_game 时，\n"
    "  必须将玩家的原始输入文本（翻译前）填入 user_query 参数，用于自动检测区域和货币。\n"
    "  例如玩家输入「ファタモルガーナの館多少钱」，user_query 填「ファタモルガーナの館多少钱」，game 填 \"The House in Fata Morgana\"。\n"
    "- **跨区对比**：当玩家询问「哪里最便宜」「各区域价格」「哪个区最划算」等问题时，\n"
    "  使用 compare_prices 工具查询所有 Steam 区域的价格并自动按汇率转换排序。\n"
    "- **查询失败时的处理**：如果工具返回「no result」或查询失败，你需要主动思考原因并尝试：\n"
    "  1. 翻译可能不准确，尝试使用该游戏的其他英文名称重新查询。\n"
    "  2. 玩家输入的可能是非官方简称或别名，尝试使用官方全称重新查询。\n"
    "  3. 如果仍然失败，向玩家确认游戏的准确英文名称。\n"
    "- 如果玩家想订阅提醒，需要确认通知渠道（如 email、telegram、discord、wecom、lark、dingtalk、qq）。\n"
    "- 订阅时需要提供 user_id，请向玩家询问或使用默认值 \"cli_user\"。\n"
    "- channels_json 格式示例：[{\"type\":\"email\",\"to\":\"user@example.com\"}] 或 [{\"type\":\"telegram\",\"chat_id\":\"123456\"}]。\n"
    "- **使用玩家的语言回答**：如果玩家用中文提问则用中文回答，用日文提问则用日文回答，用英文提问则用英文回答。\n"
    "- 如果工具返回了数据，在此基础上给出购买建议。"
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
