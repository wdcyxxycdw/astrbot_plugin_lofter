# 项目架构

## 概览

AstrBot 插件，功能：自动解析 Lofter 帖子链接提取图片、订阅标签/博主定时推送、搜索标签内容，并支持按会话屏蔽指定作者。

## 文件结构

```
main.py            # 插件入口，注册所有命令和事件处理器
core/
  client.py        # HTTP 客户端，封装 GET 和 DWR 标签搜索
  parser.py        # Post 数据模型 + 博主主页 HTML 解析
  formatter.py     # 统一帖子文本格式化（format_post），各推送/搜索/解析场景共用
  dwr_parser.py    # DWR 响应解析，调用 dwr_engine 执行 JS，提取对象图
  dwr_engine.py    # 用 dukpy 执行 DWR 响应脚本，提取对象图
  db.py            # SQLite 操作层（WAL 模式，async via run_in_executor）
  db_migrations.py # 数据库 schema 迁移定义和执行
  storage.py       # Subscription dataclass + SubscriptionStorage
  scheduler.py     # 订阅轮询调度器
  author_block.py  # 会话级作者屏蔽：输入归一化、名单存储和 Post 过滤
  tag_count.py     # 标签统计表达式解析、求值、分页统计和 CSV 生成
  count_commands.py # /lofter count 系列统计命令 mixin
  e2e_test.py            # /lofter test 端到端集成测试执行器（E2ETestRunner + format_report）
  e2e_steps_network.py   # E2E 步骤 mixin：step 01-11（配置/网络/解析/过滤/格式化）
  e2e_steps_flow.py      # E2E 步骤 mixin：step 12-20（搜索/订阅/调度/推送）
```

## 数据库表

```sql
config(key, value)                                    -- Cookie 等配置，schema_version 迁移标记
subscriptions(id, session_id, type, role, target)     -- type ∈ {tag, blog}；role ∈ {subscribe, exclude}
seen_posts(session_id, type, post_id)                 -- 会话+类型维度，冷启动保护 + 新帖检测
sent_posts(session_id, post_id)                       -- 会话维度，跨订阅去重
count_conditions(name, expression, updated_at)        -- 命名标签统计表达式
author_blocks(session_id, kind, value, display)       -- 会话级作者屏蔽；kind ∈ {name, username}
```

每条订阅是一个原子条件：订阅 tag A = (tag, subscribe, A)，排除 tag B = (tag, exclude, B)，互相独立可单独删除。

## 轮询流程

```
_poll_all → 按 (session_id, type) 分组 → 不同 session 并发
  tag 路径：_check_tag_session(session_id, all_tag_subs)
      → _build_tag_rule()        # subscribe → search_tags；exclude → exclude_tags
      → fetch_tag_posts()        # 按 search_tags 逐个调 DWR，合并去重
      → apply_filter()           # 按 exclude_tags 过滤
      → filter_unseen_session()  # 会话级新帖检测
      → 冷启动判断（seen_count==0）→ mark_seen_session()
      → 作者屏蔽过滤           # 已屏蔽作者作品不推送，但已写入 seen_posts
      → filter_unsent()          # 会话级去重
      → _push_tag_posts()        # 最多5条，倒序
      → mark_sent()

  blog 路径：_check_blog_sub(sub) × N（每博主独立）
      → fetch_blog_posts()       # HTML 抓取
      → filter_unseen_session()  # 会话级（type='blog'）
      → 冷启动判断
      → mark_seen_session()
      → 作者屏蔽过滤           # 已屏蔽作者作品不推送，但已写入 seen_posts
      → filter_unsent() → _enrich_blog_posts() → 作者屏蔽过滤 → _push_blog_post() → mark_sent()
```

## Warmup 机制

新增 subscribe 记录时（`/lofter subtag`），立即抓取该 target 的当前帖子并 `mark_seen_session`，不推送。防止中途新增订阅触发全量旧帖推送。

## DWR 请求关键参数

`POST https://www.lofter.com/dwr/call/plaincall/TagBean.search.dwr`

- `c0-param3=string:new`：按最新排序
- `c0-param6=number:{limit}`：返回条数
- `c0-param7=number:0`：固定 0（不是 limit）
- `c0-param8=number:0`：固定 0（传时间戳会导致内容不是最新）

## 关键设计决策

- 订阅原子化：每条记录对应单一搜索条件（subscribe 或 exclude），可单独增删
- `seen_posts` 改为会话级（session_id, type, post_id）：聚合轮询后不再绑定 subscription_id；删单条订阅不影响历史 seen 记录
- `sent_posts` 不变（会话维度跨订阅去重）
- tag 按 session 聚合拉取：同 session 所有 subscribe target 合并成一次多 API 调用，reduce 请求次数
- blog 仍按行独立轮询：每个博主 URL 不同，无法聚合
- 作者屏蔽按 session 隔离，同时支持昵称和 Lofter 用户名匹配；订阅轮询中被屏蔽作品仍写入 seen_posts，解除屏蔽后不补推旧内容
- tag 统计独立于订阅：`/lofter count` 系列读取 `count_conditions`，不改变 `subtag` 订阅规则
- count 表达式支持 AND / OR / NOT / 括号，用于一次性统计组合标签条件
- `count-all` 汇总所有已保存统计条件并输出 CSV，优先通过 AstrBot 文件消息发送
- 统计按 `post_id` 去重；分页统计无人工页数上限，仅以空页或无新候选自然停止，保证精准统计
- `subtagpreview` 只写 `mark_seen`，不写 `mark_sent`：用户主动预览，不污染推送去重状态
- E2E 测试用隔离 session（`__lofter_e2e_test__`）跑真实网络，20 步失败不中断，测完强制清理；`db.clear_session` 和 `db.delete_config` 专为清理新增
