"""백테스트 결정론 불변식 — run_backtest를 완전히 동일한 입력으로 두 번 돌리면
equity curve/trades/metrics이 전부 동일해야 한다.

이 불변식이 지키는 것: StubDataFeed는 seed 고정 합성 데이터라 "데이터 자체는"
결정론적이라고 문서화돼 있지만(quant/adapters/data/stub.py), 그걸 소비하는 리플레이
엔진(quant/backtest/engine.py)이 실행 시각이나 우발적인 공유 가변 상태에
의존하면 전체 파이프라인은 결정론이 깨질 수 있다. 백테스트로 전략 파라미터를
비교/최적화하려면 "같은 입력 -> 같은 출력"이 전제조건이다 — 이게 깨지면 두 실행
결과의 차이가 파라미터 때문인지 우연 때문인지 구분할 수 없게 된다.
"""
from __future__ import annotations

import pandas as pd

from quant.backtest.engine import run_backtest


def test_run_backtest_is_deterministic_across_repeated_runs():
    result_a = run_backtest(strategy_id="donchian", days=20, interval="15m", source="stub")
    result_b = run_backtest(strategy_id="donchian", days=20, interval="15m", source="stub")

    pd.testing.assert_series_equal(result_a.equity_curve, result_b.equity_curve)
    pd.testing.assert_frame_equal(result_a.trades, result_b.trades)
    assert result_a.metrics == result_b.metrics


def test_determinism_check_is_meaningful_because_trades_actually_happen():
    """거래가 0건이면 위 결정론 비교가 사실상 빈 데이터끼리의 비교가 되어 의미가
    옅어진다 — 실제로 체결이 발생하는 조건에서 검증하고 있음을 sanity-check한다."""
    result = run_backtest(strategy_id="donchian", days=20, interval="15m", source="stub")
    assert result.metrics["n_trades"] > 0


def test_different_day_counts_are_not_trivially_identical():
    """위 결정론 테스트가 "뭘 넣어도 같은 결과가 나오는" 깨진 캐시 때문에 우연히
    통과하는 게 아님을 방증한다 — 입력(days)이 바뀌면 실제로 다른 결과가 나와야 한다."""
    short_result = run_backtest(strategy_id="donchian", days=10, interval="15m", source="stub")
    long_result = run_backtest(strategy_id="donchian", days=20, interval="15m", source="stub")

    assert len(short_result.equity_curve) != len(long_result.equity_curve)
