import time

import aiohttp
from typing import Optional


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) "
        "Gecko/20100101 Firefox/145.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Referer": "https://www.lofter.com/",
}

DWR_SEARCH_URL = "https://www.lofter.com/dwr/call/plaincall/TagBean.search.dwr"


class LofterClient:
    def __init__(self, cookie: str = ""):
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

    async def search_tag(self, tag: str, offset: int = 0, limit: int = 20) -> str:
        """调用 DWR TagBean.search 接口，返回原始响应文本。"""
        ts = int(time.time() * 1000)
        body = (
            f"callCount=1\n"
            f"scriptSessionId=${{scriptSessionId}}187\n"
            f"httpSessionId=\n"
            f"c0-scriptName=TagBean\n"
            f"c0-methodName=search\n"
            f"c0-id=0\n"
            f"c0-param0=string:{tag}\n"
            f"c0-param1=number:{offset}\n"
            f"c0-param2=string:\n"
            f"c0-param3=string:new\n"
            f"c0-param4=boolean:false\n"
            f"c0-param5=number:0\n"
            f"c0-param6=number:{limit}\n"
            f"c0-param7=number:{limit}\n"
            f"c0-param8=number:{ts}\n"
            f"batchId=1"
        )
        headers = {**HEADERS, "Content-Type": "text/plain", "Referer": f"https://www.lofter.com/tag/{tag}"}
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
