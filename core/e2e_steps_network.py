from __future__ import annotations

import re

from .dwr_engine import execute_dwr
from .filter import FilterRule, apply_filter, parse_tag_expr
from .formatter import format_post, visible_images
from .parser import Post
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

            self._source.update_cookie(cookie)
            details.append("source.update_cookie 无异常")
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
        name = "ContentSource 单帖"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            page = await self._source.list_blog(self.TEST_BLOG, None, 1)
            if not page.items:
                return self._skip(name, "博主列表为空，无法选择单帖")
            post = await self._source.get_post(page.items[0].url)
            self._artifacts["rich_post"] = post
            details.append(f"get_post → {post.post_id}")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_04_dwr_search(self) -> object:
        name = "ContentSource 标签页"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            page = await self._source.list_tag(self.TEST_TAG, None, 20, "new")
            self._artifacts["tag_posts"] = page.items
            details.append(
                f"source={page.source}，映射 {page.mapped_count}，丢弃 {page.dropped_count}"
            )
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_05_dwr_parse(self) -> object:
        name = "ContentSource 标签结果"
        posts: list[Post] | None = self._artifacts.get("tag_posts")
        if posts is None:
            return self._skip(name, "依赖 ContentSource 标签页未就绪")
        return self._pass(name, 0, [f"得到 {len(posts)} 条帖子"])

    async def _step_06_blog_fetch(self) -> object:
        name = "ContentSource 博主页"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            page = await self._source.list_blog(self.TEST_BLOG, None, 20)
            self._artifacts["blog_posts"] = page.items
            details.append(f"source={page.source}，得到 {len(page.items)} 条帖子")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_07_blog_parse(self) -> object:
        name = "ContentSource 博主结果"
        posts: list[Post] | None = self._artifacts.get("blog_posts")
        if posts is None:
            return self._skip(name, "依赖 ContentSource 博主页未就绪")
        return self._pass(name, 0, [f"得到 {len(posts)} 条帖子"])

    async def _step_08_post_parse(self) -> object:
        name = "ContentSource 帖子结果"
        rich: Post | None = self._artifacts.get("rich_post")
        if rich is None:
            return self._skip(name, "依赖 ContentSource 单帖未就绪")
        image_count = (
            str(len(visible_images(rich)))
            if rich.has_fields({"images"})
            else "unknown"
        )
        details = [
            f"title={rich.title!r}, author={rich.author!r}, images={image_count}"
        ]
        return self._pass(name, 0, details)

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

            body_post = (
                post
                if post.has_fields({"content"})
                else Post(
                    post_id="x", title="测试标题", summary="",
                    content="完整正文", url="https://x.lofter.com/post/1",
                )
            )
            t3 = format_post(body_post, body="自定义正文")
            assert "自定义正文" in t3, "body 未出现"
            details.append("format_post(body=...) OK")

            t4 = format_post(post, include_time=True)
            assert t4, "include_time 调用失败"
            details.append("format_post(include_time=True) OK")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)
