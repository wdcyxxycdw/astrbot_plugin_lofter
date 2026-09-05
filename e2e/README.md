# 开发期 E2E

从仓库根目录运行：

```sh
uv run --directory e2e --locked pytest -q
```

测试使用固定版本 AstrBot 4.27.5 和真实 OneBot v11 适配器，不依赖已部署的机器人。需要 Python 3.12+，依赖和环境由 uv 单独管理。

每次运行在临时目录创建 AstrBot 配置、数据库及插件目录，通过正式 PluginManager 加载当前工作区插件。OneBot 协议消息进入反向 WebSocket，经完整 PipelineScheduler 分发，发送请求由本地协议端接收并返回成功或失败回执。未配置模型服务，测试不会调用 LLM。

离线测试通过本地 HTTP/TLS 服务提供 DWR、HTML 和图片样本，覆盖：

- 插件注册、命令分发、权限校验、DWR 抓取和文字发送。
- 图片实际下载、编码和 OneBot 消息序列化。
- 自动解析链接、长文本分段及合并转发。
- count 的时间游标、标签匹配、失败与重复页状态。
- 订阅跨页补抓、待发送队列、发送中途失败后的逐条重试与去重。

样本是按公开 DWR 字段结构构造的固定数据，并非当前线上响应的录制。离线通过证明开发链路可运行，不代表 LOFTER 当前网络接口可用。

## 真实 LOFTER 测试

将根目录 `.env.test.example` 复制为 `.env.test`，填写自己的 `LOFTER_COOKIE` 和 `LOFTER_TAG`。分页样本标签需要至少两页可见作品。

```sh
uv run --directory e2e --locked pytest -q --live
```

`--live` 会请求真实 LOFTER DWR 服务并验证分页新增 ID，将搜索结果发送到本地 OneBot 测试端，不会发送到实际群聊。显式指定 `--live` 但缺少必要环境变量时，测试报错退出。

如果有已知准确数量的小规模样本，额外设置 `LOFTER_COUNT_EXPRESSION` 和 `LOFTER_EXPECTED_COUNT`，即可执行完整 count 命令并核对结果。未设置时，这个精确数量验收会明确跳过。

默认 CI 运行离线 E2E，不需要 Cookie。真实网络测试可在开发机或配置凭据的可信环境运行。

### 已验证的真实样本

2026-09-05 使用「2026艾希艾七夕接力」完成真实网络验收，预期作品数为 31：

- DWR 第二页返回新增作品 ID。
- 完整 count 命令返回“扫描结束：已发现 31 个作品”。
- 搜索结果经正式 AstrBot 消息管线发送到本地 OneBot 协议端。

运行 `uv run --directory e2e --locked pytest -q --live test_live.py`，3 个真实网络测试全部通过。Cookie 只保存在被 Git 忽略的根目录 `.env.test`，不包含在测试样本或文档中。

## 测试范围

OneBot 回执验证插件与适配器的发送协议，不证明 QQ 服务端最终投递。线上 `/lofter test` 是运行环境诊断，与本目录的开发期 E2E 分开维护。

标签搜索、统计和标签订阅使用 DWR；现有单帖链接解析与博主详情仍使用 HTML，E2E 对它们验证的是当前实现。尚未验证可替代这两条路径的 DWR 详情接口，不提供 mobile API 回退。
