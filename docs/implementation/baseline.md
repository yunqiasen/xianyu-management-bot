# 仓库与运行底座

- 仓库：[yunqiasen/xianyu-management-bot](https://github.com/yunqiasen/xianyu-management-bot)，GitHub fork parent为 [zhinianboke/xianyu-auto-reply](https://github.com/zhinianboke/xianyu-auto-reply)。
- `main` 只同步原版；`xianyu-management-bot-fork` 承载增强。实施/总审查固定点为 `fdc8eb039be771456ecfbbbe43fa26f57feb3947`。
- 研究固定点 `86474fd70670f814dd1fe1ba53575394ffa93668` 比实施点早；18个上游差异已作为新版底座复测，不回盖旧文件。
- GitHub上游发布工作流在仓库设置层关闭；重新启用先核对目标镜像仓库与发布权限。main不塞定制同步工作流。

## 服务边界

Web管理配置和权限；消息服务持有账号连接和执行权；调度经同一执行方执行定时业务。默认端口分别8089/8090/8091，以部署配置为准。`promotion`等原版额外子项目保留，不作为本轮新增自动执行方。

MySQL保存配置、意图、版本、业务及防重事实；Redis协调租期、共享预算和缓存。公共模型通过启动DDL锁迁移，请求期间不建表。运行版本分源码、构建/镜像与数据版本；健康检查区分服务存活、依赖状态和账号业务可用性。

`compose.integration.yml` 只建立隔离MySQL、第二恢复MySQL和Redis；本机端口19006/19007/19379、独立卷，不挂旧数据。Web采集、消息连接、自动调度缺省关闭；测试进程在独立端口启动并自行清理。

## 工程验证

`tools/verification/run.py` 完整收集领域测试、特殊命名suite、前端检查/构建、实际Chromium、MySQL/Redis及三服务进程测试。源码指纹包含已跟踪与未跟踪文件，测试中变化、零测试、跳过和超时均阻止通过。日志/截图位于仓库外；现用旧服务、业务库和挂载源码保持独立。

固定输入与来源见 [来源](../SOURCES.md)；覆盖范围见 [覆盖索引](../COVERAGE.md)；正式提交、推送、部署及单账号交接见 [发布门禁](../RELEASE.md)。
