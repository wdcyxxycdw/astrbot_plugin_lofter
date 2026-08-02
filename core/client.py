import asyncio
import json
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, Mapping
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from .errors import (
    SourceClosingError,
    SourceHTTPError,
    SourceLimitError,
    SourceRetryExhaustedError,
    SourceSchemaError,
    SourceTimeoutError,
)
from .source_limits import MAX_URL_BYTES, validate_text_bytes

CONNECT_TIMEOUT = 10
TOTAL_TIMEOUT = 30
MAX_BODY_BYTES = 5 * 1024 * 1024
GLOBAL_HTTP_LIMIT = 8
HOST_HTTP_LIMIT = 4
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 5
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}

PUBLIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) "
        "Gecko/20100101 Firefox/145.0"
    ),
    "Accept-Encoding": "gzip, deflate",
}
HTML_HEADERS = {
    **PUBLIC_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.lofter.com/",
}
JSON_HEADERS = {
    **PUBLIC_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
DWR_SEARCH_URL = "https://www.lofter.com/dwr/call/plaincall/TagBean.search.dwr"


@dataclass(frozen=True)
class _RequestSpec:
    method: str
    url: str
    data: object | None
    headers: Mapping[str, str]
    credentialed: bool
    cookie: str


class _RetryableStatus(Exception):
    def __init__(self, status: int, retry_after: float | None):
        super().__init__(str(status))
        self.status = status
        self.retry_after = retry_after


def build_tag_search_body(tag: str, offset: int = 0, limit: int = 20) -> str:
    return (
        "callCount=1\n"
        "scriptSessionId=${scriptSessionId}187\n"
        "httpSessionId=\n"
        "c0-scriptName=TagBean\n"
        "c0-methodName=search\n"
        "c0-id=0\n"
        f"c0-param0=string:{tag}\n"
        "c0-param1=number:0\n"
        "c0-param2=string:\n"
        "c0-param3=string:new\n"
        "c0-param4=boolean:false\n"
        "c0-param5=number:0\n"
        f"c0-param6=number:{limit}\n"
        f"c0-param7=number:{offset}\n"
        "c0-param8=number:0\n"
        "batchId=1"
    )


def _default_jitter(attempt: int) -> float:
    return ((attempt * 17) % 10) / 100


def _parse_url(url: str) -> URL:
    validate_text_bytes(url, "url", MAX_URL_BYTES)
    try:
        parsed = URL(url)
        port = parsed.port
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SourceSchemaError("url") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise SourceSchemaError("url")
    return parsed


def _origin(url: str) -> tuple[str, str, int]:
    parsed = _parse_url(url)
    default_port = 443 if parsed.scheme == "https" else 80
    return (
        parsed.scheme.lower(),
        parsed.host.lower(),
        parsed.port if parsed.port is not None else default_port,
    )


def _same_origin(left: str, right: str) -> bool:
    return _origin(left) == _origin(right)


def _validate_credentialed_target(url: str) -> None:
    parsed = _parse_url(url)
    scheme, hostname, port = _origin(url)
    first_party = hostname == "lofter.com" or hostname.endswith(".lofter.com")
    if parsed.user is not None or parsed.password is not None:
        raise SourceSchemaError("url")
    if scheme != "https" or port != 443 or not first_party:
        raise SourceSchemaError("url")


def _redirect_method(method: str, status: int) -> str:
    if status == 303 and method != "HEAD":
        return "GET"
    if status in {301, 302} and method == "POST":
        return "GET"
    return method


class LofterClient:
    def __init__(
        self,
        cookie: str = "",
        *,
        session_factory: Callable[..., aiohttp.ClientSession] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[int], float] = _default_jitter,
    ):
        self._cookie = cookie
        self._session_factory = session_factory or aiohttp.ClientSession
        self._sleep = sleep
        self._jitter = jitter
        self._session: aiohttp.ClientSession | None = None
        self._state = asyncio.Condition()
        self._initialize_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._active = 0
        self._closing = False
        self._closed = False
        self._request_slots = asyncio.Semaphore(GLOBAL_HTTP_LIMIT)

    async def initialize(self) -> None:
        async with self._initialize_lock:
            if self._session is not None:
                return
            if self._closing or self._closed:
                raise SourceClosingError("HTTP client is closing")
            timeout = aiohttp.ClientTimeout(
                total=TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT, sock_connect=CONNECT_TIMEOUT
            )
            connector = aiohttp.TCPConnector(
                limit=GLOBAL_HTTP_LIMIT, limit_per_host=HOST_HTTP_LIMIT
            )
            try:
                self._session = self._session_factory(
                    headers=PUBLIC_HEADERS,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    connector=connector,
                    timeout=timeout,
                    trust_env=False,
                )
            except BaseException:
                await connector.close()
                raise

    def update_cookie(self, cookie: str) -> None:
        self._cookie = cookie.strip()

    async def close(self) -> None:
        task = await self._get_close_task()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except BaseException:
            async with self._close_lock:
                if self._close_task is task:
                    self._close_task = None
            raise

    async def _get_close_task(self) -> asyncio.Task[None]:
        async with self._close_lock:
            if self._closed:
                return asyncio.create_task(self._noop())
            if self._close_task is not None and self._close_task.done():
                try:
                    failure = self._close_task.exception()
                except asyncio.CancelledError:
                    failure = True
                if failure is not None:
                    self._close_task = None
            if self._close_task is None:
                self._closing = True
                self._close_task = asyncio.create_task(self._close_session())
            return self._close_task

    async def _close_session(self) -> None:
        async with self._state:
            await self._state.wait_for(lambda: self._active == 0)
            session = self._session
        if session is not None:
            await session.close()
        async with self._state:
            if self._session is session:
                self._session = None
            self._closed = True

    @staticmethod
    async def _noop() -> None:
        return None

    async def get(self, url: str, *, credentialed: bool = False) -> str:
        return await self.request_text(
            "GET", url, headers=HTML_HEADERS, credentialed=credentialed
        )

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        data: object | None = None,
        headers: Mapping[str, str] | None = None,
        credentialed: bool = False,
    ) -> object:
        text = await self.request_text(
            method, url, data=data, headers=headers or JSON_HEADERS,
            credentialed=credentialed,
        )
        try:
            return json.loads(text)
        except (TypeError, ValueError, RecursionError):
            raise SourceSchemaError("json") from None

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        data: object | None = None,
        headers: Mapping[str, str] | None = None,
        credentialed: bool = False,
    ) -> str:
        self._validate_url(url)
        if credentialed:
            self._validate_credential_target(url)
        session, cookie = await self._begin_request(credentialed)
        spec = _RequestSpec(
            method.upper(), url, data, headers or PUBLIC_HEADERS, credentialed, cookie
        )
        try:
            return await self._request_with_retries(session, spec)
        finally:
            await self._end_request()

    async def search_tag_paged(self, tag: str, total: int) -> list[str]:
        pages = []
        for offset in range(0, min(total, 100), 20):
            limit = min(20, total - offset)
            pages.append(await self.search_tag(tag, offset=offset, limit=limit))
        return pages

    async def search_tag(self, tag: str, offset: int = 0, limit: int = 20) -> str:
        body = build_tag_search_body(tag, offset=offset, limit=limit)
        headers = {
            **HTML_HEADERS,
            "Content-Type": "text/plain",
            "Referer": f"https://www.lofter.com/tag/{tag}",
        }
        return await self.request_text(
            "POST", DWR_SEARCH_URL, data=body, headers=headers, credentialed=True
        )

    async def _begin_request(
        self, credentialed: bool
    ) -> tuple[aiohttp.ClientSession, str]:
        async with self._state:
            if self._closing or self._closed:
                raise SourceClosingError("HTTP client is closing")
            if self._session is None:
                raise SourceClosingError("HTTP client is not initialized")
            self._active += 1
            cookie = self._cookie if credentialed else ""
            return self._session, cookie

    async def _end_request(self) -> None:
        async with self._state:
            self._active -= 1
            if self._active == 0:
                self._state.notify_all()

    async def _request_with_retries(
        self, session: aiohttp.ClientSession, spec: _RequestSpec
    ) -> str:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await self._request_redirects(session, spec)
            except _RetryableStatus as exc:
                delay = exc.retry_after
                reason = f"HTTP {exc.status}"
            except asyncio.TimeoutError:
                delay = None
                reason = "timeout"
            except aiohttp.ClientConnectionError as exc:
                delay = None
                reason = type(exc).__name__
            if attempt == MAX_ATTEMPTS:
                if reason == "timeout":
                    raise SourceTimeoutError(
                        f"request timed out after {MAX_ATTEMPTS} attempts"
                    )
                raise SourceRetryExhaustedError(MAX_ATTEMPTS)
            await self._sleep(self._retry_delay(attempt, delay))
        raise AssertionError("unreachable")

    async def _request_redirects(
        self, session: aiohttp.ClientSession, spec: _RequestSpec
    ) -> str:
        url, method, data = spec.url, spec.method, spec.data
        for redirect_count in range(MAX_REDIRECTS + 1):
            self._validate_url(url)
            async with self._request_slots:
                response = await self._request_once(session, method, url, data, spec)
                if response.status not in REDIRECT_STATUSES:
                    return await self._consume_response(response)
                location = response.headers.get("Location")
                response.release()
            if not location:
                raise SourceHTTPError(response.status, "redirect has no location")
            next_url = urljoin(url, location)
            self._validate_url(next_url)
            if spec.credentialed:
                if not _same_origin(url, next_url):
                    raise SourceHTTPError(
                        response.status, "credentialed redirect changed origin"
                    )
                self._validate_credential_target(next_url)
            if redirect_count == MAX_REDIRECTS:
                raise SourceHTTPError(response.status, "too many redirects")
            next_method = _redirect_method(method, response.status)
            data = None if next_method == "GET" and method != "GET" else data
            method, url = next_method, next_url
        raise AssertionError("unreachable")

    async def _request_once(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        data: object | None,
        spec: _RequestSpec,
    ) -> aiohttp.ClientResponse:
        headers = {
            key: value
            for key, value in spec.headers.items()
            if key.lower() not in {"authorization", "cookie"}
        }
        if spec.credentialed and spec.cookie:
            headers["Cookie"] = spec.cookie
        return await session.request(
            method, url, data=data, headers=headers, allow_redirects=False
        )

    async def _consume_response(self, response: aiohttp.ClientResponse) -> str:
        try:
            if response.status in RETRY_STATUSES:
                retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
                raise _RetryableStatus(response.status, retry_after)
            if response.status >= 400:
                raise SourceHTTPError(response.status, "HTTP request failed")
            raw = await self._read_limited(response)
            encoding = response.charset or "utf-8"
            return raw.decode(encoding, errors="replace")
        finally:
            response.release()

    async def _read_limited(self, response: aiohttp.ClientResponse) -> bytes:
        chunks = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            chunks.extend(chunk)
            if len(chunks) > MAX_BODY_BYTES:
                raise SourceLimitError("body", MAX_BODY_BYTES)
        return bytes(chunks)

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return max(0.0, retry_after)
        return 0.25 * (2 ** (attempt - 1)) + self._jitter(attempt)

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp()
            except (TypeError, ValueError, OverflowError):
                return None
            return max(0.0, delay - time.time())

    @staticmethod
    def _validate_url(url: str) -> None:
        _parse_url(url)

    @staticmethod
    def _validate_credential_target(url: str) -> None:
        _validate_credentialed_target(url)
