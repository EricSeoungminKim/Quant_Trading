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
    # 2026-08-28 **소유자 결정으로 6종 체제**(이 테스트가 요구하던 "늘리려면 소유자
    # 결정"이 실제로 일어났다): 스캘핑 병렬 실험 — "스캘핑 전략을 여러 개 병렬로
    # 돌려서, 계속 돌려봤을 때 수익이 나는 전략을 찾자". 추가된 둘은 우리 원장
    # 실측에서 나온 가설이다 — pullback_impulse(손절 후 76% 회복 → 눌림에서 산다),
    # mr_vwap_quiet(고RVOL 역상관 -0.46 → 조용한 종목만 노린다).
    #
    # **이 집합이 늘면 다중검정 시행 횟수도 는다**: 성적을 볼 때
    # `strategy-report --trials <활성 스캘핑 수>` 로 신고해 Deflated Sharpe 보정을
    # 받아야 한다(quant/backtest/statistics.py). 이 테스트가 계속 정확한 수를
    # 강제하는 이유이기도 하다.
    # 2026-08-28 저녁: overnight_drift 추가(소유자 위임 "알아서 수익률 높게" —
    # 문헌·비용 근거 최우위 + 일중 전략들의 벤치마크 역할). 7종 체제.
    # 2026-08-29 새벽: **소유자 결정으로 11종 체제** — "대회인데 더 다양한 전략들을
    # 웹서치로 근거를 찾고 다 시도해보자. 하락장 전략, 상승장 전략 등을 단타
    # 기준으로 다 긁어서 병렬로 돌려야 의미가 있지". 추가 4종은 전부 외부 문헌
    # 근거(각 모듈 docstring 에 출처·한계 명시): vol_breakout(Larry Williams),
    # intraday_momentum(SSRN 4824172 — 하락장 레인, 인버스 ETF 매수로 표현),
    # gap_fade(야간-주간 반전 문헌, 근거 혼재 고지), rsi2_dip(Connors, 오버나이트).
    # 시행 횟수 재신고: 스캘핑/단기 레인 8개 → `strategy-report --trials 8`.
    assert ids == {
        "news_momentum", "scalp_1m", "close_bet", "frgn_accumulate",
        "pullback_impulse", "mr_vwap_quiet", "overnight_drift",
        "vol_breakout", "intraday_momentum", "gap_fade", "rsi2_dip",
    }, "활성 전략 목록이 바뀌었다 — 늘리려면 소유자 결정 + 시행 횟수 재신고"
