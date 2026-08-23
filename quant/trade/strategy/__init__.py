"""전략 레지스트리 + settings.yaml 기반 팩토리."""
from __future__ import annotations

from quant.core.models import market_of
from quant.trade.strategy.confluence import ConfluenceStrategy
from quant.trade.strategy.cross_momentum import CrossMomentumStrategy
from quant.trade.strategy.donchian import DonchianStrategy, DonchianPureShell
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
}


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
    _TAGS_OF_CONSUMERS = (NewsMomentumStrategy, NewsScalpStrategy, FrgnAccumulateStrategy)
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
