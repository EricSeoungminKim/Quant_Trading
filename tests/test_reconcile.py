"""브로커 대사(app/reconcile.py).

지키는 불변식:
- 엔진 원장과 브로커 실보유가 어긋나면 **신규 진입만** halt하고 알린다.
- 청산은 그 상태에서도 그대로 나간다(halt의 기존 의미 — loop.run_cycle은 halt를
  ENTER/SCALE_IN에만 적용한다). 불일치 중에 청산까지 막히면 방어하려던 리스크보다
  더 나쁜 상태가 된다.
- 사용자 수동 보유의 변화는 halt 사유가 아니다 — 정보성 리포트로만 남는다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from quant.trade.control import TradingControl
from quant.trade.loop import run_cycle
from quant.trade.reconcile import Reconciler
from quant.core.ports import Context
from quant.core.models import Fill, Order, Position, Quote, Side, Signal, SignalAction

_OHLCV = ["open", "high", "low", "close", "volume"]


class _Broker:
    """엔진 소유 원장(engine_*)과 브로커 실보유(positions)를 독립적으로 조작할 수 있는
    페이크 — 대사가 검증하는 것이 정확히 이 둘의 차이다."""

    def __init__(self, holdings: dict[str, float], owned: dict[str, float]):
        self._holdings = dict(holdings)
        self._owned = dict(owned)
        self.orders: list[Order] = []

    def positions(self) -> dict[str, Position]:
        return {s: Position(symbol=s, qty=q, avg_cost=100.0) for s, q in self._holdings.items()}

    def cash(self) -> float:
        return 1_000_000.0

    def engine_owned_qty(self, symbol: str) -> float:
        return self._owned.get(symbol, 0.0)

    def engine_owned_symbols(self) -> set[str]:
        return {s for s, q in self._owned.items() if q > 0}

    def place_order(self, order: Order) -> Fill:
        self.orders.append(order)
        return Fill(symbol=order.symbol, side=order.side, qty=order.qty, price=100.0,
                    ts=datetime.now(timezone.utc), strategy_id=order.strategy_id)


class _PlainBroker:
    """엔진 소유 원장을 노출하지 않는 브로커(PaperBroker 상당)."""

    def positions(self) -> dict[str, Position]:
        return {}

    def cash(self) -> float:
        return 0.0

    def place_order(self, order):
        return None


class _Notifier:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


@pytest.fixture
def control(tmp_path) -> TradingControl:
    return TradingControl(state_path=tmp_path / "control.json")


# ------------------------------------------------------------------- 불일치 감지

def test_engine_position_missing_at_broker_halts_new_entries(control):
    broker = _Broker(holdings={}, owned={"TQQQ": 10.0})
    notifier = _Notifier()

    report = Reconciler(broker, control, notifier).check(force=True)

    assert not report.ok
    assert control.is_halted()
    assert "브로커 보유 없음" in control.halt_reason()
    assert any("대사 불일치" in m for m in notifier.messages)


def test_broker_qty_below_ledger_halts(control):
    broker = _Broker(holdings={"TQQQ": 4.0}, owned={"TQQQ": 10.0})

    report = Reconciler(broker, control).check(force=True)

    assert not report.ok
    assert control.is_halted()


def test_matching_state_does_not_halt(control):
    broker = _Broker(holdings={"TQQQ": 10.0}, owned={"TQQQ": 10.0})
    notifier = _Notifier()

    report = Reconciler(broker, control, notifier).check(force=True)

    assert report.ok
    assert not control.is_halted()
    assert notifier.messages == []


def test_user_manual_holding_is_informational_not_a_halt(control):
    """엔진이 산 적 없는 종목이 계좌에 있는 것은 정상이다 — 사용자가 손으로 샀다."""
    broker = _Broker(holdings={"TQQQ": 10.0, "AAPL": 7.0}, owned={"TQQQ": 10.0})
    notifier = _Notifier()

    report = Reconciler(broker, control, notifier).check(force=True)

    assert report.ok
    assert not control.is_halted()
    assert any("AAPL" in c for c in report.manual_changes)


def test_broker_qty_above_ledger_is_manual_buy_not_mismatch(control):
    """같은 종목을 사용자가 추가로 산 경우 — 초과분은 수동 보유지 불일치가 아니다."""
    broker = _Broker(holdings={"TQQQ": 25.0}, owned={"TQQQ": 10.0})

    report = Reconciler(broker, control).check(force=True)

    assert report.ok
    assert not control.is_halted()


def test_mismatch_notification_is_sent_once_not_every_cycle(control):
    broker = _Broker(holdings={}, owned={"TQQQ": 10.0})
    notifier = _Notifier()
    reconciler = Reconciler(broker, control, notifier)

    reconciler.check(force=True)
    reconciler.check(force=True)
    reconciler.check(force=True)

    assert len(notifier.messages) == 1


def test_check_respects_its_interval(control):
    broker = _Broker(holdings={"TQQQ": 10.0}, owned={"TQQQ": 10.0})
    reconciler = Reconciler(broker, control, interval_minutes=5)

    assert reconciler.check(force=True).checked is True
    assert reconciler.check().checked is False  # 주기 미도달 — 조회조차 하지 않는다


def test_reconciler_is_inactive_for_brokers_without_an_ownership_ledger(control):
    reconciler = Reconciler(_PlainBroker(), control)

    assert reconciler.supported is False
    assert reconciler.check(force=True).checked is False
    assert not control.is_halted()


# --------------------------------------------- 불일치 상태에서도 청산은 통과해야 한다

def test_exits_still_execute_while_halted_for_mismatch(control):
    broker = _Broker(holdings={"TQQQ": 4.0}, owned={"TQQQ": 10.0})
    Reconciler(broker, control).check(force=True)
    assert control.is_halted()

    class _Data:
        def quote(self, symbol):
            return Quote(symbol=symbol, ts=datetime.now(timezone.utc), price=100.0)

        def history(self, symbol, interval, n):
            return pd.DataFrame(columns=_OHLCV)

    class _Clock:
        def now(self):
            return datetime.now(timezone.utc)

        def is_market_open(self, market):
            return True

        def minutes_to_close(self, market):
            return 120.0

        def cadence_minutes(self):
            return 15.0

        def should_flatten(self, market, minutes):
            return False

    class _Risk:
        last_block = ""

        def approve(self, signal, ctx, risk_multiplier: float = 1.0, marks=None):
            side = Side.BUY if signal.action in (
                SignalAction.ENTER_LONG, SignalAction.SCALE_IN) else Side.SELL
            return Order(symbol=signal.symbol, side=side, qty=1.0,
                         strategy_id=signal.strategy_id)

    class _Sink:
        def on_signal(self, signal): ...
        def on_fill(self, fill): ...

    class _Strategy:
        id = "s"
        symbols = ["TQQQ"]

        def on_cycle(self, ctx):
            return [
                Signal(strategy_id="s", symbol="TQQQ",
                       action=SignalAction.ENTER_LONG, target_weight=1.0),
                Signal(strategy_id="s", symbol="TQQQ",
                       action=SignalAction.EXIT_LONG, target_weight=0.0, exit_fraction=1.0),
            ]

    ctx = Context(clock=_Clock(), data=_Data(), broker=broker)
    run_cycle([_Strategy()], ctx, _Risk(), _Sink(), control=control)

    sides = [o.side for o in broker.orders]
    assert Side.SELL in sides       # 청산은 나갔다
    assert Side.BUY not in sides    # 신규 진입만 막혔다


# ------------------------------------------------------------------- 루프 배선

def test_run_paper_loop_reconciles_at_startup_before_the_first_cycle(tmp_path, control):
    """재시작 직후가 원장과 실보유가 가장 어긋나기 쉬운 시점이다 — 첫 사이클보다 먼저 봐야 한다."""
    from quant.apps.config import Settings
    from quant.trade.loop import run_paper_loop

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("engine:\n  poll_seconds: 0\n", encoding="utf-8")
    settings = Settings({"engine": {"poll_seconds": 0}}, settings_path)

    broker = _Broker(holdings={}, owned={"TQQQ": 10.0})
    calls: list[bool] = []

    class _RecordingReconciler(Reconciler):
        def check(self, force: bool = False):
            calls.append(force)
            return super().check(force)

    class _Clock:
        def now(self):
            return datetime.now(timezone.utc)

        def is_market_open(self, market):
            return False

        def minutes_to_close(self, market):
            return None

        def cadence_minutes(self):
            return 15.0

        def should_flatten(self, market, minutes):
            return False

    class _Data:
        def quote(self, symbol):
            return None

        def history(self, symbol, interval, n):
            return pd.DataFrame(columns=_OHLCV)

    class _Sink:
        def on_signal(self, signal): ...
        def on_fill(self, fill): ...

    class _StopLoop(Exception):
        pass

    async def _stop(_seconds):
        raise _StopLoop

    ctx = Context(clock=_Clock(), data=_Data(), broker=broker)
    with patch("asyncio.sleep", _stop):
        with pytest.raises(_StopLoop):
            asyncio.run(run_paper_loop(
                [], ctx, None, _Sink(), settings, control=control,
                reconciler=_RecordingReconciler(broker, control),
            ))

    assert calls[0] is True  # 기동 대사는 force
    assert control.is_halted()
