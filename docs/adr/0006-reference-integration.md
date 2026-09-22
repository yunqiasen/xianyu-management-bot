---
status: accepted
acceptance_scope: design-direction-only
repository: yunqiasen/xianyu-management-bot
branch: xianyu-management-bot-fork
date: 2026-09-21
---

# 增强既有模块而非并排重造

原版已有议价计数和商品监控，参考项目用于补差而不是新增另一套后台或执行器。先完成GuDong覆盖与可靠性里程碑，再将shaxiu的人工交接、回复/跳过和议价语义用于增强既有客服，将LENKIN的基线、分层错误、响应关联、去重、续跑及通知重试用于增强既有监控。

这样保留一套账号、代理和调度规则，避免多系统竞争登录；不照搬已发现的议价误判或历史窗口缺陷，也不额外启用自动私信、直接下单。参考能力的接入以业务结果验收，不以复制了多少源码验收。

确认依据：用户总体同意GuDong优先、参考能力随后，以及复用现有模块的路线。

本记录描述已接受的设计方向，不表示代码已实现或已通过业务验收。
