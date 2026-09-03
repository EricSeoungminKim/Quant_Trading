from datetime import date, datetime, timezone

from quant.analyze.mentions import collect_mentions, continuity
from quant.collect.contracts import SourceResult, Snapshot

UTC = timezone.utc


# ── 뉴스 소스 유니언(2026-09-03) 소비 — collect_mentions 는 build_sources 의
# "news" 소스가 만든 feeds dict 를 그대로 읽는다. 그게 저장소∪실시간 유니언
# 결과든 실시간 단독이든 collect_mentions 입장에선 구분할 이유가 없다 — 이
# 소비 지점이 실제로 그 계약을 지키는지, 그리고 그 자체로 네트워크를
# 새로 타지 않는지(collect 평면 → trade 평면 경계와 별개로, collect_mentions
# 은 순수 함수여야 한다)를 확인한다.

def _snap_with_news(feeds: dict, market="KR") -> Snapshot:
    news = SourceResult(
        key="news", ok=True, data={"feeds": feeds}, error=None,
        url="https://news.google.com", fetched_at=datetime(2026, 9, 2, tzinfo=UTC),
        latency_ms=1,
    )
    return Snapshot(
        schema_version=1, market=market, session_date=date(2026, 9, 2),
        generated_at=datetime(2026, 9, 2, 7, 0, tzinfo=UTC), results={"news": news},
    )


def test_collect_mentions_reads_whatever_feeds_the_news_source_produced():
    """`feeds` 가 저장소 유니언 결과든 실시간 단독이든 collect_mentions 은
    구분 없이 그대로 읽는다 — merge 로직을 몰라도 된다는 게 계약이다."""
    table = [("삼성전자", "005930")]
    feeds = {
        "저장소출처": [{"title": "삼성전자 신고가", "link": "https://x.com/1", "outlet": ""}],
        "실시간출처": [{"title": "삼성전자 추가 상승", "link": "https://x.com/2", "outlet": ""}],
    }
    snap = _snap_with_news(feeds)

    rows = collect_mentions(snap, table, market="KR")

    assert {r["feed"] for r in rows} == {"저장소출처", "실시간출처"}
    assert {r["link"] for r in rows} == {"https://x.com/1", "https://x.com/2"}
    assert all(r["symbol"] == "005930" for r in rows)


def test_collect_mentions_is_a_pure_function_of_the_snapshot():
    """네트워크를 새로 타지 않는다 — snap.results["news"] 밖의 어떤 소스도
    건드리지 않아야 한다(입력이 snap 하나뿐이라는 시그니처 자체가 이미 그
    계약을 강제하지만, 명시적으로 문서화해 둔다)."""
    import inspect

    sig = inspect.signature(collect_mentions)
    assert list(sig.parameters) == ["snap", "table", "market"]



# ── 시장 분리 ────────────────────────────────────────────────

def _mrow(d, sym, market, title="t"):
    return {"date": d, "symbol": sym, "name": "X", "title": title,
            "link": f"https://a.test/{sym}{title}", "feed": "F", "market": market}


def test_continuity_filters_by_market():
    """원장은 공유지만 집계는 시장별이다 — 안 그러면 US 리포트에 KR 종목이 섞인다."""
    led = [_mrow("2026-08-12", "005930", "KR"), _mrow("2026-08-12", "NVDA", "US")]
    assert set(continuity(led, date(2026, 8, 12), market="KR")) == {"005930"}
    assert set(continuity(led, date(2026, 8, 12), market="US")) == {"NVDA"}


def test_continuity_without_market_counts_everything():
    led = [_mrow("2026-08-12", "005930", "KR"), _mrow("2026-08-12", "NVDA", "US")]
    assert len(continuity(led, date(2026, 8, 12))) == 2


def test_legacy_rows_without_market_are_treated_as_kr():
    """US 추출이 붙기 전 기록은 전부 한국 종목이었다."""
    led = [{"date": "2026-08-12", "symbol": "005930", "name": "삼성전자",
            "title": "t", "link": "https://a.test/1", "feed": "F"}]
    assert set(continuity(led, date(2026, 8, 12), market="KR")) == {"005930"}
    assert continuity(led, date(2026, 8, 12), market="US") == {}


# ── 랭킹 출처 표시 ────────────────────────────────────────────

from quant.analyze.mentions import mark_origin  # noqa: E402


def _cont_entry(**kw):
    base = {"name": "X", "days": 1, "articles": 1, "today_articles": 1,
            "streak_days": 1, "is_new": False, "history": [], "titles": []}
    return {**base, **kw}


def test_symbol_in_ranking_is_marked_both():
    cont = {"005930": _cont_entry()}
    out = mark_origin(cont, {"005930", "000660"})
    assert out["005930"]["origin"] == "both"
    assert out["005930"]["in_ranking"] is True


def test_symbol_not_in_ranking_stays_news():
    cont = {"003540": _cont_entry()}
    out = mark_origin(cont, {"005930"})
    assert out["003540"]["origin"] == "news"
    assert out["003540"]["in_ranking"] is False


def test_mark_origin_preserves_other_fields():
    cont = {"005930": _cont_entry(today_articles=18, streak_days=3)}
    out = mark_origin(cont, {"005930"})
    assert out["005930"]["today_articles"] == 18
    assert out["005930"]["streak_days"] == 3


def test_mark_origin_does_not_mutate_input():
    cont = {"005930": _cont_entry()}
    mark_origin(cont, {"005930"})
    assert "origin" not in cont["005930"]


def test_empty_ranking_set_marks_everything_news():
    cont = {"005930": _cont_entry(), "000660": _cont_entry()}
    out = mark_origin(cont, set())
    assert all(c["origin"] == "news" for c in out.values())


# ── 랭킹 편입이 매수세 근거인가 (2026-08-13) ────────────────────────────────
#
# 회귀: 펄어비스가 하락률 보드에 오른 채 in_ranking=True 가 되어 RANK 태그를 달았고,
# 그게 엔진 어휘의 TREND 로 번역돼 08:42 자동 편입 → 그날 손실 청산됐다.


def test_declining_symbol_is_in_ranking_but_not_bullish():
    """원사실(랭킹에 있었다)은 남기고 판정(매수세 근거다)만 분리한다."""
    cont = {"263750": {"today_articles": 2}}
    out = mark_origin(cont, {"263750"}, bullish_symbols=set())
    assert out["263750"]["in_ranking"] is True, "3층 채점 원장은 원사실을 잃으면 안 된다"
    assert out["263750"]["ranking_bullish"] is False


def test_bullish_symbol_is_both():
    out = mark_origin({"005930": {}}, {"005930"}, bullish_symbols={"005930"})
    assert out["005930"]["in_ranking"] is out["005930"]["ranking_bullish"] is True


def test_bullish_defaults_to_in_ranking_when_not_given():
    """인자를 안 주면 옛 동작 그대로 — 호출부를 한꺼번에 바꾸지 않아도 된다."""
    out = mark_origin({"005930": {}}, {"005930"})
    assert out["005930"]["ranking_bullish"] is True
