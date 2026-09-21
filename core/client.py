import asyncio
from urllib.parse import quote

import aiohttp


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) "
        "Gecko/20100101 Firefox/145.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.lofter.com/",
}

DWR_SEARCH_URL = "https://www.lofter.com/dwr/call/plaincall/TagBean.search.dwr"


def build_tag_search_body(tag: str, offset: int = 0, limit: int = 20, before: int = 0) -> str:
    return (
        f"callCount=1\n"
        f"scriptSessionId=${{scriptSessionId}}187\n"
        f"httpSessionId=\n"
        f"c0-scriptName=TagBean\n"
        f"c0-methodName=search\n"
        f"c0-id=0\n"
        f"c0-param0=string:{quote(tag, safe='')}\n"
        f"c0-param1=number:0\n"
        f"c0-param2=string:\n"
        f"c0-param3=string:new\n"
        f"c0-param4=boolean:false\n"
        f"c0-param5=number:0\n"
        f"c0-param6=number:{limit}\n"
        f"c0-param7=number:{offset}\n"
        f"c0-param8=number:{before}\n"
        f"batchId=1"
    )


class LofterClient:
    def __init__(self, cookie: str = ""):
        self._cookie = cookie
        self._session: aiohttp.ClientSession | None = None
        self._request_lock = asyncio.Lock()
        self._next_request = 0.0

    def update_cookie(self, cookie: str):
        self._cookie = cookie
        if self._session is not None:
            self._session.cookie_jar.clear()

    def _make_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(headers=HEADERS)

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def _wait_for_request(self):
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self._next_request - loop.time()))
            self._next_request = loop.time() + 0.3

    async def _request(self, method: str, url: str, *, timeout: int, data=None, headers=None) -> str:
        if self._session is None or self._session.closed:
            self._session = self._make_session()
        request_headers = dict(headers or {})
        request_headers["Cookie"] = self._cookie
        for attempt in range(3):
            await self._wait_for_request()
            try:
                async with self._session.request(
                    method, url, data=data, headers=request_headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    resp.raise_for_status()
                    return await resp.text()
            except aiohttp.ClientResponseError as exc:
                if exc.status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                if attempt == 2:
                    raise
            await asyncio.sleep(2 ** attempt)
        raise RuntimeError("LOFTER 请求失败")

    async def get(self, url: str, timeout: int = 15) -> str:
        return await self._request("GET", url, timeout=timeout)

    async def search_tag_paged(self, tag: str, total: int) -> list[str]:
        """翻页获取多页 DWR 响应，total 上限 100，每页 20 条，返回各页原始文本列表。"""
        from .dwr_parser import parse_dwr_response

        total = min(total, 100)
        page_size = 20
        pages = []
        before = 0
        for offset in range(0, total, page_size):
            limit = min(page_size, total - offset)
            raw = await self.search_tag(tag, offset=offset, limit=limit, before=before)
            pages.append(raw)
            posts = await parse_dwr_response(raw)
            if not posts:
                break
            timestamps = [post.publish_time_ms for post in posts if post.publish_time_ms > 0]
            if timestamps:
                before = min(timestamps)
        return pages

    async def search_tag(self, tag: str, offset: int = 0, limit: int = 20, before: int = 0) -> str:
        """调用 DWR TagBean.search 接口，返回原始响应文本。"""
        body = build_tag_search_body(tag, offset=offset, limit=limit, before=before)
        headers = {
            **HEADERS,
            "Content-Type": "text/plain",
            "Referer": f"https://www.lofter.com/tag/{quote(tag, safe='')}",
        }
        return await self._request("POST", DWR_SEARCH_URL, data=body, headers=headers, timeout=20)
