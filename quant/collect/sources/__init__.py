"""시장별 소스 레지스트리.

각 항목은 `key -> (출처 URL, 무인자 호출가능객체)`다. URL은 리포트에 출처로
그대로 렌더되므로 실제 조회 주소를 넣는다.

키 이름은 템플릿과 machine payload가 참조하므로 함부로 바꾸지 않는다.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Callable

from quant.collect.sources import calendar as calendar_src
from quant.collect.sources import (
    after_hours, feeds, fred, kr_flow, market, naver_flow, seeded_news, sentiment,
    technical, toss,
)

Source = tuple[str, Callable[[], dict]]


def build_sources(
    market_code: str, session_date: date,
    news_since: datetime | None = None,
) -> dict[str, Source]:
    """`news_since`: 뉴스 표본의 시작 시각(직전 리포트 생성시각). None 이면 피드가
    주는 전부를 받는다 — 첫 실행이거나 직전 스냅샷이 없을 때."""
    bizdate = session_date.strftime("%Y%m%d")

    common: dict[str, Source] = {
        "market": (
            "https://finance.yahoo.com",
            lambda: market.fetch_quotes(market_code),
        ),
        "calendar": (
            calendar_src.FRED_DATES,
            lambda: calendar_src.fetch_calendar(session_date),
        ),
        "macro": ("https://api.stlouisfed.org/fred", fred.fetch_macro),
        "news": (
            "https://news.google.com",
            lambda: feeds.fetch_news(market_code, since=news_since),
        ),
        # 기술적·심리 지표는 미국 시장 기준이지만 한국장 판단에도 그대로 쓰인다
        # (밤사이 미국장이 한국 시초가를 만든다) — 두 리포트 모두에 싣는다.
        "vix_term": ("https://finance.yahoo.com", technical.fetch_vix_term),
        "sectors": ("https://finance.yahoo.com", technical.fetch_sectors),
        "breadth": (technical.SP500_LIST_URL, technical.fetch_breadth_52w),
        "sentiment": ("https://production.dataviz.cnn.io", sentiment.fetch_sentiment),
        # 토스는 EC2 의 고정 IP 만 허용한다 — 로컬에서는 403 이 정상이고
        # SourceResult(ok=False) 로 기록된다. 조용히 빈 값을 넣지 않는다.
        "toss_rankings": (
            "https://openapi.tossinvest.com/api/v1/rankings",
            lambda: toss.fetch_rankings(market_code),
        ),
        "youtube": ("https://www.youtube.com", lambda: feeds.fetch_youtube(market_code)),
    }

    if market_code == "KR":
        return {
            **common,
            # 장후 시간외단일가 급등 — 정규장 마감 뒤 움직인 종목. 08:00 리포트 시점에
            # **전일 저녁 데이터는 이미 존재한다**. 2026-08-13 한화생명(088350)이
            # 전날 시간외에서 +7.23%(64만주)였고 다음날 +6.4% 갭업 → +18.3% 갔는데,
            # 우리는 시간외를 안 봐서 뉴스 190건·토스 랭킹 4보드 어디에도 못 잡았다.
            "after_hours": (
                "https://api.kiwoom.com/api/dostk/rkinfo",
                after_hours.fetch_after_hours_movers,
            ),
            "kospi_flow": (
                naver_flow.URL_TEMPLATE.format(bizdate=bizdate, sosok="01"),
                lambda: naver_flow.fetch_flow("01", bizdate),
            ),
            "kr_funding": ("https://freesis.kofia.or.kr", kr_flow.fetch_kr_flow),
            "toss_flow": (
                "https://openapi.tossinvest.com/api/v1/market-indicators",
                toss.fetch_investor_trading,
            ),
            "kosdaq_flow": (
                naver_flow.URL_TEMPLATE.format(bizdate=bizdate, sosok="02"),
                lambda: naver_flow.fetch_flow("02", bizdate),
            ),
        }
    return common


# resolver 팩토리 타입 — symbol→회사명 함수를 **나중에**(스레드풀 안에서) 만들어
# 돌려주는 무인자 호출가능객체. 종목 사전은 분석 평면 소유라 수집이 직접 만들지
# 않고 주입받는다(부채 상환 2026-08-24 — 예전엔 quant.analyze.entities 를 여기서
# 임포트했다). None 이면 이름 해석 없이 동작한다(기존 cache_dir=None 과 동일).
ResolverFactory = Callable[[], Callable[[str], "str | None"]]


def build_seeded_source(
    market_code: str, rankings: dict, resolver_factory: "ResolverFactory | None" = None,
) -> dict[str, Source]:
    """랭킹 시드 뉴스 — **1차 수집이 끝난 뒤 2차로 돈다.**

    이 소스만 다른 소스의 결과(토스 랭킹)에 의존한다. 1차 배치에 같이 넣으면
    랭킹 API를 병렬로 두 번 부르게 되고 실제로 레이트 리밋에 걸렸다
    (2026-08-13 US 실측). 의존이 있는 것은 순서를 지켜 돌리는 게 맞다.
    """
    return {
        "seeded_news": (
            "https://news.google.com/rss/search",
            lambda: seeded_news.fetch_seeded_news(
                market_code,
                resolver_factory() if resolver_factory else (lambda symbol: None),
                rankings,
            ),
        ),
    }
