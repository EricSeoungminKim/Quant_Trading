"""quant/trade/indicators/trend_gate.py 단위 테스트 — QuantConnect #407(ADX+DI
추세 필터)/#478(ATR 변동성 억제기) 이식분. adx_di/atr_ratio는 손으로 계산 가능한
작은 시계열로 정확성을(기대값은 표준 Wilder 정의를 손으로 계산해 박아 넣은 것),
trend_ok/volatility_ok는 합성 상승추세/횡보 시퀀스로 방향성 판정을 검증한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.trade.indicators.trend_gate import adx_di, atr_ratio, trend_ok, volatility_ok


# ------------------------------------------------------------------- adx_di

def test_adx_di_hand_calculated_small_series():
    """period=2, 정확히 최소 봉 수(2*period=4)로 손 계산 값과 일치하는지 확인.

    손 계산(Wilder 평활, 시드=values[1:period+1] 단순평균):
    tr=[_,3,1,2.5], plus_dm=[_,2,0,2], minus_dm=[_,0,0,0]
    smoothed_tr[2]=mean(3,1)=2.0, smoothed_tr[3]=(2.0*1+2.5)/2=2.25
    smoothed_plus_dm[2]=mean(2,0)=1.0, smoothed_plus_dm[3]=(1.0*1+2)/2=1.5
    smoothed_minus_dm 항상 0
    dx[2]=100*|50-0|/50=100.0, dx[3]=100*|66.667-0|/66.667=100.0
    adx=mean(dx[2],dx[3])=100.0
    """
    bars = pd.DataFrame({
        "high": [10.0, 12.0, 11.0, 13.0],
        "low": [8.0, 9.0, 10.0, 11.0],
        "close": [9.0, 11.0, 10.5, 12.5],
    })
    result = adx_di(bars, period=2)
    assert result is not None
    adx, plus_di, minus_di = result
    assert adx == pytest.approx(100.0)
    assert plus_di == pytest.approx(200 / 3)
    assert minus_di == pytest.approx(0.0)


def test_adx_di_insufficient_bars_returns_none():
    bars = pd.DataFrame({
        "high": [10.0, 12.0, 11.0],
        "low": [8.0, 9.0, 10.0],
        "close": [9.0, 11.0, 10.5],
    })
    assert adx_di(bars, period=2) is None  # 2*period=4 봉 필요, 3개뿐


def test_adx_di_none_when_bars_none():
    assert adx_di(None) is None


def test_adx_di_none_on_nan_in_bars():
    bars = pd.DataFrame({
        "high": [10.0, 12.0, np.nan, 13.0],
        "low": [8.0, 9.0, 10.0, 11.0],
        "close": [9.0, 11.0, 10.5, 12.5],
    })
    assert adx_di(bars, period=2) is None


def test_adx_di_strong_uptrend_high_adx_and_plus_di_dominant():
    """뚜렷한 상승 추세(고가/저가/종가가 매일 같은 폭으로 상승) → ADX 높음,
    +DI가 -DI를 압도한다(하락 방향 움직임이 전혀 없으므로 -DI≈0)."""
    n = 60
    close = np.linspace(100.0, 160.0, n)
    bars = pd.DataFrame({"high": close + 1.0, "low": close - 1.0, "close": close})
    result = adx_di(bars, period=14)
    assert result is not None
    adx, plus_di, minus_di = result
    assert adx >= 25.0
    assert plus_di > minus_di


def test_adx_di_sideways_low_adx():
    """횡보(좁은 박스권 진동) → ADX가 상승 추세보다 뚜렷이 낮다."""
    n = 60
    x = np.linspace(0, 20, n)
    close_flat = 100 + np.sin(x) * 0.5
    bars_flat = pd.DataFrame({
        "high": close_flat + 0.3, "low": close_flat - 0.3, "close": close_flat,
    })
    close_up = np.linspace(100.0, 160.0, n)
    bars_up = pd.DataFrame({"high": close_up + 1.0, "low": close_up - 1.0, "close": close_up})

    adx_flat, _, _ = adx_di(bars_flat, period=14)
    adx_up, _, _ = adx_di(bars_up, period=14)
    assert adx_flat < adx_up


# ----------------------------------------------------------------- atr_ratio

def test_atr_ratio_hand_calculated_small_series():
    """period=2, 정확히 최소 봉 수(period+1=3)로 손 계산 값과 일치하는지 확인.

    tr=[_,3,1] (첫 값 제외), atr=mean(3,1)=2.0, last_price=10.5 → ratio=2/10.5.
    """
    bars = pd.DataFrame({
        "high": [10.0, 12.0, 11.0],
        "low": [8.0, 9.0, 10.0],
        "close": [9.0, 11.0, 10.5],
    })
    ratio = atr_ratio(bars, period=2)
    assert ratio == pytest.approx(2.0 / 10.5)


def test_atr_ratio_insufficient_bars_returns_none():
    bars = pd.DataFrame({"high": [10.0, 12.0], "low": [8.0, 9.0], "close": [9.0, 11.0]})
    assert atr_ratio(bars, period=2) is None  # period+1=3 봉 필요, 2개뿐


def test_atr_ratio_none_on_non_positive_price():
    bars = pd.DataFrame({
        "high": [10.0, 12.0, 11.0],
        "low": [8.0, 9.0, 10.0],
        "close": [9.0, 11.0, 0.0],
    })
    assert atr_ratio(bars, period=2) is None


def test_atr_ratio_none_when_bars_none():
    assert atr_ratio(None) is None


# ---------------------------------------------------------- trend_ok / volatility_ok

def test_trend_ok_true_on_missing_data_gate_absence_is_default_behavior():
    """봉 부족 → adx_di가 None → trend_ok는 True(게이트 부재=기존 동작,
    모듈 docstring 명시)."""
    bars = pd.DataFrame({"high": [10.0], "low": [8.0], "close": [9.0]})
    assert trend_ok(bars) is True


def test_trend_ok_false_when_adx_below_min():
    n = 60
    x = np.linspace(0, 20, n)
    close_flat = 100 + np.sin(x) * 0.5
    bars_flat = pd.DataFrame({
        "high": close_flat + 0.3, "low": close_flat - 0.3, "close": close_flat,
    })
    adx, _, _ = adx_di(bars_flat, period=14)
    assert trend_ok(bars_flat, adx_min=adx + 1.0) is False


def test_trend_ok_true_when_adx_and_di_pass():
    n = 60
    close_up = np.linspace(100.0, 160.0, n)
    bars_up = pd.DataFrame({"high": close_up + 1.0, "low": close_up - 1.0, "close": close_up})
    assert trend_ok(bars_up, adx_min=25.0, require_di=True) is True


def test_trend_ok_false_when_require_di_and_di_direction_wrong():
    """ADX는 충족해도 -DI가 +DI를 앞서면(하락 추세) require_di=True에서 거부."""
    n = 60
    close_down = np.linspace(160.0, 100.0, n)
    bars_down = pd.DataFrame({"high": close_down + 1.0, "low": close_down - 1.0, "close": close_down})
    adx, plus_di, minus_di = adx_di(bars_down, period=14)
    assert minus_di > plus_di
    assert trend_ok(bars_down, adx_min=25.0, require_di=True) is False


def test_volatility_ok_true_on_missing_data():
    bars = pd.DataFrame({"high": [10.0], "low": [8.0], "close": [9.0]})
    assert volatility_ok(bars) is True


def test_volatility_ok_boundary_at_threshold():
    """volatility_ok는 atr_ratio를 기본 period(14)로 호출하므로 최소 15개 봉이
    필요하다 — 부족하면 게이트 부재(True)로 빠져 경계 비교 자체가 성립하지
    않는다(위 test_volatility_ok_true_on_missing_data가 그 경로를 이미 고정)."""
    n = 20
    close = np.linspace(100.0, 110.0, n)
    bars = pd.DataFrame({"high": close + 1.0, "low": close - 1.0, "close": close})
    ratio = atr_ratio(bars)  # 기본 period=14 — volatility_ok와 동일 조건
    assert ratio is not None
    assert volatility_ok(bars, max_atr_ratio=ratio) is True  # 경계값(이하)은 통과
    assert volatility_ok(bars, max_atr_ratio=ratio - 0.001) is False  # 초과는 거부
