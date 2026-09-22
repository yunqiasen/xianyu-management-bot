# 候选版本门禁与交接

适用 XYMB-SPEC-001、DEV-43/44/47；工程实现及测试与正式部署分别验收。固定审查基线：`fdc8eb039be771456ecfbbbe43fa26f57feb3947`，增强分支：`xianyu-management-bot-fork`。

## 三道独立检查

| 命令参数 | 通过含义 | 不代表 |
|---|---|---|
| `--gate code` | 已提交干净源码、全范围证据、构建、迁移恢复、文档和主代理总审查齐备 | 获准推送、部署或真实业务成功 |
| `--gate trial` | code 门通过，另有 commit / push / deploy 三份显式许可 | 已接管账号、已试跑、可扩大范围 |
| `--gate acceptance`（默认） | 上述条件及真实单账号场景、至少48小时观察齐备 | 自动发布命令或后续版本的许可 |

检查器只读取 Git、清单、源码和证据文件；不联网、不连数据库、不构建、不修改标签、不提交、不推送、不部署。退出 `0` 仅指选中的门通过；退出 `1` 为缺项/不通过，`2` 为命令或输入错误。报告始终分别给出 `code_verified`、`trial_ready`、`release_accepted`。

`init` 的退出 `0` 仅代表生成待填写清单，所有验收项仍为 pending。

## 建立、更新证据

证据目录放在源码仓库外；否则未跟踪证据会使干净工作区检查失败。以下操作仅生成本地 JSON：

```bash
# EVIDENCE_DIR 由维护者指定为受控、独立的证据目录。
.venv/bin/python -m tools.release init --candidate first > "$EVIDENCE_DIR/candidate.json"
.venv/bin/python -m tools.release check \
  --manifest "$EVIDENCE_DIR/candidate.json" --evidence-root "$EVIDENCE_DIR" \
  > "$EVIDENCE_DIR/report.json"
# 只检查离线工程门时，显式追加 --gate code。
# 第二候选另建清单：init --candidate enhanced
```

默认读取仓库规格及 `docs/provenance/` 中冻结的 `feature-coverage-matrix.json`、`local-patch-dispositions.json`；位置可用 `--spec/--matrix/--patches` 指定，内容 SHA256 仍须匹配固定输入。输入缺失、缩减或改动都阻断；不访问原始旧库。规格变更需要有审查的门禁升级，而非删除验收项。

主代理逐项追加真实证据回执到 `evidence`，再将 `coverage[范围ID]` 填为 `{"status":"passed","evidence":["receipt-id"]}`。同一个回执可覆盖多个范围，但回执自身须逐项列明 `scope`，且附件实际包含每项结果。保持源码版本、报告范围、收集时间一致；源码变更后重跑关联验证并更新哈希。

`source.tags` 是首次建立该候选清单时的旧标签快照（含 annotated tag 对象）。保留初始清单及其哈希供总审查核对，勿通过重建快照掩盖移动或删除。新标签可另建；既有标签保持原指向，禁用强制挪动。检查开始和结束都核验 Git 状态；正式收集时停止并行源码写入。

## 回执契约（schema_version=1）

清单中的每条 `evidence`：

```json
{"id":"f06-http","path":"receipts/f06-http.json","sha256":"填写回执文件完整SHA256"}
```

回执文件示例结构如下。这里故意是待验收状态，不是可直接用于通过的证据：

```json
{
  "schema_version": 1,
  "id": "f06-http",
  "candidate": "first",
  "commit": "填写40位候选源码提交",
  "scope": ["F06", "AT03"],
  "kind": "integration",
  "seams": ["S1", "S2"],
  "status": "pending",
  "synthetic": true,
  "checks": {"executed": 0, "failed": 0, "skipped": 0},
  "attachments": [{"path":"logs/f06-http.json","sha256":"填写附件完整SHA256"}],
  "entrypoints": [
    {"path":"common/services/account_policy.py","role":"implementation","start_line":1,"end_line":10,"sha256":"填写源码文件完整SHA256"},
    {"path":"tests/accounts/test_transport.py","role":"test","start_line":1,"end_line":10,"sha256":"填写测试文件完整SHA256"}
  ]
}
```

- `kind`：`integration/browser/build/static/migration/restore/runtime/document/review/approval`，与范围要求一致。函数探针或源码存在不算 `integration`。
- 执行类回执：`executed > 0`、`failed = 0`、`skipped = 0`；跳过或无测试的绿色进程不算通过。
- `integration` 须同时关联已跟踪实现入口和测试入口；检查文件完整哈希、真实行范围及角色，不仅检查路径存在。
- 至少一个附件；回执和附件均检查完整文件 SHA256。路径相对证据根目录，禁止绝对路径、越界和符号链接；文件缺失、篡改、重复ID或重复JSON键均失败。
- `synthetic=true` 表示回执本身是演示/伪造结果，始终不算验收。实际跑完的离线测试即使使用合成业务数据，其执行日志仍是真实测试证据；可填 `synthetic=false`，但 kind 仍为 integration 等，绝非 runtime。测试输入类型在附件中说明。
- 只有真实平台试跑记录才填 `kind=runtime`、`synthetic=false`。本项目 `tests/release` 的正向判定测试全部属于临时合成环境，禁止登记为发布回执。
- 哈希证明文件一致，不证明作者诚实、附件完整业务语义或平台真实执行。主代理总审查必须核对附件、场景断言及来源；工具不自动从测试文件名推断通过。
- 错误输出只给固定原因码、受控范围ID、计数和哈希，不回显路径、回执正文、账号、异常文本或秘密。附件本身先脱敏，勿放凭据、卡密、业务库和原始截图。

## 覆盖范围

- 第一候选：F01–F49（对应 US-001–049）、AT01–20及AT25–30、G01–03、D01–19，加15项工程门；P5明确排除。
- 增强候选：上述全部，加 AT21–24、DEV45、DEV46；重新收集候选版本和许可，先核实现用实际版本，不假设第一候选已部署。
- D01–19 对应原始处置清单的固定顺序，见 [COVERAGE.md](COVERAGE.md)。归档项关联新版本/部署入口的验证，保留处置说明；相同于GuDong的6项不重复移植。
- S1/S2/S3 要求从规格表解析，单个覆盖项可由多个同类回执合并入口，但每条引用须属于该项。

| 工程门 | 回执 kind / 必需入口 | 附件最低内容 |
|---|---|---|
| regression | integration / S1 S2 S3 | 本候选全量回归、场景断言和实际测试统计 |
| frontend_build | build | tsc、构建日志；独立于页面操作 |
| candidate_build | build | 干净提交构建记录、三服务镜像不可变摘要清单；回执额外 `source_clean:true`、`image_digest:"sha256:…"` |
| frontend_operations | browser / S1 | 关键按钮、保存/应用、加载/错误、危险动作预览、断线恢复 |
| static_checks | static | 类型/静态检查日志 |
| api_contracts | integration / S1 | 正式路由、前后端契约、权限、错误和任务阶段 |
| mysql_redis | integration / S1 S2 | 隔离真实MySQL事务/约束、Redis和并发/重启结果 |
| migration | migration / S3 | 一致性样本、全表/附件、ID映射、数量/规范化校验和、未知键和冲突、重复导入/续跑 |
| restore | restore / S1 S3 | 真正 SQL.gz 导出与隔离MySQL恢复、约束和坏备份失败 |
| rollback | migration / S1 S2 S3 | 在途登记、交接边界、增量对账、消息/订单/卡密占位/确认保留 |
| documentation | document | 使用、接口、迁移、恢复文档与当前提交对应 |
| sources | document | 固定来源、许可证、署名和19项处置 |
| review | review | 主代理唯一总审查，审查范围从固定基线到候选提交 |
| caller_integration | integration / S1 S2 | Web、消息、调度、监控及旧调用方共用执行权/代理/预算/持久状态 |
| recovery_points | restore / S3 | 旧镜像、经过验证的恢复点、切换前检查点及保留策略 |

`image_digest` 对多服务指不可变发布镜像清单摘要（附件列出每个服务的镜像摘要），单镜像可直接用镜像摘要；实际部署证据必须与构建记录一致。

测试入口按领域独立进程运行，避免不同服务的 `app` 和同名测试模块串用。现有入口盘点在 COVERAGE；不要把根目录 discover 的空结果当全回归。发布检查器自测：

```bash
.venv/bin/python -m unittest discover -s tests/release -v
```

## 三项许可与真实观察

`permissions.commit/push/deploy` 分别填 `approved:true` 和独立证据引用。对应回执 `kind=approval`、`scope=["permit:commit"]`（或 push/deploy）、`approved:true`、同一候选与提交；附件保留脱敏的明确许可、时间和批准者别名。实施批准、拆票批准、一次试跑批准均不自动代替这三项。

顺序：实现→回归/文档→主代理唯一总审查→单独获准提交→单独获准推送→单独获准部署→实际发布验证。提交许可先按审定变更范围记录，提交产生后关联最终SHA并复核；检查器不会代替前置许可，更不会执行这些动作。

`observation` 必填：带时区 `started_at/ended_at`、`account_alias=account-01`、`unresolved_severe=0`、`actual_version_verified=true`、实测 `previous_version`（40位SHA）、`image_digest`、`data_checkpoint=checkpoint-…`。时间至少48小时，结束不晚于检查时刻；只观察一个脱敏账号别名。

十二项 `scenarios` 均填通过回执（`scope=["real:场景名"]`、`kind=runtime`），每条带同一 `account_alias` 及窗口内 `occurred_at`：

1. messages：真实收发及确认，非只连上WebSocket。
2. ai：真实消息链采用所配模型，不只是测试按钮。
3. publish_single / publish_batch：分别记录平台商品身份、部分成功与未知结果。
4. orders_inventory_confirmation：订单、库存、内容发送、平台确认分别对账。
5. notifications：实际渠道受理及账号归属。
6. recovery：故障后真实恢复，不以在线时长代替。
7. handoff_single_executor：旧执行方已停、在途登记、最终一致增量、新执行权；其他账号不受影响。
8. rollback_preserves_delta：回退后新增事实仍可追踪、不重复执行。
9. actual_deployed_version：核实现用版本；回执另带 `previous_version`、`image_digest`，与清单一致。
10. retained_image_checkpoint：旧镜像/数据恢复点保留；另带 `image_digest`、`data_checkpoint`。
11. observation_window：覆盖整个观察窗口的运行与事件记录；另带 `window_started_at/window_ended_at/unresolved_severe`，与清单一致。

消息或订单尚未发生时继续待验收；延长时间或安排明确场景，不补写虚构记录。重复发卡/扣费、未绑定出口、跨用户泄漏、双执行方、丢数据、错误全成功或持续恢复故障阻止扩大范围。

回退先冻结相关账号新执行权、登记在途、保存检查点，再对账切换后增量；直接覆盖新业务库会丢失新增事实。旧镜像、旧标签及经恢复验证的数据点继续保留；其余账号保持原状。
