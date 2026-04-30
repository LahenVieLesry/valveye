from __future__ import annotations

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI

from valveye.config import settings

SYSTEM_PROMPT = """
你是 Valveye 的 Steam 价格与订阅助手。

你的目标：
1. 优先用工具查询游戏价格与史低信息，不要凭空编造价格。
2. 当用户要订阅时，优先询问缺失字段（user_id、游戏名、渠道）。
3. 渠道参数必须是 JSON 数组字符串，例如：
   [{"type":"telegram","chat_id":"123456"},{"type":"discord","webhook":"https://..."}]
4. 回答时给出清晰结论：是否史低、来源、下一步建议。
""".strip()


def build_agent_executor(tools: list):
    model = ChatOpenAI(
        api_key=settings.openai_api_key or None,
        base_url=settings.openai_base_url or None,
        model=settings.openai_model,
        temperature=0,
    )

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )

    agent = create_tool_calling_agent(llm=model, tools=tools, prompt=prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=False)
