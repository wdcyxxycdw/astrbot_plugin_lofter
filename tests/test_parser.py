import pytest
from core.parser import parse_post_page, parse_blog_posts

# ── Fixtures ──────────────────────────────────────────────────────────────────

TEXT_POST_HTML = """\
<!DOCTYPE html><html><head>
<title>二阶堂希罗做梦也没想到魔女岛的遗留魔法居然这么强劲有力-总有人叫我老白</title>
<meta name="Keywords" content="魔法少女的魔女审判,エマヒロ,希艾"/>
<meta name="Description" content="全文2w4，这里被自主规制了约1w5。"/>
</head><body></body></html>"""

IMAGE_POST_HTML = """\
<!DOCTYPE html><html><head>
<title>猫猫，需要更多猫猫-粉红咕咕头</title>
<meta name="Keywords" content="魔法少女的魔女审判,ヒロエマ"/>
<meta name="Description" content="稿件，转载请注明"/>
</head><body>
<img src="https://imglf5.lf127.net/img/07b9d0e46c6c766b/abc.png?imageView&amp;thumbnail=1680x0&amp;quality=96&amp;stripmeta=0"/>
<img src="https://imglf3.lf127.net/img/4bd6a3f68b634fe6/def.jpg?imageView&amp;thumbnail=1680x0&amp;quality=96&amp;stripmeta=0"/>
</body></html>"""

MULTI_DASH_HTML = """\
<!DOCTYPE html><html><head>
<title>标题-含有-连字符-作者名</title>
</head><body></body></html>"""

NO_DASH_HTML = """\
<!DOCTYPE html><html><head>
<title>纯标题无作者</title>
</head><body></body></html>"""

EMPTY_HTML = """\
<!DOCTYPE html><html><head>
<title>某标题-某作者</title>
</head><body></body></html>"""

LONG_DESC_HTML = """\
<!DOCTYPE html><html><head>
<title>标题-作者</title>
<meta name="Description" content="{desc}"/>
</head><body></body></html>""".format(desc="字" * 350)

HTML_ENTITY_HTML = """\
<!DOCTYPE html><html><head>
<title>标题-作者</title>
<meta name="Description" content="a &amp; b &lt;c&gt;"/>
</head><body></body></html>"""

DEDUP_IMAGE_HTML = """\
<!DOCTYPE html><html><head><title>t-a</title></head><body>
<img src="https://imglf5.lf127.net/img/abc/foo.jpg?imageView&quality=96"/>
<img src="https://imglf5.lf127.net/img/abc/foo.jpg?imageView&quality=96"/>
<img src="https://imglf5.lf127.net/img/abc/bar.png?imageView&quality=96"/>
</body></html>"""

BLOG_HOME_HTML = """\
<!DOCTYPE html><html><body>
<a href="https://user.lofter.com/post/aaa_111">帖子一</a>
<a href="https://user.lofter.com/post/bbb_222">帖子二</a>
<a href="https://user.lofter.com/post/aaa_111">重复链接</a>
<a href="https://user.lofter.com/about">非帖子链接</a>
</body></html>"""

BLOG_HOME_EMPTY_HTML = "<html><body><p>没有帖子</p></body></html>"

POST_URL = "https://test.lofter.com/post/abc_123def"


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_text_post_title_and_author():
    post = await parse_post_page(TEXT_POST_HTML, POST_URL)
    assert post.title == "二阶堂希罗做梦也没想到魔女岛的遗留魔法居然这么强劲有力"
    assert post.author == "总有人叫我老白"


@pytest.mark.asyncio
async def test_text_post_summary_and_tags():
    post = await parse_post_page(TEXT_POST_HTML, POST_URL)
    assert "自主规制" in post.summary
    assert "エマヒロ" in post.tags
    assert "希艾" in post.tags
    assert post.images == []


@pytest.mark.asyncio
async def test_image_post_images_stripped():
    post = await parse_post_page(IMAGE_POST_HTML, POST_URL)
    assert len(post.images) == 2
    for img in post.images:
        assert "?" not in img  # query params stripped


@pytest.mark.asyncio
async def test_title_with_multiple_dashes():
    post = await parse_post_page(MULTI_DASH_HTML, POST_URL)
    assert post.title == "标题-含有-连字符"
    assert post.author == "作者名"


@pytest.mark.asyncio
async def test_title_without_dash():
    post = await parse_post_page(NO_DASH_HTML, POST_URL)
    assert post.title == "纯标题无作者"
    assert post.author == ""


@pytest.mark.asyncio
async def test_silent_condition_when_no_summary_no_images():
    post = await parse_post_page(EMPTY_HTML, POST_URL)
    assert not post.summary
    assert not post.images


@pytest.mark.asyncio
async def test_post_id_extracted_from_url():
    post = await parse_post_page(TEXT_POST_HTML, "https://user.lofter.com/post/aaf97d58_34d8bede3")
    assert post.post_id == "aaf97d58_34d8bede3"


@pytest.mark.asyncio
async def test_summary_truncated_at_300_chars():
    post = await parse_post_page(LONG_DESC_HTML, POST_URL)
    assert len(post.summary) == 301  # 300 chars + "…"
    assert post.summary.endswith("…")


@pytest.mark.asyncio
async def test_summary_html_entities_unescaped():
    post = await parse_post_page(HTML_ENTITY_HTML, POST_URL)
    assert post.summary == "a & b <c>"


@pytest.mark.asyncio
async def test_images_deduplicated():
    post = await parse_post_page(DEDUP_IMAGE_HTML, POST_URL)
    assert len(post.images) == 2
    assert len(set(post.images)) == 2  # 无重复


# ── parse_blog_posts ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_blog_posts_extracted():
    posts = await parse_blog_posts(BLOG_HOME_HTML)
    assert len(posts) == 2
    ids = [p.post_id for p in posts]
    assert "aaa_111" in ids
    assert "bbb_222" in ids


@pytest.mark.asyncio
async def test_blog_posts_deduplicated():
    posts = await parse_blog_posts(BLOG_HOME_HTML)
    assert len(posts) == len({p.post_id for p in posts})


@pytest.mark.asyncio
async def test_blog_posts_have_url():
    posts = await parse_blog_posts(BLOG_HOME_HTML)
    for p in posts:
        assert p.url.startswith("https://")


@pytest.mark.asyncio
async def test_blog_posts_empty_page():
    posts = await parse_blog_posts(BLOG_HOME_EMPTY_HTML)
    assert posts == []
