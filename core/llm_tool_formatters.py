from __future__ import annotations

from typing import Any

from .formatter import format_post, visible_images


def format_subscription_line(index: int, sub: Any) -> str:
    if sub.type == "tag":
        role_label = "订阅" if sub.role == "subscribe" else "排除"
        return f"{index}. [标签｜{role_label}] {sub.target}"
    return f"{index}. [博主]       {sub.target}"


def format_added_tag_result(added_subs: list[str], added_excls: list[str]) -> str:
    if not added_subs and not added_excls:
        return "订阅已存在，无需重复添加"
    parts = []
    if added_subs:
        parts.append(f"新增订阅：{', '.join(added_subs)}")
    if added_excls:
        parts.append(f"新增排除：{', '.join(added_excls)}")
    return "\n".join(parts)


def format_preview_result(tag: str, posts: list[Any], max_images: int) -> str:
    if not posts:
        return f"已订阅标签「{tag}」，暂无未屏蔽作者的匹配内容"
    count = min(3, len(posts))
    lines = [f"已订阅标签「{tag}」，以下是最新 {count} 条内容："]
    lines.extend(format_post_for_tool(post, include_time=True, max_images=max_images) for post in posts[:3])
    return "\n\n".join(lines)


def missing_remove_target(sub_type: str, role: str) -> str:
    if sub_type == "blog":
        return "请提供要取消的博主"
    return "请提供要取消的排除标签" if role == "exclude" else "请提供要取消的订阅标签"


def format_remove_result(ok: bool, sub_type: str, target: str, role: str) -> str:
    if sub_type == "blog":
        return f"已取消订阅博主「{target}」" if ok else f"未找到博主「{target}」的订阅"
    if role == "exclude":
        return f"已取消排除标签「{target}」" if ok else f"未找到标签「{target}」的排除规则"
    return f"已取消订阅标签「{target}」" if ok else f"未找到标签「{target}」的订阅"


def format_index_remove_result(ok: bool, index: int, sub: Any) -> str:
    if not ok:
        return "删除失败，请重新 list 确认编号"
    role_label = "排除" if sub.role == "exclude" else "订阅"
    type_label = "标签" if sub.type == "tag" else "博主"
    return f"已删除第 {index} 条：[{type_label}｜{role_label}] {sub.target}"


def format_post_for_tool(post: Any, include_time: bool = False, max_images: int = 3) -> str:
    text = format_post(post, include_time=include_time)
    images = visible_images(post)
    if not images:
        return text
    image_lines = "\n".join(
        f"图片：{url}" for url in images[:max_images]
    )
    return f"{text}\n{image_lines}"


def unknown_action(tool_name: str, actions: tuple[str, ...]) -> str:
    return f"未知 {tool_name} action，请使用：{', '.join(actions)}"
