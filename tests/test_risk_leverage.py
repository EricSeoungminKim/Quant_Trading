"""레버리지 인지 사이징(risk.manager._leverage_haircut) + 레버리지 총노출 레일
(risk.max_leveraged_exposure_pct) 테스트.

이 스위트가 고정하는 것:
- `leverage_of`가 아예 주입되지 않으면(None, 기본값) 헤어컷도 노출 레일도 완전히
  꺼진다 — 백테스트/기존 테스트 결과가 이 기능 추가로 바뀌면 안 된다.
- `leverage_of`가 주입되면(dict, 빈 dict 포함) 3배 ETF는 1/3 명목으로 줄어들고,
  레버리지 조정 노출 합이 상한을 넘으면 신규 진입이 차단된다.
- 두 기능 모두 dict에 없는 심볼("모르는 것")은 안전한 기본값(헤어컷 없음/노출
  미포함)으로 처리하되 조용히 넘어가지 않는다(헤어컷은 최초 1회 경고).
- 청산(EXIT_LONG/SCALE_OUT)은 두 기능 모두와 무관하다 — 절대 막히지 않는다.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.fx import FixedFxProvider
from quant.core.ports import Context
from quant.core.models import Position, Quote, Signal, SignalAction
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
SOXL = "SOXL"  # 3배 레버리지 ETF (가정)
SPY = "SPY"  # 비레버리지
MARKET_OF = {SOXL: "US", SPY: "US"}
FX_RATE = 1500.0
PRICE = 100.0  # USD


class _Broker:
    def __init__(self, cash: float, positions: dict[str, Position] | None = None):
        self._cash = cash
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self) -> float:
        return self._cash


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=NOW, price=PRICE)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _risk(leverage_of=None, **risk_overrides) -> RiskManagerImpl:
    cfg = dict(
        sizing_mode="capital_fraction",
        max_position_pct=100,
        max_symbol_pct_total=0,
        daily_loss_limit_pct=100,
        max_orders_per_day=1000,
        cooldown_bars_after_stop=0,
        max_order_notional_pct=0,
        max_total_exposure_pct=0,
        max_concurrent_positions=0,
        max_leveraged_exposure_pct=50,
    )
    cfg.update(risk_overrides)
    return RiskManagerImpl(
        {"risk": cfg}, capital_fraction={"s": 1.0},
        market_of=dict(MARKET_OF), fx=FixedFxProvider(FX_RATE), leverage_of=leverage_of,
    )


def _ctx(cash: float, positions: dict[str, Position] | None = None, fake_clock_cls=None) -> Context:
    return Context(clock=fake_clock_cls(now=NOW), data=_Data(), broker=_Broker(cash, positions))


def _entry(symbol: str, target_weight: float = 0.3) -> Signal:
    return Signal(strategy_id="s", symbol=symbol, action=SignalAction.ENTER_LONG,
                  target_weight=target_weight)


# ------------------------------------------------------------- 사이징 헤어컷

def test_leverage_of_none_disables_haircut_entirely(fake_clock_cls):
    """주입 자체가 없으면(기본값) 3배 ETF도 1배와 동일하게 사이징된다 — 기존 동작 보존."""
    risk = _risk(leverage_of=None)
    order = risk.approve(_entry(SOXL), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    baseline_risk = _risk(leverage_of=None)
    baseline = baseline_risk.approve(_entry(SPY, target_weight=0.3), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    assert order.qty == pytest.approx(baseline.qty)


def test_3x_etf_gets_one_third_notional_of_1x(fake_clock_cls):
    risk_3x = _risk(leverage_of={SOXL: 3.0, SPY: 1.0})
    risk_1x = _risk(leverage_of={SOXL: 3.0, SPY: 1.0})

    # target_weight=0.45 → budget 4,500,000원 — 정수 floor 왜곡 없이 3으로 나눠떨어지게
    # 고른 값(1x: qty=30, 3x: qty=10)이다. 임의 비율로는 floor()가 정확한 1/3 관계를 깬다.
    order_3x = risk_3x.approve(_entry(SOXL, target_weight=0.45), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    order_1x = risk_1x.approve(_entry(SPY, target_weight=0.45), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))

    assert order_3x.qty == pytest.approx(order_1x.qty / 3)


def test_inverse_leverage_uses_absolute_value(fake_clock_cls):
    """인버스(-3x)도 절대값 3으로 헤어컷한다 — 부호는 무관하다."""
    risk_inverse = _risk(leverage_of={SOXL: -3.0, SPY: 1.0})
    risk_positive = _risk(leverage_of={SOXL: 3.0, SPY: 1.0})

    inverse_order = risk_inverse.approve(_entry(SOXL), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    positive_order = risk_positive.approve(_entry(SOXL), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))

    assert inverse_order.qty == pytest.approx(positive_order.qty)


def test_unknown_symbol_in_leverage_of_gets_no_haircut_but_warns_once(fake_clock_cls, caplog):
    """leverage_of가 주입됐지만(빈 dict) 이 심볼만 없으면 — 헤어컷 없음(1.0배 취급),
    단 최초 1회 WARNING을 남긴다(반복 호출에서 스팸이 되면 안 된다)."""
    risk = _risk(leverage_of={})
    baseline = _risk(leverage_of=None)

    with caplog.at_level(logging.WARNING):
        order = risk.approve(_entry(SOXL), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
        risk.approve(_entry(SOXL), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))

    ref = baseline.approve(_entry(SOXL), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    assert order.qty == pytest.approx(ref.qty)
    warnings = [r for r in caplog.records if "레버리지 배수 미상" in r.message]
    assert len(warnings) == 1


def test_haircut_applies_in_cash_pct_mode_too(fake_clock_cls):
    risk_3x = _risk(leverage_of={SOXL: 3.0, SPY: 1.0}, sizing_mode="cash_pct")
    risk_1x = _risk(leverage_of={SOXL: 3.0, SPY: 1.0}, sizing_mode="cash_pct")

    order_3x = risk_3x.approve(_entry(SOXL, target_weight=0.45), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    order_1x = risk_1x.approve(_entry(SPY, target_weight=0.45), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))

    assert order_3x.qty == pytest.approx(order_1x.qty / 3)


def test_exit_is_never_affected_by_leverage_haircut(fake_clock_cls):
    """청산 수량은 보유 수량 기준이지 헤어컷과 무관하다."""
    risk = _risk(leverage_of={SOXL: 3.0})
    positions = {SOXL: Position(symbol=SOXL, qty=10.0, avg_cost=PRICE)}
    exit_signal = Signal(strategy_id="s", symbol=SOXL, action=SignalAction.EXIT_LONG,
                          target_weight=0.0, exit_fraction=1.0)
    order = risk.approve(exit_signal, _ctx(0.0, positions, fake_clock_cls=fake_clock_cls))
    assert order is not None
    assert order.qty == pytest.approx(10.0)


# ------------------------------------------------------- 레버리지 총노출 레일

def test_leveraged_exposure_rail_blocks_new_entry_over_cap(fake_clock_cls):
    """상한 50% — 이미 보유한 레버리지 노출 + 이번 주문이 상한을 넘으면 차단."""
    risk = _risk(leverage_of={SOXL: 3.0}, max_leveraged_exposure_pct=50, max_position_pct=1000)
    # 기존 보유: SOXL 20주 x $100 x FX = 3,000,000원 명목, x3배 = 9,000,000원 조정노출.
    # equity = cash(10,000,000) + 보유평가(3,000,000) = 13,000,000원. 상한 50% = 6,500,000원.
    # 기존 조정노출(9,000,000원)만으로 이미 상한을 초과한 상태 — 추가 진입은 반드시 막혀야 한다.
    positions = {SOXL: Position(symbol=SOXL, qty=20.0, avg_cost=PRICE)}

    order = risk.approve(_entry(SOXL, target_weight=0.1), _ctx(10_000_000.0, positions, fake_clock_cls=fake_clock_cls))

    assert order is None
    assert "레버리지 총노출 상한" in risk.last_block


def test_leveraged_exposure_rail_allows_entry_under_cap(fake_clock_cls):
    risk = _risk(leverage_of={SOXL: 3.0}, max_leveraged_exposure_pct=50, max_position_pct=1000)
    order = risk.approve(_entry(SOXL, target_weight=0.05), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    assert order is not None


def test_leveraged_exposure_rail_ignores_non_leveraged_entries(fake_clock_cls):
    """이 레일은 '신규 진입 자체가 레버리지 상품'일 때만 건다 — 과거 레버리지
    노출이 상한을 넘어도 비레버리지 신규 진입까지 막지는 않는다."""
    risk = _risk(leverage_of={SOXL: 3.0, SPY: 1.0}, max_leveraged_exposure_pct=50, max_position_pct=1000)
    positions = {SOXL: Position(symbol=SOXL, qty=100.0, avg_cost=PRICE)}  # 압도적으로 상한 초과 상태

    order = risk.approve(_entry(SPY, target_weight=0.05), _ctx(10_000_000.0, positions, fake_clock_cls=fake_clock_cls))

    assert order is not None


def test_leveraged_exposure_rail_disabled_when_leverage_of_is_none(fake_clock_cls):
    """leverage_of가 주입되지 않으면(None) 이 레일은 판정 불가로 완전히
    비활성 — max_leveraged_exposure_pct 설정값과 무관하게 절대 막지 않는다."""
    risk = _risk(leverage_of=None, max_leveraged_exposure_pct=1, max_position_pct=1000)
    order = risk.approve(_entry(SOXL, target_weight=0.9), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    assert order is not None


def test_leveraged_exposure_rail_zero_disables(fake_clock_cls):
    risk = _risk(leverage_of={SOXL: 3.0}, max_leveraged_exposure_pct=0, max_position_pct=1000)
    positions = {SOXL: Position(symbol=SOXL, qty=100.0, avg_cost=PRICE)}
    order = risk.approve(_entry(SOXL, target_weight=0.05), _ctx(10_000_000.0, positions, fake_clock_cls=fake_clock_cls))
    assert order is not None


def test_leveraged_exposure_rail_never_blocks_exits(fake_clock_cls):
    risk = _risk(leverage_of={SOXL: 3.0}, max_leveraged_exposure_pct=1)
    positions = {SOXL: Position(symbol=SOXL, qty=100.0, avg_cost=PRICE)}
    exit_signal = Signal(strategy_id="s", symbol=SOXL, action=SignalAction.EXIT_LONG,
                          target_weight=0.0, exit_fraction=1.0)
    order = risk.approve(exit_signal, _ctx(0.0, positions, fake_clock_cls=fake_clock_cls))
    assert order is not None
