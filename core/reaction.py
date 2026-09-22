"""给触发解析的消息贴 QQ 表情，让用户知道链接正在被处理。

仅 OneBot（aiocqhttp/NapCat）平台支持 set_msg_emoji_like，其他平台一律静默跳过。
表情 ID 走配置，因为 QQ 只接受白名单内的 ID，而 NapCat 未公开这份名单。
"""

from astrbot.api import logger

EMOJI_PARSING = 128064  # 👀
EMOJI_DONE = 124  # OK
EMOJI_FAILED = 123  # NO


async def set_reaction(event, emoji_id: int, *, on: bool = True) -> bool:
    """贴上或撤下一个表情，成功返回 True。任何失败都只记日志，不影响解析流程。"""
    call_action = getattr(getattr(event, "bot", None), "call_action", None)
    if not callable(call_action):
        return False
    message_id = getattr(getattr(event, "message_obj", None), "message_id", "")
    if not message_id:
        return False
    try:
        await call_action(
            "set_msg_emoji_like",
            message_id=str(message_id),
            emoji_id=emoji_id,
            set=on,
        )
        return True
    except Exception as e:
        logger.warning("Lofter: 贴表情失败 emoji_id=%s set=%s: %s", emoji_id, on, e)
        return False


async def replace_reaction(event, old_emoji_id: int, new_emoji_id: int | None) -> None:
    """把「处理中」换成结果表情；new_emoji_id 为 None 时只撤不贴，与旧表情相同时原样保留。"""
    if new_emoji_id == old_emoji_id:
        return
    await set_reaction(event, old_emoji_id, on=False)
    if new_emoji_id is not None:
        await set_reaction(event, new_emoji_id)
