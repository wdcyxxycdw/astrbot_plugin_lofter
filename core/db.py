from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from enum import Enum, auto
from typing import TypeVar

from .db_migrations import FaultHook, initialize_schema
from .db_repository import LofterRepositoryMixin

T = TypeVar("T")
BUSY_TIMEOUT_MS = 5000


class SQLiteBusyError(RuntimeError):
    pass


class DatabaseClosedError(RuntimeError):
    pass


class DatabaseState(Enum):
    CLOSED = auto()
    OPENING = auto()
    OPEN = auto()
    CLOSING = auto()


class LofterDB(LofterRepositoryMixin):
    def __init__(self, db_path: str, *, migration_fault_hook: FaultHook | None = None):
        self._path = db_path
        self._conn: sqlite3.Connection | None = None
        self._migration_fault_hook = migration_fault_hook
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lofter-db")
        self._executor_closed = False
        self._state = DatabaseState.CLOSED
        self._state_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._initialize_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None

    def _run(self, fn: Callable[[], T]):
        if self._executor_closed:
            raise DatabaseClosedError("database executor is closed")
        return asyncio.get_running_loop().run_in_executor(self._executor, fn)

    async def initialize(self) -> None:
        async with self._state_lock:
            if self._state is DatabaseState.OPEN:
                return
            if self._state is DatabaseState.CLOSING or self._executor_closed:
                raise DatabaseClosedError("database is closing or closed")
            if self._state is DatabaseState.CLOSED:
                self._state = DatabaseState.OPENING
                self._initialize_task = asyncio.create_task(self._finish_initialize())
            task = self._initialize_task
        await asyncio.shield(task)

    async def _finish_initialize(self) -> None:
        try:
            conn = await self._run(self._open_connection)
        except BaseException:
            async with self._state_lock:
                if self._state is DatabaseState.OPENING:
                    self._state = DatabaseState.CLOSED
            raise
        async with self._state_lock:
            if self._state is DatabaseState.OPENING:
                self._conn = conn
                self._state = DatabaseState.OPEN
                return
        await self._run(conn.close)

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            initialize_schema(conn, self._migration_fault_hook)
            return conn
        except BaseException as exc:
            conn.close()
            classified = _classify_sqlite_error(exc)
            if classified is exc:
                raise
            raise classified from exc

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._finish_close())
            task = self._close_task
        await _await_cleanup_before_cancel(task)

    async def _finish_close(self) -> None:
        task, conn = await self._begin_close()
        try:
            if task is not None:
                await _await_initialize_for_close(task)
            if conn is None:
                conn = await self._take_connection()
            if conn is not None:
                await self._run(conn.close)
        finally:
            self._executor.shutdown(wait=True)
            self._executor_closed = True
            async with self._state_lock:
                self._conn = None
                self._state = DatabaseState.CLOSED
                self._initialize_task = None

    async def _begin_close(
        self,
    ) -> tuple[asyncio.Task | None, sqlite3.Connection | None]:
        async with self._state_lock:
            self._state = DatabaseState.CLOSING
            task = self._initialize_task
            conn, self._conn = self._conn, None
            return task, conn

    async def _take_connection(self) -> sqlite3.Connection | None:
        async with self._state_lock:
            conn, self._conn = self._conn, None
            return conn

    async def transaction(self, callback: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._require_connection()

        def run_transaction() -> T:
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = callback(conn)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise TypeError("database transaction callback must be synchronous")
                conn.commit()
                return result
            except BaseException as exc:
                if conn.in_transaction:
                    conn.rollback()
                classified = _classify_sqlite_error(exc)
                if classified is exc:
                    raise
                raise classified from exc

        return await self._run(run_transaction)

    def _require_connection(self) -> sqlite3.Connection:
        if self._state is not DatabaseState.OPEN or self._conn is None:
            raise DatabaseClosedError("database is not initialized")
        return self._conn


async def _await_cleanup_before_cancel(task: asyncio.Task) -> None:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def _await_initialize_for_close(task: asyncio.Task) -> None:
    try:
        await asyncio.shield(task)
    except (Exception, asyncio.CancelledError):
        pass


def _classify_sqlite_error(exc: BaseException) -> BaseException:
    if not isinstance(exc, sqlite3.OperationalError):
        return exc
    code = getattr(exc, "sqlite_errorcode", None)
    locked_codes = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    message = str(exc).lower()
    if code in locked_codes or "database is locked" in message or "database table is locked" in message:
        return SQLiteBusyError(str(exc))
    return exc
