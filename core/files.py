"""发送文件：文件名清洗，以及构造 AstrBot 的文件消息段。

不同 AstrBot 版本的 File 构造方式不一致，逐个试。
"""

import re
from pathlib import Path

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_NAME_LENGTH = 80


def safe_filename(title: str, fallback: str, suffix: str) -> str:
    """用文章标题当文件名。去掉文件系统不接受的字符，标题为空时退回帖子 ID。"""
    name = _UNSAFE_CHARS.sub("", title).strip().rstrip(".")
    return f"{name[:MAX_NAME_LENGTH] or fallback}{suffix}"


def build_file_component(path: Path):
    try:
        import astrbot.api.message_components as Comp
    except Exception:
        return None
    file_cls = getattr(Comp, "File", None)
    if file_cls is None:
        return None
    return _try_file_factories(file_cls, path)


def _try_file_factories(file_cls, path: Path):
    for name in ("fromPath", "fromFileSystem", "fromLocalPath"):
        component = _try_file_factory(getattr(file_cls, name, None), path)
        if component is not None:
            return component
    return _try_direct_file(file_cls, path)


def _try_file_factory(factory, path: Path):
    if not callable(factory):
        return None
    try:
        return factory(str(path))
    except Exception:
        return None


def _try_direct_file(file_cls, path: Path):
    for args, kwargs in _file_constructor_candidates(path):
        try:
            return file_cls(*args, **kwargs)
        except Exception:
            continue
    return None


def _file_constructor_candidates(path: Path):
    text = str(path)
    return [((path.name,), {"file": text}), ((text,), {}), ((), {"path": text}), ((), {"file": text})]
