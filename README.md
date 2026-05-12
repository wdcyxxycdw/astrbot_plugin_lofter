# astrbot_plugin_lofter

AstrBot 插件，用于解析 Lofter 链接、订阅 Lofter 标签/博主、搜索 Lofter 内容。
开发中，有 bug 或想添加新功能随时提交 issue

## 功能

- **自动解析**：消息中出现 Lofter 帖子链接时，自动提取内容——图片贴返回图片消息链，文字贴提取完整正文并以合并转发方式发送
- **搜索**：按关键词搜索 Lofter 内容
- **标签统计**：按标签表达式统计作品数量，支持保存多组条件并导出 CSV
- **订阅标签**：订阅指定标签，有新内容时自动推送；支持同时添加排除规则
- **订阅博主**：订阅指定博主，发布新文章时自动推送（含标题、作者、摘要、标签和图片）

## 安装

在 AstrBot WebUI 的插件市场中搜索 `lofter` 安装，或将本仓库克隆到 `data/plugins/` 目录。

## 配置

在 WebUI 插件配置页填写以下参数：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `lofter_cookie` | Lofter 登录 Cookie（从浏览器开发者工具复制） | 空 |
| `poll_interval` | 订阅轮询间隔（分钟） | 30 |
| `max_images` | 解析帖子时最多展示的图片数量 | 3 |
| `search_limit` | 搜索结果最多返回的条数（最大 100，超过 20 条自动翻页） | 3 |

### 获取 Cookie

1. 浏览器登录 [lofter.com](https://www.lofter.com)
2. 打开开发者工具（F12）→ Network 标签
3. 刷新页面，点击任意请求，复制请求头中的 `Cookie` 字段值
4. 粘贴到插件配置的 `lofter_cookie` 中，或运行时使用 `/lofter cookie <值>` 命令更新

## 使用

### 自动解析链接

直接在聊天中发送 Lofter 帖子链接，插件会自动解析并返回内容：

```
https://username.lofter.com/post/xxxxxx
```

**图片贴**（含 Lofter CDN 图片）：
```
▸ 帖子标题
作者：xxx

#tag1 #tag2

摘要内容

──────────────
https://...
[图片]
```

**文字贴**（无图片）：以合并转发消息发送，第一条为标题/作者/标签，中间为正文分段内容，最后一条为原链接。正文超过 2 万字时最多发送前 10 块（约 2 万字），并附提示。

### 命令

| 命令 | 说明 |
|------|------|
| `/lofter search <关键词>` | 搜索 Lofter 内容 |
| `/lofter subtag <标签名> [-排除标签...]` | 订阅标签，可同时指定排除规则 |
| `/lofter subtagpreview <标签名> [-排除标签...]` | 订阅标签并立即预览最新 3 条内容 |
| `/lofter subblog <用户名>` | 订阅博主 |
| `/lofter count <名称> = <表达式>` | 保存并执行标签表达式统计 |
| `/lofter count-list` | 查看已保存的全局统计条件（带编号） |
| `/lofter count-del <名称或编号>` | 按名称或 `count-list` 编号删除统计条件 |
| `/lofter count-all` | 执行全部统计条件并生成 CSV |
| `/lofter list` | 查看当前会话的订阅列表（带编号） |
| `/lofter unsub <编号>` | 按编号取消订阅（编号来自 `/lofter list`） |
| `/lofter unsubtag <标签名>` | 取消订阅指定标签 |
| `/lofter unexcludetag <标签名>` | 取消指定标签的排除规则 |
| `/lofter unsubblog <用户名>` | 取消订阅博主 |
| `/lofter cookie <值>` | 运行时更新 Lofter Cookie，立即生效 |

### 订阅标签与排除规则

`/lofter subtag` 支持在标签名后用 `-` 添加排除规则，排除规则可以单独删除，不影响订阅本身：

```
/lofter subtag 原神                    # 订阅「原神」标签
/lofter subtag 原神 -R18               # 订阅「原神」，同时排除带「R18」标签的帖子
/lofter subtag 原神 -R18 -暴力         # 可同时添加多条排除规则
```

订阅列表（`/lofter list`）会显示每条记录的编号和类型：

```
1. [标签｜订阅] 原神
2. [标签｜排除] R18
3. [博主]       username
```

用 `/lofter unsub 2` 可直接按编号删除某条记录。

### 标签统计命令

统计条件是全局保存的命名表达式，只有管理员可用：

```
/lofter count 米哈游相关 = 原神|崩铁 -R18
/lofter count-list
/lofter count-del 米哈游相关
/lofter count-del 1
/lofter count-all
```

表达式规则：空格表示 AND，`|` 表示 OR，`-` 表示 NOT，支持半角/全角括号分组。统计至少需要一个正向标签；结果会显示候选作品数、每个正向标签扫描页数。若接口在翻页后持续返回重复内容，会提示“疑似分页未生效或接口返回重复页”。

`/lofter count-all` 会统计所有已保存条件并生成 CSV，CSV 包含作品数、候选作品、扫描页数、状态和错误/提示信息。

## 数据存储

插件使用 SQLite 持久化数据，文件位于：

```
<astrbot_root>/data/plugins/astrbot_plugin_lofter/lofter.db
```

- **精确去重**：通过 `seen_posts` 表记录会话已处理的帖子 ID，两次轮询间出现多条新帖也不会漏推
- **跨订阅去重**：通过 `sent_posts` 表记录会话维度已推送的帖子 ID，同一帖子匹配多个订阅标签时只推送一次
- **冷启动保护**：首次订阅及每次新增订阅时自动预热，不会刷屏推送历史帖子
- **JSON 迁移**：若目录下存在旧版 `subscriptions.json`，首次启动会自动导入，原文件保留

## 许可证

AGPLv3
