# 双上游兼容更新记录

截至2026-10-05，GuDong e8fe7ba；zhinianboke 1be6493；旧原版基线fdc8eb0后12提交。
不将整套React/MySQL/Redis服务覆盖到GuDong单服务SQLite架构，不用空merge标记已完成。

|原版提交|内容|处理|
|---|---|---|
|2b95816、565df70|可编辑通知模板及完善|GuDong已有七类模板；适配一次替换逻辑并兼容双大括号，同步测试发送路径；渠道级配置/原版变量别名未迁移，属部分适配|
|0764f11|Token续期Redis锁|待对照GuDong现有账号锁；不直接引入Redis|
|43b6e3e|数据库session调整|待判断；SQLAlchemy会话不能直接应用SQLite manager|
|2657d38、31db652、3b2de87|单品发布修复|待逐段核对GuDong发布器及测试|
|4e78c07|素材库导入|待对照GuDong素材JSON与SKU数据模型|
|a0640a7|安卓极验修复|原版独立移动端代码；GuDong浏览器场景另验，不复制移动端工程|
|b78a0c3、f182bf1|移动端完善|同上；待场景核对|
|1be6493|推荐云服务器链接|非业务更新，不移植广告链接|

## 已适配模板行为

参考原版 `common/utils/notification_template.py` 的正则单次替换：替换变量中的用户消息不再次解释成模板。GuDong `{account_id}`保持兼容，新增`{{ account_id }}`写法；保留GuDong未知占位符原样，不改变旧事件分类和变量名。该实现是适配，不冒充完整原版提交合并。

验证：`tests/test_notification_template_compat.py`覆盖单双括号、消息中的占位符不被再次展开、未知旧变量保留。`reply_server.py`测试发送和生产通知共用formatter。

## 更新流程

fetch两家 → 记录SHA与逐项决策 → 在fork适配 → 回归+实际功能验收 → 提交发布。
main只允许GuDong快进同步；原版对照分支只允许zhinianboke快进同步。
