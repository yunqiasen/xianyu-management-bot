# 唯一账号执行方与网络出口

对应账号、回复、商品、监控及履约的共同通信边界。

```text
Web / scheduler
  → AccountDispatchClient（内部令牌、持久请求ID）
  → /internal/account-operations
  → MySQL 操作意图
  → 现有消息执行方：owner / 租约代次 / 配置及凭据版本
  → 最新规则、暂停、共享 Redis 预算
  → 账号绑定代理及既有 HTTP / WebSocket
  → confirmed / failed / unknown
```

启动入口已注册模型、迁移、内部路由及执行器。`ImSessionManager` 是执行方客户端，不另建平台登录；旧 `ImClient` 的独立连接入口停止使用。`mtop_call`、商品同步、搜索、平台名单、订单读取、运营动作与媒体上传均通过固定命令派发。

## 命令与身份

- 聊天：`get_conversations`、`get_messages`、`send_text_message`、`send_image_message`、`recall_message`。
- 平台：`platform_request` 只允许登记的API和指定业务头，不接受调用方Cookie、认证头、Host或任意平台URL。
- 媒体：`upload_image`、`upload_video`；监控：`monitor_search_page`。
- 运营：`polish_item`、`rate_buyer`、`request_red_flower`。
- 保留命令 `sync_items` / `publish_single` 未默认注册，返回 `command_unavailable`；实际同步和发布由原业务服务拆为已注册的底层命令，非通过这两个保留命令运行。

操作/事件身份以MySQL 8 NO PAD二进制排序规则区分大小写和尾空格。同请求ID同内容返回原操作；内容不一致报冲突。网络丢回执保留原ID查询，客户端不重新提交。断进程遗留 submitted 到期后转 unknown；缺少可靠平台核对时留待人工，不猜成功或重新执行。

## 请求预算

`AccountRequestBudget` 采用 Redis TIME + Lua；账号换执行方、换命令或换配置均不清零共享预算。缺失/非法业务频率停止业务并提示配置。平台 Retry-After 支持秒数和 HTTP-date，冷却仅延长。

等待可取消，外发前重新核对账号、规则、版本与执行权。明确未发送的等待/失败与已外发待核实分开。恢复的“两次”是另一条失败保护，不作为业务请求限额。

## 媒体

- Web传入本地已校验内容或远程URL，执行方完成读取和上传；远程图片/视频不在Web进程自行下载。
- 远程URL仅HTTP/HTTPS、80/443；逐次解析并检查全部IP，连接已检查IP并保留Host/TLS名称。跳转重新校验且有次数上限；私网、用户信息和不合规URL在请求前终止。
- 下载不带账号Cookie、不继承环境代理、不保存第三方Cookie；固定账号代理、执行权与预算仍有效。图片10MB、视频100MB上限，限流保留共享冷却，不内联重试。
- 本地文件需账号所属用户的资源目录；图片/视频上传及发送沿用同一持久操作身份，分段回执丢失保留待核实。

## 验证

`tests/dispatch/` 覆盖真实FastAPI客户端/执行器、回执、重复提交、最新规则、在途失权、媒体、固定代理及预算。`integration_check.py` 使用隔离MySQL/Redis；`test_proxy_transport.py` 使用真实认证HTTPS CONNECT；`test_remote_media.py` 使用本地来源端与认证代理，检查实际出站而非只检查函数参数。

平台端点为可控样本，HTTPS远程媒体目标的实际站点兼容性及正式平台回执仍需发布试跑。
