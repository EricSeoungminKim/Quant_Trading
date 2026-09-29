"""갈래 A/B(news_scalp/frgn_accumulate) 레지스트리 등록 + tags_of 배선 회귀 가드.

build_strategies는 클래스별로 어떤 kwarg를 넘길지 명시적으로 분기한다
(quant/trade/strategy/__init__.py) — 새 태그 소비 전략을 등록하고 이 분기에
추가하는 걸 잊으면 TypeError 없이 조용히 tags_of=None으로 생성돼(생성자
기본값) 태그 게이트가 영원히 닫힌 채로 배포된다. 이 테스트가 그 실수를 잡는다.
"""
from __future__ import annotations

from quant.trade.strategy import STRATEGY_REGISTRY, build_strategies
from quant.trade.strategy.frgn_accumulate import FrgnAccumulateStrategy
from quant.trade.strategy.news_scalp import NewsScalpStrategy


def test_registry_has_branch_a_and_b():
    assert STRATEGY_REGISTRY["news_scalp"] is NewsScalpStrategy
    assert STRATEGY_REGISTRY["frgn_accumulate"] is FrgnAccumulateStrategy


def _cfg(**strategy_overrides):
    base = {
        "universe": {"kr": ["005930"], "us": []},
        "strategies": {
            "news_scalp": {
                "class": "news_scalp", "enabled": True, "symbols": ["005930"],
                "params": {},
            },
            "frgn_accumulate": {
                "class": "frgn_accumulate", "enabled": True, "symbols": ["005930"],
                "params": {},
            },
        },
    }
    for sid, over in strategy_overrides.items():
        base["strategies"][sid].update(over)
    return base


def test_build_strategies_injects_tags_of_into_news_scalp():
    tags_of = {"005930": ["EVENT_SCALP"]}
    strategies = build_strategies(_cfg(frgn_accumulate={"enabled": False}), tags_of=tags_of)
    [strat] = strategies
    assert isinstance(strat, NewsScalpStrategy)
    assert strat.tags_of == tags_of


def test_build_strategies_injects_tags_of_into_frgn_accumulate():
    tags_of = {"005930": ["FRGN"]}
    strategies = build_strategies(_cfg(news_scalp={"enabled": False}), tags_of=tags_of)
    [strat] = strategies
    assert isinstance(strat, FrgnAccumulateStrategy)
    assert strat.tags_of == tags_of


def test_real_config_runs_only_kr_cat_and_preserves_historical_accounts():
    """2026-09-30 소유자 결정: KR 촉매형 단독 PAPER 관찰, 과거 계좌 보존."""
    from quant.apps.config import load_settings

    settings = load_settings()
    strat_cfg = settings.raw["strategies"]
    assert {sid for sid, cfg in strat_cfg.items() if cfg.get("enabled")} == {"vol_breakout_cat"}
    focus = strat_cfg["vol_breakout_cat"]
    assert focus["markets"] == ["KR"]
    assert focus["validation"]["status"] == "burn_in"
    # US 계좌 선언은 과거 성과 분모/손익 보존용이며, 현재 US 거래 허가가 아니다.
    assert focus["capital_fraction"] == {"KR": 0.07, "US": 0.05}
    raw = {**settings.raw, "strategies": {
        **strat_cfg, "vol_breakout_cat": {**focus, "symbols": ["005930", "TQQQ"]},
    }}
    built = build_strategies(raw, tags_of={"005930": ["FRGN"], "TQQQ": ["EVENT", "TREND"]})
    assert {s.id for s in built} == {"vol_breakout_cat"}
    assert built[0].symbols == ["005930"]
