ADMIN_ONLY_MESSAGE = "只有 AstrBot 管理员可以使用此功能"


def is_admin_event(event) -> bool:
    try:
        checker = getattr(event, "is_admin", None)
        if not callable(checker):
            return False
        return bool(checker())
    except Exception:
        return False
