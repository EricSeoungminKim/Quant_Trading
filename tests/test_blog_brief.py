from pathlib import Path

from quant.collect.sources.blog_brief import BLOG_FEEDS, fetch_blog_posts, fetch_briefs

FIXTURE = Path(__file__).parent / "report" / "fixtures" / "blog_brief_chimchakhae.xml"
_CHIMCHAKHAE_URL = "https://rss.blog.naver.com/stock_blog.xml"  # 침착해 — 실측 확인된 RSS URL


def test_blog_feeds_has_chimchakhae_verified_url():
    """추측 금지 — RSS 실측 확인된 블로그만 등록돼 있어야 한다."""
    names = {name for name, _url, _desc in BLOG_FEEDS}
    assert "침착해" in names
    urls = {url for _name, url, _desc in BLOG_FEEDS}
    assert _CHIMCHAKHAE_URL in urls


def test_fetch_blog_posts_parses_real_rss_fragment():
    """실 네이버 블로그 RSS 조각(침착해, 2026-08-17 실측 저장)을 파싱한다."""
    xml = FIXTURE.read_text(encoding="utf-8")
    posts = fetch_blog_posts(_CHIMCHAKHAE_URL, getter=lambda url: xml, limit=3)
    assert len(posts) == 3
    assert posts[0]["title"] == "코스피 선물 1100억 트레이딩 관점"
    assert posts[0]["link"] == (
        "https://blog.naver.com/stock_blog/224261045776?fromRss=true&trackingCode=rss"
    )
    assert posts[0]["published"] == "Wed, 22 Apr 2026 11:47:53 +0900"


def test_fetch_blog_posts_respects_limit():
    xml = FIXTURE.read_text(encoding="utf-8")
    posts = fetch_blog_posts(_CHIMCHAKHAE_URL, getter=lambda url: xml, limit=1)
    assert len(posts) == 1


def test_fetch_blog_posts_passes_url_to_getter():
    seen = {}

    def getter(url):
        seen["url"] = url
        return FIXTURE.read_text(encoding="utf-8")

    fetch_blog_posts(_CHIMCHAKHAE_URL, getter=getter)
    assert seen["url"] == _CHIMCHAKHAE_URL


def test_fetch_blog_posts_network_failure_returns_empty_list():
    """블로그 하나가 죽어도(네트워크 실패) 예외 없이 빈 리스트 — 그 블로그만 생략."""
    def boom(url):
        raise ConnectionError("boom")

    assert fetch_blog_posts("https://rss.blog.naver.com/dead_blog.xml", getter=boom) == []


def test_fetch_blog_posts_broken_xml_returns_empty_list():
    assert fetch_blog_posts(_CHIMCHAKHAE_URL, getter=lambda url: "<not xml", limit=3) == []


def test_fetch_briefs_returns_posts_for_registered_blog():
    xml = FIXTURE.read_text(encoding="utf-8")
    result = fetch_briefs(getter=lambda url: xml)
    assert "침착해" in result
    assert len(result["침착해"]) == 3


def test_fetch_briefs_returns_empty_dict_when_all_blogs_fail():
    """전 블로그 실패면 섹션 자체를 생략할 수 있도록 빈 dict(빈 섹션 아님)를 돌려준다."""
    def boom(url):
        raise ConnectionError("boom")

    assert fetch_briefs(getter=boom) == {}
