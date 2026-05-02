# Valveye

一个基于 LangChain 的 Steam 游戏价格 Agent，支持对话式交互，能查询游戏史低价格、跨区比价、推荐相关游戏以及订阅史低通知。

## 功能

- **对话式交互** — 基于 LangChain Agent + LangGraph，支持多轮对话和流式输出
- **史低价格查询** — 优先使用第三方公开接口（IsThereAnyDeal / SteamDB / CheapShark），自动降级
- **跨区价格对比** — 查询 23 个 Steam 区域的价格，自动汇率转换，按价格排序
- **区域自动检测** — 根据用户输入语言和系统环境自动选择区域/货币（中文→国区/CNY，日文→日区/JPY 等）
- **多语言游戏名** — 支持中文、日文、韩文等非英文游戏名查询，自动翻译为 Steam 官方英文名
- **推荐相似游戏** — 基于标签、评价和相似产品推荐
- **订阅价格提醒** — 设置价格监控，史低时通过多渠道发送通知
- **多通知渠道** — Email / Telegram / 企业微信 / 飞书 / 钉钉 / Discord / QQ
- **定时检测** — 每天固定时间检测订阅游戏是否史低

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 API Key 等配置

# 对话模式
python src/main.py chat

# 单次查询
python src/main.py chat -m "艾尔登法环多少钱"

# 跨区比价
python src/main.py chat -m "Persona 5 哪个区最便宜"
```

## 工具列表

| 工具 | 说明 |
|------|------|
| `query_low_price` | 查询游戏当前价与史低信息 |
| `compare_prices` | 对比所有 Steam 区域的价格 |
| `recommend_similar_games` | 推荐同类游戏 |
| `subscribe_game` | 订阅价格提醒 |
| `list_subscriptions` | 查看有效订阅列表 |

## 区域支持

自动检测支持 23 个 Steam 区域：

美区(USD) / 英区(GBP) / 欧区(EUR) / 国区(CNY) / 日区(JPY) / 韩区(KRW) / 台区(TWD) / 港区(HKD) / 新加坡区(SGD) / 澳区(AUD) / 加区(CAD) / 墨区(MXN) / 巴区(BRL) / 阿区(ARS) / 智利区(CLP) / 哥伦比亚区(COP) / 印区(INR) / 俄区(RUB) / 土区(TRY) / 乌区(UAH) / 哈区(KZT) / 南非区(ZAR) / 阿联酋区(AED)

检测优先级：非拉丁文字直接匹配 > 系统语言/时区推断 > 美区兜底

## 项目结构

```
src/valveye/
├── agent.py           # LangChain Agent 构建与对话入口
├── agent_tools.py     # Agent 工具定义
├── cli.py             # CLI 入口（chat / subscribe / check 子命令）
├── config.py          # 配置管理
├── domain.py          # 领域模型
├── notifications.py   # 多渠道通知
├── pricing.py         # 价格查询、区域检测、汇率转换
├── recommendation.py  # 游戏推荐
├── scheduler.py       # 定时任务
├── subscriptions.py   # 订阅存储
├── time_utils.py      # 时区工具
└── data_sources/
    ├── base.py        # 价格源基类
    ├── itad.py        # IsThereAnyDeal
    ├── steamdb.py     # SteamDB
    └── cheapshark.py  # CheapShark
```

## TODO

- [ ] 补充测试用例
- [ ] 通知去重 + 重试退避 + 失败落盘
- [ ] Web UI
