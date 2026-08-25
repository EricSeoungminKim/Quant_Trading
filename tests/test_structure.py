"""시장 구조 층(`quant/trade/structure.py`) — 손계산 값으로 고정 (2026-08-25).

이 층이 틀리면 그 위의 모든 전략이 같은 방향으로 틀린다 — 값을 전부 손으로
계산해 박는다.
"""
from __future__ import annotations

import pandas as pd
import pytest

from quant.trade.structure import (
    StructureBracket, broke_prior_high, broke_prior_low, ma_alignment,
    moving_averages, nearest_resistance, nearest_support, structure_bracket,
    swing_points, trend_slope, williams_r,
)


def _bars(closes, highs=None, lows=None):
    n = len(closes)
    return pd.DataFrame({
        "open": closes,
        "high": highs if highs is not None else [c + 1 for c in closes],
        "low": lows if lows is not None else [c - 1 for c in closes],
        "close": closes,
        "volume": [100.0] * n,
    }, index=pd.RangeIndex(n))


# ── 이동평균 ─────────────────────────────────────────────────────────────

def test_moving_averages_hand_computed_and_honest_about_shortage():
    close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    mas = moving_averages(close, periods=(3, 5, 10))
    assert mas[3] == pytest.approx((4 + 5 + 6) / 3)
    assert mas[5] == pytest.approx((2 + 3 + 4 + 5 + 6) / 5)
    assert mas[10] is None, "봉 6개로 10일선을 지어내면 안 된다"


def test_ma_alignment_verdicts():
    assert ma_alignment({5: 10.0, 20: 9.0, 50: 8.0}) == "정배열"
    assert ma_alignment({5: 8.0, 20: 9.0, 50: 10.0}) == "역배열"
    assert ma_alignment({5: 10.0, 20: 8.0, 50: 9.0}) == "혼조"
    assert ma_alignment({5: 10.0, 20: None, 50: None}) == "판정불가"


# ── Williams %R ──────────────────────────────────────────────────────────

def test_williams_r_hand_computed():
    # 14봉: 최고 110, 최저 90, 종가 105 → (110-105)/(110-90)*-100 = -25
    closes = [100.0] * 13 + [105.0]
    highs = [110.0] + [100.0] * 13
    lows = [90.0] + [100.0] * 13
    assert williams_r(_bars(closes, highs, lows), period=14) == pytest.approx(-25.0)


def test_williams_r_refuses_flat_range_and_short_sample():
    flat = _bars([100.0] * 14, [100.0] * 14, [100.0] * 14)
    assert williams_r(flat) is None, "레인지 0에서 극단값을 지어내지 않는다"
    assert williams_r(_bars([100.0] * 5)) is None


# ── 스윙 고점/저점 (전고·전저) ───────────────────────────────────────────

def _swing_frame():
    #        0    1    2    3    4     5    6    7    8    9   10
    highs = [10, 11, 12, 15, 12, 11, 13, 17, 13, 12, 11]
    lows = [9, 8, 7, 9, 8, 6, 8, 10, 9, 8, 7]
    closes = [(h + l) / 2 for h, l in zip(highs, lows)]
    return _bars(closes, highs, lows)


def test_swing_points_finds_local_extremes_excluding_unconfirmed_tail():
    highs, lows = swing_points(_swing_frame(), wing=2)
    assert highs == [15.0, 17.0]  # 3번(15), 7번(17) — 마지막 2봉은 미확정이라 제외
    assert lows == [7.0, 6.0]     # 2번(7), 5번(6)


def test_support_resistance_nearest():
    highs, lows = swing_points(_swing_frame(), wing=2)
    assert nearest_support(8.0, lows) == 7.0
    assert nearest_resistance(16.0, highs) == 17.0
    assert nearest_resistance(18.0, highs) is None, "신고가 영역 — 저항을 지어내지 않는다"


def test_breakout_and_breakdown():
    df = _swing_frame()
    up = df.copy(); up.loc[10, "close"] = 18.0   # 전고 17 돌파
    dn = df.copy(); dn.loc[10, "close"] = 5.0    # 전저 6 이탈
    assert broke_prior_high(up, wing=2) is True
    assert broke_prior_low(up, wing=2) is False
    assert broke_prior_low(dn, wing=2) is True


# ── 빗각(추세 기울기) ────────────────────────────────────────────────────

def test_trend_slope_sign_and_flat():
    up = pd.Series([100 + i for i in range(20)])
    dn = pd.Series([120 - i for i in range(20)])
    flat = pd.Series([100.0] * 20)
    assert trend_slope(up) > 0
    assert trend_slope(dn) < 0
    assert trend_slope(flat) == pytest.approx(0.0)
    assert trend_slope(pd.Series([1.0] * 5), lookback=20) is None


# ── 구조 브래킷 (손절=지지 아래, 익절=전고) ──────────────────────────────

def test_bracket_stop_below_support_and_partial_at_resistance():
    # 진입 7.05, 지지(전저) 7.0 — 거리 0.7% < hard cap 3% → 구조 손절이 잡힌다.
    br = structure_bracket(7.05, _swing_frame(), wing=2, stop_buffer_pct=0.0)
    assert isinstance(br, StructureBracket)
    assert br.stop == pytest.approx(7.0)
    assert br.partial_target == pytest.approx(15.0)  # 위쪽 가장 가까운 전고
    assert br.stop_basis == "swing_low"


def test_bracket_hard_cap_limits_deep_support():
    """지지가 3% 밖이면 hard cap — 구조라는 이유로 크게 잃지 않는다."""
    br = structure_bracket(100.0, _swing_frame(), wing=2, hard_cap_pct=3.0)
    assert br.stop == pytest.approx(97.0)
    assert br.stop_basis == "hard_cap"


def test_bracket_refuses_when_no_support_visible():
    """지지가 안 보이는 자리 = 손절선을 정할 수 없는 자리 — 진입 금지 신호."""
    rising = _bars([float(i) for i in range(1, 12)])  # 단조 상승: 스윙 저점 없음
    assert structure_bracket(0.5, rising, wing=2) is None
