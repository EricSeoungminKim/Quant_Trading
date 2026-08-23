from __future__ import annotations

import math

from quant.analyze.indicators import cum_return, describe, disparity, range_position, realized_vol, sma


def test_sma():
    assert sma([1, 2, 3, 4, 5], 5) == 3.0
    assert sma([1, 2], 5) is None


def test_disparity():
    assert disparity(110, 100) == 10.0
    assert disparity(100, None) is None
    assert disparity(100, 0) is None


def test_range_position():
    assert range_position([10, 20, 30]) == 100.0
    assert range_position([30, 20, 10]) == 0.0
    assert range_position([10, 10, 10]) is None


def test_cum_return():
    assert cum_return([100, 110], 1) == 10.0
    assert cum_return([100], 1) is None


def test_realized_vol_flat_series_is_zero():
    flat = [100.0] * 25
    assert realized_vol(flat, 20) == 0.0


def test_realized_vol_insufficient_data_is_none():
    assert realized_vol([100.0] * 10, 20) is None


def test_realized_vol_positive_for_moving_series():
    values = [100 + (i % 3) for i in range(25)]
    vol = realized_vol(values, 20)
    assert vol is not None
    assert vol > 0


def test_describe_missing_history_returns_all_none():
    for quote in ({}, {"history": []}):
        result = describe(quote)
        assert all(v is None for v in result.values())


def test_describe_no_nan():
    quote = {"history": [100 + (i % 5) for i in range(60)]}
    result = describe(quote)
    for k, v in result.items():
        if isinstance(v, float):
            assert not math.isnan(v), f"{k} is NaN"


def test_describe_no_nan_full_year_history():
    """252개(1년치) 데이터에서도 NaN 이 없는지 — ma120/pos_52w/ret_60d 포함."""
    quote = {"history": [100 + (i % 7) for i in range(252)]}
    result = describe(quote)
    for k, v in result.items():
        if isinstance(v, float):
            assert not math.isnan(v), f"{k} is NaN"


def test_describe_above_ma20_matches_comparison():
    quote = {"history": list(range(1, 61))}  # 우상향 → close(60) > ma20
    result = describe(quote)
    close = quote["history"][-1]
    assert result["above_ma20"] == (close > result["ma20"])


def test_describe_ma120_and_ret_60d_need_enough_data():
    short = {"history": [100 + (i % 5) for i in range(60)]}
    result = describe(short)
    assert result["ma120"] is None
    assert result["disparity120"] is None
    assert result["above_ma120"] is None
    assert result["ret_60d"] is None

    long_enough = {"history": [100 + (i % 5) for i in range(120)]}
    result = describe(long_enough)
    assert result["ma120"] is not None
    assert result["ret_60d"] is not None


def test_describe_pos_52w_present_even_under_252_days():
    """252개 미만(상장 초기 등)이어도 pos_52w 는 None 이 아니라 있는 만큼으로 계산돼야 한다."""
    quote = {"history": [100 + (i % 5) for i in range(60)]}
    result = describe(quote)
    assert result["pos_52w"] is not None


def test_describe_pos_52w_uses_last_252_only():
    # 앞부분(30~130)이 최고가여도 최근 252개 밖이면 위치 계산에 영향을 주면 안 된다.
    old_high = [130.0] * 10
    recent = [100 + (i % 5) for i in range(252)]
    quote = {"history": old_high + recent}
    result = describe(quote)
    expected = describe({"history": recent})["pos_52w"]
    assert result["pos_52w"] == expected
