# 实现与验证覆盖索引

规格固定为XYMB-SPEC-001 v1.0，实施/总审查基线 `fdc8eb039be771456ecfbbbe43fa26f57feb3947`。49组功能、30个场景、3项工程核验和19项旧差异分别索引；原规格及研究矩阵保留原字节。

## 结果口径

- 下表是已接入的代码与本地验证入口，不把路径存在、配置保存或旧领域报告当作通过。每批结果以完整验证器生成的仓库外 `results.json`、测试日志和唯一总审查为准。
- S1：现有API/业务事件及React操作；S2：可控平台、代理、模型、通知和供应商通信；S3：一致性样本、隔离迁移、校验和回退。MySQL/Redis与第二MySQL恢复实际运行，外部平台使用合成数据端点。
- `tools/verification/run.py` 显式收集非标准suite、前端、浏览器；零测试、跳过、超时、源码变化及缺套件都阻止通过。组件harness操作不冒充整个后台视觉验收。
- DEV44、DEV47的真实交接/发布、不可变候选镜像、真实场景及48小时观察单列待执行。AT27本地演练与真实交接分开，AT30始终保留真实发布门。

## F01–F49

测试文件均相对 `tests/`；入口路径相对仓库。S入口需求及US-001–049沿冻结规格。

| 功能 | 实现入口 / 说明 | 本地验证文件 |
|---|---|---|
| F01 后台入口与健康检查 | `common/runtime_health.py`；`backend-web/app/api/routes/system_control.py`；[底座](implementation/baseline.md) | `runtime/test_health.py`；`runtime/service_status_suite.py`；`runtime/process_smoke_suite.py` |
| F02 后台登录、注册、邮箱与密码 | `backend-web/app/api/routes/auth.py`；`backend-web/app/api/routes/captcha.py`；[管理](implementation/admin.md) | `admin/test_auth_lifecycle.py`；`runtime/auth_boundary_suite.py`；`migration/test_mapping.py` |
| F03 后台登录防护与配置 | `backend-web/app/services/login_protection_service.py`；`frontend/src/pages/admin/LoginProtection.tsx`；[管理](implementation/admin.md) | `admin/test_auth.py`；`admin/browser_flow.py` |
| F04 经营总览、公告与统计 | `backend-web/app/services/operating_summary_service.py`；`frontend/src/pages/dashboard/OperatingSummary.tsx`；[管理](implementation/admin.md) | `admin/test_stats.py`；`admin/browser_flow.py` |
| F05 账号资料、启停与删除 | `backend-web/app/services/account_service.py`；`common/services/account_policy.py`；[账号](implementation/accounts.md) | `accounts/test_service.py`；`accounts/test_api.py`；`accounts/test_infrastructure.py`；`migration/test_runner.py` |
| F06 账号代理配置 | `common/services/account_proxy.py`；`common/services/account_runtime.py`；[账号](implementation/accounts.md) | `accounts/test_transport.py`；`accounts/test_browser_proxy.py`；`dispatch/test_proxy_transport.py` |
| F07 手动 Cookie 导入与状态 | `backend-web/app/services/account_jobs.py`；`backend-web/app/api/routes/cookies.py`；[账号](implementation/accounts.md) | `accounts/test_api.py`；`accounts/test_live_chain_wiring.py`；`accounts/test_infrastructure.py` |
| F08 账密登录、查询与取消 | `common/services/account_browser.py`；`common/services/account_recovery.py`；[账号](implementation/accounts.md) | `accounts/test_login.py`；`accounts/test_recovery_chain.py`；`dispatch/test_login_boundary.py` |
| F09 扫码登录、刷新与冷却 | `backend-web/app/services/qr_login/manager.py`；`common/services/account_renewal.py`；[账号](implementation/accounts.md) | `accounts/test_api.py`；`accounts/test_login.py`；`dispatch/test_renewal.py`；`accounts/test_legacy_guards.py`；`accounts/test_qr_token_handshake.py` |
| F10 人脸验证提示与截图 | `backend-web/app/services/qr_login/face_verification.py`；`common/services/account_browser.py`；[账号](implementation/accounts.md) | `accounts/test_browser_session.py`；`runtime/auth_boundary_suite.py`；`runtime/asset_privacy_suite.py` |
| F11 浏览器验证会话与人工接管 | `common/services/account_browser.py`；`frontend/src/pages/accounts/VerificationPointerSurface.tsx`；[账号](implementation/accounts.md) | `accounts/test_browser_session.py`；`accounts/test_verification_pointer.py`；`accounts/test_browser_proxy.py` |
| F12 运行状态、主动保活与自动刷新 | `common/services/account_renewal.py`；`common/services/account_execution.py`；[账号](implementation/accounts.md) | `accounts/test_execution.py`；`accounts/test_runtime.py`；`accounts/test_infrastructure.py`；`dispatch/test_renewal.py` |
| F13 人工回复后的暂停与恢复 | `common/services/reply_state.py`；`websocket/app/services/xianyu/auto_reply_service.py`；[回复](implementation/replies.md) | `replies/test_state.py`；`replies/test_runtime.py`；`replies/live_ai_suite.py`；`migration/test_continuation.py` |
| F14 关键词、图片、商品范围与导入导出 | `common/services/reply_policy.py`；`backend-web/app/api/routes/keywords.py`；[回复](implementation/replies.md) | `replies/test_policy.py`；`replies/keyword_roundtrip_suite.py`；`replies/browser_flow.py`；`migration/test_mapping.py` |
| F15 回复图片上传与素材资源 | `common/services/reply_images.py`；`backend-web/app/api/routes/upload.py`；[回复](implementation/replies.md) | `replies/test_images.py`；`replies/api_suite.py`；`runtime/asset_privacy_suite.py`；`migration/test_continuation.py` |
| F16 账号默认回复与只回一次 | `common/services/reply_state.py`；`common/services/reply_policy.py`；[回复](implementation/replies.md) | `replies/test_state.py`；`replies/test_policy.py`；`replies/test_followup.py`；`migration/test_mapping.py` |
| F17 指定商品回复 | `backend-web/app/api/routes/reply_management.py`；`frontend/src/pages/keywords/ReplyControls.tsx`；[回复](implementation/replies.md) | `replies/api_suite.py`；`replies/test_policy.py`；`replies/browser_flow.py`；`migration/test_mapping.py` |
| F18 AI接口、提示词、测试与历史 | `common/services/ai_gateway.py`；`websocket/app/services/xianyu/ai_reply_engine.py`；[AI](implementation/ai.md) | `ai/test_protocols.py`；`ai/test_live_path.py`；`replies/live_ai_suite.py`；`ai/browser_smoke.py`；`migration/test_mapping.py` |
| F19 可复用AI配置预设 | `backend-web/app/services/ai_preset_service.py`；`frontend/src/pages/accounts/AISettingsPanel.tsx`；[AI](implementation/ai.md) | `ai/test_presets.py`；`ai/test_api.py`；`ai/browser_smoke.py`；`migration/test_continuation.py` |
| F20 高级消息过滤与AI发送前检查 | `common/services/reply_policy.py`；`backend-web/app/api/routes/reply_management.py`；[回复](implementation/replies.md) | `replies/test_followup.py`；`replies/api_suite.py`；`replies/browser_flow.py`；`dispatch/test_outbound.py`；`migration/test_native_filter_mapping.py` |
| F21 个人/平台黑名单与聊天操作 | `backend-web/app/services/blacklist_service.py`；`backend-web/app/services/chat_new/official_blacklist_service.py`；[回复](implementation/replies.md) | `replies/blacklist_suite.py`；`replies/api_suite.py`；`migration/test_mapping.py` |
| F22 聊天连接、会话列表、头像与历史 | `backend-web/app/services/chat_new/im_session_manager.py`；`common/services/reply_state.py`；[回复](implementation/replies.md) | `dispatch/test_chat_adapter.py`；`replies/api_suite.py`；`replies/live_ai_suite.py`；`migration/test_continuation.py` |
| F23 手动发送及程序回复入口 | `backend-web/app/api/routes/chat_new.py`；`common/services/account_dispatch.py`；[回复](implementation/replies.md) | `dispatch/test_flow.py`；`dispatch/test_outbound.py`；`replies/test_state.py`；`replies/api_suite.py` |
| F24 聊天及订单实时事件 | `common/services/reply_state.py`；`frontend/src/pages/chat-new/replyEventState.ts`；[回复](implementation/replies.md) | `replies/mysql_stream_suite.py`；`replies/frontend.cjs`；`replies/api_suite.py`；`dispatch/test_flow.py` |
| F25 商品列表、同步、详情与删除 | `common/services/item_service.py`；`common/services/product_item_operations.py`；[商品](implementation/products.md) | `products/test_sync.py`；`products/test_continuation.py`；`products/browser_flow.py`；`migration/test_mapping.py` |
| F26 商品搜索与多页获取 | `backend-web/app/services/search/searcher.py`；`common/services/xianyu_search_client.py`；[商品](implementation/products.md) | `products/test_search.py`；`products/test_search_gateway.py`；`dispatch/test_platform_gateway.py` |
| F27 商品素材库 | `backend-web/app/services/product_publish_service.py`；`frontend/src/pages/product-publish/ProductSpecificationsEditor.tsx`；[商品](implementation/products.md) | `products/test_materials.py`；`products/browser_flow.py`；`migration/test_mapping.py` |
| F28 单品/批量发布与发布日志 | `common/services/publish_execution_service.py`；`common/services/product_batch_service.py`；[商品](implementation/products.md) | `products/test_publish.py`；`products/test_batch.py`；`products/test_api.py`；`products/browser_flow.py` |
| F29 多规格与多数量发货 | `common/services/order_lines.py`；`common/services/order_delivery_runtime.py`；[履约](implementation/commerce.md) | `commerce/test_runtime.py`；`commerce/test_fulfillment.py`；`commerce/test_mysql_inventory.py`；`commerce/ws_legacy.py`；`migration/test_native_rule_mapping.py` |
| F30 卡券、批量卡密、API和图片卡 | `common/services/card_purchase.py`；`common/services/delivery_execution.py`；[履约](implementation/commerce.md) | `commerce/test_purchase.py`；`commerce/test_fulfillment.py`；`commerce/test_transport.py`；`commerce/test_mysql_inventory.py`；`migration/test_continuation.py` |
| F31 发货规则、日志和自动触发 | `common/services/delivery_rules.py`；`common/services/delivery_rule_configuration.py`；[履约](implementation/commerce.md) | `commerce/test_delivery_rules.py`；`commerce/test_runtime.py`；`commerce/ws_behavior.py`；`commerce/browser_flow.py`；`migration/test_native_rule_mapping.py` |
| F32 订单查询、历史同步、补单与刷新 | `common/services/order_history.py`；`common/services/order_platform_gateway.py`；[履约](implementation/commerce.md) | `commerce/test_history.py`；`commerce/test_order_gateway.py`；`commerce/ws_order_gateway.py`；`commerce/browser_flow.py`；`migration/test_runner.py` |
| F33 人工补发与仅补平台确认 | `backend-web/app/api/routes/order_commerce.py`；`frontend/src/pages/orders/CommercePanel.tsx`；[履约](implementation/commerce.md) | `commerce/test_api.py`；`commerce/test_commerce_followup.py`；`commerce/ws_scheduler.py`；`commerce/browser_flow.py`；`migration/test_rollback_control.py` |
| F34 自动确认发货 | `common/services/delivery_execution.py`；`websocket/app/services/shipping/commerce_confirmation.py`；[履约](implementation/commerce.md) | `commerce/test_shipping.py`；`commerce/test_commerce_followup.py`；`commerce/ws_behavior.py`；`migration/test_mapping.py` |
| F35 自动评价、模板和历史补评 | `common/services/product_feedback_service.py`；`scheduler/app/services/scheduler/rate_task.py`；[商品](implementation/products.md) | `products/test_feedback.py`；`products/test_scheduler.py`；`products/browser_flow.py`；`migration/test_continuation.py` |
| F36 求小红花、记录与立即执行 | `common/services/product_red_flower.py`；`scheduler/app/services/scheduler/red_flower_task.py`；[商品](implementation/products.md) | `products/test_feedback.py`；`products/test_scheduler.py`；`products/browser_flow.py`；`migration/test_continuation.py` |
| F37 一键及按账号定时擦亮 | `common/services/product_polish_schedule.py`；`common/services/product_polish_service.py`；[商品](implementation/products.md) | `products/test_polish_schedule.py`；`products/test_scheduler.py`；`products/browser_flow.py`；`migration/test_polish_schedule_mapping.py` |
| F38 通知渠道与测试 | `common/services/notification_transport.py`；`backend-web/app/api/routes/notifications.py`；[管理](implementation/admin.md) | `admin/test_notifications.py`；`admin/test_legacy_templates.py`；`migration/test_continuation.py` |
| F39 账号通知绑定与事件发送 | `common/services/notification_delivery_service.py`；`common/services/notification_worker.py`；[管理](implementation/admin.md) | `admin/test_notifications.py`；`admin/test_account_events.py`；`dispatch/test_renewal.py`；`migration/test_mapping.py` |
| F40 通知模板编辑、测试与恢复默认 | `common/services/notification_template_service.py`；`frontend/src/pages/notifications/NotificationTemplates.tsx`；[管理](implementation/admin.md) | `admin/test_templates.py`；`admin/test_legacy_templates.py`；`admin/browser_flow.py`；`migration/test_continuation.py` |
| F41 系统及用户设置、注册开关 | `common/services/typed_settings.py`；`backend-web/app/services/user_service.py`；[管理](implementation/admin.md) | `accounts/test_config_inheritance.py`；`admin/test_auth_lifecycle.py`；`admin/test_retention_defaults.py`；`migration/test_continuation.py` |
| F42 后台用户及管理员权限 | `backend-web/app/api/deps.py`；`backend-web/app/api/routes/users.py`；[管理](implementation/admin.md) | `admin/test_auth.py`；`runtime/auth_boundary_suite.py`；`runtime/asset_privacy_suite.py`；`admin/test_data.py` |
| F43 运行日志、任务日志及导出 | `common/utils/logging_utils.py`；`backend-web/app/services/admin_log_archive_service.py`；`common/services/audit_retention.py`；[管理](implementation/admin.md) | `admin/test_log_collection.py`；`admin/test_log_archive.py`；`admin/test_audit_retention.py`；`runtime/error_privacy_suite.py` |
| F44 账号风控事件与验证统计 | `common/services/account_recovery.py`；`backend-web/app/services/account_login_log_service.py`；[账号](implementation/accounts.md) | `accounts/test_recovery_chain.py`；`admin/test_account_events.py`；`admin/browser_flow.py` |
| F45 配置备份、整库备份与恢复 | `scheduler/app/services/scheduler/db_backup_task.py`；`backend-web/app/services/backup_verification_service.py`；[管理](implementation/admin.md) | `admin/test_backup.py`；`admin/test_backup_roundtrip.py`；`admin/test_backup_protection.py`；`admin/browser_flow.py` |
| F46 数据表查看、导出与删除 | `backend-web/app/services/admin_data_service.py`；`frontend/src/pages/admin/DataManagement.tsx`；[管理](implementation/admin.md) | `admin/test_data.py`；`admin/test_backup_protection.py`；`admin/browser_flow.py` |
| F47 配置重载与运行缓存 | `common/services/account_configuration.py`；`frontend/src/pages/accounts/AccountTypedSettings.tsx`；[账号](implementation/accounts.md) | `accounts/test_config_inheritance.py`；`accounts/test_infrastructure.py`；`accounts/browser_policy.py`；`runtime/scheduler_config_suite.py` |
| F48 版本、热更新、重启与部署 | `common/runtime_version.py`；`backend-web/app/services/version_service.py`；`tools/release/checker.py`；[底座](implementation/baseline.md) | `runtime/test_runtime_baseline.py`；`runtime/process_smoke_suite.py`；`release/test_gate.py` |
| F49 后台队列、心跳、防抖、锁与清理 | `common/services/account_execution.py`；`common/services/account_request_budget.py`；`common/services/reply_state.py`；[执行方](implementation/dispatch.md) | `integration/test_mysql_redis.py`；`dispatch/integration_check.py`；`accounts/test_infrastructure.py`；`replies/mysql_stream_suite.py`；`migration/test_handoff.py` |

## AT01–AT30

| 场景 | 测试文件（相对tests） | 证据边界 |
|---|---|---|
| AT01 账号刷新、清除动作和版本竞争 | `accounts/test_api.py`；`accounts/test_infrastructure.py`；`accounts/test_service.py` | 凭据/配置/代次竞争、旧任务查询/取消和到期替换均不暂停新会话，资料保留 |
| AT02 执行权、租期和重启 | `integration/test_mysql_redis.py`；`accounts/test_infrastructure.py`；`dispatch/integration_check.py`；`dispatch/test_flow.py` | 真实租约竞争、迟到写入、在途unknown |
| AT03 代理故障闭合 | `accounts/test_browser_proxy.py`；`accounts/test_transport.py`；`dispatch/test_proxy_transport.py` | 本地观测HTTP/WS/浏览器、认证失败和冲突环境代理 |
| AT04 恢复分类和预算 | `accounts/test_recovery_chain.py`；`accounts/test_runtime.py`；`dispatch/test_renewal.py` | 两次恢复、反复失效、限流/代理分类 |
| AT05 人工验证控制 | `accounts/test_browser_session.py`；`accounts/test_verification_pointer.py`；`accounts/test_browser_proxy.py` | 指针控制、归属、完成检查、取消及真实浏览器子进程退出 |
| AT06 回复兼容组合 | `replies/test_policy.py`；`replies/test_runtime.py`；`replies/live_ai_suite.py` | 双策略、专属与默认、空回复停止 |
| AT07 只回一次和出站过滤 | `replies/test_state.py`；`replies/test_followup.py`；`dispatch/test_outbound.py` | 名额预占、unknown不释放、最新规则和人工接管 |
| AT08 完整协议矩阵 | `ai/test_protocols.py`；`ai/test_live_path.py`；`replies/live_ai_suite.py`；`ai/browser_smoke.py` | 六协议独立HTTP矩阵与实际回复复验 |
| AT09 人工历史和消息关联 | `replies/test_runtime.py`；`replies/test_followup.py`；`replies/live_ai_suite.py` | 人工历史、账号隔离、消息身份 |
| AT10 发送确认及实时恢复 | `dispatch/test_flow.py`；`replies/api_suite.py`；`replies/mysql_stream_suite.py`；`replies/frontend.cjs` | 回执缺失、幂等提交、提交水位和重连事件 |
| AT11 库存与多规格竞争 | `commerce/test_mysql_inventory.py`；`commerce/test_runtime.py`；`commerce/ws_legacy.py` | 真实MySQL抢最后库存、整单多SKU预占回滚 |
| AT12 API卡未知结果 | `commerce/test_purchase.py`；`commerce/test_commerce_followup.py`；`commerce/test_process_recovery.py`；`commerce/ws_behavior.py` | 真实进程在采购请求前、受理后及响应后SIGKILL，重启先查询且不重复购买 |
| AT13 仅补确认与真正补发 | `commerce/test_commerce_followup.py`；`commerce/test_process_recovery.py`；`commerce/ws_behavior.py`；`commerce/ws_scheduler.py` | 真实进程发送/确认各边界SIGKILL及重启；补确认不重复出库/发送 |
| AT14 先确认模式和终态 | `commerce/test_shipping.py`；`commerce/test_commerce_followup.py` | 先确认/先发送/仅卡密两个完成维度 |
| AT15 发布部分成功 | `products/test_batch.py`；`products/test_publish.py`；`products/browser_flow.py` | 逐项部分成功、明确失败重试、unknown核对 |
| AT16 历史订单同步与状态冲突 | `commerce/test_history.py`；`commerce/test_order_gateway.py`；`commerce/ws_order_gateway.py`；`commerce/browser_flow.py`；`migration/test_continuation.py` | 续页、退款/关闭防旧状态覆盖、多明细身份、切账号后迟到列表/内容隔离 |
| AT17 运营计划与暂停 | `products/test_polish_schedule.py`；`products/test_feedback.py`；`products/test_scheduler.py`；`migration/test_polish_schedule_mapping.py` | 随机计划持久、跨午夜/时区/重启、适用性与防重 |
| AT18 通知渠道与模板 | `admin/test_notifications.py`；`admin/test_templates.py`；`admin/test_legacy_templates.py`；`admin/test_account_events.py` | 逐渠道受理、模板、故障代次及恢复通知 |
| AT19 后台归属与管理防护 | `admin/test_auth.py`；`admin/test_auth_lifecycle.py`；`runtime/auth_boundary_suite.py`；`runtime/asset_privacy_suite.py` | 身份生命周期、定向解锁、降权/停用及资源归属 |
| AT20 配置部分应用与类型 | `accounts/test_config_inheritance.py`；`accounts/test_infrastructure.py`；`accounts/browser_policy.py`；`runtime/scheduler_config_suite.py` | 部分应用、并行确认、0/false/空/继承与旧编辑入口 |
| AT21 shaxiu参考语义 | `bargaining/test_history.py`；`bargaining/test_candidate.py`；`bargaining/browser_smoke.py`；`integration/test_candidates_mysql.py` | 反例、事件唯一、完整最近往来和MySQL重启 |
| AT22 监控基线与空/错结果 | `monitor/test_monitor_reliability.py`；`monitor/test_monitor_transport.py`；`monitor/test_monitor_ui.py` | 完整基线、真实空与HTTP/平台/结构错误 |
| AT23 监控乱序、重启与降价 | `monitor/test_monitor_reliability.py`；`integration/test_candidates_mysql.py` | 代次/页码、重启、价格修订及通知分离 |
| AT24 监控与客服共享约束 | `monitor/test_monitor_scheduler.py`；`dispatch/test_monitor_wiring.py`；`dispatch/test_budget.py` | 账号选择、冲突代理/私信/下单待处理、共享预算 |
| AT25 全量迁移完整性 | `migration/test_snapshot.py`；`migration/test_mapping.py`；`migration/test_continuation.py`；`migration/test_mysql.py` | 完整样本、附件、归属、规范化校验、配置/库存/历史 |
| AT26 重复导入与坏输入 | `migration/test_runner.py`；`migration/test_cli.py`；`migration/test_rollback_control.py` | 重复/中断、坏输入、未知键和目标已修改 |
| AT27 单账号交接和增量回退 | `migration/test_handoff.py`；`migration/test_rollback_control.py`；`migration/test_mysql.py` | 本地单账号交接、消息/订单/卡密增量；真实交接待执行 |
| AT28 SQL.gz备份和恢复 | `admin/test_backup_roundtrip.py`；`admin/test_backup_protection.py`；`admin/test_backup.py` | 正式SQL.gz导出→第二MySQL，值/约束、坏备份和保留门 |
| AT29 日志、清理与敏感内容 | `runtime/error_privacy_suite.py`；`admin/test_log_archive.py`；`admin/test_audit_retention.py`；`admin/test_data.py`；`admin/test_stats.py` | 秘密脱敏、终态归档、保护待核实和统计口径 |
| AT30 阶段发布与真实业务 | `verification/test_runner.py`；`runtime/process_smoke_suite.py`；`release/test_gate.py` | 工程验证器及发布检查器；真实场景/48小时/部署仍待执行 |

## G01–G03工程链核验

| 核验 | 原缺口及已接路径 | 验证入口 |
|---|---|---|
| G01 登录防护 | 原版仅用户计数，缺用户名/IP分离管理；补持久统计、配置、定向解锁及即时身份核验，不以验证码弹窗代替管理链 | AT19；`tests/admin/test_auth.py`、`test_auth_lifecycle.py`、`browser_flow.py` |
| G02 手动Cookie导入 | 创建持久任务→归属与版本→验证→条件写回；轮询、取消、超时、旧结果与同账号身份均明确 | AT01/04；`tests/accounts/test_api.py`、`test_infrastructure.py` |
| G03 人工验证 | 原登录入口→同账号浏览器会话→截图/指针操作→再次检查凭据→关闭资源；取消/过期不清资料 | AT05；`tests/accounts/test_browser_session.py`、`test_verification_pointer.py`、`test_browser_proxy.py` |

G项关闭按上述实际调用链和本批通过日志登记在外部开发台账；静态研究矩阵中的“待查证”保持历史原文。

## D01–D19旧差异承接

固定SHA256见 [原始处置输入](provenance/local-patch-dispositions.json)。旧源码原地只读，最终逐项保留检查写入本批归档；“留档”不是删除。

| ID | 旧文件 | 本次承接 / 验证 |
|---|---|---|
| D01 | `.dockerignore` | 部署差异留档；原版多服务配置和隔离编排，F48及三服务进程测试 |
| D02 | `Dockerfile-cn` | 不移植单体镜像；原版浏览器依赖和固定代理，F06/F48 |
| D03 | `XianyuAutoAsync.py` | 按能力接入账号维度暂停、AI出站过滤及出口一致；F06/F13/F20 |
| D04 | `db_manager.py` | 固定GuDong同字节；映射正式模型/事务与S3，不复制单体数据库管理器 |
| D05 | `docker-compose-cn.yml` | 旧编排留档，独立MySQL/Redis/恢复库与关闭自动业务，F48 |
| D06 | `docker-compose.yml` | 旧端口/挂载不搬入新服务；三服务隔离测试，F48 |
| D07 | `reply_server.py` | 固定GuDong同字节；管理能力按F01–49映射，复用原版API |
| D08 | `static/css/items.css` | 固定GuDong同字节；React商品/规格页面承接，F25/F27与Chromium |
| D09 | `static/index.html` | React补来源选择、AI出站及全账号过滤，不搬旧HTML；F20 |
| D10 | `static/js/app.js` | 同上，实际按钮/API/状态测试，`tests/replies/browser_flow.py` |
| D11 | `static/update_log.txt` | 历史元数据保留，增强版用源码/构建身份；F48 |
| D12 | `static/version.txt` | 历史版本保留，原版版本接口增强；F48 |
| D13 | `tests/test_slider_verification_guards.py` | Token正文JSON及query/form单次编码；真实浏览器取消后回收进程，`test_legacy_guards.py`/`test_browser_proxy.py` |
| D14 | `utils/item_publisher.py` | 固定GuDong同字节；原生发布服务/SKU/图片接入执行方；F27/F28 |
| D15 | `Dockerfile-runtime` | 单体运行镜像留档；增强版原多服务构建与独立数据；F48 |
| D16 | `docker-compose-cn.yml.bak-network-20260627-102032` | 历史部署备份保持原样，不作为有效运行配置；D05部署处置 |
| D17 | `docker-compose.yml.bak-netnorm-20260627-121659` | 历史部署备份保持原样，不作为有效运行配置；D06部署处置 |
| D18 | `utils/item_pagination.py` | 固定GuDong同字节；分页去重/局部失败保留；F25/F26 |
| D19 | `utils/product_sku.py` | 固定GuDong同字节；完整规格、订单明细、数量及整单预占；F27/F29 |

## 后续发布界线

- 本地开发覆盖与正式候选发布是两张清单。完整回归通过后执行一次固定HEAD总审查，修复有效发现并复验。
- 提交、推送、从干净提交构建三服务镜像、部署、单账号交接和至少48小时观察分别保留证据；当前工作区的成功测试不会自动生成这些许可或事实。
- 来源/许可见 [SOURCES](SOURCES.md)，接口见 [API](API.md)，迁移/备份操作见 [OPERATIONS](OPERATIONS.md)，候选回执见 [RELEASE](RELEASE.md)。
