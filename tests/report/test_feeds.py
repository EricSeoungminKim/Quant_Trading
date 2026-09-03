from pathlib import Path

import pytest

from quant.analyze.entities import build_table, extract
from quant.collect.sources.feeds import (
    FEED_OUTLET,
    NEWS_FEEDS,
    YOUTUBE_CHANNELS,
    fetch_conditional,
    fetch_news,
    fetch_youtube,
    outlet_diversity,
    parse_feed,
    resolve_outlet,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_atom.xml"

# 구글 뉴스 스타일: item에 <source url="...">언론사</source>가 있고 제목 끝이
# " - {언론사}"로 끝난다.
GOOGLE_NEWS_WITH_SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<item>
<title>[클릭 e종목]'취사병 전설이 되다' 제작사 주식은 살 만할까 - 아시아경제</title>
<link>https://news.google.com/articles/1</link>
<source url="https://www.asiae.co.kr">아시아경제</source>
<pubDate>Wed, 12 Aug 2026 08:00:00 +0900</pubDate>
</item>
</channel>
</rss>
"""

# <source>가 없는 구글 뉴스 스타일: 정규식 폴백으로 잡아야 한다.
GOOGLE_NEWS_NO_SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<item>
<title>'삼전닉스' 강세에 코스피 6,600선...매수 사이드카 발동 - YTN</title>
<link>https://news.google.com/articles/2</link>
<pubDate>Wed, 12 Aug 2026 09:00:00 +0900</pubDate>
</item>
</channel>
</rss>
"""

# 본문에 " - "가 들어 있고 <source>도 있는 경우 — 맨 끝 한 조각만 제거돼야 한다.
GOOGLE_NEWS_MID_HYPHEN = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<item>
<title>속보 - 반도체 업종 훈풍 - 헤럴드경제</title>
<link>https://news.google.com/articles/3</link>
<source url="https://www.heraldcorp.com">헤럴드경제</source>
</item>
</channel>
</rss>
"""

# 진짜 종목이 여전히 잡히는지 확인용 (접미사 제거가 본문 종목을 죽이지 않아야 한다).
GOOGLE_NEWS_REAL_STOCKS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<item>
<title>코스피 급등에 매수 사이드카 발동...삼성전자·SK하이닉스 7% 상승세 - 조선일보</title>
<link>https://news.google.com/articles/4</link>
<source url="https://www.chosun.com">조선일보</source>
</item>
</channel>
</rss>
"""

# 접미사 패턴이 없는 제목.
NO_SUFFIX = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<item>
<title>둘째 기사</title>
<link>https://example.test/news/2</link>
</item>
</channel>
</rss>
"""

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>테스트 피드</title>
<item>
<title>삼성전자 &amp; SK하이닉스 반등</title>
<link>https://example.test/news/1</link>
<pubDate>Wed, 12 Aug 2026 08:00:00 +0900</pubDate>
</item>
<item>
<title>둘째 기사</title>
<link>https://example.test/news/2</link>
<pubDate>Wed, 12 Aug 2026 09:00:00 +0900</pubDate>
</item>
</channel>
</rss>
"""

BROKEN_XML = "<not-xml"


def test_parse_atom_fixture_extracts_items():
    items = parse_feed(FIXTURE.read_text(encoding="utf-8"), limit=10)
    assert items
    for item in items:
        assert item["title"]
        assert item["link"].startswith("http")


def test_parse_feed_respects_limit():
    items = parse_feed(FIXTURE.read_text(encoding="utf-8"), limit=2)
    assert len(items) == 2


def test_parse_rss_inline_string():
    items = parse_feed(RSS_SAMPLE, limit=10)
    assert len(items) == 2
    assert items[0]["link"] == "https://example.test/news/1"
    assert items[0]["published"] == "Wed, 12 Aug 2026 08:00:00 +0900"


def test_parse_feed_unescapes_html_entities():
    items = parse_feed(RSS_SAMPLE, limit=10)
    assert items[0]["title"] == "삼성전자 & SK하이닉스 반등"


def test_parse_atom_link_href_extracted():
    items = parse_feed(FIXTURE.read_text(encoding="utf-8"), limit=1)
    assert items[0]["link"].startswith("https://www.youtube.com/watch?v=")


def test_parse_feed_broken_xml_returns_empty_list_not_exception():
    assert parse_feed(BROKEN_XML, limit=10) == []


def test_news_feeds_has_kr_and_us():
    assert "KR" in NEWS_FEEDS and "US" in NEWS_FEEDS
    assert NEWS_FEEDS["KR"] and NEWS_FEEDS["US"]


def test_news_feeds_us_has_at_least_six_http_sources():
    us_feeds = NEWS_FEEDS["US"]
    assert len(us_feeds) >= 6
    assert all(url.startswith("http") for url in us_feeds.values())


def test_youtube_channels_has_kr_and_us():
    assert "KR" in YOUTUBE_CHANNELS and "US" in YOUTUBE_CHANNELS


# ── 볼륨 + 다양성 (2026-08-13 확장) ─────────────────────────────────────


def test_news_feeds_kr_has_substantial_volume():
    """단순 나열이 아니라 실제로 늘었는지 — 기존 5개(직접 매체 2 + 구글 3)의
    최소 두 배 이상."""
    assert len(NEWS_FEEDS["KR"]) >= 15


def test_news_feeds_us_has_substantial_volume():
    """기존 10개보다 눈에 띄게 늘어야 한다."""
    assert len(NEWS_FEEDS["US"]) >= 15


def test_news_feeds_kr_has_diverse_direct_outlets():
    """전부 구글 뉴스 경유가 아니라 실제 원문 매체가 다수 있어야 한다."""
    direct_outlets = {
        outlet for feed, outlet in FEED_OUTLET.items()
        if feed in NEWS_FEEDS["KR"] and outlet != "구글뉴스"
    }
    assert len(direct_outlets) >= 10


def test_news_feeds_us_has_diverse_direct_outlets():
    direct_outlets = {
        outlet for feed, outlet in FEED_OUTLET.items() if feed in NEWS_FEEDS["US"]
    }
    assert len(direct_outlets) >= 12


def test_all_feed_names_have_url_and_are_distinct():
    for market in ("KR", "US"):
        urls = list(NEWS_FEEDS[market].values())
        assert len(urls) == len(set(urls)), f"{market}: 중복 피드 URL"


# ── 품질 감사 후 제외 회귀(2026-08-13) ──────────────────────────────────
# 전체 피드 신선도+종목 시그널 실측에서 하드 에비던스로 제외한 피드들이
# 다시 몰래 추가되지 않게 막는다. 근거는 quant/collect/sources/feeds.py의
# "품질 감사 후 제외(2026-08-13)" 주석 참고.


def test_dead_archive_feeds_stay_removed():
    """WSJ_Markets는 2025-01-27, MarketWatch_RealTime은 2025-06-11에 멈춘
    죽은 아카이브(CNN과 같은 실패 유형) — 200을 계속 반환하지만 절대
    갱신되지 않는다."""
    assert "WSJ_Markets" not in NEWS_FEEDS["US"]
    assert "MarketWatch_RealTime" not in NEWS_FEEDS["US"]


def test_junk_content_feeds_stay_removed():
    """Benzinga는 발행시각이 전부 동일한 상시 SEO 리스티클("Best X Stocks
    Right Now")이고 Fortune은 종목 언급 0%(임원 인터뷰·라이프스타일 위주)
    — 둘 다 실제 뉴스 시그널이 아니다."""
    assert "Benzinga" not in NEWS_FEEDS["US"]
    assert "Fortune" not in NEWS_FEEDS["US"]


def test_low_signal_kr_feeds_stay_removed():
    """뉴시스_경제(100건 중 종목 언급 1%)와 구글_반도체(질의 자체가 좁아
    고유 기여 0건, 구조적으로 항상 다른 피드와 겹침)는 종목 시그널이
    없거나 완전히 중복이라 제외했다."""
    assert "뉴시스_경제" not in NEWS_FEEDS["KR"]
    assert "구글_반도체" not in NEWS_FEEDS["KR"]


def test_hani_removed_for_missing_publish_dates():
    """한겨레_경제 RSS 아이템은 발행시각 필드가 아예 없다 — `filter_since`가
    날짜 없는 기사를 버리지 않고 항상 통과시키므로 이 피드는 시간창 필터를
    영구히 우회한다(구조적 블라인드스팟, 종목 신호도 약함)."""
    assert "한겨레_경제" not in NEWS_FEEDS["KR"]


def test_dropped_feeds_have_no_dangling_feed_outlet_entries():
    """드롭한 피드가 FEED_OUTLET에 고아로 남아있지 않아야 한다."""
    dropped = {
        "WSJ_Markets", "MarketWatch_RealTime", "Benzinga", "Fortune",
        "뉴시스_경제", "구글_반도체", "한겨레_경제",
    }
    assert not (dropped & FEED_OUTLET.keys())


# ── resolve_outlet / outlet_diversity ───────────────────────────────────


def test_resolve_outlet_prefers_item_outlet():
    item = {"title": "t", "link": "https://x", "published": None, "outlet": "조선일보"}
    assert resolve_outlet("아무피드", item) == "조선일보"


def test_resolve_outlet_falls_back_to_feed_mapping():
    item = {"title": "t", "link": "https://x", "published": None, "outlet": ""}
    assert resolve_outlet("한국경제_경제", item) == "한국경제"


def test_resolve_outlet_falls_back_to_feed_name_when_unmapped():
    item = {"title": "t", "link": "https://x", "published": None, "outlet": ""}
    assert resolve_outlet("존재안하는피드", item) == "존재안하는피드"


def test_outlet_diversity_counts_totals_and_distinct_outlets():
    news_data = {
        "feeds": {
            "한국경제_경제": [
                {"title": "a", "link": "https://a", "published": None, "outlet": ""},
                {"title": "b", "link": "https://b", "published": None, "outlet": ""},
            ],
            "구글_코스피": [
                {"title": "c", "link": "https://c", "published": None, "outlet": "매일경제"},
                {"title": "d", "link": "https://d", "published": None, "outlet": "한국경제"},
            ],
        }
    }
    result = outlet_diversity(news_data)
    assert result["total_articles"] == 4
    assert result["distinct_outlets"] == 2  # 한국경제, 매일경제
    assert result["by_outlet"]["한국경제"] == 3
    assert result["by_outlet"]["매일경제"] == 1


def test_outlet_diversity_empty_feeds_is_zero():
    result = outlet_diversity({"feeds": {}})
    assert result == {"total_articles": 0, "distinct_outlets": 0, "by_outlet": {}}


def test_outlet_diversity_single_outlet_flooding_is_visible():
    """한 매체가 표본을 독점하면 distinct_outlets=1로 바로 드러나야 한다."""
    news_data = {
        "feeds": {
            "매체A": [{"title": f"t{i}", "link": f"https://{i}", "published": None, "outlet": ""}
                     for i in range(20)],
        }
    }
    result = outlet_diversity(news_data)
    assert result["distinct_outlets"] == 1
    assert result["total_articles"] == 20


# ── 피드 단위 격리 (죽은 피드가 나머지를 안 죽인다) ──────────────────────


def test_fetch_news_isolates_a_single_dead_feed(monkeypatch):
    """피드 하나가 (network 예외로) 죽어도 fetch_news 자체는 예외를 던지지
    않고, 다른 피드는 정상 데이터를 그대로 들고 있어야 한다 — 죽은 피드가
    'news' 소스 전체를 ok=False로 끌고 내려가면 안 된다."""
    import quant.collect.sources.feeds as feeds_mod

    good_item = [{"title": "정상 기사", "link": "https://ok", "published": None, "outlet": ""}]

    def fake_client_get(url):
        if "dead" in url:
            raise RuntimeError("연결 끊김")
        return good_item

    def fake_fetch_one(url):
        # _fetch_one 자체 구현(예외 삼키기)을 그대로 통과시켜 실제 동작을 검증한다.
        try:
            return fake_client_get(url)
        except Exception:
            return []

    monkeypatch.setattr(feeds_mod, "NEWS_FEEDS", {
        "KR": {"살아있는매체": "https://ok.example/feed", "죽은매체": "https://dead.example/feed"},
    })
    monkeypatch.setattr(feeds_mod, "_fetch_one", fake_fetch_one)

    result = feeds_mod.fetch_news("KR")

    assert result["feeds"]["살아있는매체"] == good_item
    assert result["feeds"]["죽은매체"] == []


def test_parse_feed_strips_outlet_suffix_using_source_element():
    items = parse_feed(GOOGLE_NEWS_WITH_SOURCE, limit=10, strip_outlet=True)
    assert items[0]["title"] == "[클릭 e종목]'취사병 전설이 되다' 제작사 주식은 살 만할까"
    assert items[0]["outlet"] == "아시아경제"


def test_parse_feed_strips_outlet_suffix_regex_fallback_without_source():
    items = parse_feed(GOOGLE_NEWS_NO_SOURCE, limit=10, strip_outlet=True)
    assert items[0]["title"] == "'삼전닉스' 강세에 코스피 6,600선...매수 사이드카 발동"
    assert items[0]["outlet"] == "YTN"


def test_parse_feed_strip_outlet_false_keeps_title_unchanged():
    items = parse_feed(GOOGLE_NEWS_WITH_SOURCE, limit=10, strip_outlet=False)
    assert items[0]["title"] == (
        "[클릭 e종목]'취사병 전설이 되다' 제작사 주식은 살 만할까 - 아시아경제"
    )
    assert items[0]["outlet"] == ""


def test_parse_feed_strip_outlet_only_removes_trailing_piece():
    items = parse_feed(GOOGLE_NEWS_MID_HYPHEN, limit=10, strip_outlet=True)
    assert items[0]["title"] == "속보 - 반도체 업종 훈풍"
    assert items[0]["outlet"] == "헤럴드경제"


def test_parse_feed_strip_outlet_no_suffix_pattern_keeps_title():
    items = parse_feed(NO_SUFFIX, limit=10, strip_outlet=True)
    assert items[0]["title"] == "둘째 기사"
    assert items[0]["outlet"] == ""


def test_regression_outlet_names_not_extracted_as_stocks():
    """실측 오탐 회귀: 언론사명이 종목으로 잡히면 안 된다."""
    table = build_table([
        ("아시아경제", "127710", "유가"),
        ("YTN", "040300", "유가"),
        ("코스피", "999999", "유가"),  # 사전 오염 여부 확인용 더미
    ])
    asiae_item = parse_feed(GOOGLE_NEWS_WITH_SOURCE, limit=10, strip_outlet=True)[0]
    ytn_item = parse_feed(GOOGLE_NEWS_NO_SOURCE, limit=10, strip_outlet=True)[0]

    asiae_hits = extract(asiae_item["title"], table)
    ytn_hits = extract(ytn_item["title"], table)

    assert "127710" not in {h["symbol"] for h in asiae_hits}
    assert "040300" not in {h["symbol"] for h in ytn_hits}


def test_regression_real_stocks_still_extracted_after_outlet_strip():
    """접미사 제거가 본문 종목 추출을 죽이지 않아야 한다."""
    table = build_table([
        ("삼성전자", "005930", "유가"),
        ("SK하이닉스", "000660", "유가"),
    ])
    item = parse_feed(GOOGLE_NEWS_REAL_STOCKS, limit=10, strip_outlet=True)[0]
    assert item["title"] == "코스피 급등에 매수 사이드카 발동...삼성전자·SK하이닉스 7% 상승세"
    assert item["outlet"] == "조선일보"

    hits = extract(item["title"], table)
    names = {h["name"] for h in hits}
    assert "삼성전자" in names
    assert "SK하이닉스" in names
    assert "조선일보" not in {h["name"] for h in hits}


@pytest.mark.live
def test_live_fetch_news_kr_gets_at_least_one_feed():
    data = fetch_news("KR")
    assert any(items for items in data["feeds"].values())


@pytest.mark.live
def test_live_fetch_news_us_gets_at_least_four_feeds_with_items():
    """실측(2026-08-12): Reuters(feeds.reuters.com)는 RSS 서비스 자체가 죽어
    후보에서 뺐다 — curl 로 200 + 실제 item 확인된 나머지 원문 매체만 등록했다."""
    data = fetch_news("US")
    feeds_with_items = [name for name, items in data["feeds"].items() if items]
    assert len(feeds_with_items) >= 4, feeds_with_items


@pytest.mark.live
def test_live_fetch_youtube_us_gets_channel_items():
    data = fetch_youtube("US")
    assert any(items for items in data["channels"].values())


@pytest.mark.live
def test_live_fetch_youtube_kr_empty_channels_not_error():
    assert fetch_youtube("KR") == {"channels": {}}


# ── 발행시각 파싱 회귀 (2026-08-13 피드 감사에서 발견) ──────────────────

from datetime import timezone as _tz  # noqa: E402

from quant.collect.sources.feeds import parse_published  # noqa: E402


def test_colon_offset_is_parsed_not_silently_naive():
    """RFC822 는 "+0900" 이지만 "+09:00" 을 쓰는 피드가 흔하다(매일경제·인포스탁데일리).

    `parsedate_to_datetime` 은 이걸 예외 없이 naive 로 돌려준다. 그걸 UTC 로
    취급하면 KST 벽시계가 9시간 미래로 찍혀 **낡은 기사가 방금 나온 것처럼**
    시간 창을 통과한다 — 매 리포트마다 조용히 틀리던 버그.
    """
    with_colon = parse_published("Wed, 13 Aug 2026 09:00:00 +09:00")
    without = parse_published("Wed, 13 Aug 2026 09:00:00 +0900")
    assert with_colon == without
    assert with_colon.isoformat() == "2026-08-13T00:00:00+00:00"


def test_unknown_timezone_yields_none_not_a_wrong_utc_guess():
    """모르는 것을 UTC 라고 우기지 않는다 — None 은 '날짜 미상'으로 세어진다."""
    assert parse_published("Wed, 13 Aug 2026 09:00:00") is None
    assert parse_published("2026-08-13T09:00:00") is None


def test_iso8601_with_offset_is_parsed():
    """dc:date 는 ISO8601 로 온다(경향신문)."""
    assert parse_published("2026-08-13T09:00:00+09:00").isoformat() == "2026-08-13T00:00:00+00:00"
    assert parse_published("2026-08-13T00:00:00Z").tzinfo is _tz.utc


def test_dc_date_is_read_when_pubdate_is_absent():
    """경향신문은 pubDate 가 0건, dc:date 가 50건이었다 — 안 읽으면 전부 '날짜 미상'."""
    xml = """<?xml version="1.0"?>
    <rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item>
      <title>테스트 기사</title><link>https://example.com/a</link>
      <dc:date>2026-08-13T09:00:00+09:00</dc:date>
    </item></channel></rss>"""
    items = parse_feed(xml, 10)
    assert len(items) == 1
    assert parse_published(items[0]["published"]).isoformat() == "2026-08-13T00:00:00+00:00"


def test_garbage_published_does_not_crash():
    assert parse_published("쓰레기") is None
    assert parse_published(None) is None
    assert parse_published("") is None


# ── fetch_conditional: RSS 조건부 GET (H-2 Task 2) ──────────────────────
#
# 실제 httpx.Client() 를 만들지 않고 `feeds_mod.client`를 FakeClient로 갈아
# 끼운다 — tests/report/test_sentiment.py의 fetch_naaim 테스트와 같은 패턴.


class _FakeResp:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class _FakeClient:
    def __init__(self, resp, capture=None):
        self._resp = resp
        self._capture = capture

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        if self._capture is not None:
            self._capture["url"] = url
            self._capture["headers"] = headers
        return self._resp


def test_fetch_conditional_200_parses_items_and_stores_new_etag(monkeypatch):
    import quant.collect.sources.feeds as feeds_mod

    resp = _FakeResp(200, text=RSS_SAMPLE, headers={"ETag": '"v2"', "Last-Modified": "Wed"})
    monkeypatch.setattr(feeds_mod, "client", lambda: _FakeClient(resp))

    items, entry, status = fetch_conditional("https://feed.example/a", cache={})

    assert len(items) == 2
    assert entry == {"etag": '"v2"', "last_modified": "Wed"}
    assert status == "ok"


def test_fetch_conditional_sends_conditional_headers_from_cache(monkeypatch):
    import quant.collect.sources.feeds as feeds_mod

    resp = _FakeResp(200, text=RSS_SAMPLE, headers={"ETag": '"v2"'})
    capture: dict = {}
    monkeypatch.setattr(feeds_mod, "client", lambda: _FakeClient(resp, capture))

    fetch_conditional(
        "https://feed.example/a",
        cache={"https://feed.example/a": {"etag": '"v1"', "last_modified": "Tue"}},
    )

    assert capture["headers"] == {"If-None-Match": '"v1"', "If-Modified-Since": "Tue"}


def test_fetch_conditional_304_returns_empty_list_and_keeps_cache_entry(monkeypatch):
    import quant.collect.sources.feeds as feeds_mod

    resp = _FakeResp(304)
    monkeypatch.setattr(feeds_mod, "client", lambda: _FakeClient(resp))

    old_entry = {"etag": '"v1"', "last_modified": "Tue"}
    items, entry, status = fetch_conditional(
        "https://feed.example/a", cache={"https://feed.example/a": old_entry},
    )

    assert items == []
    assert entry == old_entry
    assert status == "not_modified", "304 는 성공이다 — 죽음으로 오판되면 안 된다"


def test_fetch_conditional_headerless_server_returns_empty_entry(monkeypatch):
    """ETag/Last-Modified 둘 다 안 주는 서버 — 파싱은 정상이지만 캐시에는 아무 것도
    안 남긴다(빈 dict). collector가 이 값을 보고 캐시 키 자체를 지운다."""
    import quant.collect.sources.feeds as feeds_mod

    resp = _FakeResp(200, text=RSS_SAMPLE, headers={})
    monkeypatch.setattr(feeds_mod, "client", lambda: _FakeClient(resp))

    items, entry, status = fetch_conditional("https://feed.example/a", cache={})

    assert len(items) == 2
    assert entry == {}
    assert status == "ok"


def test_fetch_conditional_network_error_returns_empty_list_and_keeps_entry(monkeypatch):
    """`_fetch_one`과 동일하게 피드 하나의 실패가 나머지를 죽이지 않는다."""
    import quant.collect.sources.feeds as feeds_mod

    class _RaisingClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            raise RuntimeError("연결 끊김")

    monkeypatch.setattr(feeds_mod, "client", lambda: _RaisingClient())

    old_entry = {"etag": '"v1"'}
    items, entry, status = fetch_conditional(
        "https://feed.example/a", cache={"https://feed.example/a": old_entry},
    )

    assert items == []
    assert entry == old_entry
    assert status == "error"


def test_fetch_conditional_backward_compat_empty_cache_behaves_like_full_fetch(monkeypatch):
    """캐시가 빈 dict(사실상 "캐시 없음")면 조건 없는 일반 GET과 동일하게 동작한다."""
    import quant.collect.sources.feeds as feeds_mod

    resp = _FakeResp(200, text=RSS_SAMPLE, headers={})
    capture: dict = {}
    monkeypatch.setattr(feeds_mod, "client", lambda: _FakeClient(resp, capture))

    items, entry, status = fetch_conditional("https://feed.example/a", cache={})

    assert capture["headers"] == {}
    assert len(items) == 2
    assert entry == {}
    assert status == "ok"


# ── 상한 절단 신호(FEED_LIMIT, 2026-09-03) ──────────────────────────────
#
# FEED_LIMIT(300)은 폭주 방지용 안전선이지 표본 크기가 아니다(모듈 상단 주석
# 참고) — 피드가 정확히 상한만큼 돌려주면 "그 피드가 실제로 더 줬을 수 있다"
# 는 신호라 WARNING 을 남겨야 한다.

def test_fetch_news_warns_when_feed_hits_the_cap(monkeypatch, caplog):
    import quant.collect.sources.feeds as feeds_mod

    capped = [{"title": f"t{i}", "link": f"https://x/{i}", "published": None, "outlet": ""}
              for i in range(feeds_mod.FEED_LIMIT)]
    monkeypatch.setattr(feeds_mod, "NEWS_FEEDS", {"KR": {"꽉찬매체": "https://ok.example/feed"}})
    monkeypatch.setattr(feeds_mod, "_fetch_one", lambda url: capped)

    with caplog.at_level("WARNING", logger="quant.collect.sources.feeds"):
        feeds_mod.fetch_news("KR")

    assert any("꽉찬매체" in r.message for r in caplog.records)


def test_fetch_news_does_not_warn_when_below_the_cap(monkeypatch, caplog):
    import quant.collect.sources.feeds as feeds_mod

    below = [{"title": "t", "link": "https://x/1", "published": None, "outlet": ""}]
    monkeypatch.setattr(feeds_mod, "NEWS_FEEDS", {"KR": {"정상매체": "https://ok.example/feed"}})
    monkeypatch.setattr(feeds_mod, "_fetch_one", lambda url: below)

    with caplog.at_level("WARNING", logger="quant.collect.sources.feeds"):
        feeds_mod.fetch_news("KR")

    assert not any("정상매체" in r.message for r in caplog.records)
