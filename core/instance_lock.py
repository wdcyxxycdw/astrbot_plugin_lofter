from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class InstanceLockError(RuntimeError):
    pass


class InstanceLockHeldError(InstanceLockError):
    pass


class InstanceLock:
    def __init__(self, database_path: str):
        self.path = f"{database_path}.lock"
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            _lock_handle(handle)
        except BaseException:
            handle.close()
            raise
        self._file = handle

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            _unlock_handle(handle)
        finally:
            handle.close()

    close = release

    @property
    def acquired(self) -> bool:
        return self._file is not None


def _lock_handle(handle: BinaryIO) -> None:
    if os.name == "posix":
        _lock_posix(handle)
        return
    if os.name == "nt":
        _lock_windows(handle)
        return
    raise InstanceLockError(f"unsupported advisory lock platform: {os.name}")


def _unlock_handle(handle: BinaryIO) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    raise InstanceLockError(f"unsupported advisory lock platform: {os.name}")


def _lock_posix(handle: BinaryIO) -> None:
    import errno
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise InstanceLockHeldError("another Lofter instance owns this database") from exc
        raise InstanceLockError(f"failed to acquire instance lock: {exc}") from exc


def _lock_windows(handle: BinaryIO) -> None:
    import errno
    import msvcrt

    handle.seek(0)
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise InstanceLockHeldError("another Lofter instance owns this database") from exc
        raise InstanceLockError(f"failed to acquire instance lock: {exc}") from exc
