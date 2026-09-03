"""뉴스 소스 유니언(누적 저장소 ∪ 실시간, 2026-09-03).

핵심 회귀: `build_sources()`의 "news" 소스가 실시간 RSS 만 읽으면, 30분마다
도는 수집기(`news-collect@`)가 쌓은 하루치 누적분의 극히 일부만 리포트에
실린다 — 2026-09-02 EC2 실측으로 KR 21%(970/4,642건), US 26%(318/1,246건)만
봤다. `_fetch_news_merged`가 저장소 창(`collector.load_window`)과 실시간 한
번(`feeds.fetch_news`)을 유니언해 이 손실을 막는다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from quant.collect.sources import _fetch_news_merged, _merge_news_feeds

UTC = timezone.utc


def _art(title, link, published="2026-09-02T00:00:00+00:00", outlet=""):
    return {"title": title, "link": link, "published": published, "outlet": outlet}


# --- _merge_news_feeds: 순수 병합 로직 ---

def test_merge_unions_distinct_articles_across_feeds():
    from_store = {"A": [_art("저장1", "https://x.com/1")]}
    live = {"A": [_art("실시간1", "https://x.com/2")], "B": [_art("실시간2", "https://x.com/3")]}
    merged = _merge_news_feeds(from_store, live)
    links = {it["link"] for items in merged.values() for it in items}
    assert links == {"https://x.com/1", "https://x.com/2", "https://x.com/3"}


def test_merge_dedupes_same_article_by_normalized_link():
    from_store = {"A": [_art("제목(저장)", "https://x.com/1?utm_source=rss")]}
    live = {"A": [_art("제목(실시간)", "https://x.com/1")]}
    merged = _merge_news_feeds(from_store, live)
    assert len(merged["A"]) == 1


def test_merge_prefers_live_on_duplicate_link():
    """같은 기사가 양쪽에 있으면 실시간이 우선한다 — 더 최신 파싱 결과."""
    from_store = {"A": [_art("저장 버전", "https://x.com/1")]}
    live = {"A": [_art("실시간 버전", "https://x.com/1")]}
    merged = _merge_news_feeds(from_store, live)
    assert merged["A"][0]["title"] == "실시간 버전"


def test_merge_with_empty_store_returns_live_only():
    live = {"A": [_art("a", "https://x.com/1")]}
    merged = _merge_news_feeds({}, live)
    assert merged == live


def test_merge_with_empty_live_returns_store_only():
    store = {"A": [_art("a", "https://x.com/1")]}
    merged = _merge_news_feeds(store, {})
    assert merged["A"] == store["A"]


# --- _fetch_news_merged: 저장소 유무 분기 + 카운트 ---

def test_fetch_news_merged_unions_store_and_live(tmp_path, monkeypatch):
    from quant.collect import collector

    store_dir = tmp_path / "data" / "news" / "KR"
    store_dir.mkdir(parents=True)

    monkeypatch.setattr(
        collector, "load_window",
        lambda root, market, since, until=None: {"A": [_art("저장분", "https://x.com/1")]},
    )
    from quant.collect.sources import feeds

    monkeypatch.setattr(
        feeds, "fetch_news",
        lambda market, since=None: {
            "feeds": {"A": [_art("실시간분", "https://x.com/2")]},
            "window_start": since.isoformat() if since else None,
            "fetched": 1, "kept": 1, "undated": 0, "empty_feeds": [],
        },
    )

    result = _fetch_news_merged("KR", datetime(2026, 9, 2, tzinfo=UTC), tmp_path)

    titles = {it["title"] for items in result["feeds"].values() for it in items}
    assert titles == {"저장분", "실시간분"}
    assert result["from_store"] == 1
    assert result["from_live"] == 1
    assert result["kept"] == 2


def test_fetch_news_merged_missing_store_dir_falls_back_to_live_only(tmp_path, monkeypatch, caplog):
    """저장소 디렉터리가 없으면(첫 배포 등) WARNING 만 남기고 실시간만 쓴다 —
    유니언 도입 전 기존 동작이라 회귀가 아니다."""
    from quant.collect.sources import feeds

    monkeypatch.setattr(
        feeds, "fetch_news",
        lambda market, since=None: {
            "feeds": {"A": [_art("실시간분", "https://x.com/2")]},
            "window_start": None, "fetched": 1, "kept": 1, "undated": 0, "empty_feeds": [],
        },
    )

    with caplog.at_level("WARNING", logger="quant.collect.sources"):
        result = _fetch_news_merged("KR", None, tmp_path)  # tmp_path/data/news/KR 없음

    assert result["from_store"] == 0
    assert result["from_live"] == 1
    titles = {it["title"] for items in result["feeds"].values() for it in items}
    assert titles == {"실시간분"}
    assert any("저장소 없음" in r.message for r in caplog.records)


def test_build_sources_news_source_returns_merge_result(tmp_path, monkeypatch):
    """`build_sources()`가 만드는 "news" 소스가 실제로 유니언 함수를 탄다."""
    from quant.collect.sources import build_sources

    def fake_merge(market_code, news_since, root):
        assert root == tmp_path
        return {"feeds": {}, "from_store": 3, "from_live": 5, "kept": 8, "window_start": None}

    import quant.collect.sources as sources_mod
    monkeypatch.setattr(sources_mod, "_fetch_news_merged", fake_merge)

    from datetime import date

    sources = build_sources("KR", date(2026, 9, 2), news_since=None, root=tmp_path)
    _, news_fn = sources["news"]
    data = news_fn()
    assert data == {"feeds": {}, "from_store": 3, "from_live": 5, "kept": 8, "window_start": None}
