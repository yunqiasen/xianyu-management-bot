# 本地GuDong底座与发布状态

## 分支

- `main`：GuDong原样基线 e8fe7ba。
- `upstream-zhinianboke`：原版对照 1be6493。
- `fork`：GuDong底座上的定制代码，实际开发分支。
- 旧二开保存在标签 `archive/pre-gudong-rebase-20261005`；尚有旧分支用于迁移对照，完成切换后删除分支，不删标签。

## 运行目录

源码保持一个仓库。数据在源码外 `../runtime/xianyu-management-bot/gudong`。
私有 `local.env` 保存管理账号初始化信息、JWT密钥；0600，不提交。
SQLite与旧MySQL格式不同，禁止将旧库目录直接挂进新实例。

```bash
export XYMB_RUNTIME="$(realpath ../runtime/xianyu-management-bot/gudong)"
export XYMB_ENV_FILE="$XYMB_RUNTIME/local.env"
mkdir -p data logs backups static/uploads trajectory_history browser_data
# 正式端口默认19010；首次验收使用本机19011，避免替换正在运行的旧后台。
XYMB_BIND=127.0.0.1 XYMB_PORT=19011 docker compose -p xymb-gudong -f compose.local.yml up -d --build
```

源码只读挂载。Python/yaml变更由单个监督进程等待写入稳定后，终止整个Start.py进程组再启动，避免仅重启Web而留下旧CookieManager。HTML/JS由静态文件实时读取，浏览器刷新加载。代码重载会短暂断开消息连接；数据、日志、浏览器资料独立持久化。依赖和镜像修改需要重新构建。切换分支前停止开发容器，检查该分支编排后再启动；Git切分支不等于发布。

Dockerfile.local固定GuDong基础镜像摘要；依赖变化须更新镜像。VNC不发布到公网；人工验证使用原有后台入口。

## 当前结果与待办

- 备份：旧Git bundle验证成功，MySQL导出压缩校验成功；旧运行配置和容器描述私有留存。
- 清理：旧React/node_modules、旧venv、旧服务缓存和测试缓存已清理；旧日志先归档。
- 新实例目前仅本机19011，尚未替换19010。
- 真实账号数据迁移、扫码上线、消息收发、恢复及删除验收尚未完成。
- 旧七个容器和其业务数据尚未删除。切换前先核实新实例，再停旧执行方，保留数据备份。
- 目标访问仍是 http://100.126.43.55:19010；实际Tailscale访问待切换验证。
- 热加载测试、GuDong测试与通知模板兼容测试不能替代真实账号验收。
