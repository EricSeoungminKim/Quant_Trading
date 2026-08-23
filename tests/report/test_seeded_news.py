from unittest.mock import patch

import httpx
import pytest

from quant.adapters.env import get_key
from quant.collect.sources.seeded_news import (
    SEED_BOARDS,
    SEED_LIMIT,
    _seed_symbols,
    build_query,
    fetch_seeded_news,
)

# --- build_query: 검색어 문자열만 (URL 인코딩은 호출부 책임) ------------------


def test_build_query_kr_includes_name_and_주가():
    q = build_query("삼성전자", "005930", "KR")
    assert "삼성전자" in q
    assert "주가" in q


def test_build_query_us_includes_name_and_stock():
    q = build_query("Nvidia", "NVDA", "US")
    assert "Nvidia" in q
    assert "stock" in q


# --- _seed_symbols: 보드 병합 + 중복 제거 + SEED_LIMIT 절단 -------------------


def _rank_item(rank, symbol, change_pct=1.0):
    return {"rank": rank, "symbol": symbol, "change_pct": change_pct}


def test_seed_symbols_merges_boards_dedupes_and_records_all_source_boards():
    rankings = {
        "boards": {
            "거래대금": [_rank_item(1, "005930"), _rank_item(2, "000660")],
            "상승률": [_rank_item(1, "000660"), _rank_item(2, "035420")],
            "토스 사용자 거래대금": [_rank_item(1, "005930")],
        }
    }
    seeds = _seed_symbols(rankings)
    by_symbol = {s["symbol"]: s for s in seeds}

    # 중복 종목(005930, 000660)이 한 번씩만 들어간다.
    assert [s["symbol"] for s in seeds].count("005930") == 1
    assert [s["symbol"] for s in seeds].count("000660") == 1

    # 출처 보드가 모두 기록된다.
    assert by_symbol["005930"]["boards"] == ["거래대금", "토스 사용자 거래대금"]
    assert by_symbol["000660"]["boards"] == ["거래대금", "상승률"]
    assert by_symbol["035420"]["boards"] == ["상승률"]


def test_seed_symbols_order_follows_seed_boards_priority():
    rankings = {
        "boards": {
            "상승률": [_rank_item(1, "AAA")],
            "거래대금": [_rank_item(1, "BBB")],
            "토스 사용자 거래대금": [_rank_item(1, "CCC")],
        }
    }
    seeds = _seed_symbols(rankings)
    assert [s["symbol"] for s in seeds] == ["BBB", "AAA", "CCC"]


def test_seed_symbols_truncated_to_seed_limit():
    rankings = {
        "boards": {
            "거래대금": [_rank_item(i, f"S{i:03d}") for i in range(SEED_LIMIT + 5)],
        }
    }
    seeds = _seed_symbols(rankings)
    assert len(seeds) == SEED_LIMIT


def test_seed_boards_priority_order():
    assert SEED_BOARDS == ("거래대금", "상승률", "토스 사용자 거래대금")


# --- fetch_seeded_news: 랭킹 실패 raise, 이름 해석/검색 개별 실패 격리 --------


def test_fetch_seeded_news_raises_when_rankings_fail():
    with patch("quant.collect.sources.seeded_news.toss.fetch_rankings", side_effect=ValueError("토스 랭킹 실패")):
        with pytest.raises(ValueError):
            fetch_seeded_news("KR", resolve_name=lambda s: s)


def test_fetch_seeded_news_resolve_name_none_falls_back_to_symbol():
    rankings = {"boards": {"거래대금": [_rank_item(1, "005930")]}}
    with patch("quant.collect.sources.seeded_news.toss.fetch_rankings", return_value=rankings):
        with patch("quant.collect.sources.seeded_news._search_news", return_value=[]):
            with patch("quant.collect.sources.seeded_news.time.sleep"):
                result = fetch_seeded_news("KR", resolve_name=lambda s: None)
    assert result["seeded"]["005930"]["name"] == "005930"


def test_fetch_seeded_news_resolve_name_exception_falls_back_to_symbol():
    rankings = {"boards": {"거래대금": [_rank_item(1, "005930")]}}

    def boom(symbol):
        raise RuntimeError("사전 조회 실패")

    with patch("quant.collect.sources.seeded_news.toss.fetch_rankings", return_value=rankings):
        with patch("quant.collect.sources.seeded_news._search_news", return_value=[]):
            with patch("quant.collect.sources.seeded_news.time.sleep"):
                result = fetch_seeded_news("KR", resolve_name=boom)
    assert result["seeded"]["005930"]["name"] == "005930"


def test_fetch_seeded_news_one_symbol_search_failure_does_not_kill_others():
    rankings = {
        "boards": {
            "거래대금": [_rank_item(1, "005930"), _rank_item(2, "000660")],
        }
    }
    article = {"title": "t", "link": "https://x", "published": None, "outlet": ""}

    def side_effect(query, market):
        if "005930" in query or "삼성전자" in query:
            raise RuntimeError("network down")
        return [article]

    with patch("quant.collect.sources.seeded_news.toss.fetch_rankings", return_value=rankings):
        with patch("quant.collect.sources.seeded_news._search_news", side_effect=side_effect):
            with patch("quant.collect.sources.seeded_news.time.sleep"):
                result = fetch_seeded_news(
                    "KR", resolve_name=lambda s: "삼성전자" if s == "005930" else "SK하이닉스"
                )

    assert result["seeded"]["005930"]["articles"] == []
    assert result["seeded"]["000660"]["articles"] == [article]
    assert result["queried"] == 2
    assert result["with_articles"] == 1


def test_fetch_seeded_news_symbol_with_no_articles_returns_empty_list():
    rankings = {"boards": {"거래대금": [_rank_item(1, "005930")]}}
    with patch("quant.collect.sources.seeded_news.toss.fetch_rankings", return_value=rankings):
        with patch("quant.collect.sources.seeded_news._search_news", return_value=[]):
            with patch("quant.collect.sources.seeded_news.time.sleep"):
                result = fetch_seeded_news("KR", resolve_name=lambda s: "삼성전자")
    assert result["seeded"]["005930"]["articles"] == []
    assert result["with_articles"] == 0
    assert result["queried"] == 1


def test_fetch_seeded_news_output_shape():
    rankings = {
        "boards": {
            "거래대금": [_rank_item(2, "005930", change_pct=6.88)],
        }
    }
    article = {"title": "t", "link": "https://x", "published": None, "outlet": ""}
    with patch("quant.collect.sources.seeded_news.toss.fetch_rankings", return_value=rankings):
        with patch("quant.collect.sources.seeded_news._search_news", return_value=[article]):
            with patch("quant.collect.sources.seeded_news.time.sleep"):
                result = fetch_seeded_news("KR", resolve_name=lambda s: "삼성전자")
    entry = result["seeded"]["005930"]
    assert entry["name"] == "삼성전자"
    assert entry["boards"] == ["거래대금"]
    assert entry["rank"] == 2
    assert entry["change_pct"] == 6.88
    assert entry["articles"] == [article]
    assert result["queried"] == 1
    assert result["with_articles"] == 1


# --- 라이브 (토스 자격증명 없거나 403이면 skip) -------------------------------


@pytest.mark.live
def test_live_fetch_seeded_news_kr():
    if not get_key("TOSS_CLIENT_ID") or not get_key("TOSS_CLIENT_SECRET"):
        pytest.skip("TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 미설정")
    try:
        result = fetch_seeded_news("KR", resolve_name=lambda s: None)
    except (RuntimeError, ValueError) as e:
        pytest.skip(str(e))
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 403:
            pytest.skip("IP 화이트리스트 — 로컬에서는 403이 정상")
        raise
    assert result["queried"] > 0


# ── 랭킹 주입 (2026-08-13 US 실측 회귀) ───────────────────────

def test_injected_rankings_are_used_without_calling_toss(monkeypatch):
    """1차 배치가 이미 받은 랭킹을 재사용해야 한다 — 병렬로 또 부르면
    같은 엔드포인트를 동시에 두 번 때려 레이트 리밋에 걸린다."""
    from quant.collect.sources import seeded_news as sn

    called = []
    monkeypatch.setattr(sn.toss, "fetch_rankings",
                        lambda m: called.append(m) or {"boards": {}})
    monkeypatch.setattr(sn, "_search_news", lambda q, m: [])

    rankings = {"ranked_at": "x", "boards": {
        "거래대금": [{"rank": 1, "symbol": "005930", "change_pct": 1.0}]}}
    out = sn.fetch_seeded_news("KR", lambda s: "삼성전자", rankings)

    assert called == [], "주입된 랭킹이 있으면 토스를 다시 부르면 안 된다"
    assert "005930" in out["seeded"]


def test_missing_rankings_falls_back_to_direct_call(monkeypatch):
    """단독 사용·테스트용 폴백은 유지한다."""
    from quant.collect.sources import seeded_news as sn

    called = []
    monkeypatch.setattr(sn.toss, "fetch_rankings", lambda m: called.append(m) or {
        "ranked_at": "x", "boards": {"거래대금": [
            {"rank": 1, "symbol": "005930", "change_pct": 1.0}]}})
    monkeypatch.setattr(sn, "_search_news", lambda q, m: [])

    sn.fetch_seeded_news("KR", lambda s: "삼성전자")
    assert called == ["KR"]
