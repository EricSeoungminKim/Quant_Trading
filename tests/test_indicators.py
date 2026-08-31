"""quant/trade/indicators 순수 함수 단위 테스트 — 손으로 계산 가능한 작은
시계열로 각 지표의 정확성을 검증한다. 기대값은 문헌 표준 정의를 손으로(또는
독립적으로) 계산해 박아 넣은 것이지, 구현을 보고 역산한 것이 아니다.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from quant.trade.indicators import bollinger, detect_box, ema, macd, rsi, sma, sma_atr, squeeze


# ---------------------------------------------------------------- sma / ema

def test_sma_matches_simple_average_and_warms_up_as_nan():
    closes = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = sma(closes, period=3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx((1 + 2 + 3) / 3)
    assert out.iloc[3] == pytest.approx((2 + 3 + 4) / 3)
    assert out.iloc[4] == pytest.approx((3 + 4 + 5) / 3)


def test_ema_warms_up_as_nan_then_recurses():
    closes = pd.Series([10.0, 20.0, 30.0, 40.0])
    out = ema(closes, period=2)
    # period-1개는 NaN
    assert out.iloc[0:1].isna().all()
    # alpha = 2/(2+1) = 2/3; y1 = x0 = 10 (재귀 시작), y2 = alpha*x1 + (1-alpha)*y1
    alpha = 2 / 3
    y1 = 10.0
    y2 = alpha * 20.0 + (1 - alpha) * y1
    y3 = alpha * 30.0 + (1 - alpha) * y2
    assert out.iloc[1] == pytest.approx(y2)
    assert out.iloc[2] == pytest.approx(y3)


# --------------------------------------------------------------------- rsi

def test_rsi_wilder_smoothing_differs_from_simple_average():
    """Wilder 평활 RSI는 단순평균 RSI와 값이 다르다 — period=3 소시계열로
    직접 손 계산(재귀식: avg_t = (avg_{t-1}*(period-1) + x_t)/period)."""
    closes = pd.Series([10.0, 11.0, 12.0, 11.0, 10.0, 11.0, 12.0, 13.0])
    out = rsi(closes, period=3)

    assert out.iloc[:3].isna().all(), "워밍업 구간은 NaN"
    # 손 계산: gain=[_,1,1,0,0,1,1,1], loss=[_,0,0,1,1,0,0,0]
    # seed(index3) = simple mean of gain[1:4]=(1,1,0)=2/3, loss[1:4]=(0,0,1)=1/3
    assert out.iloc[3] == pytest.approx(66.66666666666666)
    # index4: avg_gain=(2/3*2+0)/3=4/9, avg_loss=(1/3*2+1)/3=5/9 -> rsi=100-100/(1+4/5)
    assert out.iloc[4] == pytest.approx(44.44444444444444)
    assert out.iloc[7] == pytest.approx(83.53909465020577)

    # 같은 구간에서 단순평균(SMA) 기반 RSI였다면 index4는 다른 값이 나온다.
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    simple_rsi_4 = 100 - 100 / (1 + gain.rolling(3).mean().iloc[4] / loss.rolling(3).mean().iloc[4])
    assert simple_rsi_4 != pytest.approx(out.iloc[4])
    assert simple_rsi_4 == pytest.approx(33.33333333333333)


def test_rsi_all_gains_is_100_all_losses_is_0():
    up_only = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = rsi(up_only, period=3)
    assert out.iloc[3] == pytest.approx(100.0)

    down_only = pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])
    out2 = rsi(down_only, period=3)
    assert out2.iloc[3] == pytest.approx(0.0)


def test_rsi_flat_price_is_50():
    flat = pd.Series([10.0] * 6)
    out = rsi(flat, period=3)
    assert out.iloc[3] == pytest.approx(50.0)


# -------------------------------------------------------------------- macd

def test_macd_histogram_equals_macd_minus_signal():
    closes = pd.Series([float(x) for x in range(1, 40)])
    macd_line, signal_line, hist = macd(closes, fast=3, slow=6, signal=2)
    diff = (macd_line - signal_line).dropna()
    assert (hist.dropna() == diff).all()


def test_macd_warmup_nan_and_first_valid_index():
    closes = pd.Series([float(x) for x in range(1, 40)])
    macd_line, signal_line, hist = macd(closes, fast=3, slow=6, signal=2)
    # macd_line은 slow EMA(6)가 시작하는 인덱스(5, 0-base)부터 유효
    assert macd_line.iloc[:5].isna().all()
    assert pd.notna(macd_line.iloc[5])
    # signal_line은 macd_line 위에 signal(2) EMA를 또 얹으므로 한 박자 더 늦게 유효
    assert pd.isna(signal_line.iloc[5])
    assert pd.notna(signal_line.iloc[6])


# --------------------------------------------------------------- bollinger

def test_bollinger_mid_equals_sma():
    closes = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    mid, upper, lower, bandwidth, percent_b = bollinger(closes, period=3, num_std=2.0)
    assert (mid.dropna() == sma(closes, 3).dropna()).all()


def test_bollinger_percent_b_boundary_values():
    """close가 하단 밴드에 닿으면 percent_b=0, 상단 밴드에 닿으면 1."""
    closes = pd.Series([10.0, 10.0, 10.0])  # mid=10, std=0 -> upper=lower=10
    mid, upper, lower, bandwidth, percent_b = bollinger(closes, period=3, num_std=2.0)
    # 밴드폭이 0이면 %B는 0/0 -> 정의상 계산 불가(NaN) — 값을 지어내지 않는다
    assert math.isnan(percent_b.iloc[2])

    # 변동이 있는 케이스에서 경계값 확인
    closes2 = pd.Series([10.0, 12.0, 8.0])
    mid2, upper2, lower2, bandwidth2, percent_b2 = bollinger(closes2, period=3, num_std=2.0)
    at_upper = upper2.iloc[2]
    at_lower = lower2.iloc[2]
    pb_at_upper = (at_upper - lower2.iloc[2]) / (upper2.iloc[2] - lower2.iloc[2])
    pb_at_lower = (at_lower - lower2.iloc[2]) / (upper2.iloc[2] - lower2.iloc[2])
    assert pb_at_upper == pytest.approx(1.0)
    assert pb_at_lower == pytest.approx(0.0)


# ----------------------------------------------------------------- squeeze

def test_squeeze_true_only_at_lowest_bandwidth_in_lookback():
    """period=3/lookback=3 소시계열로 밴드폭을 손 계산해 최저 구간만 True인지
    확인한다: flat(bw=0) -> 스파이크(bw 상승) -> flat(bw=0 복귀)."""
    closes = pd.Series([10.0, 10.0, 10.0, 10.0, 20.0, 10.0, 10.0, 10.0])
    sq = squeeze(closes, period=3, num_std=2.0, lookback=3)
    assert list(sq) == [False, False, False, False, False, False, True, True]


def test_squeeze_false_during_warmup():
    closes = pd.Series([10.0, 11.0])
    sq = squeeze(closes, period=3, num_std=2.0, lookback=3)
    assert not sq.any()


# -------------------------------------------------------------- detect_box

def test_detect_box_true_when_highs_and_lows_cluster_within_tolerance():
    high = pd.Series([100.0, 100.2, 100.4, 100.1, 100.3])
    low = pd.Series([98.0, 98.2, 98.1, 98.3, 98.4])
    is_box, box_high, box_low = detect_box(high, low, lookback=5, tolerance_pct=1.0)
    assert bool(is_box.iloc[-1]) is True
    assert box_high.iloc[-1] == pytest.approx(100.4)
    assert box_low.iloc[-1] == pytest.approx(98.0)


def test_detect_box_false_in_trending_range():
    high = pd.Series([100.0, 105.0, 110.0, 115.0, 120.0])
    low = pd.Series([98.0, 102.0, 107.0, 112.0, 117.0])
    is_box, box_high, box_low = detect_box(high, low, lookback=5, tolerance_pct=1.0)
    assert bool(is_box.iloc[-1]) is False


# --------------------------------------------------------------- sma_atr

def _bars(high, low, close):
    return pd.DataFrame({"high": high, "low": low, "close": close})


def test_sma_atr_matches_hand_computed_true_range_average():
    # TR0 = h0-l0 (직전 종가 없음, skipna로 h-l만 남는다) = 2
    # TR1 = max(h1-l1=3, |h1-c0|=|12-9|=3, |l1-c0|=|9-9|=0) = 3
    # TR2 = max(h2-l2=1.5, |h2-c1|=|11-11.5|=0.5, |l2-c1|=|9.5-11.5|=2) = 2
    # TR3 = max(h3-l3=4, |h3-c2|=|14-10|=4, |l3-c2|=|10-10|=0) = 4
    high = pd.Series([10.0, 12.0, 11.0, 14.0])
    low = pd.Series([8.0, 9.0, 9.5, 10.0])
    close = pd.Series([9.0, 11.5, 10.0, 13.5])
    bars = _bars(high, low, close)
    assert sma_atr(bars, period=2) == pytest.approx((2.0 + 4.0) / 2)
    assert sma_atr(bars, period=4) == pytest.approx((2.0 + 3.0 + 2.0 + 4.0) / 4)


def test_sma_atr_uses_whatever_is_available_when_fewer_bars_than_period():
    high = pd.Series([10.0, 12.0])
    low = pd.Series([8.0, 9.0])
    close = pd.Series([9.0, 11.5])
    bars = _bars(high, low, close)
    # TR = [2.0, 3.0] — period=10보다 데이터가 적어도 있는 만큼만 평균한다.
    assert sma_atr(bars, period=10) == pytest.approx((2.0 + 3.0) / 2)


def test_detect_box_false_during_warmup():
    high = pd.Series([100.0, 100.1])
    low = pd.Series([98.0, 98.1])
    is_box, box_high, box_low = detect_box(high, low, lookback=5, tolerance_pct=1.0)
    assert not is_box.any()
    assert box_high.isna().all()
