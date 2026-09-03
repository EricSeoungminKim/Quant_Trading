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
    # **2026-09-03 재활성**: 촉매 A/B 의 "after" 갈래로 다시 켰다(KR 0.04) —
    # EVENT_SCALP 태그가 붙은 종목만 개장 즉시 진입하는 전략이라, 그 자체가
    # "촉매 있는 종목만 사면 더 버는가"에 대한 한 각도의 답이다.
    assert strat_cfg["news_scalp"]["enabled"] is True
    # 2026-09-03 소유자 결정: 자동매매는 단타·스캘핑만 — frgn_accumulate/close_bet/
    # overnight_drift/rsi2_dip(전부 오버나이트·다일 보유가 전략 정의)는 비활성으로
    # 내려갔다. 오버나이트/장기 아이디어는 이제 quant/analyze/manual_recs.py(텔레그램
    # 추천) 레인으로 간다 — 코드·params·capital_fraction은 보존(측정 기준점 +
    # 추후 복원, 자본 재분배 없음).
    #
    # **정당한 승격이 이 assert 넷 중 하나를 깰 수 있다** — `quant.apps.cli promote`
    # (quant/control/promotion.py, 2026-09-03)가 백테스트 게이트 GO를 반영하면
    # 해당 전략의 `enabled`가 `false → true`로 뒤집힌다. 이 테스트는 그걸 회귀로
    # 오인해 막으면 안 된다: 실제로 그 전략을 승격했다면(백테스트 게이트 GO +
    # `promote` 실행이 config/settings.yaml에 반영된 게 맞다면) **이 테스트를 같은
    # 커밋에서 그 전략의 기대값을 `True`로 고치고, 위 소유자 결정 이력에 승격
    # 근거(게이트 파일 경로 등)를 남긴다** — assert를 지우거나 완화하지 않는다
    # (여기 없는 전략을 새로 승격했을 때도 동일한 절차: 그 전략용 assert를
    # 추가한다). 반대로 이 assert가 실패했는데 승격한 기억이 없다면 그건 진짜
    # 회귀다(누군가 settings.yaml을 실수로 건드렸거나 promote를 잘못된 대상에
    # 돌렸다는 뜻).
    assert strat_cfg["frgn_accumulate"]["enabled"] is False
    assert strat_cfg["close_bet"]["enabled"] is False
    assert strat_cfg["overnight_drift"]["enabled"] is False
    assert strat_cfg["rsi2_dip"]["enabled"] is False
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
    # 2026-08-30: **소유자 승인으로 12종 체제** — llm_trader 추가. "LLM 자체에게
    # 전략과 판단을 맡기는 게 하나의 전략이 되는 것, 똑같이 1,000만원 모의, 기존
    # 시스템 위에, 한 달 테스트." 다른 11개와 달리 사람이 정한 규칙이 아니라 LLM
    # 판단 자체를 실험하는 레인이라 다중검정(--trials) 모집단에는 넣지 않는다 —
    # 같은 규칙을 반복 시행해 우연한 승자를 고르는 문제(스캘핑 8개 레인)와는
    # 성격이 다르다(quant/trade/strategy/llm_trader.py 모듈 docstring).
    # 2026-09-03: **촉매 A/B 로 16종** — news_scalp 재활성 + `<id>_cat` 3개
    # (scalp_1m / pullback_impulse / vol_breakout). `_cat` 은 새 전략이 아니라
    # **같은 클래스의 다른 유니버스 갈래**다(params 를 YAML 앵커로 공유한다 —
    # tests/e2e/test_assembly.py 가 대조). 그래도 원장에는 독립 레인으로 쌓이므로
    # 시행 횟수는 재신고한다: 스캘핑/단기 레인 8 → **12**
    # (`strategy-report --trials 12`). 판정이 나면 진 갈래를 지워 다시 줄인다.
    # 2026-09-03(같은 날 저녁, 소유자 결정 갱신) — 자동매매는 단타·스캘핑만:
    # close_bet/frgn_accumulate/overnight_drift/rsi2_dip 4종을 비활성화했다(전부
    # 오버나이트·다일 보유가 전략 정의라 새 방침과 충돌 — 코드는 보존, 오버나이트
    # 아이디어는 manual_recs 레인으로 이동). 활성 12종.
    assert ids == {
        "news_momentum", "scalp_1m",
        "pullback_impulse", "mr_vwap_quiet",
        "vol_breakout", "intraday_momentum", "gap_fade",
        "llm_trader", "news_scalp",
        "scalp_1m_cat", "pullback_impulse_cat", "vol_breakout_cat",
    }, "활성 전략 목록이 바뀌었다 — 늘리려면 소유자 결정 + 시행 횟수 재신고"
