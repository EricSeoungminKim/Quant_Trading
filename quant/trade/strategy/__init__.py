"""전략 레지스트리 + settings.yaml 기반 팩토리."""
from __future__ import annotations

from quant.core.models import market_of
from quant.trade.strategy.confluence import ConfluenceStrategy
from quant.trade.strategy.cross_momentum import CrossMomentumStrategy
from quant.trade.strategy.donchian import DonchianStrategy, DonchianPureShell
from quant.trade.strategy.close_bet import CloseBetStrategy
from quant.trade.strategy.frgn_accumulate import FrgnAccumulateStrategy
from quant.trade.strategy.intraday_scan import IntradayScanStrategy
from quant.trade.strategy.mean_reversion import MeanReversionStrategy
from quant.trade.strategy.news_momentum import NewsMomentumStrategy
from quant.trade.strategy.news_scalp import NewsScalpStrategy
from quant.trade.strategy.orb import OpeningRangeBreakoutStrategy
from quant.trade.strategy.orb_scan import OrbScanStrategy
from quant.trade.strategy.scalp_1m import Scalp1mPureShell, Scalp1mStrategy

STRATEGY_REGISTRY = {
    "donchian": DonchianStrategy,
    # 순수함수 계약 파일럿(엔진 분리 설계 Phase A) — donchian과 별도 이름으로 등록,
    # settings.yaml에는 아직 배선하지 않는다(활성화는 마감 후 판단).
    "donchian_pure": DonchianPureShell,
    "orb": OpeningRangeBreakoutStrategy,
    "orb_scan": OrbScanStrategy,
    "intraday_scan": IntradayScanStrategy,
    "mean_reversion": MeanReversionStrategy,
    "cross_momentum": CrossMomentumStrategy,
    "confluence": ConfluenceStrategy,
    "news_momentum": NewsMomentumStrategy,
    # 갈래 A/B (2026-08-17 스펙 §5) — paper 등록, 태그 배선 전까지 기본 비활성.
    "news_scalp": NewsScalpStrategy,
    "frgn_accumulate": FrgnAccumulateStrategy,
    # 1분봉 스캘프 (2026-08-18 스펙) — news_scalp(A)/intraday_scan(C)와 같은
    # 유니버스에서 진입 방식만 달리한 병행 전략. 태그 없음(watchlist 유니버스만).
    "scalp_1m": Scalp1mStrategy,
    # 순수함수 계약 이전(엔진 분리 설계 Phase A, donchian_pure 다음 대상) —
    # donchian_pure와 동일하게 별도 이름으로 등록만 하고 settings.yaml은 아직
    # 건드리지 않는다(비활성).
    "scalp_1m_pure": Scalp1mPureShell,
    # 종가배팅(2026-08-25, 전략 4종 체제 ③) — 마감 강한 종목을 종가에 사서
    # 다음날 시초 갭에 판다. CLOSE_BET 태그(장중 리포트 채점)만 소비.
    "close_bet": CloseBetStrategy,
}


# 태그(관심종목 파일의 `tags: [...]`) → 그 태그를 실제로 소비하는 전략 id.
# 2026-08-26, 소유자 조직도 역할 3 — "선정된 종목을 알맞는 전략에 지정" 배정을
# 코드로 명시한다. 실제 소비 코드를 grep 으로 전수 추적해 작성했다(추측 아님):
# 각 전략 클래스 생성자가 `tags_of`를 받는지(`quant/trade/strategy/*.py`,
# `_TAGS_OF_CONSUMERS`와 정확히 같은 집합이어야 한다 — `tests/test_tag_assignment.py`가
# 대조한다), 받는다면 어떤 태그 상수로 게이트하는지(각 파일의 `_EVENT_TAG` 등).
#
# "*" 는 특정 태그 게이트가 없는 나머지(무태그·TREND·REBOUND 등, watch_scorer.
# _VALID_TAGS 중 여기 다른 키에 없는 것 전부) — `universe: watchlist`로 유니버스
# 전체를 심볼로 받지만 `tags_of`를 아예 모르는 전략들이다. 이 전략들은 태그와
# 무관하게 유니버스에 들어온 심볼이면 다 감시 대상이라 "전체감시"로 뭉뚱그린다.
TAG_ASSIGNMENT: dict[str, list[str]] = {
    # NewsMomentumStrategy._EVENT_TAG — 뉴스 촉매 있는 종목만 개장 즉시 진입.
    "EVENT": ["news_momentum"],
    # NewsScalpStrategy._SCALP_TAG — 갈래 A(2026-08-17). 2026-08-26 기준
    # config/settings.yaml `news_scalp.enabled: false`(paper 등록만, 비활성)지만
    # 코드 경로는 여전히 이 태그만 읽는다 — 활성화되면 그대로 유효.
    "EVENT_SCALP": ["news_scalp"],
    # FrgnAccumulateStrategy._FRGN_TAG/_FRGN_EXIT_TAG — 갈래 B, 외국인 적립
    # 매수/이탈 신호. 두 태그 다 같은 전략 하나만 읽는다.
    "FRGN": ["frgn_accumulate"],
    "FRGN_EXIT": ["frgn_accumulate"],
    # CloseBetStrategy._TAG — 종가배팅(2026-08-25, 전략 4종 체제 ③).
    "CLOSE_BET": ["close_bet"],
    # 게이트 없음 — universe: watchlist 인데 tags_of를 안 받는 전략들
    # (intraday_scan/orb_scan/cross_momentum/confluence/scalp_1m). 2026-08-26
    # 기준 이 중 scalp_1m만 enabled: true(전략 4종 체제).
    "*": ["intraday_scan", "orb_scan", "cross_momentum", "confluence", "scalp_1m"],
}


def assignment_for(tags: list[str]) -> list[str]:
    """태그 목록(빈 리스트=무태그) → 이 태그를 실제로 소비하는 전략 id 목록
    (중복 제거, 순서 보존). `TAG_ASSIGNMENT`에 없는 태그(TREND/REBOUND 등
    게이트가 없는 태그 포함)는 "*"(유니버스 전체 감시) 버킷으로 떨어진다."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in (tags or ["*"]):
        for sid in TAG_ASSIGNMENT.get(tag, TAG_ASSIGNMENT["*"]):
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


def describe_tags(tags: list[str]) -> str:
    """텔레그램 표시용 축약 — 특정 전략을 소비하는 태그는 전략 id 그대로,
    게이트가 없는 태그("*" 버킷: 무태그/TREND/REBOUND 등)는 "전체감시"로
    한 번만 뭉친다(전체감시 전략이 5개인데 매번 나열하면, 그 목록이 바뀔 때마다
    브리핑 문구도 흔들린다). 예: `["TREND", "EVENT"]` → "news_momentum·전체감시"."""
    specific: list[str] = []
    seen: set[str] = set()
    has_catchall = False
    for tag in (tags or ["*"]):
        if tag in TAG_ASSIGNMENT and tag != "*":
            for sid in TAG_ASSIGNMENT[tag]:
                if sid not in seen:
                    seen.add(sid)
                    specific.append(sid)
        else:
            has_catchall = True
    if has_catchall:
        specific.append("전체감시")
    return "·".join(specific) if specific else "전체감시"


def build_strategies(
    cfg: dict, leverage_of: dict[str, float] | None = None,
    tags_of: dict[str, list[str]] | None = None,
) -> list:
    """cfg["strategies"] 블록을 읽어 활성화된 전략 인스턴스 리스트를 만든다.

    `leverage_of`는 `MeanReversionStrategy`처럼 레버리지 정보를 생성자에서 받는
    전략에만, `tags_of`는 `NewsMomentumStrategy`/`NewsScalpStrategy`/
    `FrgnAccumulateStrategy`처럼 관심종목 태그(EVENT/EVENT_SCALP/FRGN 등)를
    생성자에서 받는 전략에만 전달한다 — 다른 전략 클래스는 이 kwarg를 모르므로
    무조건 넘기면 TypeError가 난다."""
    markets = market_of(cfg.get("universe", {}))
    strategies = []
    _TAGS_OF_CONSUMERS = (NewsMomentumStrategy, NewsScalpStrategy, FrgnAccumulateStrategy, CloseBetStrategy)
    for strat_id, strat_cfg in cfg.get("strategies", {}).items():
        if not strat_cfg.get("enabled", True):
            continue
        cls = STRATEGY_REGISTRY[strat_cfg["class"]]
        symbols = strat_cfg["symbols"]
        market = markets.get(symbols[0], "US") if symbols else "US"
        kwargs = {}
        if cls is MeanReversionStrategy:
            kwargs["leverage_of"] = leverage_of
        if cls in _TAGS_OF_CONSUMERS:
            kwargs["tags_of"] = tags_of
        strategies.append(cls(symbols=symbols, params=strat_cfg["params"], market=market, id=strat_id, **kwargs))
    return strategies
