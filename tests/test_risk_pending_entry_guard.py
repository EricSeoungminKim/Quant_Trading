"""미체결 매수 중복 진입 가드(2026-09-01) — 실계좌 전환 전 감사 권고 4건 중 유일하게
돈 규모에 비례해 커지는 리스크에 대한 방어선.

## 무엇을 막는가

엔진은 poll_seconds(약 5초)마다 돈다. 브로커가 느리거나 부분 체결 중이면, 같은
(전략,종목)에 대해 "아직 포지션이 없다"고 판단해 같은 신호로 여러 번 진입할 수
있다(감사 판정: 최대 3배 진입 가능). `OpenOrderBook`(quant/trade/reconcile.py,
Phase 6.5)이 이미 미체결 잔량을 추적하고 있으므로, `RiskManagerImpl.approve()`가
그 값을 물어 같은 (전략,종목)에 미체결 매수가 있으면 신규 진입을 막는다.

## 이 스위트가 지키는 불변식

1. 같은 (전략,종목)에 미체결 매수가 있으면 두 번째 진입은 차단된다.
2. **청산 신호는 미체결 매수가 있어도 절대 막히지 않는다** — 이 파일 전체의
   원칙(회로차단기는 EXIT_LONG/SCALE_OUT을 막지 않는다)과 동일선상.
3. 다른 종목/다른 전략의 미체결은 영향을 주지 않는다.
4. `pending_entry_qty` 조회가 실패하거나(예외) 콜백 자체가 없으면(None) 진입을
   막지 않는다 — "모른다"를 "차단"으로 바꾸지 않는다(가드 자체의 존재 이유가
   실계좌 안전인데, 가드의 부수 조회 실패가 새로운 차단 사유가 되면 안 된다).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.ports import Context
from quant.core.models import Position, Quote, Signal, SignalAction
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
_SYMBOL = "TQQQ"
_OTHER_SYMBOL = "SQQQ"
_MARKET_OF = {_SYMBOL: "US", _OTHER_SYMBOL: "US"}
_DEFAULT_NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
_STRATEGY = "donchian"
_OTHER_STRATEGY = "orb_scan"


class _FakeBroker:
    def __init__(self, cash: float, positions: dict[str, Position] | None = None):
        self._cash = cash
        self._positions = positions or {}

    def positions(self) -> dict[str, Position]:
        return self._positions

    def cash(self) -> float:
        return self._cash

    def place_order(self, order):
        raise NotImplementedError("이 스위트는 approve()만 검증한다")


class _FakeData:
    def __init__(self, price: float, now: datetime = _DEFAULT_NOW):
        self._price = price
        self._now = now

    def quote(self, symbol: str) -> Quote | None:
        return Quote(symbol=symbol, ts=self._now, price=self._price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _risk_cfg(**overrides) -> dict:
    """다른 회로차단기가 우연히 끼어들지 않도록 관대한 기본값."""
    cfg = dict(
        max_position_pct=100,
        max_symbol_pct_total=0,
        daily_loss_limit_pct=100,
        max_orders_per_day=1000,
        cooldown_bars_after_stop=0,
        max_order_notional_pct=0,
    )
    cfg.update(overrides)
    return {"risk": cfg}


def _ctx(fake_clock_cls, cash: float, positions=None, price: float = 100.0) -> Context:
    return Context(
        clock=fake_clock_cls(now=_DEFAULT_NOW),
        data=_FakeData(price=price),
        broker=_FakeBroker(cash, positions),
    )


def _entry(symbol: str = _SYMBOL, strategy_id: str = _STRATEGY) -> Signal:
    return Signal(strategy_id=strategy_id, symbol=symbol, action=SignalAction.ENTER_LONG, target_weight=0.1)


def _exit(symbol: str = _SYMBOL, strategy_id: str = _STRATEGY) -> Signal:
    return Signal(
        strategy_id=strategy_id, symbol=symbol, action=SignalAction.EXIT_LONG,
        target_weight=0.0, exit_fraction=1.0,
    )


class _StaticPendingEntryQty:
    """(symbol, strategy_id) -> qty 매핑을 그대로 돌려주는 페이크. 어떤 인자로
    불렸는지 기록해 "다른 종목/전략은 영향 없음"을 호출 인자로도 확인한다."""

    def __init__(self, table: dict[tuple[str, str], float]):
        self._table = table
        self.calls: list[tuple[str, str]] = []

    def __call__(self, symbol: str, strategy_id: str) -> float:
        self.calls.append((symbol, strategy_id))
        return self._table.get((symbol, strategy_id), 0.0)


class _RaisingPendingEntryQty:
    def __call__(self, symbol: str, strategy_id: str) -> float:
        raise RuntimeError("브로커 조회 실패")


def _risk(fake_clock_cls, pending_entry_qty=None, **cfg_overrides) -> RiskManagerImpl:
    return RiskManagerImpl(
        _risk_cfg(**cfg_overrides), capital_fraction={_STRATEGY: 1.0, _OTHER_STRATEGY: 1.0},
        market_of=_MARKET_OF, pending_entry_qty=pending_entry_qty,
    )


# ============================================================= (a) 같은 (전략,종목) 차단

def test_pending_buy_for_same_strategy_and_symbol_blocks_new_entry(fake_clock_cls):
    pending = _StaticPendingEntryQty({(_SYMBOL, _STRATEGY): 5.0})
    risk = _risk(fake_clock_cls, pending_entry_qty=pending)
    ctx = _ctx(fake_clock_cls, cash=10_000_000)

    order = risk.approve(_entry(), ctx)

    assert order is None
    assert "미체결" in risk.last_block
    assert _SYMBOL in risk.last_block


def test_no_pending_buy_lets_entry_through(fake_clock_cls):
    """대조군 — 가드 자체가 관대한 다른 설정 때문에 우연히 통과하는 게 아님을 보인다."""
    pending = _StaticPendingEntryQty({})  # 아무 미체결도 없음
    risk = _risk(fake_clock_cls, pending_entry_qty=pending)
    ctx = _ctx(fake_clock_cls, cash=10_000_000)

    order = risk.approve(_entry(), ctx)

    assert order is not None
    assert pending.calls == [(_SYMBOL, _STRATEGY)]


# ============================================================= (b) 청산은 절대 안 막힘

def test_exit_signal_passes_even_with_pending_buy(fake_clock_cls):
    """중복 청산 주문은 위험이 아니라 안전 쪽이고, 손절이 막히면 그게 진짜 사고다."""
    pending = _StaticPendingEntryQty({(_SYMBOL, _STRATEGY): 999.0})
    risk = _risk(fake_clock_cls, pending_entry_qty=pending)
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=10.0, avg_cost=90.0)}
    ctx = _ctx(fake_clock_cls, cash=10_000_000, positions=positions)

    order = risk.approve(_exit(), ctx)

    assert order is not None
    # 가드는 _ENTRY_ACTIONS 에만 적용된다 — 청산 경로에서는 콜백조차 불리지 않는다.
    assert pending.calls == []


# ============================================================= (c) 다른 종목/전략 무관

def test_pending_buy_for_different_symbol_does_not_block(fake_clock_cls):
    pending = _StaticPendingEntryQty({(_OTHER_SYMBOL, _STRATEGY): 5.0})
    risk = _risk(fake_clock_cls, pending_entry_qty=pending)
    ctx = _ctx(fake_clock_cls, cash=10_000_000)

    order = risk.approve(_entry(symbol=_SYMBOL), ctx)

    assert order is not None


def test_pending_buy_for_different_strategy_does_not_block(fake_clock_cls):
    pending = _StaticPendingEntryQty({(_SYMBOL, _OTHER_STRATEGY): 5.0})
    risk = _risk(fake_clock_cls, pending_entry_qty=pending)
    ctx = _ctx(fake_clock_cls, cash=10_000_000)

    order = risk.approve(_entry(symbol=_SYMBOL, strategy_id=_STRATEGY), ctx)

    assert order is not None


# ============================================================= (d) 조회 실패/미배선 = 통과

def test_pending_lookup_failure_does_not_block_entry(fake_clock_cls, caplog):
    """가드의 부수 조회가 실패했다고 진입까지 막으면, 감시가 새 장애 모드가 된다."""
    risk = _risk(fake_clock_cls, pending_entry_qty=_RaisingPendingEntryQty())
    ctx = _ctx(fake_clock_cls, cash=10_000_000)

    order = risk.approve(_entry(), ctx)

    assert order is not None
    assert "미체결" not in risk.last_block


def test_no_pending_entry_qty_wired_behaves_exactly_as_before(fake_clock_cls):
    """콜백을 아예 안 넘기면(기존 호출부 전부) 가드는 존재하지 않는 것과 동일 —
    paper 브로커/기존 테스트가 이 인자를 넘기지 않으므로 회귀가 없어야 한다."""
    risk = _risk(fake_clock_cls, pending_entry_qty=None)
    ctx = _ctx(fake_clock_cls, cash=10_000_000)

    order = risk.approve(_entry(), ctx)

    assert order is not None
