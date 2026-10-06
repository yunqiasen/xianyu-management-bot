# 本地GuDong底座与单实例运行

## 源码与分支

唯一源码：`/home/div/1_Project_dir/Project/Xianyu/xianyu-management-bot`。

- `main`：GuDong原样基线。
- `upstream-zhinianboke`：zhinianboke原样对照。
- `fork`：实际开发、部署分支。
- 旧二开只保留标签 `archive/pre-gudong-rebase-20261005`，不再独立运行。

## 运行入口

GuDong单服务SQLite，容器8090映射19010。统一使用 `http://100.126.43.55:19010` 或 `http://127.0.0.1:19010`。
旧 `/accounts` 书签重定向至新版登录入口，登录后进入GuDong后台。账号设置与校验保持GuDong原有流程。

运行数据：`/home/div/1_Project_dir/Project/Xianyu/runtime/xianyu-management-bot`，不再嵌套另一份gudong实例。
私有 `local.env` 保存后台账号初始化信息与JWT密钥，0600且不提交。现用后台用户与密码已从原私有配置保留。

```bash
# 在源码根目录执行（当前fork分支）
bash tools/local.sh up -d --build
bash tools/local.sh ps
bash tools/local.sh logs --tail 50 app
bash tools/local.sh stop
```

`tools/local.sh` 固定Compose项目名xymb-gudong，补齐只读源码下的挂载点，默认无需额外环境变量。旧19011临时入口不再使用。

## 热加载

源码只读挂载。Python/yaml变更由单个监督进程等待写入稳定后，终止整个Start.py进程组再启动，避免仅重启Web而留下旧CookieManager。HTML/JS实时读取，浏览器刷新加载。重载会短暂断开消息连接；数据、日志、浏览器资料独立持久化。依赖、Dockerfile变更后重新构建。

切换分支前停止开发容器，核对该分支编排后再启动；`main`、原版对照分支保持上游原样，因此不强行加入定制脚本。Git切分支不等于发布。
Dockerfile.local固定GuDong基础镜像摘要。VNC不发布到公网；人工验证使用后台原有入口。

## 旧数据与验收边界

旧MySQL的105张表已导出并恢复至独立MySQL核对逐表行数；旧运行数据、配置、消息历史与测试卷备份统一归档至：
`/home/div/1_Project_dir/Project/archives/xianyu-management-bot/backups/cleanup-20261006`。

迁移当前1个闲鱼账号的Cookie、账密、代理、备注与对应开关，保留后台登录。旧订单和关键词为0；旧4条消息事件等异构历史只保存在归档，没有冒充迁入GuDong。新库无额外默认回复或AI开关开启。

现行测试涵盖接口、配置和本地运行；真实扫码、消息收发和长期保活单独验收。上游兼容完成度见 `UPSTREAM-INTEGRATION.md`，不是全量合并声明。

## Git维护与网页更新

本地编排设置 `XYMB_UPDATE_MODE=git`。后台按钮显示“Git 分支维护”，检查接口明确表示此处不检查上游版本，而非声称已是最新。
文件覆盖式更新、更新器重启与哈希清单写入入口返回409，避免覆盖二开源码或启动第二个进程。
更新仍走 fetch双上游 → 兼容适配 → 测试/文档 → 提交 → Docker发布。
这与源码热加载是两回事。其他未设置该变量的GuDong安装保持原行为。
