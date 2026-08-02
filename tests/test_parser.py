import pytest
from core.errors import SourceChallengeError, SourceLimitError, SourceSchemaError
from core.parser import extract_lofter_username, parse_post_page, parse_blog_posts
from core.source_limits import MAX_TITLE_BYTES, MAX_URL_BYTES


def _exact_utf8(prefix: str, limit: int) -> str:
    remaining = limit - len(prefix.encode("utf-8"))
    return prefix + "界" * (remaining // 3) + "x" * (remaining % 3)

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
<!DOCTYPE html><html><head>
<link rel="canonical" href="https://user.lofter.com/"/>
</head><body>
<a href="https://user.lofter.com/post/aaa_111">帖子一</a>
<a href="https://user.lofter.com/post/bbb_222">帖子二</a>
<a href="https://user.lofter.com/post/aaa_111">重复链接</a>
<a href="https://user.lofter.com/about">非帖子链接</a>
</body></html>"""

BLOG_HOME_EMPTY_HTML = "<html><body><p>没有帖子</p></body></html>"

POST_URL = "https://test.lofter.com/post/abc_123def"
POST_EVIDENCE = f'<link rel="canonical" href="{POST_URL}">'


def with_post_evidence(html):
    return html.replace("<head>", f"<head>{POST_EVIDENCE}", 1)


for _fixture_name in (
    "TEXT_POST_HTML", "IMAGE_POST_HTML", "MULTI_DASH_HTML", "NO_DASH_HTML",
    "EMPTY_HTML", "LONG_DESC_HTML", "HTML_ENTITY_HTML", "DEDUP_IMAGE_HTML",
    "P_ID_WITH_FULL_POST_HTML",
):
    if _fixture_name in globals():
        globals()[_fixture_name] = with_post_evidence(globals()[_fixture_name])


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://someuser.lofter.com/post/abc", "someuser"),
        ("https://lofter.com/post/abc", ""),
        ("https://www.lofter.com/post/abc", ""),
        ("https://someuser.lofter.com.evil.com/post/abc", ""),
        ("https://evil.com/redirect?u=https://someuser.lofter.com/post/abc", ""),
        ("not a url https://someuser.lofter.com/post/abc", ""),
    ],
)
def test_extract_lofter_username_requires_strict_lofter_hostname(url, expected):
    assert extract_lofter_username(url) == expected


@pytest.mark.asyncio
async def test_text_post_title_and_author():
    post = await parse_post_page(TEXT_POST_HTML, POST_URL)
    assert post.title == "二阶堂希罗做梦也没想到魔女岛的遗留魔法居然这么强劲有力"
    assert post.author == "总有人叫我老白"


@pytest.mark.asyncio
async def test_text_post_summary_and_tags():
    post = await parse_post_page(TEXT_POST_HTML, POST_URL)
    assert "自主规制" in post.summary
    assert "summary" in post.completeness
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
    assert "summary" not in post.completeness
    assert "author" not in post.completeness
    assert "tags" not in post.completeness


@pytest.mark.asyncio
async def test_silent_condition_when_no_summary_no_images():
    post = await parse_post_page(EMPTY_HTML, POST_URL)
    assert not post.summary
    assert not post.images


@pytest.mark.asyncio
async def test_post_id_extracted_from_url():
    url = "https://user.lofter.com/post/aaf97d58_34d8bede3"
    html = TEXT_POST_HTML.replace(POST_URL, url)
    post = await parse_post_page(html, url)
    assert post.post_id == "aaf97d58_34d8bede3"


@pytest.mark.asyncio
async def test_parse_post_page_extracts_author_username_from_url():
    url = "https://SomeUser.lofter.com/post/abc123"
    html = TEXT_POST_HTML.replace(POST_URL, url)
    post = await parse_post_page(html, url)
    assert post.author_username == "someuser"


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
async def test_parse_blog_posts_extracts_author_username():
    html = """<html><head><link rel="canonical" href="https://SomeUser.lofter.com/"/></head>
    <body><a href="https://SomeUser.lofter.com/post/abc123">标题</a></body></html>"""
    posts = await parse_blog_posts(html)
    assert posts[0].author_username == "someuser"


@pytest.mark.asyncio
async def test_blog_relative_post_link_uses_validated_canonical_host():
    html = """
    <link rel="canonical" href="https://SomeUser.lofter.com/">
    <a href="/post/abc_123">相对链接帖子</a>
    """

    posts = await parse_blog_posts(html)

    assert len(posts) == 1
    assert posts[0].post_id == "abc_123"
    assert posts[0].url == "https://someuser.lofter.com/post/abc_123"
    assert posts[0].author_username == "someuser"


@pytest.mark.asyncio
async def test_blog_relative_post_uses_declared_identity_without_canonical():
    html = '<div data-blog-name="demo"><a href="/post/abc_123">帖子</a></div>'

    posts = await parse_blog_posts(html)

    assert posts[0].url == "https://demo.lofter.com/post/abc_123"
    assert posts[0].author_username == "demo"


@pytest.mark.asyncio
async def test_blog_posts_empty_page():
    posts = await parse_blog_posts(BLOG_HOME_EMPTY_HTML)
    assert posts == []


# ── _extract_body_text ────────────────────────────────────────────────────────

P_ID_HTML = """\
<!DOCTYPE html><html><body>
<div class="txtcont">
<p id="p_abc123">第一段正文内容，这是一段比较长的文字。</p>
<p id="p_def456">第二段正文内容，继续写一些内容。</p>
<p id="p_ghi789">第三段正文内容，结尾部分。</p>
</div>
</body></html>"""

TXTCONT_FALLBACK_HTML = """\
<!DOCTYPE html><html><body>
<div class="txtcont">这是一段超过一百字的正文内容，用来测试回退到 txtcont 选择器的情况，需要确保文本足够长才能触发提取逻辑，所以这里继续添加一些内容直到超过一百个字符为止。</div>
</body></html>"""

NO_CONTENT_HTML = """\
<!DOCTYPE html><html><head><title>t-a</title></head><body>
<div class="sidebar">短文本</div>
</body></html>"""

P_ID_WITH_FULL_POST_HTML = """\
<!DOCTYPE html><html><head>
<link rel="canonical" href="https://test.lofter.com/post/abc_123def">
<title>测试标题-测试作者</title>
<meta name="Description" content="摘要"/>
</head><body>
<div class="txtcont">
<p id="p_aaa">正文第一段，内容足够长以便测试提取功能是否正常工作。</p>
<p id="p_bbb">正文第二段，更多内容。</p>
</div>
</body></html>"""


@pytest.mark.asyncio
async def test_body_text_extracted_from_p_id():
    from core.parser import _extract_body_text, _make_soup
    soup = _make_soup(P_ID_HTML)
    content = _extract_body_text(soup)
    assert "第一段正文内容" in content
    assert "第二段正文内容" in content
    assert "第三段正文内容" in content


@pytest.mark.asyncio
async def test_body_text_fallback_to_txtcont():
    from core.parser import _extract_body_text, _make_soup
    soup = _make_soup(TXTCONT_FALLBACK_HTML)
    content = _extract_body_text(soup)
    assert "超过一百字的正文内容" in content


@pytest.mark.asyncio
async def test_body_text_empty_when_no_content():
    from core.parser import _extract_body_text, _make_soup
    soup = _make_soup(NO_CONTENT_HTML)
    content = _extract_body_text(soup)
    assert content == ""


@pytest.mark.asyncio
async def test_post_content_field_populated():
    post = await parse_post_page(P_ID_WITH_FULL_POST_HTML, POST_URL)
    assert "正文第一段" in post.content
    assert "正文第二段" in post.content


@pytest.mark.asyncio
async def test_blog_shell_is_schema_failure():
    with pytest.raises(SourceSchemaError):
        await parse_blog_posts("<html><body><p>欢迎访问</p></body></html>")


@pytest.mark.asyncio
async def test_blog_post_link_is_identity_evidence():
    html = '<a href="https://synthetic.lofter.com/post/abc_123">帖子</a>'

    posts = await parse_blog_posts(html)

    assert len(posts) == 1
    assert posts[0].post_id == "abc_123"
    assert posts[0].author_username == "synthetic"


@pytest.mark.asyncio
async def test_blog_identity_allows_empty_posts():
    html = '<link rel="canonical" href="https://synthetic.lofter.com/">'
    assert await parse_blog_posts(html) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("parser_kind", ["blog", "post"])
async def test_login_page_is_typed_challenge(parser_kind):
    html = '<html><head><title>登录 - LOFTER</title></head><body><input type="password"></body></html>'
    with pytest.raises(SourceChallengeError):
        if parser_kind == "blog":
            await parse_blog_posts(html)
        else:
            await parse_post_page(html, POST_URL)


@pytest.mark.asyncio
async def test_post_requires_page_evidence():
    html = TEXT_POST_HTML.replace(POST_EVIDENCE, "")
    with pytest.raises(SourceSchemaError, match="html"):
        await parse_post_page(html, POST_URL)


@pytest.mark.asyncio
async def test_post_with_evidence_requires_usable_field():
    html = '<html><head><link rel="canonical" href="https://test.lofter.com/post/abc_123def"></head></html>'
    with pytest.raises(SourceSchemaError, match="post.content"):
        await parse_post_page(html, POST_URL)


@pytest.mark.asyncio
async def test_post_page_rejects_canonical_identity_mismatch():
    html = TEXT_POST_HTML.replace(POST_URL, "https://other.lofter.com/post/def_456")
    with pytest.raises(SourceSchemaError, match="post.id"):
        await parse_post_page(html, POST_URL)


@pytest.mark.asyncio
async def test_blog_page_item_limit_is_exact():
    anchors = "".join(
        f'<a href="https://user.lofter.com/post/{index:x}_1">p</a>'
        for index in range(100)
    )
    html = '<link rel="canonical" href="https://user.lofter.com/">' + anchors
    assert len(await parse_blog_posts(html)) == 100
    extra = '<a href="https://user.lofter.com/post/64_1">p</a>'
    with pytest.raises(SourceLimitError) as exc_info:
        await parse_blog_posts(html + extra)
    assert (exc_info.value.resource, exc_info.value.limit) == ("items", 100)


@pytest.mark.asyncio
async def test_post_parser_accepts_exact_utf8_title_and_url_limits():
    prefix = "https://test.lofter.com/post/"
    exact_url = prefix + "a" * (MAX_URL_BYTES - len(prefix))
    exact_title = _exact_utf8("", MAX_TITLE_BYTES)
    html = (
        f'<link rel="canonical" href="{exact_url}">'
        f"<title>{exact_title}</title>"
    )
    post = await parse_post_page(html, exact_url)
    assert post.title == exact_title
    assert post.url == exact_url


@pytest.mark.asyncio
async def test_post_parser_checks_full_image_url_before_query_strip():
    prefix = "https://imglf5.lf127.net/img/a.jpg?quality="
    exact_image = _exact_utf8(prefix, MAX_URL_BYTES)
    html = (
        f'{POST_EVIDENCE}<title>帖子</title><img src="{exact_image}">'
    )
    post = await parse_post_page(html, POST_URL)
    assert post.images == ["https://imglf5.lf127.net/img/a.jpg"]

    oversized = html.replace(exact_image, exact_image + "x")
    with pytest.raises(SourceLimitError) as exc_info:
        await parse_post_page(oversized, POST_URL)
    assert (exc_info.value.resource, exc_info.value.limit) == (
        "url",
        MAX_URL_BYTES,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("html", "url", "resource"),
    [
        (f"{POST_EVIDENCE}<article><title>{'题' * 4097}</title></article>", POST_URL, "title"),
        (f"{POST_EVIDENCE}<article><p id='p_x'>{'文' * (2 * 1024 * 1024 + 1)}</p></article>", POST_URL, "content"),
        ("<article><title>帖子</title></article>", "https://test.lofter.com/post/" + "a" * 8192, "url"),
    ],
)
async def test_post_field_limits_are_typed(html, url, resource):
    with pytest.raises(SourceLimitError) as exc_info:
        await parse_post_page(html, url)
    assert exc_info.value.resource == resource
