# astrbot_plugin_lofter

AstrBot 插件，用于解析 Lofter 链接、订阅 Lofter 标签/博主、搜索 Lofter 内容。

## 功能

- **自动解析**：消息中出现 Lofter 帖子链接时，自动提取正文和图片
- **搜索**：按关键词搜索 Lofter 内容
- **订阅标签**：订阅指定标签，有新内容时自动推送
- **订阅博主**：订阅指定博主，发布新文章时自动推送

## 安装

在 AstrBot WebUI 的插件市场中搜索 `lofter` 安装，或将本仓库克隆到 `data/plugins/` 目录。

## 配置

在 WebUI 插件配置页填写以下参数：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `lofter_cookie` | Lofter 登录 Cookie（从浏览器开发者工具复制） | 空 |
| `poll_interval` | 订阅轮询间隔（分钟） | 30 |
| `max_images` | 解析帖子时最多展示的图片数量 | 3 |

### 获取 Cookie

1. 浏览器登录 [lofter.com](https://www.lofter.com)
2. 打开开发者工具（F12）→ Network 标签
3. 刷新页面，点击任意请求，复制请求头中的 `Cookie` 字段值
4. 粘贴到插件配置的 `lofter_cookie` 中

## 使用

### 自动解析链接

直接在聊天中发送 Lofter 帖子链接，插件会自动解析并返回内容：

```
https://username.lofter.com/post/xxxxxx
```

### 命令

| 命令 | 说明 |
|------|------|
| `/lofter search <关键词>` | 搜索 Lofter 内容 |
| `/lofter sub tag <标签名>` | 订阅标签 |
| `/lofter sub blog <用户名>` | 订阅博主 |
| `/lofter unsub tag <标签名>` | 取消订阅标签 |
| `/lofter unsub blog <用户名>` | 取消订阅博主 |
| `/lofter list` | 查看当前会话的订阅列表 |

## 许可证

AGPLv3
