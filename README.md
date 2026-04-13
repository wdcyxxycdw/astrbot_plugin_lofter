# astrbot_plugin_lofter

AstrBot 插件，用于解析 Lofter 链接、订阅 Lofter 标签/博主、搜索 Lofter 内容。
开发中，有bug或想添加新功能随时提交issue

## 功能

- **自动解析**：消息中出现 Lofter 帖子链接时，自动提取内容——图片贴返回图片消息链，文字贴提取完整正文并以合并转发方式发送
- **搜索**：按关键词搜索 Lofter 内容
- **订阅标签**：订阅指定标签，有新内容时自动推送
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

返回格式：

**图片贴**（含 Lofter CDN 图片）：
```
▸ 帖子标题
作者：xxx（如有）
#tag1 #tag2（如有）
摘要内容（如有）
https://...
[图片]
```

**文字贴**（无图片）：以合并转发消息发送，第一条为标题/作者/标签，中间为正文分段内容，最后一条为原链接。正文超过 2 万字时最多发送前 10 块（约 2 万字），并附提示。

### 命令

| 命令 | 说明 |
|------|------|
| `/lofter search <关键词>` | 搜索 Lofter 内容 |
| `/lofter subtag <标签名> [过滤条件]` | 订阅标签（支持高级过滤语法） |
| `/lofter subtagpreview <标签名> [过滤条件]` | 订阅标签并立即预览最新 3 条内容 |
| `/lofter subblog <用户名>` | 订阅博主 |
| `/lofter unsubtag <标签名>` | 取消订阅标签 |
| `/lofter unsubblog <用户名>` | 取消订阅博主 |
| `/lofter list` | 查看当前会话的订阅列表 |
| `/lofter cookie <值>` | 运行时更新 Lofter Cookie，立即生效 |

### 订阅标签高级过滤语法

`/lofter subtag` 和 `/lofter subtagpreview` 支持在标签名后附加过滤条件：

| 语法 | 说明 | 示例 |
|------|------|------|
| `标签A\|标签B` | OR 搜索，同时抓取多个标签的内容 | `/lofter subtag 魔法少女的魔女审判\|魔法少女的魔女裁判` |
| `+包含标签` | AND 过滤，只推送同时带有该标签的帖子 | `/lofter subtag 魔法少女的魔女裁判 +原创` |
| `-排除标签` | EXCLUDE 过滤，排除带有该标签的帖子 | `/lofter subtag 魔法少女的魔女裁判 -原创` |
| 组合使用 | 可同时指定多个条件 | `/lofter subtag 魔法少女的魔女裁判 +原创 -樱羽艾玛` |
| `标签X\|标签Y`（过滤条件中） | OR 过滤，至少含其中一个标签 | `/lofter subtag 魔法少女的魔女裁判 艾希\|希艾` |

**示例：**
```
/lofter subtag 魔法少女的魔女裁判                         # 订阅「魔法少女的魔女裁判」标签
/lofter subtag 魔法少女的魔女审判|魔法少女的魔女裁判  # 同时订阅两个标签名
/lofter subtag 魔法少女的魔女裁判 +艾希 -希艾              # 订阅「魔法少女的魔女裁判」，只要带「艾希」且不带「希艾」的帖子
```

> OR 搜索标签数量上限由配置项 `max_or_tags` 控制，默认为 3。

## 数据存储

插件使用 SQLite 持久化数据，文件位于：

```
<astrbot_root>/data/plugins/astrbot_plugin_lofter/lofter.db
```

- **精确去重**：通过 `seen_posts` 表记录各订阅已处理的帖子 ID，两次轮询间出现多条新帖也不会漏推
- **跨订阅去重**：通过 `sent_posts` 表记录会话维度已推送的帖子 ID，同一帖子打了多个已订阅标签时只推送一次
- **冷启动保护**：首次轮询时自动标记所有已有内容，不会刷屏推送历史帖子
- **JSON 迁移**：若目录下存在旧版 `subscriptions.json`，首次启动会自动导入，原文件保留

## 许可证

AGPLv3
