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


class LofterClient:
    def __init__(self, cookie: str = ""):
        self._cookie = cookie
        self._session: Optional[aiohttp.ClientSession] = None

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
