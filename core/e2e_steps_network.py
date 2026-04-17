from __future__ import annotations

import re

from .dwr_engine import execute_dwr
from .dwr_parser import parse_dwr_response
from .filter import FilterRule, apply_filter, parse_tag_expr
from .formatter import format_post
from .parser import Post, parse_blog_posts
from .scheduler import fetch_tag_posts
from .utils import _split_text

POST_PATTERN = re.compile(r"[a-zA-Z0-9_-]+\.lofter\.com/post/[a-zA-Z0-9_-]+")


class NetworkStepsMixin:

    async def _step_01_config_rw(self) -> object:
        name = "配置读写"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            cookie = await self._db.get_config("lofter_cookie") or ""
            details.append(f"Cookie 存在，长度 {len(cookie)}")

            await self._db.set_config(self.TEST_CONFIG_KEY, "v1")
            val = await self._db.get_config(self.TEST_CONFIG_KEY)
            assert val == "v1", f"期望 v1，实际 {val}"
            details.append(f"set_config('{self.TEST_CONFIG_KEY}', 'v1') OK")
            details.append("get_config 往返值匹配")

            self._client.update_cookie(cookie)
            details.append("client.update_cookie 无异常")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_02_dwr_engine(self) -> object:
        name = "DWR 引擎"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            import dukpy  # noqa: F401
            details.append("dukpy 加载成功")

            sample_js = "dwr.engine._remoteHandleCallback('0','0',[{answer:42}]);"
            items = await execute_dwr(sample_js)
            details.append(f"样本 JS 执行结果: {items}")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_03_http_get(self) -> object:
        name = "HTTP GET"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            url = "https://www.lofter.com/"
            html = await self._client.get(url)
            details.append(f"GET {url} → {len(html)} bytes")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_04_dwr_search(self) -> object:
        name = "DWR 标签搜索"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            raw = await self._client.search_tag(self.TEST_TAG, limit=20)
            self._artifacts["raw_dwr"] = raw
            details.append(f"search_tag('{self.TEST_TAG}', limit=20) → {len(raw)} bytes")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_05_dwr_parse(self) -> object:
        name = "DWR 响应解析"
        t0 = self._timed_start()
        details: list[str] = []
        raw_dwr = self._artifacts.get("raw_dwr")
        if raw_dwr is None:
            return self._skip(name, "依赖 step 4 (raw_dwr) 未就绪")
        try:
            posts = await parse_dwr_response(raw_dwr)
            self._artifacts["tag_posts"] = posts
            details.append(f"解析出 {len(posts)} 条帖子")
            if posts:
                p = posts[0]
                details.append(f"样本 #1: title={p.title!r}, author={p.author!r}, images={len(p.images)}")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_06_blog_fetch(self) -> object:
        name = "博主主页抓取"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            url = f"https://{self.TEST_BLOG}.lofter.com"
            html = await self._client.get(url)
            self._artifacts["blog_html"] = html
            details.append(f"GET {url} → {len(html)} bytes")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_07_blog_parse(self) -> object:
        name = "博主主页解析"
        t0 = self._timed_start()
        details: list[str] = []
        blog_html = self._artifacts.get("blog_html")
        if blog_html is None:
            return self._skip(name, "依赖 step 6 (blog_html) 未就绪")
        try:
            posts = await parse_blog_posts(blog_html)
            self._artifacts["blog_posts"] = posts
            details.append(f"解析出 {len(posts)} 条帖子")
            if posts:
                details.append(f"样本 #1: title={posts[0].title!r}, url={posts[0].url}")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_08_post_parse(self) -> object:
        name = "单帖解析"
        t0 = self._timed_start()
        details: list[str] = []
        blog_posts: list[Post] | None = self._artifacts.get("blog_posts")
        if not blog_posts:
            return self._skip(name, "依赖 step 7 (blog_posts) 未就绪或为空")
        try:
            from .parser import parse_post_page
            post = blog_posts[0]
            html = await self._client.get(post.url)
            rich = await parse_post_page(html, post.url)
            self._artifacts["rich_post"] = rich
            details.append(f"URL: {post.url}")
            details.append(f"title={rich.title!r}, author={rich.author!r}, images={len(rich.images)}")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_09_auto_parse(self) -> object:
        name = "auto_parse 链路"
        t0 = self._timed_start()
        details: list[str] = []
        blog_posts: list[Post] | None = self._artifacts.get("blog_posts")
        try:
            sample_url = "https://foo.lofter.com/post/abc123"
            assert POST_PATTERN.search(sample_url), "POST_PATTERN 未命中"
            details.append(f"(a) POST_PATTERN 匹配: {sample_url!r} OK")

            if blog_posts:
                post = blog_posts[0]
                text = format_post(post)
                assert text and len(text) > 0, "format_post 返回空字符串"
                details.append(f"(b) format_post 返回 {len(text)} 字符")

            long_content = "A" * 500 + "\n\n" + "B" * 500
            chunks = _split_text(long_content)
            assert len(chunks) >= 1, "split_text 返回空"
            details.append(f"(c) _split_text 切出 {len(chunks)} 块")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_10_filter(self) -> object:
        name = "过滤链路"
        t0 = self._timed_start()
        details: list[str] = []
        tag_posts: list[Post] | None = self._artifacts.get("tag_posts")
        if tag_posts is None:
            return self._skip(name, "依赖 step 5 (tag_posts) 未就绪")
        try:
            excl = f"{self.TEST_TAG}_unlikely_excl"
            subs, excls = parse_tag_expr(f"{self.TEST_TAG} -{excl}")
            details.append(f"parse_tag_expr → subs={subs}, excls={excls}")

            rule = FilterRule(search_tags=subs, exclude_tags=excls)
            filtered = apply_filter(tag_posts, rule)
            details.append(f"apply_filter: {len(tag_posts)} → {len(filtered)} 条")
            assert len(filtered) <= len(tag_posts)
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_11_format(self) -> object:
        name = "格式化"
        t0 = self._timed_start()
        details: list[str] = []
        tag_posts: list[Post] | None = self._artifacts.get("tag_posts")
        try:
            posts = tag_posts or []
            post = posts[0] if posts else Post(post_id="x", title="测试标题", summary="摘要", author="作者", url="https://x.lofter.com/post/1")

            t1 = format_post(post)
            assert t1, "format_post 基础调用失败"
            details.append(f"format_post(post) → {len(t1)} 字符")

            t2 = format_post(post, header="【测试头部】")
            assert t2.startswith("【测试头部】"), "header 未出现"
            details.append("format_post(header=...) OK")

            t3 = format_post(post, body="自定义正文")
            assert "自定义正文" in t3, "body 未出现"
            details.append("format_post(body=...) OK")

            t4 = format_post(post, include_time=True)
            assert t4, "include_time 调用失败"
            details.append("format_post(include_time=True) OK")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)
