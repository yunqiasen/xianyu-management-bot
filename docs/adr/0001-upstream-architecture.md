---
status: accepted
acceptance_scope: design-direction-only
repository: yunqiasen/xianyu-management-bot
branch: xianyu-management-bot-fork
date: 2026-09-21
---

# 原版架构与双分支维护

原版已演进为React、MySQL、Redis和独立Web/消息/调度模块，GuDong仍是旧单体；整仓合并会丢失原版架构及后续更新基础。保留原版架构，`main`只同步上游，`xianyu-management-bot-fork`按49组功能复用或移植GuDong，再接入参考能力；目标项目只保留一份源码仓库，版本通过分支和提交管理。

代价是逐项适配数据、接口和页面，而不是一次文件合并。收益是长期跟进上游，并让每组能力有独立验收；上游代码更新不自动触发生产发布，部署只来自已提交、干净工作区的固定版本。

确认依据：原版底座和分支路线先前已确认；用户本轮确认完整方案方向。

本记录描述已接受的设计方向，不表示代码已实现或已通过业务验收。
