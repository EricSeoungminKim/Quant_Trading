"""시장별 소스 레지스트리.

각 항목은 `key -> (출처 URL, 무인자 호출가능객체)`다. URL은 리포트에 출처로
그대로 렌더되므로 실제 조회 주소를 넣는다.

키 이름은 템플릿과 machine payload가 참조하므로 함부로 바꾸지 않는다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from quant.collect.sources import calendar as calendar_src
from quant.collect.sources import (
    after_hours, feeds, fred, kr_flow, market, naver_flow, seeded_news, sentiment,
    technical, toss,
)

logger = logging.getLogger(__name__)

Source = tuple[str, Callable[[], dict]]

# 프로젝트 루트 — `collector.load_window`가 읽는 누적 저장소(`data/news/`)의
# 기준 경로다. `build_sources`는 report_cli.py의 여러 지점(아침 build/마감
# build)에서 `root` 없이 호출되므로(호출부를 넓게 고치지 않기 위해) 여기서
# 파일 위치로 역산한다 — `quant/adapters/env.py`의 `REPO_ROOT` 계산과 같은
# 관례(이 파일은 `quant/collect/sources/__init__.py`이므로 3단계 위).
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _merge_news_feeds(from_store: dict[str, list[dict]], live_feeds: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """저장소분과 실시간분을 피드별로 합친다. 링크 기준 중복 제거, 같은 기사가
    양쪽에 있으면 **실시간이 우선**한다(더 최신 파싱 결과 — outlet/제목 정제가
    갱신됐을 수 있다)."""
    # 지연 임포트 — `collector.py`가 이 패키지의 `feeds` 서브모듈을 임포트하므로
    # 모듈 최상단에서 순환 임포트가 생긴다(어느 쪽이 먼저 로드되느냐에 따라
    # 깨진다, 실측). 함수 호출 시점엔 양쪽 다 이미 로드가 끝나 있어 안전하다.
    from quant.collect.collector import normalize_link

    merged: dict[str, list[dict]] = {name: list(items) for name, items in from_store.items()}
    index: dict[str, dict[str, int]] = {
        name: {normalize_link(it["link"]): i for i, it in enumerate(items)}
        for name, items in merged.items()
    }
    for name, items in live_feeds.items():
        bucket = merged.setdefault(name, [])
        bucket_index = index.setdefault(name, {})
        for it in items:
            key = normalize_link(it["link"])
            pos = bucket_index.get(key)
            if pos is not None:
                bucket[pos] = it  # 실시간이 우선
                continue
            bucket_index[key] = len(bucket)
            bucket.append(it)
    return merged


def _fetch_news_merged(market_code: str, news_since: datetime | None, root: Path) -> dict:
    """뉴스 소스 — 누적 저장소(`collector.load_window`, 30분마다 쌓인 것) ∪
    실시간 한 번(`feeds.fetch_news`, 빌드 직전 ~30분 갭을 메운다).

    2026-09-02 실측(EC2): 이 유니언 없이 실시간만 읽으면 KR 08:00 리포트가
    당일 누적 4,642건 중 970건(21%)만, US 20:00 리포트가 1,246건 중 318건
    (26%)만 봤다 — RSS가 "최신 N건" 스냅샷이라 30분마다 도는 수집기가 쌓은
    나머지가 빌드 시점엔 이미 창 밖으로 밀려나 있었기 때문이다
    (`collector.py` 모듈 docstring 참고).

    저장소 디렉터리가 없으면(첫 배포·테스트 등) 개수 0으로 WARNING만 남기고
    실시간만 쓴다 — 이게 유니언 도입 전의 기존 동작이라 회귀가 아니다.
    """
    from quant.collect import collector  # 지연 임포트 — _merge_news_feeds 주석 참고

    now = datetime.now(timezone.utc)
    since = news_since or (now - timedelta(hours=24))

    store_dir = root / "data" / "news" / market_code
    from_store: dict[str, list[dict]] = {}
    if store_dir.exists():
        from_store = collector.load_window(root, market_code, since, now)
    else:
        logger.warning(
            "뉴스 누적 저장소 없음(%s) — from_store=0, 실시간만 사용", store_dir,
        )

    live = feeds.fetch_news(market_code, since=news_since)
    merged_feeds = _merge_news_feeds(from_store, live["feeds"])

    from_store_count = sum(len(v) for v in from_store.values())
    from_live_count = sum(len(v) for v in live["feeds"].values())
    return {
        **live,
        "feeds": merged_feeds,
        # "기사 N건" 표시는 merged_feeds 기준(중복 제거된 실제 표본)이 맞다 —
        # from_store/from_live 는 유니언 전 각 경로의 기여분이라 합이 이 값보다
        # 클 수 있다(겹치는 기사가 양쪽에 다 있었을 때).
        "kept": sum(len(v) for v in merged_feeds.values()),
        "from_store": from_store_count,
        "from_live": from_live_count,
    }


def build_sources(
    market_code: str, session_date: date,
    news_since: datetime | None = None,
    root: Path | None = None,
) -> dict[str, Source]:
    """`news_since`: 뉴스 표본의 시작 시각(직전 리포트 생성시각). None 이면 피드가
    주는 전부를 받는다 — 첫 실행이거나 직전 스냅샷이 없을 때.

    `root`: 누적 저장소(`data/news/`) 기준 경로. 생략하면 이 파일 위치에서
    역산한 저장소 루트를 쓴다(`_REPO_ROOT`) — 기존 호출부를 고치지 않고도
    저장소를 읽을 수 있게 하기 위해서다."""
    bizdate = session_date.strftime("%Y%m%d")
    news_root = root or _REPO_ROOT

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
            lambda: _fetch_news_merged(market_code, news_since, news_root),
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
