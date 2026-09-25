# 增强版容器部署与端口

增强版使用 `compose.production.yml`，不要运行下方保留的上游一键下载/更新脚本；那些脚本拉取的是上游镜像。

## 固定拓扑

| 服务 | 容器端口 | 宿主入口 |
|---|---:|---|
| React / Nginx | 80 | 正式9000；候选通过`FRONTEND_PORT`改成独立端口 |
| Web API | 8089 | 本机18089 |
| 消息执行方 | 8090 | 本机18090 |
| 调度 | 8091 | 本机18091 |
| MySQL / 恢复MySQL | 3306 | 本机19106 / 19107 |
| Redis | 6379 | 本机19380 |

前端容器健康检查固定访问`127.0.0.1:80`，与Nginx的IPv4监听一致，避免`localhost`解析到IPv6造成误报。

浏览器始终使用同源`/api/v1`、`/static`及`/api`下的WebSocket，不填写内部8090为浏览器地址。Nginx代理到`backend-web:8089`，服务间按容器名访问8089/8090/8091；宿主端口修改不改变这些内部地址。`FRONTEND_PUBLIC_URL`填写实际浏览器入口，作为图片和下载链接的公共根地址。

## 私有配置

在仓库外建立权限0600环境文件；`XYMB_RUNTIME_DIR`指向独立正式运行目录，包含mysql、redis、恢复库、static、browser、backups及logs。旧数据库、源码和浏览器目录不挂入新容器。所需密码、`INTERNAL_API_TOKEN`不设弱默认值；该令牌至少32字符。

必填：`XYMB_BUILD_COMMIT`（完整已提交SHA）、`XYMB_RELEASE_ID`（不可变镜像标签）、`XYMB_RUNTIME_DIR`、`MYSQL_ROOT_PASSWORD`、`MYSQL_PASSWORD`、`REDIS_PASSWORD`、`XYMB_RESTORE_ROOT_PASSWORD`、`XYMB_RESTORE_PASSWORD`、`INTERNAL_API_TOKEN`。

候选设置`FRONTEND_BIND=127.0.0.1`和未占用的`FRONTEND_PORT`。消息、调度自动启动缺省false，采集始终false；账号迁移后仍停用，独立交接验证后才开启相应执行方。P5开关由同一环境文件传给三个服务，默认false。

## 构建及核验

只从已提交、工作区干净的增强分支构建。先保存旧实际运行镜像和一致性数据恢复点；旧容器使用的镜像若已从本地镜像库清除，先验证其根文件系统及配置可恢复，不能把同名新标签当作旧镜像。

```bash
git status --porcelain
# PRIVATE_ENV为上述仓库外私有配置文件。
docker compose --env-file "$PRIVATE_ENV" -f compose.production.yml config --quiet
docker compose --env-file "$PRIVATE_ENV" -f compose.production.yml build
docker compose --env-file "$PRIVATE_ENV" -f compose.production.yml up -d
```

应用镜像记录`org.opencontainers.image.revision`；三服务`/health`返回相同commit，前端`/build-info.json`也必须匹配。还要核验镜像ID、实际挂载、同源API、WebSocket升级、静态图片、刷新深层路由和浏览器错误。构建上下文排除本地依赖、环境文件、测试缓存及旧构建产物。

切换9000前完成候选页面操作和迁移对账，保存旧端最后检查点、停止旧执行方；再重建frontend端口映射。保留旧容器及恢复镜像，不删除业务卷。回退按[迁移与增量对账](implementation/migration.md)先保存新事实，不覆盖数据库。

本配置不代替[发布门禁](RELEASE.md)、GitHub推送许可或真实业务及48小时观察；外部模型成功也不等于真实闲鱼消息已经发出。
