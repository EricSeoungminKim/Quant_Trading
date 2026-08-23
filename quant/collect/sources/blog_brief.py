"""고정 브리핑 소스 — 네이버 블로그 '침착해' 최신 글 칸(2026-08-17, 사용자 지정).

`youtube_brief.py`와 같은 자리, 같은 실패 격리 계약이다: 네이버 블로그는
`https://rss.blog.naver.com/{blogId}.xml`로 RSS 2.0을 공개한다(API 키
불필요) — `feeds.parse_feed`가 이미 표준 RSS 2.0/Atom을 다루므로 그대로
재사용한다. 블로그를 늘리거나 바꾸고 싶으면 **`BLOG_FEEDS`만 바꾸면 된다.**

RSS 확인법: `https://rss.blog.naver.com/{blogId}.xml`을 직접 열어 `<title>`이
블로거명과 일치하는지, `<item>`이 실제 최신 글을 담고 있는지 본다 — 추측 금지.
아래 1개는 2026-08-17 위 방법으로 실측 확인했다(블로그 제목·최근 글도 같이
확인함): 침착해(blogId=stock_blog, 코스피 선물 트레이딩 관점 — 단타 레전드).
"""
from __future__ import annotations

from quant.adapters.http import client
from quant.collect.sources.feeds import parse_feed

# 여기만 바꾸면 된다 — (블로그명, RSS URL, 한줄 설명).
BLOG_FEEDS: list[tuple[str, str, str]] = [
    ("침착해", "https://rss.blog.naver.com/stock_blog.xml", "코스피 선물 트레이딩 관점 — 단타 레전드"),
]


def _http_get(url: str) -> str:
    # youtube_brief.py와 같은 이유로 5s — 블로그 하나의 응답 없음이 리포트
    # 빌드 전체를 오래 붙잡지 않게 한다.
    with client(timeout=5.0) as c:
        resp = c.get(url)
        resp.raise_for_status()
    return resp.text


def fetch_blog_posts(url: str, getter=None, limit: int = 3) -> list[dict]:
    """블로그 하나의 최신 글. `[{"title","link","published"}]`.

    네트워크·파싱 어느 쪽이 실패해도 예외를 올리지 않고 빈 리스트를 돌려준다 —
    `fetch_briefs`가 블로그 단위로 실패를 격리할 수 있는 건 이 계약 덕분이다.
    """
    get = getter or _http_get
    try:
        xml = get(url)
    except Exception:  # noqa: BLE001 — 블로그 하나의 네트워크 실패가 전체를 막지 않는다
        return []
    return [
        {"title": item["title"], "link": item["link"], "published": item["published"]}
        for item in parse_feed(xml, limit)
    ]


def fetch_briefs(getter=None, limit: int = 3) -> dict[str, list[dict]]:
    """`BLOG_FEEDS` 전체를 돈다. 실패한 블로그는 그 블로그만 결과에서 빠진다
    (전 블로그 실패 시 빈 dict — 호출부가 이걸 보고 섹션 자체를 생략한다)."""
    out: dict[str, list[dict]] = {}
    for name, url, _desc in BLOG_FEEDS:
        posts = fetch_blog_posts(url, getter=getter, limit=limit)
        if posts:
            out[name] = posts
    return out
