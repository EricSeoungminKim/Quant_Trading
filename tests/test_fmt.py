"""quant/trade/fmt.py — 전략 사유 문자열의 가격 표시 헬퍼.

기존 `%.4g` 공학 표기(예: "2.178e+05")를 사람이 읽는 형식으로 바꾸는 것이
목적이므로, 지수 표기가 나올 만한 큰/작은 값에서도 깨지지 않는지 고정한다.
"""
from __future__ import annotations

from quant.trade.fmt import fmt_price


def test_kr_price_has_no_decimals_and_uses_comma():
    assert fmt_price(217_800.0, "005930") == "217,800"


def test_us_price_has_two_decimals():
    assert fmt_price(123.456, "TQQQ") == "123.46"


def test_kr_large_value_does_not_use_scientific_notation():
    # %.4g였다면 "2.178e+05"로 나왔을 값.
    assert "e" not in fmt_price(217_800.0, "005930")
