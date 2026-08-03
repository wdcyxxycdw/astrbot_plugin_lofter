from __future__ import annotations

from .content_source import collect_pages
from .filter import FilterRule, apply_filter, parse_tag_expr
from .parser import Post
from .scheduler import fetch_blog_posts, fetch_tag_posts


class FlowStepsMixin:

    async def _step_12_search_flow(self) -> object:
        name = "search 流程"
        t0 = self._timed_start()
        details: list[str] = []
        try:
            page = await collect_pages(
                lambda cursor: self._source.list_tag(
                    self.TEST_TAG, cursor, 25, "new"
                ),
                limit=25,
            )
            posts = page.items
            details.append(f"source={page.source}，合计 {len(posts)} 条")
            assert posts, "搜索结果为空"
            assert len({post.post_id for post in posts}) == len(posts), "搜索结果未去重"
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_13_subscription_crud(self) -> object:
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

    async def _step_14_subtag_full(self) -> object:
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
                posts = await fetch_tag_posts([tag], self._source)
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

    async def _step_15_subblog_full(self) -> object:
        name = "subblog 完整链路"
        t0 = self._timed_start()
        details: list[str] = []
        s = self.TEST_SESSION
        try:
            from .storage import Subscription
            await self._storage.add(s, "blog", self.TEST_BLOG)
            details.append(f"add blog {self.TEST_BLOG} OK")

            sub = Subscription(id=0, session_id=s, type="blog", role="subscribe", target=self.TEST_BLOG)
            posts = await fetch_blog_posts(sub, self._source)
            if posts:
                await self._db.mark_seen_session(s, "blog", [p.post_id for p in posts])
            details.append(f"warmup blog 抓到 {len(posts)} 条")

            count = await self._db.seen_count(s, "blog")
            assert count > 0, "warmup 后 blog seen_count=0"
            details.append(f"seen_count(session, 'blog') = {count}")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_16_subtagpreview(self, real_session_id: str) -> object:
        name = "subtagpreview 推送"
        t0 = self._timed_start()
        details: list[str] = []
        s = self.TEST_SESSION
        try:
            posts = await fetch_tag_posts([self.TEST_TAG], self._source)
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
            await self._send_push(
                real_session_id, post, header, frozenset({"tag"})
            )
            details.append(f"推送首条到 real_session，含 {len(post.images)} 张图")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_17_seen_sent(self) -> object:
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

            await self._db.transaction(lambda conn: (
                conn.execute(
                    "DELETE FROM seen_posts WHERE subscription_id IN "
                    "(SELECT id FROM subscriptions WHERE session_id=?) AND post_id IN (?,?)",
                    (s, *fake_ids),
                ),
                conn.execute(
                    "DELETE FROM deliveries WHERE session_id=? AND post_id IN (?,?)",
                    (s, *fake_ids),
                ),
            ))
            details.append("fake ids 已清理")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_18_scheduler_state(self) -> object:
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

    async def _step_19_manual_poll(self) -> object:
        name = "手动轮询 _poll_all"
        t0 = self._timed_start()
        details: list[str] = []
        s = self.TEST_SESSION
        try:
            before = await self._db.seen_count(s, "tag")
            await self._scheduler._poll_all()
            after = await self._db.seen_count(s, "tag")
            details.append(f"_poll_all 完成，seen 变化: before={before}, after={after}")
            details.append("无新帖（符合 warmup 后预期）" if after == before else f"新增 {after - before} 条 seen（有新帖）")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)

    async def _step_20_push_blog(self, real_session_id: str) -> object:
        name = "推送博主帖"
        t0 = self._timed_start()
        details: list[str] = []
        blog_posts: list[Post] | None = self._artifacts.get("blog_posts")
        if not blog_posts:
            return self._skip(name, "依赖 step 7 (blog_posts) 未就绪或为空")
        try:
            post = blog_posts[0]
            header = f"【博主「{self.TEST_BLOG}」有新内容】"
            await self._send_push(
                real_session_id, post, header, frozenset({"blog"})
            )
            details.append(f"推送首条到 real_session，含 {len(post.images)} 张图")
            return self._pass(name, self._timed_end(t0), details)
        except Exception as e:
            return self._fail(name, self._timed_end(t0), e, details)
