"""`quant/core/timeseries.py` — 자본 곡선 성과 수학 (gs-quant 대조 도입, 2026-08-24).

손계산 가능한 값으로 고정한다 — 이 모듈이 틀리면 성과 보고 전체가 틀린다.
"""
from __future__ import annotations

import math

import pytest

from quant.core.timeseries import (
    annualized_volatility, cagr, max_drawdown, performance_summary,
    sharpe_ratio_rf0, simple_returns,
)


def test_simple_returns_hand_computed():
    assert simple_returns([100.0, 110.0, 99.0]) == pytest.approx([0.10, -0.10])


def test_simple_returns_rejects_corrupt_curve():
    """자본이 0 이하 = 데이터 손상. 조용히 건너뛰면 그럴듯한 왜곡이 나온다."""
    with pytest.raises(ValueError):
        simple_returns([100.0, 0.0, 110.0])


def test_max_drawdown_hand_computed():
    # 100 → 120(peak) → 90: 90/120-1 = -25%
    assert max_drawdown([100.0, 120.0, 90.0, 100.0]) == pytest.approx(-0.25)


def test_max_drawdown_monotonic_up_is_zero():
    assert max_drawdown([100.0, 101.0, 102.0]) == pytest.approx(0.0)


def test_volatility_hand_computed():
    # 수익률 [+10%, -10%]: 평균 0, 표본분산 = (0.01+0.01)/1 = 0.02
    expected = math.sqrt(0.02) * math.sqrt(252)
    assert annualized_volatility([100.0, 110.0, 99.0]) == pytest.approx(expected)


def test_sharpe_none_when_volatility_zero():
    """무변동 곡선에 무한대 샤프를 지어내지 않는다."""
    assert sharpe_ratio_rf0([100.0, 101.0, 102.5]) is not None  # 수익률이 서로 달라 sd>0
    assert sharpe_ratio_rf0([100.0, 100.0, 100.0]) is None  # 수익률 전부 0 → sd 0


def test_cagr_doubles_in_a_year():
    # 252 간격에 2배 → CAGR 100%
    vals = [100.0, 200.0]
    assert cagr(vals, n_periods=252) == pytest.approx(1.0)


def test_summary_reports_n_points_always():
    assert performance_summary([100.0]) == {"n_points": 1}
    s = performance_summary([100.0, 110.0, 99.0])
    assert s["n_points"] == 3
    assert s["total_return"] == pytest.approx(-0.01)
    assert s["max_drawdown"] == pytest.approx(-0.10)
