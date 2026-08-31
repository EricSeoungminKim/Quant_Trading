"""llm_trader 인박스 리더 — **재조립 경로 끝까지** 주입되는지 대조.

2026-08-31 실사고의 회귀 가드. `rebuild_strategies` 가 `inbox_reader` 파라미터를
받아 기본값까지 채워놓고 내부 `build_strategies(...)` 호출에서 넘기지 않아,
llm_trader 가 스텁 리더(항상 빈 목록)로 조립됐다 — LLM 판단 13건이 무체결로
증발했고, 전략 쪽 `None이면 빈 목록` 폴백이 실패를 무증상으로 만들었다.

기존 테스트는 `build_strategies` 를 직접 불러 이 한 단계 위의 누락을 못 잡았다.
그래서 이 테스트는 운영이 실제로 타는 함수(`rebuild_strategies`)를 부른다 —
"만든 것과 배선된 것은 다르다"는 이 저장소의 교훈을 배선의 끝단에서 검사한다.
"""
from __future__ import annotations

from quant.apps.assembly import _read_llm_trader_inbox, rebuild_strategies
from quant.trade.strategy.llm_trader import LlmTraderStrategy


def _minimal_cfg() -> dict:
    return {
        "universe": {"kr": [], "us": []},
        "strategies": {
            "llm_trader": {
                "class": "llm_trader", "enabled": True, "symbols": [],
                "capital_fraction": {"KR": 1.0, "US": 0.0},
                "validation": {"status": "burn_in"},
                "params": {},
            },
        },
    }


def _llm_instance(strategies) -> LlmTraderStrategy:
    [strat] = [s for s in strategies if isinstance(s, LlmTraderStrategy)]
    return strat


def test_rebuild_injects_default_inbox_reader_end_to_end():
    """운영 기본 경로: inbox_reader 를 안 넘기면 assembly 기본값(실제 파일
    리더)이 전략까지 도달해야 한다 — 스텁(빈 목록 람다)이면 실사고 재발이다."""
    strategies, _, _ = rebuild_strategies(_minimal_cfg())
    strat = _llm_instance(strategies)
    assert strat.inbox_reader is _read_llm_trader_inbox, (
        "rebuild_strategies → build_strategies 사이에서 inbox_reader 가 유실됐다 "
        "— llm_trader 가 스텁 리더로 조립되면 LLM 판단이 무증상으로 증발한다"
    )


def test_rebuild_passes_explicit_inbox_reader_through():
    """테스트/특수 배선용 명시 리더도 끝까지 전달돼야 한다."""
    marker_rows = [{"id": "x"}]
    reader = lambda: marker_rows  # noqa: E731
    strategies, _, _ = rebuild_strategies(_minimal_cfg(), inbox_reader=reader)
    strat = _llm_instance(strategies)
    assert strat.inbox_reader is reader
