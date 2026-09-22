# SQLite迁移、单账号交接和增量回退

对应 DEV-39–42，S3入口为 `tools.migration`。工具读取显式一致性SQLite样本和独立目标；不自动读应用`.env`，不连接现用旧库，不启动账号业务。

## 已接映射

| 范围 | 处理 |
|---|---|
| 身份、账号和配置 | 原ID→目标ID、账号业务身份/归属校验；保留合法0/false、账密/代理、备注和旧开关；目标账号/用户/卡券保持停用 |
| 回复、AI和历史 | 关键词、图片、专属/默认、旧只回一次作用域、legacy策略、预设、聊天、人工暂停、黑名单；用户全账号过滤保留未来账号范围和独立暂停分钟 |
| 通知 | 旧事件类型及受控变量转换，分别保存模板和渠道；未知变量/事件冲突明确报告，不覆盖别的模板 |
| 商品与规格 | 商品、素材、完整双维规格、订单明细、卡券规格与归属；图片按哈希/用户迁址并写真实引用 |
| 规则和履约 | 原生标题规则及发放倍数；旧双向SQLite LIKE语义含%/_与ASCII大小写，零分有效匹配保留；未知逐件发送事实保持unknown与库存占位 |
| 运营 | 评价模板、激活配置、评价/求花日志与独立防重屏障；擦亮保存原计划和周期，旧`delay_minutes`实际是小时，`random_delay_max`是分钟 |

旧标题兼容规则仅处理明确登记的旧规则，不拿模糊标题替代新订单SKU核验。新SKU规则、整单多明细预占和界面管理见 [订单履约](commerce.md)。

## 完整性与失败处理

- 动态盘点实际表/字段、附件、来源身份、规范化校验和及检查点；不把研究时的表数写死为输入结构。
- GuDong `enc$`仅在明确源密钥下解密原先加密的Cookie/密码/代理密码字段；原始输入加密留档，公开报告只给哈希和原因。
- 后台密码散列单独识别兼容性；未支持算法进入受控重置，不再散列已有散列或推测明文。
- 同批次幂等，源修改须`--incremental`；目标已发生新修改、唯一身份冲突或归属变化先整批预检失败，不覆盖新事实。
- 图片拒绝路径穿越、符号链接、错哈希/归属；远程引用只消费明确提供的本地文件，不联网下载。
- 未知表/字段/旧设置、非等价运营计划、缺素材快照、缺逐件履约证据等留在加密原始归档及冲突报告。`--quarantine`只允许导入已知部分，阻断项仍阻止启用；不把隔离导入称为完整业务切换。
- 迁移账本仅两张表，业务写已注册的正式模型；目标须先完成增强版启动建表/迁移，`--init-journal`只建迁移账本。

## S3命令

使用仓库Python环境。密钥与目标DSN通过私有环境提供，日志不打印其值：

- `XYMB_MIGRATION_KEY_HEX`：迁移归档密钥。
- `XYMB_MIGRATION_TARGET_URL`：CLI仅接受本机`xymb_migration_*` MySQL演练库。
- 源密钥二选一：`--source-key-env` 或权限0600的 `--source-key-file`。

```bash
.venv/bin/python -m tools.migration inspect \
  --source "$SOURCE_SNAPSHOT" --namespace rehearsal --source-key-env XYMB_LEGACY_KEY
.venv/bin/python -m tools.migration apply \
  --source "$SOURCE_SNAPSHOT" --namespace rehearsal --source-key-env XYMB_LEGACY_KEY \
  --archive-dir "$PRIVATE_BATCH_DIR" --init-journal --quarantine
.venv/bin/python -m tools.migration verify \
  --source "$SOURCE_SNAPSHOT" --namespace rehearsal --source-key-env XYMB_LEGACY_KEY \
  --archive-dir "$PRIVATE_BATCH_DIR" --account "$ACCOUNT_ID"
.venv/bin/python -m tools.migration rollback-export \
  --source "$SOURCE_SNAPSHOT" --namespace rehearsal --source-key-env XYMB_LEGACY_KEY \
  --archive-dir "$PRIVATE_BATCH_DIR" --account "$ACCOUNT_ID"
```

`inspect`只报告；`apply`写显式隔离目标；`verify`检查选中账号；`rollback-export`输出加密回退包。坏输入/冲突非零退出。附件可选参数：`--asset-manifest --asset-source-root --asset-target-root`；运行暂停输入：`--runtime-state`，绑定namespace、源校验和、归属和15分钟时效。

图片清单为数组，每项含 `reference/local_path/source_owner/sha256`；目标根对应 `/static/uploads/replies/` 的物理目录。Web/消息服务按部署需要共享同一受控素材卷，而非各自重新下载。

## 停写边界与交接

`LegacyControl`沿旧系统的账号停用/运行态接口；默认dryrun，不发请求。`--execute`才调用控制端，令牌用`XYMB_LEGACY_CONTROL_TOKEN`：

```bash
.venv/bin/python -m tools.migration legacy-stop \
  --legacy-url "$LEGACY_CONTROL_URL" --account "$ACCOUNT_ID" --dryrun
```

真实执行还需`--legacy-boundary`指向观测者提供的新鲜停写边界，包含`account_id/captured_at/stopped/watermark/inflight`。仅返回running=false不证明在途已排空；工具不伪造水位或空集合。

`Handoff.switch`按账号检查旧执行方停写、在途集合、最终一致增量和目标检查点，再允许新方接管。任一条件不满足保持账号暂停；其他账号原状。实际切换要另行确认发布及账号交接。

## 回退保留新增事实

```bash
# 省略--execute为预检；OFFLINE_STAGE必须是明确准备的离线SQLite副本。
.venv/bin/python -m tools.migration rollback-import \
  --namespace rehearsal --account "$ACCOUNT_ID" --archive-dir "$PRIVATE_BATCH_DIR" \
  --bundle "$ROLLBACK_BUNDLE" --rollback-stage "$OFFLINE_STAGE" --execute
```

回退先持久停用选中账号，再预检订单、新聊天、库存、占位及版本，冲突时不写业务行。只投影可兼容字段，保留旧端独立备注；多明细/待核实事实留包对账，不合并为一条已发送记录。用户共享的全账号配置明确处理，其他用户/账号秘密不打包。

连续包使用加密回执校验先后和期望状态；旧包不回盖新事实。已提交而回执写入失败可重放同包恢复。旧端无等价字段或新卡券身份列为gaps，保持暂停，不自动重开旧执行方。回退SQLite事务与回执顺序由单一Handoff协调器串行处理。

## 验证

`tests/migration/`覆盖一致性、密文、未知字段、图片、原生规则/过滤/计划、重复/中断/增量冲突、真实MySQL及离线回退。`test_handoff.py`使用真实隔离Redis；`test_rollback_control.py`使用实际本地HTTP控制端及离线SQLite。`tests/admin/test_backup_roundtrip.py`验证SQL.gz→第二MySQL恢复。

当前证据来自合成业务数据，不包含现用全量数据库或真实停写观测者。正式切换和48小时观察按 [发布门禁](../RELEASE.md)，不以本地工具通过替代。
