"""국면(regime) 배수 → 사이징 배선 통합 테스트 (ADR-0009 통합 결정 검증).

결정: 배수는 **요청 예산에만** 곱한다. 하드레일(max_position_pct의 room,
max_total_exposure_pct, 팻핑거, 일손실 한도)에는 절대 곱하지 않는다 — 공격
국면(>1.0)이라고 안전 한도까지 넓어지면 보호 장치가 국면 판단의 인질이 된다.
여기 테스트들이 그 결정을 고정한다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant.trade.loop import run_cycle
from quant.core.ports import Context
from quant.core.models import Order, Position, Quote, Signal, SignalAction
from quant.trade.risk.manager import RiskManagerImpl

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


class _Clock:
    def now(self):
        return _NOW

    def is_market_open(self, market):
        return True

    def minutes_to_close(self, market):
        return 300.0

    def cadence_minutes(self):
        return 5.0

    def should_flatten(self, market, flatten_minutes):
        return False


class _Data:
    def __init__(self, price=100.0):
        self._price = price

    def quote(self, symbol):
        return Quote(symbol=symbol, ts=_NOW, price=self._price)

    def history(self, symbol, interval, n):
        import pandas as pd
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class _Broker:
    def __init__(self, cash=10_000_000.0, positions=None):
        self._cash = cash
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self):
        return self._cash

    def place_order(self, order):
        return None


def _risk(**risk_over) -> RiskManagerImpl:
    cfg = {
        "risk": {
            "max_position_pct": 50, "max_symbol_pct_total": 0,
            "daily_loss_limit_pct": 100, "max_orders_per_day": 100,
            "cooldown_bars_after_stop": 0, "max_order_notional_pct": 0,
            **risk_over,
        },
        "strategies": {},
    }
    return RiskManagerImpl(cfg, capital_fraction={"s": 1.0}, market_of={"TQQQ": "US"})


def _entry(weight=0.2) -> Signal:
    return Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG,
                  target_weight=weight)


def _ctx(broker=None) -> Context:
    return Context(clock=_Clock(), data=_Data(), broker=broker or _Broker())


# ------------------------------------------------- 배수가 예산을 스케일한다


def test_multiplier_scales_budget_in_capital_fraction_mode():
    # 정수 수량 사이징(QUICKREF:207) 하에서는 floor()가 비례관계를 깰 수 있다 —
    # cash=15,000,000이면 neutral budget=0.2x15,000,000=3,000,000 -> qty=20(정확),
    # defensive(x0.5) budget=1,500,000 -> qty=10(정확). floor 전부터 정수라 내림
    # 손실이 없어 "배수가 예산을 정확히 절반으로 스케일한다"는 결론이 그대로 보인다.
    risk = _risk()
    ctx = _ctx(_Broker(cash=15_000_000.0))
    neutral = risk.approve(_entry(0.2), ctx, risk_multiplier=1.0)
    defensive = risk.approve(_entry(0.2), ctx, risk_multiplier=0.5)
    assert neutral is not None and defensive is not None
    assert defensive.qty == pytest.approx(neutral.qty * 0.5)


def test_multiplier_scales_budget_in_cash_pct_mode():
    # 위와 동일한 이유로 cash=15,000,000 사용 — neutral qty=20, defensive qty=10.
    risk = _risk(sizing_mode="cash_pct")
    ctx = _ctx(_Broker(cash=15_000_000.0))
    neutral = risk.approve(_entry(0.2), ctx, risk_multiplier=1.0)
    defensive = risk.approve(_entry(0.2), ctx, risk_multiplier=0.5)
    assert neutral is not None and defensive is not None
    assert defensive.qty == pytest.approx(neutral.qty * 0.5)


# ------------------------------------------------- 하드레일은 오염되지 않는다


def test_aggressive_multiplier_never_exceeds_position_room():
    """공격 국면(1.3x)이어도 max_position_pct의 room을 넘을 수 없다.

    target_weight=0.5(=room과 정확히 같은 요청)에 1.3을 곱하면 요청 예산이 room을
    넘는다 — 이때 room이 이겨야 한다. 배수가 room 자체를 키우면 이 테스트가 깨진다.
    """
    risk = _risk()
    capped = risk.approve(_entry(0.5), _ctx(), risk_multiplier=1.3)
    neutral_max = risk.approve(_entry(0.5), _ctx(), risk_multiplier=1.0)
    assert capped is not None and neutral_max is not None
    assert capped.qty == pytest.approx(neutral_max.qty), (
        "공격 배수가 max_position_pct 상한을 넓혔다 — 하드레일이 국면에 오염됨"
    )


def test_aggressive_multiplier_never_exceeds_total_exposure_cap():
    risk = _risk(max_total_exposure_pct=50)
    # 자산 1,000만(현금 500만 + 보유 500만) — 총노출 상한 50% = 이미 꽉 참.
    held = {"SQQQ": Position(symbol="SQQQ", qty=5_000_000 / 1500.0, avg_cost=1.0)}
    ctx = _ctx(_Broker(cash=5_000_000.0, positions=held))
    blocked = risk.approve(_entry(0.2), ctx, risk_multiplier=1.3)
    assert blocked is None
    assert "총노출 상한" in risk.last_block


def test_invalid_multiplier_falls_back_to_neutral():
    risk = _risk()
    neutral = risk.approve(_entry(0.2), _ctx(), risk_multiplier=1.0)
    nan = risk.approve(_entry(0.2), _ctx(), risk_multiplier=float("nan"))
    negative = risk.approve(_entry(0.2), _ctx(), risk_multiplier=-2.0)
    assert nan.qty == pytest.approx(neutral.qty)
    assert negative.qty == pytest.approx(neutral.qty)


# ------------------------------------------------- run_cycle이 배수를 전달한다


class _CapturingRisk:
    def __init__(self):
        self.seen: list[float] = []

    def approve(self, signal, ctx, risk_multiplier: float = 1.0, marks=None):
        self.seen.append(risk_multiplier)
        return None  # 주문은 만들지 않는다 — 전달 여부만 본다


class _OneSignalStrategy:
    id = "s"
    symbols = ["TQQQ"]

    def on_cycle(self, ctx):
        return [_entry(0.2)]


class _Sink:
    def on_signal(self, signal):
        pass

    def on_fill(self, fill):
        pass


def test_run_cycle_passes_multiplier_to_approve():
    risk = _CapturingRisk()
    run_cycle([_OneSignalStrategy()], _ctx(), risk, _Sink(), risk_multiplier=0.5)
    assert risk.seen == [0.5]


def test_run_cycle_defaults_to_neutral_for_backtests():
    """백테스트 호출부는 risk_multiplier를 넘기지 않는다 — 기본 1.0이어야
    과거 결과가 국면 모듈 추가로 조용히 바뀌지 않는다."""
    risk = _CapturingRisk()
    run_cycle([_OneSignalStrategy()], _ctx(), risk, _Sink())
    assert risk.seen == [1.0]
