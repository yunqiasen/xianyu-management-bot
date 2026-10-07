# 发布类目与素材

在「商品发布」选择账号、填写标题后点「推荐类目」。接口仅读取平台推荐，不上传图片、不发布商品。
同名分类用 ID 区分；切换类目会重新获取属性，再选择品牌等平台返回的选项；平台声明多选时显示多选框，保留全部选择。
自由输入属性/决定性属性联动未迁移为完整原版编辑器；本轮支持返回候选值的单选、多选。
更改标题、描述、类目提示或账号后清空旧选择；旧异步返回不会覆盖新表单。

素材保存 `platform_category`，载入时保留类目/属性；单品 JSON、文件上传、批量发布走同一发布器，提交前重新核对平台类目和选项。清除选择后恢复自动推荐。
老素材为空时继续自动推荐；数据库仅增加可空 JSON 文本列，不重建或清空原素材。
推荐失败、类目失效、属性冲突时返回错误，不伪装成发布成功。

素材侧栏支持 10/20/50 条分页、总数、上一页/下一页；删除末页最后一条时自动回到有效页。长标题截断，悬停查看全文。

## 接口
- `POST /product-publish/categories`：`account_id, title, description?, category?, platform_category?`，返回 `success, category, candidates, properties`。
- `/product-materials` POST/PUT、`/product-publish` POST：新增可选 `platform_category` 对象。
- `/item-publish` POST multipart：同名字段为 JSON 字符串。
- 配置示例：`{"channel_cat_id":"20","cat_name":"手机","attributes":[{"property_id":"brand","value_id":"b2","value_name":"品牌B"}]}`。ID 以实时平台响应为准。多选属性使用 `{"property_id":"tags","values":[{"value_id":"t1"},{"value_id":"t2"}]}`。
- 每次校验账号归属；类目接口不回传 Cookie、原始属性卡等内部数据。

## 验收边界
测试涵盖平台响应字段别名/嵌套解包、准确选中属性、完整卡传递、发布载荷、API权限、素材持久化及前端竞态。平台响应使用夹具，真实平台发布须另用真实商品验收。

## 类目请求令牌过期

正式环境发现 `FAIL_SYS_TOKEN_EXOIRED` 时，类目读取现在仅在响应已换发 `_m_h5_tk` 后重试一次，重新签名；没有新令牌、第二次仍失败或要求身份验证时立即返回错误。该重试不应用于商品发布写请求，不自动触发账密登录。换发Cookie仅在当前发布器实例使用，推荐接口不覆盖正在恢复的账号Cookie。

## 合并回归修复（2026-10-07）

- 推荐有候选但无默认项时，`category` 返回 `null`，候选仍供手选；不是推荐失败。
- 最终发布前必须具备 catId、channelCatId、tbCatId；缺字段返回明确错误，不发起商品发布写请求。
- 属性显示、匹配和标签统一支持 valueName、text、transportData 与 properties 名称兜底。
- 多选全部取消使用 `{"property_id":"tags","values":[]}`，保存、载入和发布时保留清空意图；省略属性仍采用平台默认值。
