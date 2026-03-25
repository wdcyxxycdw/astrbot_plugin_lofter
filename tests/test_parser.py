import pytest
from core.parser import parse_post_page

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
