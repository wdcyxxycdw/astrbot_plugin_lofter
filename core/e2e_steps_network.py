from __future__ import annotations

import re

from lofter import Post, decode_permalink

from .filter import FilterRule, apply_filter, parse_tag_expr
from .formatter import format_post
from .utils import _split_text

POST_PATTERN = re.compile(r"[a-zA-Z0-9_-]+\.lofter\.com/post/[a-zA-Z0-9_-]+")


class NetworkStepsMixin:

    async def _step_01_config_rw(self) -> object:
        name = "配置读写"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            cookie = await self._db.get_config("lofter_cookie") or ""
            details.append(f"Cookie 长度 {len(cookie)}（遗留配置，留空不影响抓取）")

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

    async def _step_02_permalink_decode(self) -> object:
        name = "帖子 ID 解码"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            blog_id, post_id = decode_permalink("1d038690_34f54df9c")
            assert (blog_id, post_id) == (486770320, 14215864220), f"解码结果异常：{blog_id}, {post_id}"
            details.append(f"样本 permalink 解码结果: blogId={blog_id}, postId={post_id}")
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

    async def _step_04_tag_fetch(self) -> object:
        name = "标签抓取"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            posts = await self._client.fetch_tag_posts(self.TEST_TAG)
            self._artifacts["tag_posts"] = posts
            details.append(f"fetch_tag_posts('{self.TEST_TAG}') → {len(posts)} 条")
            if posts:
                p = posts[0]
                details.append(f"样本 #1: title={p.title!r}, author={p.author!r}, images={len(p.images)}")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_05_tag_detail(self) -> object:
        name = "标签详情字段"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            items = await self._client.fetch_tag_posts_detailed(self.TEST_TAG)
            details.append(f"fetch_tag_posts_detailed('{self.TEST_TAG}') → {len(items)} 条")
            if items:
                d = items[0]
                details.append(f"样本 #1: type={d.post_type}, 字数={d.word_count}, 置顶={d.is_top}")
                details.append(f"互动: 回复={d.stats.responses}, 推荐={d.stats.favorites}, 浏览={d.stats.views}")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_06_blog_fetch(self) -> object:
        name = "博主作品抓取"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            posts = await self._client.fetch_blog_posts(self.TEST_BLOG)
            self._artifacts["blog_posts"] = posts
            details.append(f"fetch_blog_posts('{self.TEST_BLOG}') → {len(posts)} 条")
            if posts:
                details.append(f"样本 #1: title={posts[0].title!r}, url={posts[0].url}")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_07_blog_content(self) -> object:
        name = "博主作品正文完整性"
        t0 = self._timed_start()
        details: list[str] = []
        blog_posts: list[Post] | None = self._artifacts.get("blog_posts")
        if not blog_posts:
            return self._skip(name, "依赖 step 6 (blog_posts) 未就绪或为空")
        try:
            filled = [p for p in blog_posts if p.content or p.images or p.summary]
            details.append(f"{len(filled)}/{len(blog_posts)} 条在列表接口就带有正文或图片")
            assert filled, "博主作品列表没有任何正文/图片，接口可能已变化"
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_08_post_fetch(self) -> object:
        name = "单帖抓取"
        t0 = self._timed_start()
        details: list[str] = []
        blog_posts: list[Post] | None = self._artifacts.get("blog_posts")
        if not blog_posts:
            return self._skip(name, "依赖 step 6 (blog_posts) 未就绪或为空")
        try:
            post = blog_posts[0]
            rich = await self._client.fetch_post(post.url)
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
            return self._skip(name, "依赖 step 4 (tag_posts) 未就绪")
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
