"""토스 랭킹 상위 종목을 먼저 확보하고, 그 종목명으로 뉴스를 역검색한다.

`quant/collect/sources/feeds.py`의 정방향(뉴스 → 종목 추출)은 미국에서 구조적으로
약하다 — 미국 금융뉴스는 개별 종목보다 지수·매크로를 다뤄, 실측 97건에서 캔
게 13종목(대부분 1건씩)이었다. 여기서는 인과를 뒤집는다: 토스 랭킹이 "실제로
돈이 몰린 곳"을 직접 주므로, 그 종목명으로 구글뉴스를 검색한다. 두 방식은
잡는 게 다르므로(정방향=이야기가 도는 곳, 역방향=돈이 몰린 곳) 둘 다 유지한다
— 겹치는 종목이 가장 강한 신호다.
"""
from __future__ import annotations

import time
from typing import Callable
from urllib.parse import quote

from quant.adapters.http import client
from quant.collect.sources import toss
from quant.collect.sources.feeds import parse_feed

SEED_BOARDS = ("거래대금", "상승률", "토스 사용자 거래대금")
SEED_LIMIT = 8
ARTICLES_PER_SYMBOL = 4
_SEARCH_SLEEP_S = 0.3

_RSS_URL = {
    "KR": "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko",
    "US": "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
}


def build_query(name: str, symbol: str, market: str) -> str:
    """검색어 문자열만 반환한다(URL 인코딩은 호출부 책임) — 인코딩과 분리해야
    순수 함수로 테스트하기 쉽다."""
    if market == "KR":
        return f"{name} 주가"
    return f"{name} stock"


def _seed_symbols(rankings: dict) -> list[dict]:
    """SEED_BOARDS 순서(거래대금 → 상승률 → 토스사용자)로 상위 종목을 모아
    중복 제거하고 SEED_LIMIT개로 자른다. 뒤 보드에서 다시 나오면 boards에
    출처만 추가한다."""
    boards = rankings["boards"]
    order: list[str] = []
    meta: dict[str, dict] = {}
    for board in SEED_BOARDS:
        for item in boards.get(board, []):
            symbol = item["symbol"]
            if symbol not in meta:
                order.append(symbol)
                meta[symbol] = {
                    "symbol": symbol,
                    "boards": [board],
                    "rank": item["rank"],
                    "change_pct": item["change_pct"],
                }
            else:
                meta[symbol]["boards"].append(board)
    return [meta[symbol] for symbol in order[:SEED_LIMIT]]


def _search_news(query: str, market: str) -> list[dict]:
    url = _RSS_URL[market].format(q=quote(query))
    with client() as c:
        resp = c.get(url)
        resp.raise_for_status()
    return parse_feed(resp.text, ARTICLES_PER_SYMBOL, strip_outlet=True)


def fetch_seeded_news(
    market: str,
    resolve_name: Callable[[str], str | None],
    rankings: dict | None = None,
) -> dict:
    """market: 'KR' | 'US'. resolve_name(symbol) -> 회사명, 없으면 None.

    `rankings` 를 주입받으면 그걸 쓴다. **주입이 정상 경로다** — 수집기가
    소스들을 병렬로 돌리는데 이 함수가 랭킹 API를 또 부르면 같은 엔드포인트를
    동시에 두 번 때려 레이트 리밋에 걸린다(실측 2026-08-13: `toss_rankings`는
    US 4개 보드 전부 성공했는데 이 소스만 "랭킹을 하나도 못 가져왔다"로 실패).
    주입이 없을 때만 직접 호출한다 — 단독 사용·테스트용 폴백이다.

    토스 랭킹 실패는 그대로 raise한다 — `quant/collect/snapshot.py`가
    `SourceResult(ok=False)`로 기록하므로 여기서 조용히 빈 값을 반환하면 안
    된다. 종목별 검색 실패(그리고 resolve_name 실패)는 건너뛰고 나머지를
    계속한다.
    """
    if rankings is None:
        rankings = toss.fetch_rankings(market)
    seeds = _seed_symbols(rankings)

    seeded: dict[str, dict] = {}
    with_articles = 0
    for i, seed in enumerate(seeds):
        symbol = seed["symbol"]
        try:
            name = resolve_name(symbol) or symbol
        except Exception:
            name = symbol

        if i > 0:
            time.sleep(_SEARCH_SLEEP_S)  # 구글뉴스 연속 요청 차단 방지

        try:
            articles = _search_news(build_query(name, symbol, market), market)
        except Exception:
            articles = []

        if articles:
            with_articles += 1

        seeded[symbol] = {
            "name": name,
            "boards": seed["boards"],
            "rank": seed["rank"],
            "change_pct": seed["change_pct"],
            "articles": articles,
        }

    return {"seeded": seeded, "queried": len(seeds), "with_articles": with_articles}
