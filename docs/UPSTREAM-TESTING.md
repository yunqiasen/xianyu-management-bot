# 上游兼容测试

测试时使用当前源码只读挂载和临时数据目录，不挂正式账号、数据库或通知配置。

```bash
docker run --rm --network none --entrypoint python \
  -v "$PWD:/app:ro" --tmpfs /app/logs --tmpfs /app/data \
  --tmpfs /app/trajectory_history --tmpfs /app/browser_data \
  -w /app -e DB_PATH=/app/data/test.db -e SQL_LOG_ENABLED=false \
  -e PYTHONDONTWRITEBYTECODE=1 xianyu-management-bot:gudong-local \
  -m unittest discover -s tests
node --test tests/test_*_ui.cjs
node --check static/js/app.js
git diff --check
```

浏览器脚本：`tests/browser/verify_upstream_compat.py`。
使用 `tests/browser/upstream_fixture.py` 在隔离容器启动真实后台，DB_PATH必须为`/fixture/test.db`，`/fixture`使用tmpfs；同样不要挂载业务运行目录。
服务端和浏览器共用临时随机`TEST_TOKEN`；浏览器配置`TEST_BASE_URL`、`TEST_OUTPUT`、`CHROMIUM_PATH`。
平台推荐在HTTP请求边界替换为固定响应，其他平台API调用直接报错。素材和通知模板通过真实HTTP接口持久化；测试完成删除测试容器及凭据。

2026-10-07验收：102个Python测试、10个JS测试通过；Chromium验证素材分页、类目/单选/多选、保存/重载、模板新增/编辑/预览、390px布局，零页面脚本异常。
通知分发用本地真实HTTP接收器验证聊天、账号备注、发货正文；金额数量缺失显示未知。
这组测试未向闲鱼发布商品，也未给真实买家或生产通知渠道发送测试消息。
