# astrbot_plugin_lofter

AstrBot 插件，用于解析 Lofter 链接、订阅 Lofter 标签/博主、搜索内容、屏蔽作者和统计标签作品。

当前版本：**v2.0.12**。

## 功能

- **自动解析**：消息中出现 Lofter 帖子链接时自动提取内容；图片贴返回图片消息链，文字贴返回正文。
- **搜索**：按标签搜索 Lofter 内容。
- **标签统计**：支持 AND / OR / NOT / 括号表达式、命名条件和 CSV 汇总。
- **标签订阅**：订阅一个或多个标签，并可添加独立的排除标签规则。
- **博主订阅**：订阅指定博主，发现新文章后自动推送。
- **作者屏蔽**：按会话屏蔽作者昵称或 Lofter 用户名，应用于自动解析、搜索、预览和订阅推送。
- **LLM 工具调用**：向 AstrBot LLM 暴露搜索、订阅、作者屏蔽和标签统计工具。
- **持久化投递**：候选帖子先写入 SQLite 队列，再按稳定顺序逐条发送和确认。
- **QQ 推送**：QQ 标签和博主订阅先发送包含标题、作者、摘要和链接的文本预览；有图片时再用折叠转发发送全部图片，图片转发失败不会重试已成功的文本预览。

## 安装

在 AstrBot WebUI 的插件市场中搜索 `lofter` 安装，或将本仓库克隆到 `data/plugins/` 目录。

## 配置

在 WebUI 插件配置页填写以下参数：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `lofter_cookie` | Lofter 登录 Cookie；仅在需要授权的回退请求中按请求注入 | 空 |
| `poll_interval` | 订阅轮询间隔（分钟） | 30 |
| `max_images` | 解析帖子时最多展示的图片数量 | 3 |
| `search_limit` | 搜索结果最多返回的条数，范围 1–100 | 3 |

### 获取 Cookie

1. 浏览器登录 [lofter.com](https://www.lofter.com)。
2. 打开开发者工具（F12）→ Network。
3. 刷新页面，点击任意 Lofter 请求，复制请求头中的 `Cookie` 字段值。
4. 粘贴到插件配置的 `lofter_cookie`，或由管理员执行 `/lofter cookie <值>`。

匿名请求不会携带 Cookie。需要 Cookie 的回退请求不会把凭据带到其他 origin；跨 origin 重定向会被拒绝。

## 内容解析方式

插件不是只依赖页面 HTML：

1. 优先读取结构化的移动端 JSON 数据。
2. 按内容类型和失败原因回退到页面 embedded JSON、HTML 或 DWR 标签接口。
3. DWR 响应在受限子进程中执行并提取对象图。
4. 各来源都执行 canonical 帖子身份、字段证据和来源一致性校验；证据冲突时 fail closed，而不是拼接不可信结果。
5. Mobile detail 图片字段兼容 `photoLinks` 与 `firstImageUrl`；双字段同时出现时必须解析为相同的有序 URL 列表，否则 fail closed。
6. 所有来源 observation 都保留用于冲突验证。restart 默认延续旧业务结果的 coverage obligation；仅 Mobile 标签 cursor 切换到 DWR offset 0 时开启新业务 scope，旧 Mobile 结果不与 DWR 结果拼接，显式 dropped/unmapped evidence 仍必须被覆盖。

HTML 仍是博主页面等路径的回退来源，但不再是唯一解析方式。

## 使用

### 自动解析链接

直接在聊天正文中发送 Lofter 帖子链接，插件会自动解析并返回内容；回复或引用消息中的历史链接不会触发解析：

```text
https://username.lofter.com/post/xxxxxx
```

图片贴会发送统一格式文本和图片；QQ 自动解析优先发送最多 `max_images` 张图片的折叠转发，不再同时附加长正文转发；无图片的长文本会按适配器能力使用合并转发或文本结果。

### 权限边界

以下三个命令是当前会话的只读查询，不要求管理员权限：

- `/lofter list`
- `/lofter block-list`
- `/lofter count-list`

其余 `/lofter` 命令均为管理员命令，并同时经过 AstrBot 权限过滤器和 handler 内检查。自动链接解析不属于管理命令。

### 命令

| 命令 | 权限 | 说明 |
|------|------|------|
| `/lofter list` | 公开只读 | 查看当前会话订阅列表和编号 |
| `/lofter block-list` | 公开只读 | 查看当前会话作者屏蔽列表 |
| `/lofter count-list` | 公开只读 | 查看已保存的全局统计条件 |
| `/lofter search <标签>` | 管理员 | 搜索 Lofter 标签内容 |
| `/lofter subtag <标签> [-排除标签...]` | 管理员 | 订阅标签并可同时添加排除规则 |
| `/lofter subtagpreview <标签> [-排除标签...]` | 管理员 | 订阅标签并预览最新 3 条内容 |
| `/lofter subblog <用户名>` | 管理员 | 订阅博主 |
| `/lofter unsub <编号>` | 管理员 | 按 `/lofter list` 编号删除规则 |
| `/lofter unsubtag <标签>` | 管理员 | 取消标签订阅 |
| `/lofter unexcludetag <标签>` | 管理员 | 删除排除标签规则 |
| `/lofter unsubblog <用户名>` | 管理员 | 取消博主订阅 |
| `/lofter block-author <昵称或用户名>` | 管理员 | 屏蔽当前会话中的作者 |
| `/lofter unblock-author <昵称或用户名>` | 管理员 | 解除作者屏蔽 |
| `/lofter count <名称> = <表达式>` | 管理员 | 保存并执行标签统计 |
| `/lofter count-del <名称或编号>` | 管理员 | 删除统计条件 |
| `/lofter count-all` | 管理员 | 执行全部统计条件并生成 CSV |
| `/lofter cookie <值>` | 管理员 | 更新运行时 Lofter Cookie |
| `/lofter test` | 管理员 | 执行真实网络和真实推送的端到端测试 |

### LLM 工具调用

插件注册四个 AstrBot LLM 工具，**全部仅管理员可调用**：

| 工具 | 能力 |
|------|------|
| `lofter_content` | 搜索 Lofter 标签内容，返回文本结果和图片 URL |
| `lofter_subscription` | 查看、添加、预览和删除标签/博主订阅及排除规则 |
| `lofter_author_block` | 查看、添加和解除当前会话作者屏蔽 |
| `lofter_count` | 管理标签统计条件，执行单项或全部统计并生成 CSV |

订阅工具支持 `原神 -R18` 形式的排除表达式。作者可以使用昵称、Lofter 用户名或 `https://username.lofter.com` 表示。

LLM 工具不暴露 Cookie 更新、真实端到端测试或链接 parse；链接解析仍由自动消息事件触发。

### 标签订阅与排除规则

```text
/lofter subtag 原神
/lofter subtag 原神 -R18
/lofter subtag 原神 -R18 -暴力
```

每条订阅或排除条件都是独立记录，可单独删除：

```text
1. [标签｜订阅] 原神
2. [标签｜排除] R18
3. [博主]       username
```

`/lofter unsub 2` 会原子删除执行时列表中的第 2 条规则。

### Fetch-first 初始化与预览

新增标签或博主订阅采用 fetch-first 初始化：

1. 读取当前会话的订阅 revision 和 policy generation。
2. 在 session gate 外抓取并验证新增目标，网络请求不会占用会话锁。
3. 重新取得 gate，在单个 SQLite transaction 中复核 snapshot。
4. 创建 `warming` 订阅，只给实际返回该帖的具体 subscription row 写入历史 seen，然后统一切换为 `active`。

因此：

- 合法空 feed 可以正常激活。
- 抓取或 schema 校验失败不会留下部分订阅、排除规则、seen 或 generation 副作用。
- 抓取期间发生退订、重新订阅或作者策略变化时，旧 snapshot 不能持久化。
- 新的同类型订阅继承当前 active 订阅的 canonical seen 并集，删除旧来源后不会重推历史内容。
- `subtagpreview` 只按实际来源记录 preview seen，不创建 delivery；排除或屏蔽只控制展示，被过滤帖子仍不会在解除规则后补推。
- 只有排除规则、没有正向标签的 preview 会在写数据库前拒绝。

### 作者屏蔽

屏蔽名单按群聊或私聊会话隔离：

```text
/lofter block-author 作者昵称
/lofter block-author username
/lofter block-author https://username.lofter.com
/lofter block-list
/lofter unblock-author username
```

一次输入产生的昵称和用户名 key 会在同一个 transaction 中更新。实际变化会推进会话 policy generation，使正在抓取的旧结果失效。轮询中被屏蔽的作品会按实际订阅来源记录为已处理，解除屏蔽后不会补推旧内容。

### 标签统计

```text
/lofter count 米哈游相关 = 原神|崩铁 -R18
/lofter count-list
/lofter count-del 米哈游相关
/lofter count-del 1
/lofter count-all
```

表达式规则：

- 空格或 `&`：AND
- `|` / `｜`：OR
- `-` / `－`：NOT
- 半角或全角括号：分组
- `=` / `＝`：分隔统计名称和表达式

不支持 `AND` / `and` 关键字，以免与真实标签冲突。统计至少需要一个正向标签；结果按 canonical `post_id` 去重，并区分 `success`、`partial` 和 `failed`。`count-all` 会生成包含作品数、候选数、扫描页数、状态和错误/提示的 CSV。

## 持久化投递语义

订阅候选不会先截取 5 条再发送，而是先持久化到 `deliveries` / `delivery_sources`：

- 保存实际返回帖子的具体 subscription row provenance；它与帖子字段 provenance 是两类不同证据。
- 标签和博主共享同一个 session 队列，按 `published_at ASC, post_id ASC` 稳定消费。
- 每个 session 每轮最多进行 5 次 delivery callback；超过部分留在队列中，即使下一轮 feed 已为空也会继续 drain。QQ 有图片的标签或博主推送在一次 callback 中会依次产生文本预览和图片折叠转发两条平台消息。
- 每条成功后立即独立 ack；第 N 条失败时，之前的 accepted 保留，第 N+1 条不会在本轮发送。
- scheduler 只有在 send callback **严格返回 `True`** 时才转为 `accepted` 并写入当前有效来源的 seen。QQ 有图片推送由 Plain 文本 primary 决定 callback acceptance；后续图片转发明确失败时单独报告，但不会让已成功的 primary 重试。
- 普通确定失败按 60 秒、300 秒、1800 秒、7200 秒、之后 21600 秒退避；第 10 次进入 `dead`。
- 发送 timeout 为 60 秒，claim lease 为 5 分钟。timeout 或任务取消的结果不确定，因此 delivery 保持 `sending`，等待 lease recovery。
- 每个 session 的 `pending + sending` admission 上限为 5000；超出容量的候选不写 seen，后续轮询可再次发现。

投递保证是 **at-least-once**，不是 exactly-once：adapter 已接受但进程在本地 ack 前崩溃，或 60 秒 timeout 后底层发送最终完成，都可能在 lease 恢复后产生重复消息。

## 数据存储

SQLite 文件位于：

```text
<astrbot_root>/data/plugins/astrbot_plugin_lofter/lofter.db
```

主要持久状态包括：

- `subscriptions`、`subscription_revisions`、`session_policies`
- 按 subscription 存储的 `seen_posts` 和水位/checkpoint
- `deliveries` 和 `delivery_sources`
- `author_blocks`、`count_conditions` 和配置 marker

同一数据库路径通过 `<db>.lock` 的 OS advisory lock 保证单实例。SQLite 使用 WAL、foreign keys、显式 busy timeout 和单 callback transaction；事务不跨网络 `await`。

旧版 `subscriptions.json` 会通过 version 2 marker 原子导入，源文件保留。Schema v1–v4 会在单个 transaction 中迁移到 v5，并在提交前校验结构和外键。

当前版本**没有**自动清理 accepted/dead/seen 的 retention、dead delivery 恢复命令或 ReportStore。数据库会随历史记录持续增长，需要部署方自行监控和备份；不要直接修改状态表绕过队列协议。

## 许可证

AGPLv3
