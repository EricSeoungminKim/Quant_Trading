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


def test_enabled_in_real_config_now_that_tag_wiring_is_done():
    """config/settings.yaml의 실제 선언 — 2026-08-17 서브프로젝트 T Task 1~3에서
    EVENT_SCALP/FRGN/FRGN_EXIT 태그 배선(engine.json/frgn_flow.jsonl → market_brief
    → brief_from_report TOKENS → watch-score/watch-add)이 끝나 두 전략을
    `enabled: true`로 올렸다(이전에는 태그 배선 전이라 켜면 조용히 아무 것도 안
    하는 전략이 됐다 — 이제는 tags_of가 실제로 채워진다)."""
    from quant.apps.config import load_settings

    settings = load_settings()
    strat_cfg = settings.raw["strategies"]
    # 2026-08-25 전략 4종 체제(소유자 지시): ①news_momentum ②scalp_1m
    # ③close_bet(신규) ④frgn_accumulate. news_scalp 은 ①과 겹쳐 비활성으로
    # 내려갔다(코드·원장은 보존 — 측정 기준점).
    assert strat_cfg["news_scalp"]["enabled"] is False
    assert strat_cfg["frgn_accumulate"]["enabled"] is True
    assert strat_cfg["close_bet"]["enabled"] is True
    built = build_strategies(settings.raw)
    ids = {s.id for s in built}
    assert ids == {"news_momentum", "scalp_1m", "close_bet", "frgn_accumulate"}, (
        "활성 전략은 정확히 4종 체제여야 한다 — 늘리려면 소유자 결정"
    )
