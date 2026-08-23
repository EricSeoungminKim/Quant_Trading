"""리스크 하드레일이 실제로 걸리는지 — RiskManagerImpl.approve()가 만들어내는
Order.qty/None 여부가 규칙대로 정확히 계산되는지를 본다. mock 호출 여부가 아니라
실제 숫자(qty)를 검증한다.

이 스위트가 지키는 불변식: 유닛 테스트가 max_position_pct/daily_loss_limit_pct
같은 설정값을 개별 시나리오로 커버해도, "그 캡이 실제 사이징 산식에 진짜로
반영되는가"를 숫자로 확인하지 않으면 리스크 룰이 무력화된 채 통과할 수 있다.
여기서는 매 테스트마다 손으로 계산한 기대 qty와 실제 Order.qty를 비교한다.
"""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.fx import FixedFxProvider
from quant.core.ports import Context
from quant.core.models import Position, Quote, Side, Signal, SignalAction
from quant.core.portfolio.portfolio import to_krw
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
_SYMBOL = "TQQQ"
_MARKET_OF = {_SYMBOL: "US"}


class _FakeBroker:
    """RiskManager.approve()가 참조하는 최소 Broker 대역 — place_order는 호출되지
    않는다(사이징 산식 자체만 검증하는 테스트라 체결까지 필요 없다)."""

    def __init__(self, cash: float, positions: dict[str, Position] | None = None):
        self._cash = cash
        self._positions = positions or {}

    def positions(self) -> dict[str, Position]:
        return self._positions

    def cash(self) -> float:
        return self._cash

    def place_order(self, order):
        raise NotImplementedError("이 테스트 스위트는 사이징만 검증 — place_order는 호출되지 않아야 한다")


class _FakeData:
    def __init__(self, price: float):
        self._price = price

    def quote(self, symbol: str) -> Quote | None:
        if symbol != _SYMBOL:
            return None
        return Quote(symbol=symbol, ts=datetime(2026, 1, 5, 10, 0, tzinfo=NY), price=self._price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _ctx(fake_clock_cls, price: float, cash: float, positions: dict[str, Position] | None = None,
         now: datetime | None = None) -> Context:
    now = now or datetime(2026, 1, 5, 10, 0, tzinfo=NY)
    clock = fake_clock_cls(now=now)
    return Context(clock=clock, data=_FakeData(price), broker=_FakeBroker(cash, positions))


def _entry_signal(target_weight: float = 1.0) -> Signal:
    return Signal(
        strategy_id="donchian", symbol=_SYMBOL, action=SignalAction.ENTER_LONG,
        target_weight=target_weight,
    )


# --------------------------------------------------------------- 일일 손실 한도

def test_daily_loss_limit_blocks_new_entries_but_never_exits(fake_clock_cls):
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 100, "max_symbol_pct_total": 0, "daily_loss_limit_pct": 3}},
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )

    # 1) 당일 첫 approve — 기준 자산(day_start_equity)이 이 시점 equity로 확립된다.
    #    아직 손실이 없으므로 승인돼야 한다.
    order = risk.approve(_entry_signal(0.1), _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0))
    assert order is not None
    assert order.qty > 0

    # 2) 같은 날 자산이 4% 감소(한도 3% 초과) -> 신규 진입은 차단된다.
    blocked = risk.approve(_entry_signal(0.1), _ctx(fake_clock_cls, price=100.0, cash=9_600_000.0))
    assert blocked is None
    assert "손실" in risk.last_block

    # 3) 손실 한도 도달 상태에서도 청산(EXIT_LONG)은 항상 허용돼야 한다 — 청산까지
    #    막히면 손실이 눈덩이처럼 불어난다.
    existing = {_SYMBOL: Position(symbol=_SYMBOL, qty=50.0, avg_cost=100.0)}
    exit_signal = Signal(
        strategy_id="donchian", symbol=_SYMBOL, action=SignalAction.EXIT_LONG,
        target_weight=0.0, exit_fraction=1.0,
    )
    exit_order = risk.approve(exit_signal, _ctx(fake_clock_cls, price=100.0, cash=9_600_000.0, positions=existing))
    assert exit_order is not None
    assert exit_order.side is Side.SELL
    assert exit_order.qty == pytest.approx(50.0)


# --------------------------------------------------------------- max_position_pct

def test_max_position_pct_caps_order_size_to_exact_expected_qty(fake_clock_cls):
    fx = FixedFxProvider(1000.0)
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 20, "max_symbol_pct_total": 0, "daily_loss_limit_pct": 100}},
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF, fx=fx,
    )
    ctx = _ctx(fake_clock_cls, price=100.0, cash=1_000_000.0)

    order = risk.approve(_entry_signal(1.0), ctx)

    # equity=strategy_capital=1,000,000원. cap 20% -> 예산 200,000원 -> 200,000/1000/100 = 2주.
    assert order is not None
    assert order.qty == pytest.approx(2.0)
    assert to_krw(order.qty * 100.0, "US", fx) <= 0.2 * 1_000_000.0 + 1e-6


# ---------------------------------------------------------- max_symbol_pct_total

def test_max_symbol_pct_total_caps_aggregate_exposure_tighter_than_position_cap(fake_clock_cls):
    fx = FixedFxProvider(1000.0)
    # max_position_pct는 사실상 무제한(100%)으로 두고, max_symbol_pct_total만 좁게
    # 설정해서 "종목 총노출 캡이 실제로 더 taighter하게 이긴다"를 분리해서 본다.
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 100, "max_symbol_pct_total": 10, "daily_loss_limit_pct": 100}},
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF, fx=fx,
    )
    existing = {_SYMBOL: Position(symbol=_SYMBOL, qty=10.0, avg_cost=100.0)}
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, positions=existing)

    order = risk.approve(_entry_signal(1.0), ctx)

    # equity = 10,000,000 + to_krw(10*100) = 11,000,000. max_symbol_pct_total=10% ->
    # room_total = 0.1*11,000,000 - to_krw(10*100) = 100,000원 -> qty = 100,000/1000/100 = 1주.
    # position 캡만 있었다면(max_position_pct=100%) 훨씬 큰 수량(최대 100주)이 허용됐을 것 —
    # 종목 총노출 캡이 실제로 더 낮은 수량으로 잘랐는지가 핵심.
    assert order is not None
    assert order.qty == pytest.approx(1.0)
    assert order.qty < 100.0


# -------------------------------------------------------- 사이징이 0이면 주문 거부

def test_order_that_sizes_to_zero_is_rejected_not_sent(fake_clock_cls):
    fx = FixedFxProvider(1000.0)
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 50, "max_symbol_pct_total": 0, "daily_loss_limit_pct": 100}},
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF, fx=fx,
    )
    # 기존 포지션이 이미 max_position_pct 캡을 정확히 다 채운 상태 -> 남은 room=0.
    existing = {_SYMBOL: Position(symbol=_SYMBOL, qty=100.0, avg_cost=100.0)}
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, positions=existing)

    order = risk.approve(_entry_signal(1.0), ctx)

    assert order is None
    assert "부족" in risk.last_block


# --------------------------------------------------- 사이징은 음수/NaN을 절대 내지 않는다

def test_non_positive_equity_blocks_before_any_negative_sizing_math(fake_clock_cls):
    """현금이 음수인 방어적 상황에서도 사이징 계산으로 진입하지 않고 그 전에 차단돼야
    한다 — "0 이하 자산" 가드가 음수/NaN qty가 만들어질 가능성 자체를 없앤다."""
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 50, "max_symbol_pct_total": 0, "daily_loss_limit_pct": 100}},
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    ctx = _ctx(fake_clock_cls, price=100.0, cash=-1_000.0)

    order = risk.approve(_entry_signal(1.0), ctx)

    assert order is None
    assert "자산" in risk.last_block


@pytest.mark.parametrize("price,target_weight", [
    (0.01, 0.0001),   # 극단적으로 싼 가격 + 극단적으로 작은 비중
    (100.0, 1000.0),  # target_weight가 100%를 훨씬 초과 — room으로 클립돼야 한다
    (1e6, 1.0),       # 매우 비싼 가격
])
def test_approved_qty_is_always_positive_and_finite(fake_clock_cls, price, target_weight):
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 50, "max_symbol_pct_total": 0, "daily_loss_limit_pct": 100}},
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    ctx = _ctx(fake_clock_cls, price=price, cash=10_000_000.0)

    order = risk.approve(_entry_signal(target_weight), ctx)

    if order is not None:
        assert order.qty > 0
        assert math.isfinite(order.qty)
