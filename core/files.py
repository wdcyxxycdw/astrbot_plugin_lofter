"""构造 AstrBot 的文件消息段。不同 AstrBot 版本的 File 构造方式不一致，逐个试。"""

from pathlib import Path


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
