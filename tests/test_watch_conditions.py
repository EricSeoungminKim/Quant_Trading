"""`quant/trade/watch_conditions.py` 단위 테스트 — 알림 전용 워치 조건 평가기.

`rsi()`(quant/trade/indicators) 자체의 정확성은 `tests/test_indicators.py`가
이미 검증한다 — 여기서는 그 값을 그대로 재사용해 evaluate()의 배선(임계 비교·
쿨다운·스킵·검증)만 확인한다.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pandas as pd
import pytest

from quant.core.models import Quote
from quant.trade.indicators import rsi
from quant.trade.watch_conditions import (
    Hit,
    Rule,
    apply_cooldown,
    evaluate,
    parse_rules,
)

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)


def _daily_bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D", tz="UTC")
    return pd.DataFrame({"close": closes}, index=idx)


# --------------------------------------------------------------- parse_rules

def test_parse_rules_rejects_unknown_metric():
    with pytest.raises(ValueError, match="metric"):
        parse_rules([{"name": "x", "symbol": "QQQ", "metric": "macd", "op": "<", "threshold": 10}])


def test_parse_rules_rejects_unknown_op():
    with pytest.raises(ValueError, match="op"):
        parse_rules([{"name": "x", "symbol": "QQQ", "metric": "rsi2", "op": "!=", "threshold": 10}])


def test_parse_rules_accepts_valid_rule():
    rules = parse_rules([{"name": "x", "symbol": "QQQ", "metric": "rsi2", "op": "<", "threshold": 10}])
    assert rules == [Rule(name="x", symbol="QQQ", metric="rsi2", op="<", threshold=10.0)]


# ------------------------------------------------------------------- rsi2

def test_rsi2_condition_fires_when_oversold():
    # 계속 하락하는 종가 -> avg_gain=0, avg_loss>0 -> RSI=0 (rsi() 정의 그대로).
    closes = [100.0 - i * 5 for i in range(10)]
    bars = _daily_bars(closes)
    rule = Rule(name="과매도", symbol="QQQ", metric="rsi2", op="<", threshold=10)
    hits = evaluate([rule], {"QQQ": Quote(symbol="QQQ", ts=NOW, price=50.0)}, {"QQQ": bars}, NOW)
    assert len(hits) == 1
    assert hits[0] == Hit(rule_name="과매도", symbol="QQQ", metric="rsi2", value=0.0, op="<", threshold=10.0)


def test_rsi2_condition_does_not_fire_when_not_oversold():
    # 계속 상승하는 종가 -> avg_loss=0 -> RSI=100 (rsi() 정의: mask(avg_loss==0, 100)).
    closes = [100.0 + i * 5 for i in range(10)]
    bars = _daily_bars(closes)
    rule = Rule(name="과매도", symbol="QQQ", metric="rsi2", op="<", threshold=10)
    hits = evaluate([rule], {"QQQ": Quote(symbol="QQQ", ts=NOW, price=150.0)}, {"QQQ": bars}, NOW)
    assert hits == []


def test_rsi2_value_matches_indicators_rsi_directly():
    """evaluate()가 quant.trade.indicators.rsi()를 그대로 재사용하는지 — 새
    RSI 구현을 만들지 않는다는 계약."""
    closes = [10.0, 11.0, 12.0, 11.0, 10.0, 11.0, 12.0, 13.0]
    bars = _daily_bars(closes)
    expected = float(rsi(pd.Series(closes), 2).iloc[-1])
    rule = Rule(name="r", symbol="QQQ", metric="rsi2", op=">", threshold=-1)  # 항상 발동
    hits = evaluate([rule], {"QQQ": Quote(symbol="QQQ", ts=NOW, price=13.0)}, {"QQQ": bars}, NOW)
    assert hits[0].value == pytest.approx(expected)


# --------------------------------------------------------------- change_pct

def test_change_pct_condition_fires_on_drop():
    bars = _daily_bars([100.0])  # 전일 종가 100
    rule = Rule(name="급락", symbol="TQQQ", metric="change_pct", op="<", threshold=-3.0)
    quote = Quote(symbol="TQQQ", ts=NOW, price=95.0)  # -5%
    hits = evaluate([rule], {"TQQQ": quote}, {"TQQQ": bars}, NOW)
    assert len(hits) == 1
    assert hits[0].value == pytest.approx(-5.0)


def test_change_pct_condition_does_not_fire_on_small_move():
    bars = _daily_bars([100.0])
    rule = Rule(name="급락", symbol="TQQQ", metric="change_pct", op="<", threshold=-3.0)
    quote = Quote(symbol="TQQQ", ts=NOW, price=99.0)  # -1%
    hits = evaluate([rule], {"TQQQ": quote}, {"TQQQ": bars}, NOW)
    assert hits == []


# --------------------------------------------------------------------- price

def test_price_condition_uses_quote_only_no_bars_needed():
    rule = Rule(name="가격", symbol="QQQ", metric="price", op="<", threshold=400)
    hits = evaluate([rule], {"QQQ": Quote(symbol="QQQ", ts=NOW, price=350.0)}, {}, NOW)
    assert len(hits) == 1
    assert hits[0].value == 350.0


# ------------------------------------------------------------ 데이터 없음 스킵

def test_missing_quote_skips_rule_silently():
    rule = Rule(name="r", symbol="QQQ", metric="price", op="<", threshold=400)
    assert evaluate([rule], {"QQQ": None}, {}, NOW) == []
    assert evaluate([rule], {}, {}, NOW) == []


def test_missing_bars_skips_rsi_and_change_pct_rules():
    rsi_rule = Rule(name="r1", symbol="QQQ", metric="rsi2", op="<", threshold=10)
    chg_rule = Rule(name="r2", symbol="QQQ", metric="change_pct", op="<", threshold=-3)
    quote = Quote(symbol="QQQ", ts=NOW, price=350.0)
    assert evaluate([rsi_rule], {"QQQ": quote}, {"QQQ": None}, NOW) == []
    assert evaluate([chg_rule], {"QQQ": quote}, {}, NOW) == []


def test_insufficient_bars_for_rsi_skips_rather_than_erroring():
    # rsi(period=14)는 15개 미만이면 전부 NaN.
    bars = _daily_bars([100.0, 101.0, 99.0])
    rule = Rule(name="r", symbol="QQQ", metric="rsi14", op="<", threshold=10)
    quote = Quote(symbol="QQQ", ts=NOW, price=99.0)
    assert evaluate([rule], {"QQQ": quote}, {"QQQ": bars}, NOW) == []


# ------------------------------------------------------------------- cooldown

def test_apply_cooldown_blocks_repeat_within_window_then_allows_after():
    hit = Hit(rule_name="r", symbol="QQQ", metric="price", value=1.0, op="<", threshold=2.0)
    last_hit_mono: dict[str, float] = {}

    first = apply_cooldown([hit], last_hit_mono, cooldown_seconds=60.0, now_mono=1000.0)
    assert first == [hit]
    assert last_hit_mono["r"] == 1000.0

    # 30초 후 — 쿨다운(60초) 안 지남 -> 걸러진다.
    second = apply_cooldown([hit], last_hit_mono, cooldown_seconds=60.0, now_mono=1030.0)
    assert second == []
    assert last_hit_mono["r"] == 1000.0  # 걸러진 시도는 시각을 갱신하지 않는다

    # 60초 후 — 쿨다운 경과 -> 다시 통과.
    third = apply_cooldown([hit], last_hit_mono, cooldown_seconds=60.0, now_mono=1060.0)
    assert third == [hit]
    assert last_hit_mono["r"] == 1060.0


def test_apply_cooldown_tracks_each_rule_independently():
    hit_a = Hit(rule_name="a", symbol="QQQ", metric="price", value=1.0, op="<", threshold=2.0)
    hit_b = Hit(rule_name="b", symbol="TQQQ", metric="price", value=1.0, op="<", threshold=2.0)
    last_hit_mono = {"a": 990.0}  # a는 방금 알림 감, b는 처음

    out = apply_cooldown([hit_a, hit_b], last_hit_mono, cooldown_seconds=60.0, now_mono=1000.0)
    assert out == [hit_b]


# --------------------------------------------------------- 주문/시그널 없음 계약

def test_hit_has_no_order_or_sizing_fields():
    """Hit은 시그널이 아니다 — qty/비중/action 등 주문 관련 필드가 전혀 없다."""
    field_names = {f.name for f in dataclasses.fields(Hit)}
    forbidden = {"action", "target_weight", "qty", "target_qty", "stop", "target", "state_update"}
    assert field_names.isdisjoint(forbidden)
    assert field_names == {"rule_name", "symbol", "metric", "value", "op", "threshold"}


def test_evaluate_return_type_contains_no_signal():
    from quant.core.models import Signal

    rule = Rule(name="r", symbol="QQQ", metric="price", op="<", threshold=400)
    hits = evaluate([rule], {"QQQ": Quote(symbol="QQQ", ts=NOW, price=350.0)}, {}, NOW)
    assert len(hits) == 1
    assert not isinstance(hits[0], Signal)
    assert all(not isinstance(h, Signal) for h in hits)
