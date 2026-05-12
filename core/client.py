import asyncio

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


def build_tag_search_body(tag: str, offset: int = 0, limit: int = 20) -> str:
    return (
        f"callCount=1\n"
        f"scriptSessionId=${{scriptSessionId}}187\n"
        f"httpSessionId=\n"
        f"c0-scriptName=TagBean\n"
        f"c0-methodName=search\n"
        f"c0-id=0\n"
        f"c0-param0=string:{tag}\n"
        f"c0-param1=number:0\n"
        f"c0-param2=string:\n"
        f"c0-param3=string:new\n"
        f"c0-param4=boolean:false\n"
        f"c0-param5=number:0\n"
        f"c0-param6=number:{limit}\n"
        f"c0-param7=number:{offset}\n"
        f"c0-param8=number:0\n"
        f"batchId=1"
    )


class LofterClient:
    def __init__(self, cookie: str = ""):
        self._cookie = cookie

    def update_cookie(self, cookie: str):
        self._cookie = cookie

    def _make_session(self) -> aiohttp.ClientSession:
        headers = dict(HEADERS)
        if self._cookie:
            headers["Cookie"] = self._cookie
        return aiohttp.ClientSession(headers=headers)

    async def get(self, url: str, timeout: int = 15) -> str:
        async with self._make_session() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                resp.raise_for_status()
                return await resp.text()

    async def search_tag_paged(self, tag: str, total: int) -> list[str]:
        """翻页获取多页 DWR 响应，total 上限 100，每页 20 条，返回各页原始文本列表。"""
        total = min(total, 100)
        page_size = 20
        pages = []
        for offset in range(0, total, page_size):
            limit = min(page_size, total - offset)
            raw = await self.search_tag(tag, offset=offset, limit=limit)
            pages.append(raw)
            if offset + page_size < total:
                await asyncio.sleep(0.3)
        return pages

    async def search_tag(self, tag: str, offset: int = 0, limit: int = 20) -> str:
        """调用 DWR TagBean.search 接口，返回原始响应文本。"""
        body = build_tag_search_body(tag, offset=offset, limit=limit)
        headers = {
            **HEADERS,
            "Content-Type": "text/plain",
            "Referer": f"https://www.lofter.com/tag/{tag}",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                DWR_SEARCH_URL,
                data=body,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                resp.raise_for_status()
                return await resp.text()
