一个基于langchain的steam agent，能查询游戏史低价格，推荐相关类型的游戏以及订阅游戏史低通知。
史低价格查询优先用第三方公开接口（如 IsThereAnyDeal / steamdb / CheapShark 等）。
通知方式包含邮件 Email，Telegram Bot，企业微信/飞书/钉钉 Webhook，Discord Webhook，Tencent QQ等。
每天固定时间（冬令时为UTC+1，夏令时为UTC+2）检测订阅游戏是否史低，史低或新史低时发送通知提醒。


## 待完成

- [x] 基于 LangChain（tools 层）

- [x] 史低查询优先第三方公开源（ITAD/SteamDB/CheapShark，自动降级）

- [x] 推荐相关游戏

- [x] 订阅 + 每日定时检测

- [x] 冬令时 UTC+3 / 夏令时 UTC+4

- [x] 多通知渠道(Email/Telegram/企微/飞书/钉钉/Discord/QQ)


## 当前 TODO 状态

- [ ] 初始化项目骨架与依赖

- [ ] 实现价格源适配与降级

- [ ] 实现史低规则与推荐

- [ ] 实现订阅存储与通知通道

- [ ] 实现定时检测与时区规则

- [ ] 实现 LangChain 工具与 CLI

- [ ] 补充基础测试并验证

- [ ] 加上真正的 AgentExecutor 对话入口（不止 CLI 子命令）

- [ ] 增加“通知去重 + 重试退避 + 失败落盘”机制
