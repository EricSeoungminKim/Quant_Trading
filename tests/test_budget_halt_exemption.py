"""콜드 페치 예산 스킵 ≠ 전략 오류 — 자동 정지 오발 방지 회귀 가드.

2026-08-31 실사고: 예산 스로틀(ColdFetchBudgetExceeded)이 일반 전략 오류로
집계돼 "포지션 있는 전략 오류 3연속 → 자동 정지"를 오발했다. 금요일 밤 정지가
주말을 건너 월요일 KR 세션 전체를 무체결로 만들었고, 수동 재개 3분 만에
재정지됐다. 이 테스트는 그 구분을 고정한다: 예산 예외는 strategy_errors 에
집계되지 않고(→ 정지 승격 없음), 진짜 DataSourceError 는 여전히 집계된다.
"""
from __future__ import annotations

import logging

from quant.core.ports import ColdFetchBudgetExceeded, DataSourceError
from quant.trade.loop import run_cycle, CycleTimings


class _BudgetStrategy:
    id = "budget_victim"
    symbols = ["069500"]

    def on_cycle(self, ctx):
        raise ColdFetchBudgetExceeded("콜드 페치 예산 초과 (8/사이클, 069500 5m) — 다음 사이클")


class _BrokenStrategy:
    id = "really_broken"
    symbols = ["069500"]

    def on_cycle(self, ctx):
        raise DataSourceError("소스가 진짜로 죽었다")


class _NullSinks:
    def on_signal(self, s): pass
    def on_order(self, o): pass
    def on_fill(self, f): pass


class _Ctx:  # run_cycle 이 실제로 쓰는 최소 표면만
    pass


def _run(strategy):
    timings = CycleTimings()
    run_cycle([strategy], _Ctx(), None, _NullSinks(), timings=timings)
    return timings


def test_budget_exceeded_is_not_a_strategy_error(caplog):
    with caplog.at_level(logging.INFO):
        timings = _run(_BudgetStrategy())
    assert "budget_victim" not in timings.strategy_errors, (
        "예산 스킵이 strategy_errors 에 집계되면 자동 정지 오발이 재발한다"
    )
    assert any("예산" in r.message for r in caplog.records)


def test_real_data_source_error_still_escalates():
    timings = _run(_BrokenStrategy())
    assert "really_broken" in timings.strategy_errors
    assert "DataSourceError" in timings.strategy_errors["really_broken"]
