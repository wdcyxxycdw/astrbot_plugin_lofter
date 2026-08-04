# 项目架构

## 概览

AstrBot 插件，功能：自动解析 Lofter 帖子链接、订阅标签/博主并定时推送、搜索标签内容、统计标签表达式，以及按会话屏蔽作者。

当前插件版本为 v2.0.9，数据库 Schema 为 v5。

## 文件结构

```text
main.py                    # 插件入口、命令、自动解析事件和生命周期
core/
  client.py                # 长生命周期 HTTP client、重试/限流/重定向和 Cookie 隔离
  content_source.py        # Mobile 主路径和 DWR/embedded JSON/HTML 回退编排
  mobile_adapter.py        # Mobile API 请求适配
  mobile_parser.py         # Mobile JSON 严格解析
  parser.py                # Post 数据模型、embedded JSON/HTML 解析
  post_identity.py         # canonical post ID/URL/owner 校验
  post_fields.py           # 字段 completeness、provenance、合并和证据冲突校验
  source_scan.py           # 分页扫描、重启和 evidence items 分离
  dwr_parser.py            # DWR 响应映射和对象图提取
  dwr_engine.py            # 受限子进程执行 DWR 响应脚本
  db.py                    # SQLite async 操作层和单 callback transaction
  db_schema.py             # Schema v5 DDL 与严格结构校验
  db_migrations.py         # 空库建库、v1-v4 到 v5 单事务迁移
  db_json_migration.py     # subscriptions.json v2 原子迁移和 marker
  db_checkpoints.py        # legacy checkpoint/floor 比较与水位更新
  db_repository.py         # subscription snapshot、原子 mutation 和兼容 repository API
  instance_lock.py         # 数据库生命周期级跨平台 OS advisory lock
  storage.py               # Subscription dataclass 和兼容读取 API
  session_gate.py          # 按 session_id 共享的 asyncio gate
  subscription_service.py  # fetch-first 订阅、预览、删除和 snapshot fence
  delivery.py              # 持久 delivery queue、claim/lease/ack/backoff 状态机
  scheduler.py             # 抓取 actual sources、fenced discovery 和 session queue drain
  formatter.py             # 统一帖子文本和图片格式化
  llm_tools.py             # 四个管理员 LLM 工具
  author_block.py          # 会话级作者屏蔽和原子 policy mutation
  expression_planner.py    # 标签表达式 ROBDD 规划
  tag_count.py             # 标签统计求值、分页扫描和 CSV
  count_commands.py        # /lofter count 命令 mixin
  e2e_test.py              # /lofter test 真实端到端执行器
  e2e_steps_network.py     # E2E 网络/解析步骤
  e2e_steps_flow.py        # E2E 订阅/调度/推送步骤
```

## 权限边界

公开只读 CLI 只有：

- `/lofter list`
- `/lofter block-list`
- `/lofter count-list`

其余 `/lofter` 命令使用 AstrBot `ADMIN` permission filter，并在 handler 内通过 `event.is_admin()` 二次检查。自动链接解析保持普通消息事件能力。

四个 LLM 工具 `lofter_content`、`lofter_subscription`、`lofter_author_block`、`lofter_count` 全部只允许管理员调用。工具不暴露 Cookie 更新、真实 E2E 测试或链接 parse。

## 内容源与 HTTP 边界

`DefaultContentSource` 实现 `ContentSource`：

- 单帖：Mobile detail 优先；失败后依次尝试匿名页面 embedded JSON、HTML parser，最后才进行 credentialed HTML 回退。
- 标签：Mobile tag 优先；结果不完整时从 offset 0 重启 DWR，随后按 DWR offset 翻页。
- 博主：Mobile blog 优先；结果不完整时回退匿名/授权 HTML，并在必要时补全帖子详情。
- 所有来源 observation 都保留为 evidence，用于 canonical ID、URL、owner 和字段冲突验证。
- restart 默认要求新 scope 覆盖旧业务 identity；仅 Mobile 标签 cursor 切换到 DWR offset 0 时开启新业务 scope，旧 Mobile items 不再构成 DWR coverage obligation，也不与 DWR 业务结果拼接。
- explicit dropped/unmapped `evidence_items` 始终保留为 coverage obligation，不因 scope 切换消失。
- canonical ID、URL、owner、字段别名和重复 occurrence 发生冲突时 fail closed。

HTTP client 使用长生命周期 `aiohttp.ClientSession` 和 `DummyCookieJar`：

- 匿名请求不携带 Cookie。
- Cookie 只在 `credentialed=True` 的第一方 HTTPS 请求中按请求注入。
- credentialed redirect 不允许改变 origin。
- 全局并发 8、单 host 并发 4；连接/总 timeout、响应体大小、重试和关闭状态都有显式边界。

DWR 响应由受限子进程执行，不在插件主进程直接执行远端脚本。

## 数据库表

```text
config(key, value)                                      -- schema/json migration marker、Cookie 等配置
subscriptions(id, session_id, type, role, target, state, revision,
              initialized_at, created_at, updated_at)   -- 原子规则；warming/active
subscription_revisions(session_id, subscription_type,
                       revision, updated_at)             -- 会话+类型集合 generation
session_policies(session_id, policy_generation,
                 updated_at)                            -- 会话 policy generation
session_activity(session_id, inactive_since, updated_at) -- 活跃状态兼容数据
seen_posts(subscription_id, post_id, published_at,
           seen_at)                                     -- subscription 级历史
subscription_watermarks(subscription_id, history_before,
                        legacy_post_id_floor, updated_at) -- 初始化历史界线和 legacy floor
legacy_checkpoints(subscription_id, post_id, created_at) -- JSON last_post_id 一次性门槛
deliveries(id, session_id, post_id, status, payload_json,
           published_at, sort_key, lease_*, attempts,
           error_*, timestamps)                         -- session+post 唯一投递状态
delivery_sources(delivery_id, subscription_id,
                 subscription_revision, policy_generation,
                 discovered_at)                         -- 投递的实际 subscription 来源
count_conditions(name, expression, updated_at)          -- 命名标签统计表达式
author_blocks(session_id, kind, value, display)         -- 会话级作者屏蔽
```

不再保留旧 `sent_posts` 或会话级 `seen_posts` 表。旧业务 API 通过 v5 结构提供兼容：会话级 seen 查询聚合当前 active subscribe；兼容 `mark_sent` 写入 `accepted` delivery。

每条订阅是一个原子条件：`(tag, subscribe, A)` 与 `(tag, exclude, B)` 独立存在，可单独删除。

## 数据库与实例生命周期

- 同一数据库路径通过 `<db>.lock` 的非阻塞 OS advisory lock 保证单实例；锁在 DB initialize 和 scheduler start 前取得，启动失败或 terminate 时释放。
- SQLite 设置 WAL、foreign keys 和显式 `busy_timeout`；lock/busy 错误分类为 `SQLiteBusyError`。
- `BEGIN IMMEDIATE`、SQL/校验、COMMIT/ROLLBACK 全部位于一次同步 executor callback；transaction 不跨 `await`。
- 空库在一个 transaction 建完整 v5；真实 v1-v4 在一个 transaction 重建迁移，写 marker 前验证严格结构和 `foreign_key_check`。
- `subscriptions.json` 使用 `json_migration_version=2`：DB 外完整验证，transaction 内重读 marker、批量插入并写 marker；源文件保留。
- JSON `last_post_id` 写入具体 subscription 的 `legacy_checkpoints`；首轮 discovery 在同一 fenced transaction 中消费 checkpoint、写 seen/水位并清理 checkpoint。

## Session gate、revision 与 policy

同一个 `SessionGateRegistry` 注入订阅服务、作者屏蔽、scheduler 和 delivery queue：

- 同 session 的 mutation、fenced discovery、claim 和 ack 串行。
- 不同 session 可以并发。
- 网络 fetch、字段 enrich 和 adapter send 不持 gate。

Generation 语义：

- `subscription_revisions` 表示 session/type 的订阅集合 generation。
- 具体 subscription row 的 `revision` 是该 row 生命周期创建时的 type revision。
- add/remove/exclude/unexclude 实际改变集合时，对应 type revision 和 policy generation 各推进一次。
- block/unblock 只推进 policy generation。
- duplicate/no-op 不推进 generation。
- 未变化 row 不因同类型其他规则变化而批量更新 revision。

持久化抓取结果时同时复核：row ID、state、row revision、type generation 和 policy generation。任一不匹配，旧 snapshot 的 discovery 整体零副作用。

## Fetch-first 订阅初始化

runtime 订阅 mutation 统一经过 `SubscriptionService`，不使用 insert-first warmup：

```text
短暂取得 session gate并读取 snapshot
→ 释放 gate
→ gate 外抓取、enrich、identity/字段/publish-time 校验
→ 重新取得 gate
→ 一个 transaction 内复核 snapshot
→ 创建 warming subscription
→ 写初始化 watermark、历史继承和 actual-source seen
→ 统一切换为 active
→ 推进 revision/policy generation
```

规则：

- 任一新增正向 source 抓取或 schema 校验失败，整批 subscribe/exclude 零持久副作用。
- 合法空 feed 可以激活。
- 纯 exclude mutation 不需要网络；纯 exclude preview 在写数据库前拒绝。
- 新增同 session/type subscribe 在 transaction 内继承已有 active subscribe 的 canonical seen 并集；exclude 不继承。
- 同批新 subscription 在来源 seen 全部写完前保持 `warming`，避免互相继承尚未完成的初始化状态。
- Preview 给本次明确抓取的实际 subscription sources 写 seen，但不创建 delivery。
- Preview 的 exclude/block 只控制显示；被过滤帖子仍按实际来源写 seen，解除规则后不补推。
- unsubscribe 和 index remove 在 gate 内单 transaction 重读并删除当前行，避免 list/remove 竞态。
- 作者屏蔽一次输入产生的 name/username keys 在同一个 transaction 原子更新。

## 生产轮询与持久队列

生产 `_poll_all()` 不再使用 `seen_count == 0` 冷启动，也不沿 tag/blog 两条路径直接发送：

```text
DeliveryQueue.session_ids()
  → active subscribe sessions UNION pending/sending queue-only sessions
  → 不同 session 并发执行 _poll_delivery_session

每个 session：
  capture SessionSnapshot
  → gate 外逐个 active subscription row 抓取和 enrich
  → 每个 row 形成 SourceBatch(actual DeliverySource, posts)
  → gate 内 persist_discovery(snapshot, batches)
  → drain 统一 session delivery queue
```

### Actual subscription provenance

标签按每个具体 subscribe row/target 抓取，博主也按具体 row 抓取。`DeliverySource` 保存：

- `subscription_id`
- `subscription_revision`
- `type`
- `target`

该 provenance 表示“哪个订阅请求实际返回了帖子”，不能从帖子 tags/author metadata 推测；它与 `Post.provenance` 的字段来源不是同一概念。

在 exclude 和作者屏蔽前保留 actual source。legacy checkpoint/floor、`history_before`、exclude、block、accepted rediscovery、passive seen 和 delivery upsert 全部在最终 fenced transaction 内处理。

### Discovery

`persist_discovery()`：

- 先严格复核 snapshot 和所有具体 source row。
- 按 canonical `post_id` 合并兼容字段证据；冲突 fail closed。
- eligible candidate 先完整持久化，不在 discovery 阶段截取前 5 条。
- payload 使用 versioned JSON 保存完整 `Post` 和字段 provenance。
- `sort_key = f"{published_at:020d}:{post_id}"`，消费按 `(published_at ASC, post_id ASC)`。
- accepted 永不回 pending；dead 不自动恢复；cancelled 在重新发现当前有效来源时可恢复 pending。
- accepted 历史迁移行即使 `payload_json=NULL` 也永不 claim。
- 每 session `pending + sending` admission 上限 5000；超出的新候选不写 seen，返回 backpressure 数量并记录 warning。

### Claim、发送与 ack

Claim transaction：

1. 恢复 expired `sending`。
2. 若 session 已有未过期 `sending`，返回 `BUSY`。
3. 按稳定顺序选择最早 due pending。
4. 严格 decode payload，并按当前 subscription row、exclude 和作者屏蔽重新验证来源。
5. 无有效来源的 delivery 转 `cancelled`；畸形 payload 也转 `cancelled`，避免永久阻塞队头。
6. 验证通过后才生成 lease token，并原子切换为 `sending`。

发送在 gate 外进行：

- tag/blog 共用同一个 session queue；每轮合计最多 5 次 delivery `send_func` 调用。
- 非 QQ callback 保持一次平台发送；QQ blog 和无图 tag 只发送 Share；QQ 有图 tag 在同一 callback 中先发送 Share，再发送包含全部图片的 Nodes sidecar，因此最多产生两条平台消息。
- 每条成功立即独立 ack；第 N 条失败后停止本 session 本轮 drain。
- scheduler 仅在 `send_func` 严格返回 `True` 时 accepted，并为 ack 时仍有效的全部 sources 写 seen。
- QQ callback 只由 Share primary 决定 acceptance；Nodes 明确 rejected/error 时单独报告，delivery 仍 accepted，不重试已成功的 Share。
- 其他 `False`、`None` 或普通异常：attempts + 1、清 lease、设置 backoff，不写 seen。
- backoff：60、300、1800、7200 秒，第 5–9 次为 21600 秒；第 10 次进入 dead。
- send timeout 为 60 秒，lease 为 5 分钟。
- timeout 或 `CancelledError` 保持 `sending`；取消继续向上传播，等待 lease recovery。
- stale lease token 对状态、attempts、错误、时间戳和 seen 全部零副作用。
- claim 会在一个 transaction 中连续跳过无效 due row，寻找下一条有效 delivery。

投递语义为 at-least-once：adapter 已接受但本地 ack 前崩溃，或 timeout 后底层发送实际完成，都可能在 lease 恢复后重复发送。

抓取或 fenced persist 失败不会阻止当前 session 尝试 drain 已持久化 backlog；因此 feed 已空的 queue-only session 仍可继续发送历史 pending。

## DWR 请求关键参数

`POST https://www.lofter.com/dwr/call/plaincall/TagBean.search.dwr`

- `c0-param3=string:new`：按最新排序。
- `c0-param6=number:{limit}`：本页返回数量。
- `c0-param7=number:{offset}`：当前分页 offset，后续页按 `offset + limit` 推进。
- `c0-param8=number:0`：固定 0；不能传当前时间戳。

## 标签统计

- 统计条件存储在 `count_conditions`，独立于订阅规则。
- 表达式支持 AND / OR / NOT / 括号，由 ROBDD planner 选择扫描 cover。
- 标签比较使用 Unicode `casefold()`。
- 扫描按 canonical `post_id` 去重，不设人工页数上限，以空页或无新候选自然停止。
- primary/fallback evidence 只用于完整性和身份验证，不直接计入业务结果。
- 结果区分 `success`、`partial`、`failed`；不能证明精确完整时不会报告精确成功。
- `count-all` 汇总所有命名条件并生成 CSV，优先通过 AstrBot 文件消息发送。

## 已知生命周期边界

当前版本未实现以下能力：

- accepted/dead/seen 的自动 retention 或按 180 天/20000 条清理。
- dead delivery 的管理员恢复命令。
- ReportStore 或统一报告文件生命周期治理。
- scheduler `start()`/`stop()` 全面幂等重构、全局 poll lock 或完整 task registry。

因此数据库会随历史记录长期增长，运维侧需要监控、备份和容量规划。上述边界不得在文档或代码注释中描述为已实现。

## E2E 测试边界

`/lofter test` 是管理员显式触发的实时健康检查，固定执行九个步骤：`runtime`、`mobile_direct`、`dwr_direct`、`production_orchestration`、`fixture_detail`、`blog`、`warmup_pending`、`claim_send_ack_seen`、`cleanup`。

- runtime、Mobile 直连、DWR 直连和完整生产标签编排是四个独立 root probe；前一项失败不阻止后续 root 实际执行。
- Mobile-only probe 使用与生产编排相同的 eligibility 判定，并报告固定、无载荷的 fallback reason；显式 DWR probe 不伪报 Mobile fallback。
- fixture 优先使用 production、Mobile、DWR 中首个能提供两个不同帖子的健康来源，只有单一来源都不足时才跨来源组合；两个帖子完成公开单帖补全后才原子发布，并按发布时间选择较旧 baseline 和较新 candidate。
- blog 与标签投递链在 fixture 后分叉；blog 失败不阻断 tag flow。
- 临时 SQLite 中复用生产 `SubscriptionService`、`DeliveryQueue` 和 `SubscriptionScheduler`；flow 使用纯内存 controlled source，不再访问真实内容源，验证 `pending → sending → accepted → subscription seen`。
- skip 通过稳定 step key 传播根 `blocked_by`，不复制上游异常。
- fixture 足够时向命令所在会话发送一个带“Lofter E2E 测试”标识的 candidate；QQ 标签帖子有图片时该 candidate 最多产生 Share 与图片转发两条平台消息。
- Step 8 分别报告 primary/media 的固定 stage、outcome、异常类型和可选安全整数 retcode；primary 决定 delivery acceptance，media 失败时 delivery 仍 accepted、seen 已写入，但总体报告为 `DEGRADED`。
- cleanup 独立尝试取消临时 task、关闭 DB 和删除临时目录；不写生产订阅、seen、delivery 或 config。
- 报告区分 `HEALTHY`、`DEGRADED` 和 `INCONCLUSIVE`，且不暴露 Cookie、URL、post ID、owner、正文、图片 URL、完整 adapter response、异常文本、业务 payload 或原始异常。

该命令仅管理员可执行，不暴露给 LLM tool。开发回归只使用离线 fake；普通 pytest 通过 marker 和 socket guard 隔离真实网络，只有显式 live 配置才允许真实测试。
