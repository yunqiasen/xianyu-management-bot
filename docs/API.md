# 增强版接口契约

范围：XYMB-SPEC-001 v1.0。本页记录新增及改变语义的入口；原版其余接口沿用运行服务的 OpenAPI。前端统一前缀 `/api/v1`，后台用户令牌为 `Authorization: Bearer TOKEN`。`account_id` / 旧名 `cookie_id` 都是账号标识，不是 Cookie 正文。

## 共同规则

- 老接口可能返回 HTTP 200 + `success:false`，新接口也使用 401/403/404/409/422/503；同时检查 HTTP 状态与响应体，别只检查 200。
- 202 / `submitted` 仅表示受理；`confirmed` 表示平台确认受理；`failed` 是明确未完成；`unknown` 表示结果待核实，不直接重发。同请求ID同内容返回原操作，内容变化报冲突。
- 路由使用当前数据库用户角色及资源归属，不靠令牌中的旧角色。普通用户只操作自己的资源；管理员接口单独校验管理员身份。管理角色不隐式获得每个卖家专用路由的跨用户代操作能力。
- 省略、空白或脱敏占位的秘密字段保留原值；清除用独立字段。普通列表、错误及日志不返回原Cookie、密码、密钥、卡密正文。
- `config_version`、凭据版本、规则版本是不同版本；带当前版本保存，409 后先重读，别盲目重复覆盖。

## 后台身份

| 方法和路径 | 行为 |
|---|---|
| `POST /auth/register` | 邮箱验证码注册；注册关闭时403，且不消费已有验证码 |
| `POST /captcha/send-email-code` | 注册/登录/重置密码邮件；注册关闭时停止发送注册码 |
| `POST /auth/login` | 账号/邮箱密码或邮箱验证码登录；密码方式遵守后台验证码设置 |
| `GET /auth/verify` | 检查access token，不接受refresh token作为登录令牌 |
| `POST /auth/refresh` | Bearer内传refresh token，按用户状态及token_version验证 |
| `POST /auth/logout` | 撤销该用户当前版本的全部旧令牌并清除媒体会话 |
| `POST /auth/reset-password` | 邮箱验证码重置，成功后旧令牌失效；短密码不消费验证码 |
| `POST /users/change-password` | 当前登录用户修改密码并撤销旧令牌 |
| `GET/PUT /admin/login-protection/config` | 统计窗、锁定秒数、用户名/IP阈值；PUT需`expected_version` |
| `GET /admin/login-protection`、`POST /admin/login-protection/unlock` | 分维度统计和定向解锁，不联动清除另一维度 |

后台身份与闲鱼扫码/账密是两套会话。默认后台登录防护为15分钟窗，用户名5次、IP20次，锁定15分钟。

## 账号、代理及配置

| 方法和路径 | 主要输入/输出 |
|---|---|
| `GET /cookies/{account_id}/runtime` | 业务状态、连接状态、原因、最近成功、下次重试、版本、待应用服务、任务及秘密配置标志 |
| `POST /cookies/{account_id}/credential-jobs` | `{value, expected_version}`；返回持久job，202不等于验证通过 |
| `GET/DELETE /cookies/{account_id}/credential-jobs/{job_id}` | 查询/取消；先核对凭据/配置/代次，旧绑定返回 `superseded`，保持当前会话状态；取消或过期结果不覆盖凭据 |
| `GET /cookies/{account_id}/delete-preview` | 展示关联；未完成业务意图阻止资料删除 |
| `PUT /cookies/{account_id}/login-info` | 保存账密；独立清除动作，空白保留 |
| `GET/PUT /proxy/{account_id}` | `proxy_type/host/port/user/pass`完整字段名见下例；端口1–65535的整数 |
| `GET/PUT /cookies/{account_id}/request-policy` | 共享业务频率；PUT需`expected_config_version`，不是恢复次数设置 |
| `GET /cookies/{account_id}/configuration/effective` | 注册字段类型、来源、有效值、用户/系统/账号版本 |
| `PUT /cookies/{account_id}/configuration/settings` | `{scope,key,action,value,expected_version}`；scope为account/user/system，action为set/inherit |
| `POST /cookies/{account_id}/configuration/reload` | `{config_version}`；返回`complete/pending_consumers/results`，部分应用保留缺失消费者 |

固定代理示例（订阅先在独立客户端转固定节点端口）：

```json
{"proxy_type":"http","proxy_host":"HOST","proxy_port":7890,"proxy_user":"USER","proxy_pass":"PASSWORD","clear_password":false}
```

`none/http/https/socks5`之外的类型报错。代理变更递增配置版本并暂停旧业务；绑定失败时保留错误，不借用别的出口。`clear_password:true`才明确清除代理密码。

类型设置示例：

```json
{"scope":"account","key":"pause_duration","action":"set","value":0,"expected_version":3}
```

合法0、false、空字符串、允许字段的null与继承分别处理。系统默认值仅管理员修改；旧账号显式值不因设置默认值而被覆盖。

## 回复、历史与图片

公共根为 `/chat-new/reply-controls/{account_id}`，不是顶层 `/reply-controls`。

| 相对公共根的方法/路径 | 行为 |
|---|---|
| `GET/PUT /policy` | 新账号`ai_first`；迁移账号`legacy` |
| `GET/POST /exclusive`、`POST /exclusive/import`、`DELETE /exclusive/{id}` | 商品专属规则；与商品默认回复分开 |
| `GET/POST /filters`、`PUT/DELETE /filters/{id}` | 含版本的高级过滤；`all_accounts:true`作用于当前后台用户的全部现有及未来账号 |
| `GET /events?after=0&limit=100` | 提交水位增量，`nextCursor/hasMore`；上限200 |
| `GET /history/{chat_id}?before=CURSOR&limit=50` | 最近历史向前分页，`pauseRemaining`按账号/会话隔离 |
| `GET/POST /images`、`GET /images/{id}/references`、`DELETE /images/{id}` | multipart字段`image`；有归属、哈希、格式/大小/像素和引用保护 |
| `GET /outbound/{chat_id}/{request_id}`、`POST .../verify` | 查询发送阶段及核对未知结果 |
| `POST /platform-blacklist/sync` | 经现有账号执行方同步，不新建连接 |

过滤字段：`pattern`、`match_mode=contains/exact/regex`、`source=user/system/ai/all`、`item_id`、`actions`、`enabled`、`version`，以及`all_accounts`、`pause_minutes`。动作：`notify/skip_ai/skip_reply/pause/skip_notify`。`pause_minutes`为0–1440整数或null；null使用账号暂停设置。

人工发送沿用 `POST /chat-new/send-message/{account_id}`；图片沿用原聊天图片入口。人工/程序/机器人共享历史和发送身份。平台确认不表示买家已读；回执不明时保留unknown。

## AI

根为 `/ai-reply-settings`。`GET/PUT /{cookie_id}`读取/保存；`POST /ai-reply-test/{cookie_id}`测试当前已保存配置；模型目录为`POST /ai-reply-settings/models`。

- 协议值：`openai_compatible/responses/azure/anthropic/gemini/dashscope_app`。
- 常用字段：`ai_enabled/provider_type/model_name/base_url/api_key/custom_prompts/timeout_seconds/max_tokens/temperature/max_context_chars/config_version`；默认60秒、512输出token、24000字符上下文预算。
- Azure另传`azure_deployment/azure_api_version/azure_auth_mode`；DashScope应用另传`app_id`。根地址与完整请求地址仅规范化一次。
- `api_key_configured`标明已有密钥；`clear_api_key`是清除操作。测试与真实消息共用网关，不在测试接口临时切另一条路径。
- 预设：`GET/POST /presets`、`PUT/DELETE /presets/{preset_id}`、`POST /presets/{preset_id}/apply`；应用输入`account_ids`，逐账号返回。删除预设不影响已保存副本。
- `GET /{cookie_id}/bargaining-diagnostics`显示P5次数与历史裁剪诊断；默认关闭时返回`candidate_enabled:false`。

## 商品、发布和订单

| 方法和路径 | 行为 |
|---|---|
| `GET/PUT /items/polish-window/{account_id}` | `timezone_name/start/end/randomize`；保存窗口不打开自动擦亮开关 |
| `POST /items/polish/{account_id}`、`GET .../history` | 手动擦亮与逐件结果，遵守共享预算 |
| `GET /items/operations/{account_id}/history` | 平台上下架/删除等操作证据 |
| `/product-publish/materials`及`/{id}` | 素材CRUD；完整规格及已发布快照保持独立 |
| `POST /product-publish/publish/single|batch` | 持久发布意图；部分成功逐件显示 |
| `GET /product-publish/publish/batch/{batch_id}/status` | 查询批次，不把提交显示为完成 |
| `POST /product-publish/publish/batch/{batch_id}/{action}` | 取消/重试按允许状态处理，unknown先核对 |
| `POST /product-publish/logs/{log_id}/reconcile` | 核实平台商品身份，保留证据 |

订单增强根为 `/orders/commerce`：

- `GET/POST /history`、`POST /history/{job_id}/step|cancel|resume`：长历史任务/续页。
- `GET /order-lines?account_id=ACCOUNT&order_no=ORDER`：规范化订单明细。
- `GET/POST /rules`、`PUT /rules/{rule_id}`：标题规则、优先级、每次发放倍数、账号及卡券归属；更新需`expected_version`。新增默认关闭。
- `GET /rule-preview?account_id=ACCOUNT&order_no=ORDER`：规则与库存预览。
- `GET /intents`、`GET /intents/{id}/content`：阶段列表脱敏，内容仅所属用户读取。
- `POST /intents/{id}/confirm`：只补平台确认；`advance`：继续尚未发送阶段；`reconcile`：提交明确证据，不重复生成意图。
- `POST /intents/{id}/resend`：真正补发，必须传新`request_id`、`acknowledged:true`和`reason`。
- `POST /intents/{id}/query-supplier|supplier-evidence`：采购结果核对，不重复扣费生成。

多SKU以`line_id`区分，整单全部预占成功才推进；任一明细unknown先核对。内容发送与平台发货确认是两个完成维度。

## 通知、日志和备份

- `/notification-channels`：渠道CRUD；`POST /{id}/test`真实执行该适配器。QQ是OneBot HTTP私聊/群聊适配，不是普通Webhook别名。
- `/message-notifications/{cookie_id}`：账号绑定；`/notification-channels/templates`及`/{event_type}/preview`管理模板；`/notification-channels/deliveries`查询逐渠道状态。
- `/admin/logs`、`/admin/logs/export`：跨服务关联查询及脱敏导出。`GET /admin/log-archive`查结构化运行日志归档；`source_table=xy_admin_audit`查180天前的操作审计归档。
- `/admin/operating-summary`：明确时间窗、时区、付款/退款口径，账号数量不冒充业务可用数量。
- `/db-backup-logs`、`/{id}/download`：备份结果及SQL.gz文件。
- `POST /db-backup-logs/{id}/verify?restore=false|true`：文件校验或实际独立MySQL恢复。恢复DSN只读后端`ADMIN_RESTORE_DATABASE_URL`；请求中不接受任意目标库。
- `GET /db-backup-logs/{id}/verifications`：最近20条及所有保护记录；`PUT .../verifications/{verification_id}/protection`传严格布尔`{"protected":true}`。保护前再次校验文件SHA256，变更留审计，重复设置幂等。
- `/admin/data/{table}`：白名单、分页、掩码。通知配置删除先`POST .../preview`，然后携带`preview_id`和`confirmation=DELETE`；需与当前数据匹配且实际恢复过的备份。其余业务表只读。

监控增强沿用 `/product-monitor/listing-tasks/{task_id}/reliability` 的GET/PUT；与账号策略冲突的旧任务配置保持原值并提示处理。内部 `/internal/*` 使用独立服务令牌，不供浏览器调用。

## 兼容与文档

旧单体调用方迁移到本页的原版多服务接口，不并行启动第二套回复执行方；旧“下载/导入数据库”占位入口返回真实能力入口和明确状态。迁移及回退见 [迁移说明](implementation/migration.md)，日常操作见 [使用说明](OPERATIONS.md)，验证入口见 [覆盖索引](COVERAGE.md)。
