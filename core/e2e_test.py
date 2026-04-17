from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Literal

from .client import LofterClient
from .db import LofterDB
from .dwr_engine import execute_dwr
from .dwr_parser import parse_dwr_response
from .filter import FilterRule, apply_filter, parse_tag_expr
from .formatter import format_post
from .parser import Post, parse_blog_posts
from .scheduler import SubscriptionScheduler, fetch_blog_posts, fetch_tag_posts
from .storage import SubscriptionStorage
from .utils import _split_text

POST_PATTERN = re.compile(r"[a-zA-Z0-9_-]+\.lofter\.com/post/[a-zA-Z0-9_-]+")


@dataclass
class StepResult:
    name: str
    status: Literal["pass", "fail", "skip"]
    duration_ms: int = 0
    details: list[str] = field(default_factory=list)
    error: str | None = None


class E2ETestRunner:
    TEST_SESSION = "__lofter_e2e_test__"
    TEST_TAG = "摄影"
    # TODO: 如 lofter-news 不可用，替换为任意长期活跃博主
    TEST_BLOG = "lofter-news"
    TEST_CONFIG_KEY = "__e2e_test_kv__"

    def __init__(
        self,
        db: LofterDB,
        client: LofterClient,
        storage: SubscriptionStorage,
        scheduler: SubscriptionScheduler,
        send_push: Callable[[str, str, list], Awaitable[None]],
    ):
        self._db = db
        self._client = client
        self._storage = storage
        self._scheduler = scheduler
        self._send_push = send_push
        self._artifacts: dict = {}

    async def run_all(self, real_session_id: str) -> list[StepResult]:
        results: list[StepResult] = []

        steps = [
            self._step_01_config_rw,
            self._step_02_dwr_engine,
            self._step_03_http_get,
            self._step_04_dwr_search,
            self._step_05_dwr_parse,
            self._step_06_blog_fetch,
            self._step_07_blog_parse,
            self._step_08_post_parse,
            self._step_09_auto_parse,
            self._step_10_filter,
            self._step_11_format,
            self._step_12_search_flow,
            self._step_13_subscription_crud,
            self._step_14_subtag_full,
            self._step_15_subblog_full,
        ]
        for fn in steps:
            results.append(await fn())

        results.append(await self._step_16_subtagpreview(real_session_id))
        results.append(await self._step_17_seen_sent())
        results.append(await self._step_18_scheduler_state())
        results.append(await self._step_19_manual_poll())
        results.append(await self._step_20_push_blog(real_session_id))

        cleanup = await self._cleanup()
        results.append(cleanup)
        return results

    def _timed_start(self) -> float:
        return time.perf_counter()

    def _timed_end(self, t0: float) -> int:
        return int((time.perf_counter() - t0) * 1000)

    def _pass(self, name: str, ms: int, details: list[str]) -> StepResult:
        return StepResult(name=name, status="pass", duration_ms=ms, details=details)

    def _fail(self, name: str, ms: int, e: Exception, details: list[str]) -> StepResult:
        return StepResult(name=name, status="fail", duration_ms=ms, details=details, error=str(e))

    def _skip(self, name: str, reason: str) -> StepResult:
        return StepResult(name=name, status="skip", details=[reason])

    async def _step_01_config_rw(self) -> StepResult:
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

    async def _step_02_dwr_engine(self) -> StepResult:
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

    async def _step_03_http_get(self) -> StepResult:
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

    async def _step_04_dwr_search(self) -> StepResult:
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

    async def _step_05_dwr_parse(self) -> StepResult:
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

    async def _step_06_blog_fetch(self) -> StepResult:
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

    async def _step_07_blog_parse(self) -> StepResult:
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

    async def _step_08_post_parse(self) -> StepResult:
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

    async def _step_09_auto_parse(self) -> StepResult:
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

    async def _step_10_filter(self) -> StepResult:
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

    async def _step_11_format(self) -> StepResult:
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

    async def _step_12_search_flow(self) -> StepResult:
        name = "search 流程"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            pages = await self._client.search_tag_paged(self.TEST_TAG, total=25)
            details.append(f"search_tag_paged → {len(pages)} 页")

            seen_ids: set[str] = set()
            all_posts: list[Post] = []
            for raw in pages:
                for p in await parse_dwr_response(raw):
                    all_posts.append(p)
                    seen_ids.add(p.post_id)

            details.append(f"合计 {len(all_posts)} 条，去重后 {len(seen_ids)} 个 ID")
            assert len(seen_ids) > 0, "搜索结果为空"
            assert len(seen_ids) <= len(all_posts), "去重后数量异常"
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_13_subscription_crud(self) -> StepResult:
        name = "订阅 CRUD"
        t0 = self._timed_start()
        details: list[str] = []
        s = self.TEST_SESSION
        try:
            ok = await self._storage.add(s, "tag", "TestA", "subscribe")
            assert ok, "add tag TestA 失败"
            details.append("add(tag, TestA, subscribe) OK")

            subs = await self._storage.list_by_session(s)
            assert any(x.target == "TestA" for x in subs), "list 未找到 TestA"
            details.append(f"list_by_session → {len(subs)} 条")

            sub = await self._storage.get(s, "tag", "TestA", "subscribe")
            assert sub is not None, "get 返回 None"
            details.append("get(tag, TestA) OK")

            ok = await self._storage.remove(s, "tag", "TestA", "subscribe")
            assert ok, "remove TestA 失败"
            details.append("remove(tag, TestA) OK")

            ok = await self._storage.add(s, "tag", "TestB", "exclude")
            assert ok
            ok = await self._storage.remove(s, "tag", "TestB", "exclude")
            assert ok
            details.append("add/remove exclude tag OK")

            ok = await self._storage.add(s, "blog", "TestC")
            assert ok
            ok = await self._storage.remove(s, "blog", "TestC")
            assert ok
            details.append("add/remove blog OK")

            ok = await self._storage.add(s, "tag", "TestD", "subscribe")
            assert ok
            sub_d = await self._storage.get(s, "tag", "TestD", "subscribe")
            assert sub_d is not None
            ok = await self._storage.remove_by_id(sub_d.id)
            assert ok
            details.append("remove_by_id OK")

            final = await self._storage.list_by_session(s)
            assert len(final) == 0, f"CRUD 后仍有 {len(final)} 条残留"
            details.append("最终 list 为空，CRUD 正确")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_14_subtag_full(self) -> StepResult:
        name = "subtag 完整链路"
        t0 = self._timed_start()
        details: list[str] = []
        s = self.TEST_SESSION
        try:
            excl_tag = f"{self.TEST_TAG}_unlikely_excl"
            subs, excls = parse_tag_expr(f"{self.TEST_TAG} -{excl_tag}")

            for tag in subs:
                await self._storage.add(s, "tag", tag, "subscribe")
            for tag in excls:
                await self._storage.add(s, "tag", tag, "exclude")
            details.append(f"添加订阅 {subs}，排除 {excls}")

            for tag in subs:
                posts = await fetch_tag_posts([tag], self._client)
                if posts:
                    await self._db.mark_seen_session(s, "tag", [p.post_id for p in posts])
            details.append("warmup tag 完成")

            count = await self._db.seen_count(s, "tag")
            assert count > 0, f"warmup 后 seen_count=0"
            details.append(f"seen_count(session, 'tag') = {count}")

            all_subs = await self._storage.list_by_session(s)
            has_sub = any(x.type == "tag" and x.role == "subscribe" and x.target == self.TEST_TAG for x in all_subs)
            has_excl = any(x.type == "tag" and x.role == "exclude" and x.target == excl_tag for x in all_subs)
            assert has_sub, "DB 中未找到 subscribe 记录"
            assert has_excl, "DB 中未找到 exclude 记录"
            details.append("DB 订阅记录验证 OK")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_15_subblog_full(self) -> StepResult:
        name = "subblog 完整链路"
        t0 = self._timed_start()
        details: list[str] = []
        s = self.TEST_SESSION
        try:
            from .storage import Subscription
            await self._storage.add(s, "blog", self.TEST_BLOG)
            details.append(f"add blog {self.TEST_BLOG} OK")

            sub = Subscription(id=0, session_id=s, type="blog", role="subscribe", target=self.TEST_BLOG)
            posts = await fetch_blog_posts(sub, self._client)
            if posts:
                await self._db.mark_seen_session(s, "blog", [p.post_id for p in posts])
            details.append(f"warmup blog 抓到 {len(posts)} 条")

            count = await self._db.seen_count(s, "blog")
            assert count > 0, "warmup 后 blog seen_count=0"
            details.append(f"seen_count(session, 'blog') = {count}")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_16_subtagpreview(self, real_session_id: str) -> StepResult:
        name = "subtagpreview 推送"
        t0 = self._timed_start()
        details: list[str] = []
        s = self.TEST_SESSION
        try:
            posts = await fetch_tag_posts([self.TEST_TAG], self._client)
            details.append(f"fetch_tag_posts 得 {len(posts)} 条")

            rule = FilterRule(search_tags=[self.TEST_TAG], exclude_tags=[])
            posts = apply_filter(posts, rule)
            details.append(f"apply_filter 后 {len(posts)} 条")

            await self._db.mark_seen_session(s, "tag", [p.post_id for p in posts])
            details.append(f"mark_seen_session 标记 {len(posts)} 条")

            if not posts:
                details.append("无可推送帖子，跳过推送")
                return self._pass(name, self._timed_end(t0), details)

            post = posts[0]
            header = f"【标签「{self.TEST_TAG}」有新内容】"
            text = format_post(post, header=header)
            await self._send_push(real_session_id, text, post.images)
            details.append(f"推送首条到 real_session，含 {len(post.images)} 张图")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_17_seen_sent(self) -> StepResult:
        name = "seen/sent 追踪"
        t0 = self._timed_start()
        details: list[str] = []
        s = self.TEST_SESSION
        fake_ids = ["__e2e_fake_1__", "__e2e_fake_2__"]
        try:
            await self._db.mark_seen_session(s, "tag", fake_ids)
            unseen = await self._db.filter_unseen_session(s, "tag", fake_ids)
            assert unseen == [], f"seen 后 filter_unseen 应返回空，实际 {unseen}"
            count = await self._db.seen_count(s, "tag")
            details.append(f"mark_seen + filter_unseen OK，seen_count={count}")

            await self._db.mark_sent(s, fake_ids)
            unsent = await self._db.filter_unsent(s, fake_ids)
            assert unsent == [], f"sent 后 filter_unsent 应返回空，实际 {unsent}"
            details.append("mark_sent + filter_unsent OK")

            conn = self._db._conn
            loop = __import__("asyncio").get_event_loop()
            await loop.run_in_executor(None, lambda: (
                conn.execute("DELETE FROM seen_posts WHERE session_id=? AND post_id IN (?,?)", (s, *fake_ids)),
                conn.execute("DELETE FROM sent_posts WHERE session_id=? AND post_id IN (?,?)", (s, *fake_ids)),
                conn.commit(),
            ))
            details.append("fake ids 已清理")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_18_scheduler_state(self) -> StepResult:
        name = "调度器状态"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            task = self._scheduler._task
            interval = self._scheduler._interval
            assert task is not None, "_task 为 None"
            assert not task.done(), "_task 已结束"
            assert interval > 0, f"_interval={interval} 非正数"
            details.append(f"_task 存在且运行中")
            details.append(f"_interval={interval}s ({interval // 60} 分钟)")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_19_manual_poll(self) -> StepResult:
        name = "手动轮询 _poll_all"
        t0 = self._timed_start()
        details: list[str] = []
        s = self.TEST_SESSION
        try:
            before = await self._db.seen_count(s, "tag")
            await self._scheduler._poll_all()
            after = await self._db.seen_count(s, "tag")
            details.append(f"_poll_all 完成，sent_posts 变化: before={before}, after={after}")
            details.append("无新帖（符合 warmup 后预期）" if after == before else f"新增 {after - before} 条 seen（有新帖）")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_20_push_blog(self, real_session_id: str) -> StepResult:
        name = "推送博主帖"
        t0 = self._timed_start()
        details: list[str] = []
        blog_posts: list[Post] | None = self._artifacts.get("blog_posts")
        if not blog_posts:
            return self._skip(name, "依赖 step 7 (blog_posts) 未就绪或为空")
        try:
            post = blog_posts[0]
            header = f"【博主「{self.TEST_BLOG}」有新内容】"
            text = format_post(post, header=header)
            await self._send_push(real_session_id, text, post.images)
            details.append(f"推送首条到 real_session，含 {len(post.images)} 张图")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _cleanup(self) -> StepResult:
        name = "测试清理"
        t0 = self._timed_start()
        details: list[str] = []
        s = self.TEST_SESSION
        try:
            subs = await self._storage.list_by_session(s)
            for sub in subs:
                await self._storage.remove_by_id(sub.id)
            details.append(f"删除 {len(subs)} 条订阅")

            await self._db.clear_session(s)
            details.append("seen/sent 记录已清理")

            await self._db.delete_config(self.TEST_CONFIG_KEY)
            details.append(f"配置键 {self.TEST_CONFIG_KEY} 已删除")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)


_STATUS_ICON = {"pass": "✓", "fail": "✗", "skip": "○"}


def format_report(results: list[StepResult]) -> str:
    lines = ["━━━ Lofter 端到端测试 ━━━", ""]

    total = len(results)
    for i, r in enumerate(results, 1):
        icon = _STATUS_ICON.get(r.status, "?")
        if r.status == "skip":
            lines.append(f"[{i}/{total}] {icon} {r.name}")
        else:
            lines.append(f"[{i}/{total}] {icon} {r.name} ({r.duration_ms} ms)")
        for d in r.details:
            lines.append(f"  · {d}")
        if r.error:
            lines.append(f"  · 错误：{r.error}")
        lines.append("")

    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    skipped = sum(1 for r in results if r.status == "skip")
    total_ms = sum(r.duration_ms for r in results)

    skip_refs = []
    for i, r in enumerate(results, 1):
        if r.status == "skip" and r.details:
            skip_refs.append(f"[{i}] {r.details[0]}")

    lines.append("━━━ 结果 ━━━")
    skip_note = f"，跳过 {skipped}（{'；'.join(skip_refs)}）" if skipped else ""
    lines.append(f"通过 {passed}，失败 {failed}{skip_note}")
    lines.append(f"总耗时 {total_ms / 1000:.1f}s")

    cleanup = next((r for r in results if r.name == "测试清理"), None)
    if cleanup and cleanup.status == "pass":
        lines.append("测试 session 清理完成")
    else:
        lines.append("注意：测试 session 清理可能未完成")

    return "\n".join(lines)
