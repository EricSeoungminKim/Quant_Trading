"""엔진 회복탄력성 테스트: 사이클 예외 격리/에스컬레이션, 킬 스위치(halt/flatten),
하트비트(텔레그램 + 상태 파일), 세션 마감 요약, 데이터 스테일 가드.
전부 오프라인 페이크만 사용 — 네트워크/sleep 없음.

run_paper_loop은 무한루프라 asyncio.sleep을 패치해 지정한 사이클 수만큼만 돌리고
_StopLoop으로 빠져나온다(실제 대기 없이 결정론적으로 N 사이클 구동)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from quant.apps.config import Settings
from quant.trade.control import TradingControl
from quant.trade.loop import (
    CycleTimings, _build_marks_and_unpriced, _flatten_all, _retry_pending_flatten,
    run_cycle, run_paper_loop,
)
from quant.core.ports import Context
from quant.core.models import Fill, Order, Position, Quote, Side, Signal, SignalAction
from quant.trade.risk.manager import MARKET_CLOSED_MARKER

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


# 하트비트 상태 파일 기본 경로 격리는 tests/conftest.py의 autouse 픽스처가 담당한다.


# --------------------------------------------------------------------- fakes

class FakeClock:
    """market_open을 시장별로 다르게 줄 수 있다 — KR/US 마감이 각각 감지되는지 보려면
    필수다. per_market이 없으면 모든 시장에 같은 값을 쓴다(기존 동작)."""

    def __init__(self, market_open: bool = True, per_market: dict[str, bool] | None = None):
        self._market_open = market_open
        self.per_market = per_market

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def is_market_open(self, market: str) -> bool:
        if self.per_market is not None:
            return self.per_market.get(market, self._market_open)
        return self._market_open

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return 0.1

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


class FakeDataFeed:
    def quote(self, symbol: str) -> Quote | None:
        return Quote(symbol=symbol, ts=datetime.now(timezone.utc), price=100.0)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)


class FakeBroker:
    """qty=0 이하가 되면 포지션을 완전히 닫는다(실제 PaperBroker와 동일한 축소판)."""

    def __init__(
        self,
        positions: dict[str, Position] | None = None,
        cash: float = 1_000_000.0,
        fee: float = 0.0,
        market_of: dict[str, str] | None = None,
    ):
        self._positions = positions or {}
        self._cash = cash
        self._fee = fee  # 체결당 수수료(표시 통화) — 세션 마감 요약의 수수료 합계 검증용
        self.market_of = market_of or {}  # PaperBroker와 같은 부가 속성(duck-typing)
        self.orders: list[Order] = []

    def place_order(self, order: Order):
        self.orders.append(order)
        pos = self._positions.get(order.symbol)
        if order.side is Side.BUY:
            if pos is None:
                pos = Position(symbol=order.symbol, qty=0.0, avg_cost=0.0)
                self._positions[order.symbol] = pos
            pos.qty += order.qty
            pos.avg_cost = 100.0
        else:
            if pos is None or order.qty <= 0:
                return None
            pos.qty = max(pos.qty - order.qty, 0.0)
            if pos.qty <= 1e-9:
                pos.qty = 0.0
        return Fill(
            symbol=order.symbol, side=order.side, qty=order.qty, price=100.0,
            ts=datetime.now(timezone.utc), strategy_id=order.strategy_id, reason=order.reason,
            fee=self._fee,
        )

    def positions(self) -> dict[str, Position]:
        return self._positions

    def cash(self) -> float:
        return self._cash


class FakeRisk:
    """ENTER/SCALE_IN은 무조건 승인(qty=1), EXIT/SCALE_OUT은 보유 수량 x exit_fraction."""

    def __init__(self, day_pnl_pct: float | None = None):
        self.calls: list[Signal] = []
        self.last_block = ""
        self.day_pnl_pct = day_pnl_pct

    def breaker_state(self) -> dict:
        """RiskManagerImpl.breaker_state()와 같은 형태의 최소 스냅샷 — 세션 마감 요약이
        이 구조를 duck-typing으로 읽는다."""
        return {
            "day": "2026-08-08",
            "max_orders_per_day": {"count": len(self.calls), "limit": 20, "tripped": False},
            "daily_loss_limit_pct": {
                "limit_pct": 3.0, "day_pnl_pct": self.day_pnl_pct, "tripped": False,
            },
            "cooldown_bars_after_stop": {"limit_bars": 3, "symbols_in_cooldown": []},
        }

    def approve(self, signal: Signal, ctx: Context, risk_multiplier: float = 1.0, marks=None):
        self.calls.append(signal)
        if signal.action in (SignalAction.ENTER_LONG, SignalAction.SCALE_IN):
            return Order(symbol=signal.symbol, side=Side.BUY, qty=1.0,
                         strategy_id=signal.strategy_id, reason=signal.reason)
        pos = ctx.broker.positions().get(signal.symbol)
        qty = (pos.qty if pos is not None else 0.0) * signal.exit_fraction
        if qty <= 0:
            self.last_block = "보유 없음"
            return None
        return Order(symbol=signal.symbol, side=Side.SELL, qty=qty,
                     strategy_id=signal.strategy_id, reason=signal.reason)


class FakeMarketGatedRisk:
    """FakeRisk와 동일하되, `closed_symbols`에 있는 심볼은 항상
    RiskManagerImpl.approve()의 실제 장 마감 게이트(MARKET_CLOSED_MARKER)처럼
    막는다 — pending_flatten 재시도 배관을 실제 리스크 판정 로직 없이 검증한다."""

    def __init__(self, closed_symbols: frozenset[str] = frozenset()):
        self.closed_symbols = set(closed_symbols)
        self.calls: list[Signal] = []
        self.last_block = ""

    def approve(self, signal: Signal, ctx: Context, risk_multiplier: float = 1.0, marks=None):
        self.calls.append(signal)
        self.last_block = ""  # 실제 RiskManagerImpl.approve()처럼 매 호출 시작에 리셋
        if signal.symbol in self.closed_symbols:
            self.last_block = f"{MARKET_CLOSED_MARKER} — 주문 불가 (테스트)"
            return None
        if signal.action in (SignalAction.ENTER_LONG, SignalAction.SCALE_IN):
            return Order(symbol=signal.symbol, side=Side.BUY, qty=1.0,
                         strategy_id=signal.strategy_id, reason=signal.reason)
        pos = ctx.broker.positions().get(signal.symbol)
        qty = (pos.qty if pos is not None else 0.0) * signal.exit_fraction
        if qty <= 0:
            self.last_block = "보유 없음"
            return None
        return Order(symbol=signal.symbol, side=Side.SELL, qty=qty,
                     strategy_id=signal.strategy_id, reason=signal.reason)


class _FixedClock:
    """`_retry_pending_flatten`의 게이트(is_market_open + in_continuous_session)를
    결정론적으로 테스트하기 위한 고정 시각 클록. `now`는 tz-aware UTC datetime."""

    def __init__(self, now: datetime, market_open: dict[str, bool]):
        self._now = now
        self._market_open = market_open

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, market: str) -> bool:
        return self._market_open.get(market, False)

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return 0.1

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


# 2026-01-05(월) 10:00 ET = US 연속 거래 구간(09:30~16:00 ET) 한복판.
_US_CONTINUOUS_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
# 2026-01-05(월) 10:00 KST = KR 연속 거래 구간(09:00~15:20 KST) 한복판.
_KR_CONTINUOUS_NOW = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
# 동시호가 구간(US 09:00 ET, 정규장 09:30 전) — is_market_open은 True일 수 있어도
# in_continuous_session은 False여야 한다.
_US_PREOPEN_NOW = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)


class FakeSink:
    def __init__(self, fail_on_signal: bool = False):
        self.signals: list[Signal] = []
        self.fills: list = []
        self.fail_on_signal = fail_on_signal

    def on_signal(self, signal: Signal) -> None:
        if self.fail_on_signal:
            raise RuntimeError("sink boom")
        self.signals.append(signal)

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


class FakeStrategy:
    def __init__(self, id: str = "fake", signals_fn=None, raise_always: bool = False):
        self.id = id
        self.symbols = ["TQQQ"]
        self._signals_fn = signals_fn or (lambda: [])
        self.raise_always = raise_always
        self.calls = 0

    def on_cycle(self, ctx: Context) -> list[Signal]:
        self.calls += 1
        if self.raise_always:
            raise RuntimeError("strategy boom")
        return self._signals_fn()


@dataclass
class _FakeHealth:
    degraded: bool


class FakeMarketData:
    def __init__(self, degraded: bool = False):
        self.degraded = degraded

    def health(self) -> _FakeHealth:
        return _FakeHealth(degraded=self.degraded)


def make_settings(tmp_path, engine_overrides: dict | None = None) -> Settings:
    path = tmp_path / "settings.yaml"
    path.write_text("engine:\n  poll_seconds: 0\n")
    raw = {"engine": {"poll_seconds": 0, **(engine_overrides or {})}}
    return Settings(raw=raw, path=path)


class _StopLoop(Exception):
    """run_paper_loop을 정확히 N 사이클만 돌리기 위한 탈출 신호."""


async def _drive_n_cycles(n: int, after_cycle=None, **kwargs) -> None:
    """after_cycle(cycle_number)은 각 사이클이 끝난 직후(다음 사이클 시작 전) 호출된다 —
    장중/마감 전환을 사이클 사이에 끼워 넣기 위한 훅."""
    state = {"count": 0}

    async def fake_sleep(_seconds):
        state["count"] += 1
        if after_cycle is not None:
            after_cycle(state["count"])
        if state["count"] >= n:
            raise _StopLoop()

    with patch("quant.trade.loop.asyncio.sleep", fake_sleep):
        with pytest.raises(_StopLoop):
            await run_paper_loop(**kwargs)


# -------------------------------------------------------- _build_marks_and_unpriced: 시장 게이트

class _RecordingDataFeed:
    """quote() 호출을 기록한다 — 장이 닫힌 심볼은 애초에 호출되지 않아야 한다."""

    def __init__(self, prices: dict[str, float] | None = None):
        self.prices = prices or {}
        self.calls: list[str] = []

    def quote(self, symbol: str) -> Quote | None:
        self.calls.append(symbol)
        price = self.prices.get(symbol)
        if price is None:
            return None
        return Quote(symbol=symbol, ts=datetime.now(timezone.utc), price=price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)


def test_closed_market_symbol_is_never_quoted_and_never_unpriced():
    """US 세션 도중 KR 종목처럼 장이 닫힌 심볼은 quote()를 부르지 않고, 그래서
    시세가 없어도 '시세 끊김'(unpriced)으로 오보하지 않는다 — 장 마감은 장애가
    아니다(2026-08-12 실측 695건 오탐의 원인)."""
    positions = {
        "TQQQ": Position(symbol="TQQQ", qty=1.0, avg_cost=50.0),
        "005930": Position(symbol="005930", qty=1.0, avg_cost=70000.0),
    }
    broker = FakeBroker(positions=positions)
    data = _RecordingDataFeed(prices={"TQQQ": 55.0, "005930": 71000.0})
    clock = FakeClock(per_market={"US": True, "KR": False})
    ctx = Context(clock=clock, data=data, broker=broker)

    marks, unpriced = _build_marks_and_unpriced(ctx)

    assert data.calls == ["TQQQ"]  # KR(005930)은 조회 자체를 하지 않음
    assert marks == {"TQQQ": 55.0}
    assert unpriced == []  # 005930은 장이 닫혀서 없는 것 — 끊긴 게 아니다


def test_open_market_symbol_without_quote_is_still_unpriced():
    """회귀 가드: 장이 열려 있는데 시세를 못 받으면 여전히 unpriced로 잡혀야 한다 —
    시장 게이트가 진짜 시세 끊김까지 가려서는 안 된다."""
    positions = {"TQQQ": Position(symbol="TQQQ", qty=1.0, avg_cost=50.0)}
    broker = FakeBroker(positions=positions)
    data = _RecordingDataFeed(prices={})  # quote 없음
    clock = FakeClock(per_market={"US": True})
    ctx = Context(clock=clock, data=data, broker=broker)

    marks, unpriced = _build_marks_and_unpriced(ctx)

    assert data.calls == ["TQQQ"]
    assert marks == {}
    assert unpriced == ["TQQQ"]


# --------------------------------------------------------------------- run_cycle: 격리/halt

def test_one_strategy_raising_doesnt_stop_others():
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink()
    bad = FakeStrategy(id="bad", raise_always=True)
    good_signal = Signal(strategy_id="good", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=1.0)
    good = FakeStrategy(id="good", signals_fn=lambda: [good_signal])

    run_cycle([bad, good], ctx, risk, sinks)

    assert len(risk.calls) == 1
    assert len(sinks.fills) == 1


def test_halt_blocks_new_entries_but_permits_exits(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.halt("테스트 중단")

    broker = FakeBroker(positions={"TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=90.0)})
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=broker)
    risk = FakeRisk()
    sinks = FakeSink()
    enter = Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=1.0)
    exit_ = Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0)
    strat = FakeStrategy(signals_fn=lambda: [enter, exit_])

    run_cycle([strat], ctx, risk, sinks, control=control)

    # 진입은 risk.approve까지 가지도 못하고 스킵, 청산만 실제로 승인/체결됐어야 한다
    assert len(risk.calls) == 1
    assert risk.calls[0].action is SignalAction.EXIT_LONG
    assert len(sinks.fills) == 1
    assert sinks.fills[0].side is Side.SELL
    assert broker.positions()["TQQQ"].qty == 0.0


# --------------------------------------------------------------------- run_paper_loop: 예외 격리/에스컬레이션

def test_cycle_exception_is_logged_reported_and_loop_continues(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    settings = make_settings(tmp_path, {"max_consecutive_cycle_failures": 100})
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink(fail_on_signal=True)  # run_cycle 내부에서 잡히지 않는 예외를 유발
    signal = Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=1.0)
    strat = FakeStrategy(signals_fn=lambda: [signal])
    notifier = FakeNotifier()

    asyncio.run(_drive_n_cycles(
        3, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control,
    ))

    assert strat.calls == 3  # 매 사이클 실패해도 다음 사이클이 계속 돈다
    assert not control.is_halted()  # 임계치(100) 미달이라 아직 중단 안 됨
    assert any("사이클 실패" in m for m in notifier.messages)


def test_n_consecutive_failures_escalate_to_halt(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    settings = make_settings(tmp_path, {"max_consecutive_cycle_failures": 2})
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink(fail_on_signal=True)
    signal = Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=1.0)
    strat = FakeStrategy(signals_fn=lambda: [signal])
    notifier = FakeNotifier()

    asyncio.run(_drive_n_cycles(
        3, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control,
    ))

    assert control.is_halted() is True
    assert "연속 2회" in control.halt_reason()
    assert any("거래 자동 중단" in m for m in notifier.messages)


# --------------------------------------------------------------------- flatten

def test_flatten_is_one_shot_and_clears(tmp_path):
    control_path = tmp_path / "control.json"
    control = TradingControl(state_path=control_path)
    control.request_flatten()

    positions = {
        "TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=90.0),
        "SQQQ": Position(symbol="SQQQ", qty=5.0, avg_cost=20.0),
    }
    broker = FakeBroker(positions=positions)
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=broker)
    risk = FakeRisk()
    sinks = FakeSink()
    strat = FakeStrategy(signals_fn=lambda: [
        Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=1.0)
    ])
    settings = make_settings(tmp_path)
    notifier = FakeNotifier()

    asyncio.run(_drive_n_cycles(
        1, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control,
    ))

    assert strat.calls == 0  # flatten 사이클엔 일반 전략 사이클을 돌리지 않는다
    assert broker.positions()["TQQQ"].qty == 0.0
    assert broker.positions()["SQQQ"].qty == 0.0
    assert any("청산 완료" in m for m in notifier.messages)

    # one-shot: 소비된 뒤엔 재시작해도 다시 청산 트리거 안 됨
    fresh = TradingControl(state_path=control_path)
    assert fresh.consume_flatten() is False


# ------------------------------------------ flatten: 장 마감 보류 → 개장 시 자동 재시도
#
# 결함(2026-09-03/09-04 실측): /flatten이 장 마감 중이면 대상 종목을 로그로만
# 알리고 요청 자체를 소비해버려, 개장 후 아무도 재시도하지 않았다 — 소유자가
# 매번 수동으로 다시 요청해야 했다. 아래는 그 수리(control.pending_flatten +
# loop._retry_pending_flatten)를 고정한다.

def test_flatten_all_parks_market_closed_symbols_and_persists_pending(tmp_path):
    """장이 닫힌 종목은 청산되지 않고 control.json에 pending으로 남는다 — 열린
    시장 종목은 그 사이클에 정상 청산된다(부분 실행)."""
    positions = {
        "005930": Position(symbol="005930", qty=10.0, avg_cost=70000.0),  # 닫힘
        "TQQQ": Position(symbol="TQQQ", qty=5.0, avg_cost=50.0),          # 열림
    }
    broker = FakeBroker(positions=positions)
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=broker)
    risk = FakeMarketGatedRisk(closed_symbols=frozenset({"005930"}))
    sinks = FakeSink()
    notifier = FakeNotifier()
    control = TradingControl(state_path=tmp_path / "control.json")

    blocked = _flatten_all(ctx, risk, sinks, notifier, scope="all", control=control)

    assert blocked == {"005930"}
    assert broker.positions()["005930"].qty == 10.0  # 청산되지 않음
    assert broker.positions()["TQQQ"].qty == 0.0      # 정상 청산

    pending = control.pending_flatten()
    assert pending is not None
    assert pending["scope"] == "all"
    assert pending["symbols"] == ["005930"]
    assert any("자동으로 재시도" in m for m in notifier.messages)


def test_flatten_all_without_control_still_returns_blocked_but_does_not_persist():
    """control을 안 주는 기존 호출부(테스트 등)는 예전처럼 동작 — 반환값으로
    블록된 종목을 알 수는 있지만 저장은 하지 않는다(하위호환)."""
    positions = {"005930": Position(symbol="005930", qty=10.0, avg_cost=70000.0)}
    broker = FakeBroker(positions=positions)
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=broker)
    risk = FakeMarketGatedRisk(closed_symbols=frozenset({"005930"}))
    sinks = FakeSink()

    blocked = _flatten_all(ctx, risk, sinks, notifier=None, scope="all")

    assert blocked == {"005930"}


def test_retry_pending_flatten_noop_when_nothing_pending(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(
        clock=_FixedClock(_US_CONTINUOUS_NOW, {"US": True}),
        data=FakeDataFeed(), broker=FakeBroker(),
    )
    risk = FakeMarketGatedRisk()
    _retry_pending_flatten(ctx, risk, FakeSink(), None, control, books=None)
    assert risk.calls == []  # 대기가 없으면 approve()를 아예 부르지 않는다


def test_retry_pending_flatten_noop_while_market_still_closed(tmp_path):
    """대기 중인데 아직 개장 전이면 이번 사이클엔 아무 시도도 하지 않는다 —
    매 사이클 헛되이 신호를 내지 않는다(로그 스팸 방지 원칙)."""
    control = TradingControl(state_path=tmp_path / "control.json")
    control.set_pending_flatten("all", ["TQQQ"])

    ctx = Context(
        clock=_FixedClock(_US_CONTINUOUS_NOW, {"US": False}),  # 아직 닫힘
        data=FakeDataFeed(), broker=FakeBroker(positions={
            "TQQQ": Position(symbol="TQQQ", qty=5.0, avg_cost=50.0),
        }),
    )
    risk = FakeMarketGatedRisk()
    _retry_pending_flatten(ctx, risk, FakeSink(), None, control, books=None)

    assert risk.calls == []
    pending = control.pending_flatten()
    assert pending is not None
    assert pending["symbols"] == ["TQQQ"]


def test_retry_pending_flatten_noop_during_preopen_auction_even_if_market_open_flag_true(tmp_path):
    """동시호가처럼 `is_market_open`은 True여도 연속 거래 구간이 아니면 재시도하지
    않는다 — risk.approve가 실제로 쓰는 게이트와 같은 기준."""
    control = TradingControl(state_path=tmp_path / "control.json")
    control.set_pending_flatten("all", ["TQQQ"])

    ctx = Context(
        clock=_FixedClock(_US_PREOPEN_NOW, {"US": True}),
        data=FakeDataFeed(), broker=FakeBroker(positions={
            "TQQQ": Position(symbol="TQQQ", qty=5.0, avg_cost=50.0),
        }),
    )
    risk = FakeMarketGatedRisk()
    _retry_pending_flatten(ctx, risk, FakeSink(), None, control, books=None)

    assert risk.calls == []
    assert control.pending_flatten()["symbols"] == ["TQQQ"]


def test_retry_pending_flatten_executes_when_market_opens_and_clears_pending(tmp_path):
    """장 마감으로 보류됐던 종목이 개장하면 다음 사이클에 자동으로 청산되고
    pending에서 지워진다 — 이 결함의 핵심 요구사항."""
    control = TradingControl(state_path=tmp_path / "control.json")
    control.set_pending_flatten("all", ["TQQQ"])

    broker = FakeBroker(positions={"TQQQ": Position(symbol="TQQQ", qty=5.0, avg_cost=50.0)})
    ctx = Context(clock=_FixedClock(_US_CONTINUOUS_NOW, {"US": True}), data=FakeDataFeed(), broker=broker)
    risk = FakeMarketGatedRisk()
    sinks = FakeSink()
    notifier = FakeNotifier()

    _retry_pending_flatten(ctx, risk, sinks, notifier, control, books=None)

    assert broker.positions()["TQQQ"].qty == 0.0
    assert control.pending_flatten() is None
    assert any("보류됐던 청산 실행" in m for m in notifier.messages)


def test_retry_pending_flatten_survives_engine_restart(tmp_path):
    """엔진 프로세스가 재시작돼도(새 TradingControl 인스턴스가 같은 파일을 읽어도)
    보류됐던 청산이 개장 시 그대로 재시도된다."""
    path = tmp_path / "control.json"
    first = TradingControl(state_path=path)
    first.set_pending_flatten("all", ["TQQQ"])

    restarted = TradingControl(state_path=path)
    broker = FakeBroker(positions={"TQQQ": Position(symbol="TQQQ", qty=5.0, avg_cost=50.0)})
    ctx = Context(clock=_FixedClock(_US_CONTINUOUS_NOW, {"US": True}), data=FakeDataFeed(), broker=broker)
    risk = FakeMarketGatedRisk()

    _retry_pending_flatten(ctx, risk, FakeSink(), None, restarted, books=None)

    assert broker.positions()["TQQQ"].qty == 0.0
    assert restarted.pending_flatten() is None
    # 재확인: control.json에서 새로 읽어도 대기가 사라져 있어야 한다.
    assert TradingControl(state_path=path).pending_flatten() is None


def test_retry_pending_flatten_drops_symbol_already_flat_without_order(tmp_path):
    """대기 중이던 종목의 포지션이 (다른 경로로) 이미 사라졌으면 주문 없이
    pending에서만 지운다."""
    control = TradingControl(state_path=tmp_path / "control.json")
    control.set_pending_flatten("all", ["TQQQ"])

    broker = FakeBroker(positions={})  # 이미 청산됨/보유 없음
    ctx = Context(clock=_FixedClock(_US_CONTINUOUS_NOW, {"US": True}), data=FakeDataFeed(), broker=broker)
    risk = FakeMarketGatedRisk()
    sinks = FakeSink()

    _retry_pending_flatten(ctx, risk, sinks, None, control, books=None)

    assert risk.calls == []  # 신호 자체가 만들어지지 않는다 — 주문 없음
    assert sinks.signals == []
    assert control.pending_flatten() is None  # 그래도 대기 목록에서는 지워진다


def test_retry_pending_flatten_retries_only_the_now_open_symbol_kr_us_mixed(tmp_path):
    """KR/US가 섞인 대기열에서 US만 개장했으면 US만 재시도하고 KR은 계속 대기."""
    control = TradingControl(state_path=tmp_path / "control.json")
    control.set_pending_flatten("all", ["TQQQ", "005930"])

    broker = FakeBroker(positions={
        "TQQQ": Position(symbol="TQQQ", qty=5.0, avg_cost=50.0),
        "005930": Position(symbol="005930", qty=10.0, avg_cost=70000.0),
    })
    ctx = Context(
        clock=_FixedClock(_US_CONTINUOUS_NOW, {"US": True, "KR": False}),
        data=FakeDataFeed(), broker=broker,
    )
    risk = FakeMarketGatedRisk()

    _retry_pending_flatten(ctx, risk, FakeSink(), None, control, books=None)

    assert broker.positions()["TQQQ"].qty == 0.0
    assert broker.positions()["005930"].qty == 10.0  # KR은 아직 손대지 않음
    pending = control.pending_flatten()
    assert pending is not None
    assert pending["symbols"] == ["005930"]


# --------------------------------------------------------------------- 하트비트

def test_heartbeat_emits_at_cadence_and_includes_halted_state(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.halt("점검")
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink()
    strat = FakeStrategy()
    settings = make_settings(tmp_path, {"heartbeat_minutes": 0, "telegram_heartbeat": True})  # 매 사이클 발생하도록
    notifier = FakeNotifier()
    market_data = FakeMarketData(degraded=False)

    asyncio.run(_drive_n_cycles(
        2, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control, market_data=market_data,
    ))

    # 2026-08-31 강화: 정지 중 하트비트는 "점검" 부속 줄이 아니라 머리기사다 —
    # 사유와 재개 방법(/resume)을 함께 보여야 한다(실사고: "중단됨" 한 줄만으로
    # 월요일 세션 전체가 무체결로 방치됐다).
    heartbeats = [m for m in notifier.messages if "거래 중단이 계속되고" in m]
    assert len(heartbeats) >= 1
    assert "점검" in heartbeats[0]          # halt("점검") 의 사유가 표시된다
    assert "/resume" in heartbeats[0]


def test_heartbeat_silent_when_market_closed(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(clock=FakeClock(market_open=False), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink()
    strat = FakeStrategy()
    settings = make_settings(tmp_path, {"heartbeat_minutes": 0, "telegram_heartbeat": True})
    notifier = FakeNotifier()

    asyncio.run(_drive_n_cycles(
        2, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control, active_markets=frozenset({"US"}),
    ))

    assert not any("엔진 상태 점검" in m for m in notifier.messages)


# --------------------------------------------------------------------- 데이터 스테일 가드

def test_staleness_guard_fires_once_after_n_stale_cycles(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink()
    strat = FakeStrategy()
    settings = make_settings(tmp_path, {"max_consecutive_stale_data": 2, "heartbeat_minutes": 999})
    notifier = FakeNotifier()
    market_data = FakeMarketData(degraded=True)

    asyncio.run(_drive_n_cycles(
        4, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control, market_data=market_data,
    ))

    stale_msgs = [m for m in notifier.messages if "시세 조회 연속" in m]
    assert len(stale_msgs) == 1  # 임계치 넘는 순간 한 번만 — 매 사이클 스팸 방지


def test_staleness_guard_does_not_fire_when_market_closed(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(clock=FakeClock(market_open=False), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink()
    strat = FakeStrategy()
    settings = make_settings(tmp_path, {"max_consecutive_stale_data": 2, "heartbeat_minutes": 999})
    notifier = FakeNotifier()
    market_data = FakeMarketData(degraded=True)

    asyncio.run(_drive_n_cycles(
        5, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control, market_data=market_data, active_markets=frozenset({"US"}),
    ))

    assert not any("데이터 소스" in m for m in notifier.messages)


# --------------------------------------------------------------------- 사이클 지연 계측

def test_run_cycle_fills_timings_per_stage():
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink()
    signal = Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=1.0)
    strat = FakeStrategy(id="donchian", signals_fn=lambda: [signal])
    timings = CycleTimings()

    run_cycle([strat], ctx, risk, sinks, timings=timings)

    assert "donchian" in timings.strategy_ms
    assert timings.strategy_ms["donchian"] >= 0.0
    assert timings.risk_approve_ms >= 0.0
    assert timings.broker_place_order_ms >= 0.0
    assert timings.sinks_ms >= 0.0
    assert timings.total_ms > 0.0
    # 총 소요시간은 개별 단계 합보다 항상 크거나 같아야 한다(루프 오버헤드 포함).
    assert timings.total_ms >= (
        sum(timings.strategy_ms.values())
        + timings.risk_approve_ms + timings.broker_place_order_ms + timings.sinks_ms
    )


def test_run_cycle_without_timings_arg_is_unaffected():
    """timings를 안 넘기면(backtest 경로) 계측 코드가 아예 스킵되고 동작은 이전과 동일하다."""
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink()
    signal = Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=1.0)
    strat = FakeStrategy(signals_fn=lambda: [signal])

    run_cycle([strat], ctx, risk, sinks)  # 예외 없이 그대로 동작해야 한다

    assert len(sinks.fills) == 1


def test_slow_cycle_emits_warning_with_breakdown(tmp_path, caplog):
    """slow_cycle_warn_ms=0으로 두면 실측 소요시간이 무엇이든 임계를 넘어 WARNING이 찍힌다."""
    control = TradingControl(state_path=tmp_path / "control.json")
    settings = make_settings(tmp_path, {"slow_cycle_warn_ms": 0, "heartbeat_minutes": 999})
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink()
    strat = FakeStrategy(id="donchian")
    notifier = FakeNotifier()

    with caplog.at_level("WARNING", logger="quant.trade.loop"):
        asyncio.run(_drive_n_cycles(
            1, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
            notifier=notifier, control=control,
        ))

    warnings = [r for r in caplog.records if "느린 사이클" in r.message]
    assert len(warnings) == 1
    assert "donchian" in warnings[0].message
    assert "risk.approve" in warnings[0].message
    assert "broker.place_order" in warnings[0].message


def test_heartbeat_includes_cycle_latency_line(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink()
    strat = FakeStrategy()
    settings = make_settings(tmp_path, {"heartbeat_minutes": 0, "telegram_heartbeat": True})
    notifier = FakeNotifier()

    asyncio.run(_drive_n_cycles(
        2, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control,
    ))

    heartbeats = [m for m in notifier.messages if "엔진 상태 점검" in m]
    assert len(heartbeats) >= 1
    assert any("처리 속도" in m for m in heartbeats)


def test_heartbeat_includes_breaker_line(tmp_path):
    """탈진(트립)된 레일이 장 마감 세션 요약까지 안 보이던 결함 — 하트비트에도
    회로차단기 상태가 세션 마감 요약과 같은 한 줄로 찍혀야 한다."""
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk(day_pnl_pct=-0.05)  # FakeRisk.breaker_state()가 tripped=False로 굳어있어도 라인 자체는 찍혀야 한다
    sinks = FakeSink()
    strat = FakeStrategy()
    settings = make_settings(tmp_path, {"heartbeat_minutes": 0, "telegram_heartbeat": True})
    notifier = FakeNotifier()

    asyncio.run(_drive_n_cycles(
        2, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control,
    ))

    heartbeats = [m for m in notifier.messages if "엔진 상태 점검" in m]
    assert len(heartbeats) >= 1
    assert any("안전장치" in m for m in heartbeats)


# --------------------------------------------------------------------- 하트비트 상태 파일

def test_heartbeat_file_is_refreshed_every_cycle(tmp_path):
    """외부 워치독(cron 등)이 읽는 파일 — 사이클마다 갱신돼야 "행(hang)"이 드러난다."""
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    settings = make_settings(tmp_path, {"heartbeat_minutes": 999})
    hb_path = tmp_path / "state" / "heartbeat.json"
    snapshots: list[dict] = []

    asyncio.run(_drive_n_cycles(
        3, strategies=[FakeStrategy()], ctx=ctx, risk=FakeRisk(), sinks=FakeSink(),
        settings=settings, control=control, heartbeat_path=hb_path,
        after_cycle=lambda _n: snapshots.append(json.loads(hb_path.read_text(encoding="utf-8"))),
    ))

    assert len(snapshots) == 3
    # cycle은 엄격히 증가해야 한다 — 파일이 실제로 매 사이클 다시 쓰였다는 증거.
    # ts는 벽시계라 같은 사이클 안에서 동률이 날 수 있어 단조 비감소로만 본다.
    assert [s["cycle"] for s in snapshots] == [1, 2, 3]
    assert snapshots[0]["ts"] <= snapshots[1]["ts"] <= snapshots[2]["ts"]
    assert snapshots[-1]["ts"] > 0
    assert snapshots[-1]["market_open"] is True
    assert snapshots[-1]["halted"] is False
    assert snapshots[-1]["last_cycle_ms"] >= 0.0


def test_heartbeat_file_reflects_halted_and_closed_market(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    control.halt("점검")
    ctx = Context(clock=FakeClock(market_open=False), data=FakeDataFeed(), broker=FakeBroker())
    settings = make_settings(tmp_path, {"heartbeat_minutes": 999})
    hb_path = tmp_path / "heartbeat.json"

    asyncio.run(_drive_n_cycles(
        1, strategies=[FakeStrategy()], ctx=ctx, risk=FakeRisk(), sinks=FakeSink(),
        settings=settings, control=control, active_markets=frozenset({"US"}),
        heartbeat_path=hb_path,
    ))

    payload = json.loads(hb_path.read_text(encoding="utf-8"))
    assert payload["halted"] is True
    assert payload["market_open"] is False


def test_heartbeat_file_write_failure_does_not_kill_loop(tmp_path, caplog):
    """부모가 파일이라 mkdir부터 실패하는 경로. 거래는 계속되고 경고는 1회만."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    settings = make_settings(tmp_path, {"heartbeat_minutes": 999})
    strat = FakeStrategy()

    with caplog.at_level("WARNING", logger="quant.trade.loop"):
        asyncio.run(_drive_n_cycles(
            3, strategies=[strat], ctx=ctx, risk=FakeRisk(), sinks=FakeSink(),
            settings=settings, control=control, heartbeat_path=blocker / "heartbeat.json",
        ))

    assert strat.calls == 3  # 파일을 못 써도 사이클은 그대로 돈다
    warnings = [r for r in caplog.records if "하트비트 파일 쓰기 실패" in r.message]
    assert len(warnings) == 1  # 매 사이클 스팸 금지


# --------------------------------------------------------------------- 세션 마감 요약

def _close_after(clock: FakeClock, market: str, cycle: int):
    """cycle번째 사이클이 끝난 뒤 해당 시장을 마감 상태로 바꾸는 after_cycle 훅 팩토리."""
    def _hook(n: int) -> None:
        if n == cycle:
            clock.per_market[market] = False
    return _hook


def test_session_summary_sent_exactly_once_on_close(tmp_path):
    clock = FakeClock(per_market={"US": True})
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(clock=clock, data=FakeDataFeed(), broker=FakeBroker())
    settings = make_settings(tmp_path, {"heartbeat_minutes": 999})
    notifier = FakeNotifier()

    # 1사이클 개장 → 이후 계속 마감. 마감 사이클이 3번 돌아도 요약은 1회여야 한다.
    asyncio.run(_drive_n_cycles(
        4, strategies=[FakeStrategy()], ctx=ctx, risk=FakeRisk(day_pnl_pct=0.42),
        sinks=FakeSink(), settings=settings, notifier=notifier, control=control,
        active_markets=frozenset({"US"}), after_cycle=_close_after(clock, "US", 1),
    ))

    summaries = [m for m in notifier.messages if "세션 마감" in m]
    assert len(summaries) == 1
    assert "미국 시장" in summaries[0]
    assert "오늘 손익: 🔺 +0.42%" in summaries[0]
    assert "안전장치" in summaries[0]


def test_no_session_summary_when_market_already_closed_at_startup(tmp_path):
    """기동 시점이 장외면 "마감"이 아니다 — 유령 요약이 나가면 안 된다."""
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(clock=FakeClock(market_open=False), data=FakeDataFeed(), broker=FakeBroker())
    settings = make_settings(tmp_path, {"heartbeat_minutes": 999})
    notifier = FakeNotifier()

    asyncio.run(_drive_n_cycles(
        3, strategies=[FakeStrategy()], ctx=ctx, risk=FakeRisk(), sinks=FakeSink(),
        settings=settings, notifier=notifier, control=control,
        active_markets=frozenset({"US"}),
    ))

    assert not any("세션 마감" in m for m in notifier.messages)


def test_kr_and_us_close_are_reported_independently(tmp_path):
    """KR 마감(15:30 KST)과 US 마감은 다른 사건이다. any()로 뭉치면 KR 마감이
    US 세션에 가려 영영 알림되지 않는다."""
    clock = FakeClock(per_market={"KR": True, "US": True})
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(clock=clock, data=FakeDataFeed(), broker=FakeBroker())
    settings = make_settings(tmp_path, {"heartbeat_minutes": 999})
    notifier = FakeNotifier()

    def hook(n: int) -> None:
        if n == 1:
            clock.per_market["KR"] = False   # KR만 먼저 마감, US는 아직 장중
        elif n == 2:
            clock.per_market["US"] = False

    asyncio.run(_drive_n_cycles(
        4, strategies=[FakeStrategy()], ctx=ctx, risk=FakeRisk(), sinks=FakeSink(),
        settings=settings, notifier=notifier, control=control,
        active_markets=frozenset({"KR", "US"}), after_cycle=hook,
    ))

    summaries = [m for m in notifier.messages if "세션 마감" in m]
    assert len(summaries) == 2
    assert "한국 시장" in summaries[0]
    assert "미국 시장" in summaries[1]


def test_session_summary_counts_fills_and_fees_then_resets(tmp_path):
    """비용 회계 한 줄: 세션 중 체결 수와 KRW 환산 수수료 합계. 다음 세션엔 0부터."""
    clock = FakeClock(per_market={"US": True})
    control = TradingControl(state_path=tmp_path / "control.json")
    # fee=1.5 USD/체결, 기본 fallback 환율 1500 → 체결 1건당 2,250원
    broker = FakeBroker(fee=1.5, market_of={"TQQQ": "US"})
    ctx = Context(clock=clock, data=FakeDataFeed(), broker=broker)
    settings = make_settings(tmp_path, {"heartbeat_minutes": 999})
    notifier = FakeNotifier()
    sinks = FakeSink()

    emitted = {"n": 0}

    def once_only():
        emitted["n"] += 1
        if emitted["n"] > 1:
            return []
        return [Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG,
                       target_weight=1.0)]

    def hook(n: int) -> None:
        if n == 1:
            clock.per_market["US"] = False   # 1세션 마감 (체결 1건)
        elif n == 2:
            clock.per_market["US"] = True    # 새 세션 개장
        elif n == 3:
            clock.per_market["US"] = False   # 2세션 마감 (체결 0건)

    asyncio.run(_drive_n_cycles(
        4, strategies=[FakeStrategy(signals_fn=once_only)], ctx=ctx, risk=FakeRisk(),
        sinks=sinks, settings=settings, notifier=notifier, control=control,
        active_markets=frozenset({"US"}), after_cycle=hook,
    ))

    summaries = [m for m in notifier.messages if "세션 마감" in m]
    assert len(summaries) == 2
    assert "체결 1건 · 수수료 합계 2,250원" in summaries[0]
    assert "체결 0건 · 수수료 합계 0원" in summaries[1]
    # 래퍼가 기존 sink를 가로채지 않고 그대로 통과시키는지 — 체결 로그는 여전히 남아야 한다
    assert len(sinks.fills) == 1


# ---------------------------------------------------------------------------
# 포지션 현황 리포트 (1분 주기, 보유 중일 때만) — 2026-08-10
# ---------------------------------------------------------------------------
def test_position_report_shows_entry_pnl_and_rails():
    from datetime import datetime, timedelta, timezone

    from quant.trade.loop import _position_report_text
    from quant.core.ports import Context
    from quant.core.models import Position

    now = datetime(2026, 8, 10, 9, 30, tzinfo=timezone.utc)
    pos = Position(symbol="088350", qty=363, avg_cost=4606.15,
                   opened_at=now - timedelta(minutes=18))
    pos.meta.update(entry=4606.15, stop=4522.0, target=None)

    class _Clock:
        def now(self):
            return now

    class _Broker:
        def positions(self):
            return {"088350": pos}

    ctx = Context(clock=_Clock(), data=None, broker=_Broker())
    text = _position_report_text(ctx, {"088350": 4650.0})

    assert "🏢 088350" in text and "(363주)" in text
    assert "평단가 4,606원" in text, "진입가가 아니라 평단가 표기 (2026-08-10)"
    assert "+0.95%" in text, "평단가 대비 수익률"
    assert "손절폭 대비" not in text, "R 표기는 제거됨 — 목표가로 대체"
    assert "🎯 목표가 없음" in text
    assert "손절가 4,522원" in text
    assert "보유 18분" in text


def test_position_report_header_shows_cash_and_invested_mixed_markets():
    """헤더 밑 잔고 요약: 남은 현금 + 주식 투자금(US는 KRW 환산) — 미장에서도
    동일하게 동작해야 한다(2026-08-10 사용자 요청)."""
    from datetime import datetime, timezone

    from quant.trade.loop import _position_report_text
    from quant.core.fx import FixedFxProvider
    from quant.core.ports import Context
    from quant.core.models import Position

    kr = Position(symbol="069500", qty=100, avg_cost=10_000.0)
    kr.meta.update(entry=10_000.0, stop=9_800.0, target=11_000.0)
    us = Position(symbol="TQQQ", qty=10, avg_cost=70.0)
    us.meta.update(entry=70.0, stop=68.0, target=None)

    class _Portfolio:
        cash = 5_000_000.0

    class _Broker:
        portfolio = _Portfolio()
        fx = FixedFxProvider(1_000.0)

        def positions(self):
            return {"069500": kr, "TQQQ": us}

    class _Clock:
        def now(self):
            return datetime(2026, 8, 10, 23, 40, tzinfo=timezone.utc)  # US 세션 시간대

    text = _position_report_text(Context(clock=_Clock(), data=None, broker=_Broker()),
                                 {"069500": 10_500.0, "TQQQ": 75.0})
    assert "🏦 남은 현금 5,000,000원" in text
    # 투자금 = 100x10,000 + 10x70x1,000(환산) = 1,700,000원
    assert "📦 주식 투자금 1,700,000원" in text
    assert "🎯 목표가 11,000원" in text, "KR 목표가 표기"
    assert "현재가 $75.00" in text, "US 종목은 달러 표기 그대로"


def test_position_report_footer_shows_daily_pnl():
    """맨 밑 금일 손익: 시작 자산 대비 ±원 + % (2026-08-10 사용자 요청)."""
    from datetime import datetime, timezone

    from quant.trade.loop import _position_report_text
    from quant.core.ports import Context
    from quant.core.models import Position

    pos = Position(symbol="069500", qty=100, avg_cost=10_000.0)
    pos.meta.update(entry=10_000.0, stop=9_800.0, target=None)

    class _Portfolio:
        cash = 9_000_000.0
        positions = {"069500": pos}

        def equity(self, prices, market_of=None, fx=None):
            return 10_050_000.0  # 현금 900만 + 100주x10,500

    class _Quote:
        price = 10_500.0

    class _Data:
        def quote(self, symbol):
            return _Quote()

    class _Broker:
        portfolio = _Portfolio()
        fx = None

        def positions(self):
            return {"069500": pos}

    class _Risk:
        def breaker_state(self):
            return {"daily_loss_limit_pct": {"day_start_equity": 10_000_000.0,
                                             "limit_pct": 3.0, "day_pnl_pct": 0.5,
                                             "tripped": False},
                    "max_orders_per_day": {"count": 1, "limit": 30, "tripped": False},
                    "cooldown_bars_after_stop": {"symbols_in_cooldown": []}}

    class _Clock:
        def now(self):
            return datetime(2026, 8, 10, 5, 0, tzinfo=timezone.utc)

    text = _position_report_text(Context(clock=_Clock(), data=_Data(), broker=_Broker()),
                                 {"069500": 10_500.0}, risk=_Risk())
    assert "📈 금일 손익 🔺 +50,000원 (+0.50%)" in text


def test_position_report_is_none_without_positions():
    from quant.trade.loop import _position_report_text
    from quant.core.ports import Context

    class _Broker:
        def positions(self):
            return {}

    assert _position_report_text(Context(clock=None, data=None, broker=_Broker()), {}) is None


def test_position_report_survives_missing_mark():
    """시세 조회 실패 종목도 리포트를 죽이지 않고 '미상'으로 남긴다."""
    from quant.trade.loop import _position_report_text
    from quant.core.ports import Context
    from quant.core.models import Position

    pos = Position(symbol="TQQQ", qty=10, avg_cost=70.0)
    pos.meta.update(entry=70.0, stop=68.0, target=None)

    class _Clock:
        def now(self):
            from datetime import datetime, timezone
            return datetime(2026, 8, 10, 9, 30, tzinfo=timezone.utc)

    class _Broker:
        def positions(self):
            return {"TQQQ": pos}

    text = _position_report_text(Context(clock=_Clock(), data=None, broker=_Broker()), {})
    assert "수익률 확인 불가" in text


# ---------------------------------------------------------------------------
# 포지션 meta 영속화 — 재시작이 손절을 2% 폴백으로 갈아치우는 것 방지 (2026-08-10)
# ---------------------------------------------------------------------------
def test_position_meta_is_persisted_when_strategy_writes_stop(tmp_path):
    """전략이 _ensure_state로 meta를 채운 뒤 주문이 없어도 디스크에 반영돼야 한다.
    실측 버그: save()가 주문 시점에만 불려 디스크 meta가 {}로 남았다."""
    from quant.trade.loop import _persist_position_meta
    from quant.core.ports import Context
    from quant.core.models import Position
    from quant.core.portfolio.portfolio import Portfolio

    pos = Position(symbol="088350", qty=363, avg_cost=4606.15)
    portfolio = Portfolio(cash=1000.0, positions={"088350": pos},
                          state_path=tmp_path / "portfolio.json")
    portfolio.save()

    class _Broker:
        def __init__(self):
            self.portfolio = portfolio

        def positions(self):
            return portfolio.positions

    ctx = Context(clock=None, data=None, broker=_Broker())
    sig = _persist_position_meta(ctx, "")

    pos.meta.update(entry=4606.15, stop=4516.09, target=None)  # 전략이 나중에 채움
    sig2 = _persist_position_meta(ctx, sig)
    assert sig2 != sig, "meta가 바뀌면 시그니처도 바뀐다"

    reloaded = Portfolio.load_or_init(start_cash=0.0, state_path=tmp_path / "portfolio.json")
    assert reloaded.positions["088350"].meta["stop"] == 4516.09, "손절이 디스크에 남아야 한다"


def test_persist_position_meta_skips_when_unchanged(tmp_path):
    """변경이 없으면 쓰지 않는다 — 5초마다 tmp-replace는 불필요한 I/O."""
    from quant.trade.loop import _persist_position_meta
    from quant.core.ports import Context
    from quant.core.models import Position
    from quant.core.portfolio.portfolio import Portfolio

    pos = Position(symbol="TQQQ", qty=10, avg_cost=70.0)
    pos.meta.update(entry=70.0, stop=68.0)
    portfolio = Portfolio(cash=1.0, positions={"TQQQ": pos},
                          state_path=tmp_path / "p.json")
    portfolio.save()

    class _Broker:
        def __init__(self):
            self.portfolio = portfolio

        def positions(self):
            return portfolio.positions

    ctx = Context(clock=None, data=None, broker=_Broker())
    sig = _persist_position_meta(ctx, "")
    before = (tmp_path / "p.json").stat().st_mtime_ns
    assert _persist_position_meta(ctx, sig) == sig
    assert (tmp_path / "p.json").stat().st_mtime_ns == before, "변경 없으면 재기록 없음"


def test_persist_position_meta_noop_for_broker_without_portfolio():
    """실거래 브로커처럼 portfolio를 노출하지 않으면 아무 것도 하지 않는다."""
    from quant.trade.loop import _persist_position_meta
    from quant.core.ports import Context

    class _Broker:
        def positions(self):
            return {}

    assert _persist_position_meta(Context(clock=None, data=None, broker=_Broker()), "x") == "x"


# ================ 유니버스 롤 경계: US 개장 직전 (2026-08-11)

def test_universe_roll_has_a_boundary_before_us_open():
    """21:50 US 자동 편입분이 자정이 아니라 **US 개장 전**에 흡수돼야 한다.

    경계가 없으면 US 세션의 앞 1.5~2.5시간(개장 5분 돌파 창 포함)을 통째로
    놓친다 — KR이 08:27 경계 덕분에 동시호가부터 반영되는 것과 비대칭이었다.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from quant.trade.loop import _universe_roll_bucket

    kst = ZoneInfo("Asia/Seoul")
    d = lambda h, m: _universe_roll_bucket(datetime(2026, 8, 11, h, m, tzinfo=kst))  # noqa: E731

    # 자정~08:27: 직전 US 세션 마지막 경계(04:30)가 이월된다(같은 날짜 문자열 안).
    assert d(0, 5) == "2026-08-11"
    assert d(0, 30) == "2026-08-11+0030"  # US 장중 30분 경계(2026-08-28)
    assert d(4, 30) == "2026-08-11+0430"
    assert d(8, 26) == "2026-08-11+0430", "US 마감(05:00)~08:27 사이엔 새 경계가 없다"
    # KR 동시호가(08:30) 직전 경계 (2026-08-17 전진: 08:57 → 08:27)
    assert d(8, 27) == "2026-08-11+0827"
    # 장중 30분 경계(2026-08-28 소유자 지시 — flow-scan 장중 편입 흡수)
    assert d(9, 29) == "2026-08-11+0827"
    assert d(9, 30) == "2026-08-11+0930"
    assert d(13, 59) == "2026-08-11+1330"
    assert d(14, 53) == "2026-08-11+1453"  # 2026-08-25 종가배팅 체인: 14:00→14:53
    assert d(15, 30) == "2026-08-11+1453", "KR 마감까지 14:53 유지 — 종가배팅 진입 창(15:00~15:19)에 경계 없음"
    # 21:50 US 발굴 직후 ~ 22:10 전
    assert d(22, 9) == "2026-08-11+1453"
    # US 개장 직전 경계 — 여기서 새 US 종목이 흡수된다
    assert d(22, 10) == "2026-08-11+2210"
    assert d(22, 59) == "2026-08-11+2210"
    assert d(23, 0) == "2026-08-11+2300"
    assert d(23, 59) == "2026-08-11+2330"


def test_universe_roll_buckets_are_strictly_ordered_within_a_day():
    """경계가 4개로 늘어도 하루 안에서 버킷은 단조 증가해야 한다 — 되돌아가면
    같은 롤이 두 번 일어나거나 흡수가 통째로 스킵된다."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from quant.trade.loop import _universe_roll_bucket

    kst = ZoneInfo("Asia/Seoul")
    t = datetime(2026, 8, 11, 0, 0, tzinfo=kst)
    seen: list[str] = []
    while t.day == 11:
        b = _universe_roll_bucket(t)
        if not seen or b != seen[-1]:
            seen.append(b)
        t += timedelta(minutes=1)
    # 2026-08-28: 장중 30분 경계 추가(소유자 지시 — flow-scan 장중 편입 흡수).
    # 체인 경계 3개(0827/1453/2210)는 그대로 존재해야 한다.
    assert len(seen) == len(set(seen)), "버킷이 되돌아가면 같은 롤이 두 번 돈다"
    for must in ("2026-08-11+0827", "2026-08-11+1453", "2026-08-11+2210"):
        assert must in seen
    us_intra = [s for s in seen if s.split("+")[-1] in ("0030", "0430", "2300", "2330")]
    kr_intra = [s for s in seen if s.split("+")[-1] in ("0930", "1200", "1430")]
    assert len(us_intra) == 4 and len(kr_intra) == 3, "장중 30분 경계가 살아 있어야 한다"


# ========== 전략 예외가 손절을 침묵시키는 문제 (2026-08-12 감사 A-1)

def test_broken_strategy_with_open_position_is_reported_not_silently_skipped():
    """전략이 on_cycle에서 죽으면 그 전략의 손절·목표가·EoD청산이 통째로 건너뛰어진다
    (포지션 관리가 전부 on_cycle 안에 있다). 예전에는 WARNING 로그만 남고 사이클은
    '성공'으로 집계돼 **청산이 멈춘 상태를 시스템이 정상이라고 보고**했다."""
    from quant.trade.loop import _strategies_with_open_lots
    from quant.core.models import Position

    class _Broker:
        def __init__(self, positions):
            self._p = positions

        def positions(self):
            return self._p

    class _Ctx:
        def __init__(self, broker):
            self.broker = broker

    held = Position(symbol="TQQQ", qty=10, avg_cost=100.0,
                    meta={"lots": {"donchian": {"qty": 10.0, "avg_cost": 100.0}}})
    ctx = _Ctx(_Broker({"TQQQ": held}))

    # 포지션을 든 전략이 죽었다 → 위험, 보고 대상
    assert _strategies_with_open_lots(ctx, {"donchian": "ValueError: boom"}) == ["donchian"]
    # 포지션이 없는 전략이 죽었다 → 진입 기회 손실일 뿐, 다음 사이클에 회복된다
    assert _strategies_with_open_lots(ctx, {"orb_scan": "ValueError: boom"}) == []
    # 에러가 없으면 아무것도 보고하지 않는다
    assert _strategies_with_open_lots(ctx, {}) == []


def test_untagged_open_position_counts_every_broken_strategy_as_risky():
    """랏 태그가 없으면 누가 소유자인지 알 수 없다 — 모르는 것을 안전하다고
    가정하지 않고 위험 쪽으로 센다."""
    from quant.trade.loop import _strategies_with_open_lots
    from quant.core.models import Position

    class _Broker:
        def positions(self):
            return {"AAA": Position(symbol="AAA", qty=5, avg_cost=1.0, meta={})}

    class _Ctx:
        broker = _Broker()

    assert _strategies_with_open_lots(_Ctx(), {"whoever": "boom"}) == ["whoever"]


def test_no_open_positions_means_broken_strategy_is_not_escalated():
    from quant.trade.loop import _strategies_with_open_lots
    from quant.core.models import Position

    class _Broker:
        def positions(self):
            return {"AAA": Position(symbol="AAA", qty=0.0, avg_cost=0.0, meta={})}

    class _Ctx:
        broker = _Broker()

    assert _strategies_with_open_lots(_Ctx(), {"donchian": "boom"}) == []


def test_cycle_records_strategy_error_so_the_loop_can_escalate():
    """run_cycle이 예외를 삼키더라도 그 사실이 timings에 남아야 한다."""
    from quant.trade.loop import CycleTimings, run_cycle
    from quant.adapters.persistence.sink import MultiSink

    class _Boom:
        id = "boom"
        symbols = ["AAA"]

        def on_cycle(self, ctx):
            raise ValueError("의도된 실패")

    class _Broker:
        def positions(self):
            return {}

        def cash(self):
            return 0.0

    class _Ctx:
        broker = _Broker()
        clock = None
        data = None

    timings = CycleTimings()
    run_cycle([_Boom()], _Ctx(), risk=None, sinks=MultiSink([]), timings=timings)
    assert "boom" in timings.strategy_errors
    assert "의도된 실패" in timings.strategy_errors["boom"]


# --------------------------------------------------------------------- 고아 포지션(A-6)

def test_find_orphan_lots_flags_lot_whose_owner_is_not_active():
    """`_owns` 규칙(lots 구조가 있으면 내 lot 없이는 입양하지 않음)의 이면 —
    그 lot의 주인 전략이 활성 목록에서 빠지면 아무 전략도 그 lot을 관리하지 않는다."""
    from quant.trade.loop import _find_orphan_lots

    positions = {
        "TQQQ": Position(
            symbol="TQQQ", qty=10.0, avg_cost=90.0,
            meta={"lots": {
                "active_strategy": {"qty": 4.0, "avg_cost": 90.0},
                "disabled_strategy": {"qty": 6.0, "avg_cost": 88.0},
            }},
        ),
    }
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker(positions=positions))

    orphans = _find_orphan_lots(ctx, active_strategy_ids={"active_strategy"})

    assert orphans == [("TQQQ", "disabled_strategy")]


def test_find_orphan_lots_ignores_legacy_flat_positions_and_closed_or_empty_lots():
    """lots 키 자체가 없는 레거시 포지션은 범위 밖(`_owns`가 이미 "미상 → 입양"으로
    처리)이고, qty<=0인 lot이나 닫힌 포지션은 orphan으로 세지 않는다."""
    from quant.trade.loop import _find_orphan_lots

    positions = {
        "LEGACY": Position(symbol="LEGACY", qty=5.0, avg_cost=10.0, meta={}),
        "CLOSED": Position(
            symbol="CLOSED", qty=0.0, avg_cost=10.0,
            meta={"lots": {"disabled_strategy": {"qty": 0.0}}},
        ),
        "ZERO_LOT": Position(
            symbol="ZERO_LOT", qty=3.0, avg_cost=10.0,
            meta={"lots": {"active_strategy": {"qty": 3.0}, "disabled_strategy": {"qty": 0.0}}},
        ),
    }
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker(positions=positions))

    assert _find_orphan_lots(ctx, active_strategy_ids={"active_strategy"}) == []


def test_orphan_position_is_alerted_once_and_resets_when_strategy_reactivated(tmp_path):
    """A-6: 소유 전략이 비활성인 lot을 가진 포지션은 1회 알림하고, 같은 상태가
    반복되는 사이클에는 재알림하지 않는다. 전략이 다시 켜져 그 lot을 다시 관리하기
    시작하면(활성 목록 복귀) 알림 상태가 리셋돼, 다시 꺼지면 재알림한다."""
    control = TradingControl(state_path=tmp_path / "control.json")
    positions = {
        "TQQQ": Position(
            symbol="TQQQ", qty=10.0, avg_cost=90.0,
            meta={"lots": {"disabled_strategy": {"qty": 10.0, "avg_cost": 90.0}}},
        ),
    }
    broker = FakeBroker(positions=positions)
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=broker)
    risk = FakeRisk()
    sinks = FakeSink()
    settings = make_settings(tmp_path)
    notifier = FakeNotifier()
    active_strat = FakeStrategy(id="active_strategy")  # disabled_strategy는 목록에 없음

    asyncio.run(_drive_n_cycles(
        3, strategies=[active_strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control,
    ))

    orphan_msgs = [m for m in notifier.messages if "고아 포지션" in m]
    assert len(orphan_msgs) == 1, "3사이클 반복해도 같은 고아는 1회만 알린다"
    assert "disabled_strategy" in orphan_msgs[0]
    assert "TQQQ" in orphan_msgs[0]

    # 전략이 다시 켜지면(활성 목록 복귀) 더 이상 orphan이 아니다 — 알림 상태 리셋
    notifier.messages.clear()
    reactivated = FakeStrategy(id="disabled_strategy")
    asyncio.run(_drive_n_cycles(
        1, strategies=[reactivated], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control,
    ))
    assert not any("고아 포지션" in m for m in notifier.messages)

    # 다시 꺼지면 재알림된다 — 리셋이 실제로 일어났다는 증거
    asyncio.run(_drive_n_cycles(
        1, strategies=[active_strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control,
    ))
    assert any("고아 포지션" in m for m in notifier.messages)


def test_no_orphan_alert_when_every_lot_owner_is_active(tmp_path):
    control = TradingControl(state_path=tmp_path / "control.json")
    positions = {
        "TQQQ": Position(
            symbol="TQQQ", qty=10.0, avg_cost=90.0,
            meta={"lots": {"active_strategy": {"qty": 10.0, "avg_cost": 90.0}}},
        ),
    }
    broker = FakeBroker(positions=positions)
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=broker)
    risk = FakeRisk()
    sinks = FakeSink()
    settings = make_settings(tmp_path)
    notifier = FakeNotifier()
    strat = FakeStrategy(id="active_strategy")

    asyncio.run(_drive_n_cycles(
        2, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control,
    ))

    assert not any("고아 포지션" in m for m in notifier.messages)


# ========== 하트비트 문구 (2026-08-12: "N번째 확인"이 무슨 뜻인지 물음)

def test_heartbeat_labels_the_number_as_cycles_not_checks():
    """cycle_count는 엔진이 한 바퀴 돈 횟수(poll 5초 → 분당 ~10회)인데 하트비트는
    30분마다 온다. "N번째 확인"이라고 쓰면 알림 횟수처럼 읽혀 숫자가 300~400씩
    점프하는 이유를 알 수 없다 — 세는 단위를 정확히 쓴다."""
    from quant.trade.loop import _heartbeat_text
    from quant.trade.control import TradingControl
    from quant.core.models import Position

    class _Broker:
        def positions(self):
            return {"TQQQ": Position(symbol="TQQQ", qty=0.0, avg_cost=0.0, meta={})}

        def cash(self):
            return 1_000_000.0

    class _Ctx:
        broker = _Broker()
        data = None
        clock = None

    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as d:
        control = TradingControl(state_path=_P(d) / "control.json")
        text = _heartbeat_text(4906, _Ctx(), control, None, uptime_seconds=3600 * 6 + 51 * 60)

    assert "번째 확인" not in text, "'확인'은 알림 횟수로 오해된다"
    assert "4,906번째 사이클" in text, "천단위 구분 + 정확한 단위"
    assert "가동 6시간 51분" in text, "재시작을 알아채려면 가동시간이 필요하다"


def test_heartbeat_omits_uptime_when_unknown():
    """가동시간을 모르면 그 줄만 빠지고 나머지는 정상이어야 한다."""
    from quant.trade.loop import _heartbeat_text
    from quant.trade.control import TradingControl

    class _Broker:
        def positions(self):
            return {}

        def cash(self):
            return 0.0

    class _Ctx:
        broker = _Broker()
        data = None
        clock = None

    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as d:
        control = TradingControl(state_path=_P(d) / "control.json")
        text = _heartbeat_text(12, _Ctx(), control, None)

    assert "12번째 사이클" in text
    assert "가동" not in text


def test_heartbeat_and_position_report_default_to_telegram_silent(tmp_path):
    """텔레그램 소음 다이어트(2026-08-25 소유자 지시) — 주기 발송은 기본 off.

    채팅에는 리포트 발행·체결만 남기고, 현재가/보유상태는 /status /balance
    (온디맨드)로 뺐다. 하트비트 **파일**(워치독)과 로그는 이 기본값과 무관하게
    계속 쓴다 — 여기서 검증하는 것은 '채팅으로의 발송'이 없다는 것뿐이다."""
    control = TradingControl(state_path=tmp_path / "control.json")
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    settings = make_settings(tmp_path, {"heartbeat_minutes": 0, "position_report_minutes": 0})
    notifier = FakeNotifier()

    asyncio.run(_drive_n_cycles(
        3, strategies=[FakeStrategy()], ctx=ctx, risk=FakeRisk(), sinks=FakeSink(),
        settings=settings, notifier=notifier, control=control,
        market_data=FakeMarketData(degraded=False),
    ))

    assert not any("엔진 상태 점검" in m for m in notifier.messages), "하트비트가 기본으로 채팅에 가면 안 된다"
    assert not any("보유 종목 현황" in m for m in notifier.messages), "포지션 현황이 기본으로 채팅에 가면 안 된다"


# --------------------------------------------------------------------- 전략 간 합산 노출 감시 (2026-08-30)
#
# quant.control.exposure(순수 계산)를 실제로 쓴다 — loop.py는 그 모듈을 직접
# 임포트할 수 없으므로(아키텍처 규칙), 이 클로저 주입 방식 자체가 배선이 맞는지
# 검증하는 게 이 테스트들의 핵심이다(단위 테스트는 tests/test_exposure.py가 담당).

def _make_exposure_check():
    from quant.control.exposure import build_report

    def _check(lots, prices, capital_krw):
        return build_report(lots=lots, prices=prices, capital_krw=capital_krw).to_dict()

    return _check


def test_exposure_alert_fires_once_per_cooldown_for_offsetting_pair(tmp_path):
    """TQQQ 롱 + SQQQ 롱 동시 보유(상쇄 쌍) — 매 사이클 조건이 계속 참이어도
    쿨다운(기본 60분) 안에서는 텔레그램 알림이 한 번만 나가야 한다."""
    positions = {
        "TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=70.0),
        "SQQQ": Position(symbol="SQQQ", qty=20.0, avg_cost=10.0),
    }
    positions["TQQQ"].meta["lots"] = {"donchian": {"qty": 10.0}}
    positions["SQQQ"].meta["lots"] = {"mean_reversion": {"qty": 20.0}}
    broker = FakeBroker(positions=positions)
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=broker)
    settings = make_settings(tmp_path, {"position_report_minutes": 0})
    notifier = FakeNotifier()

    asyncio.run(_drive_n_cycles(
        5, strategies=[FakeStrategy()], ctx=ctx, risk=FakeRisk(), sinks=FakeSink(),
        settings=settings, notifier=notifier,
        exposure_check=_make_exposure_check(),
    ))

    alerts = [m for m in notifier.messages if m.startswith("⚠️ 전략 간 합산 노출 경고")]
    assert len(alerts) == 1, "쿨다운 안에서는 5사이클이 지나도 한 번만 보내야 한다"
    assert "상쇄 쌍 보유 TQQQ/SQQQ" in alerts[0]


def test_exposure_check_silent_when_no_conflict(tmp_path):
    """단일 전략의 단일 보유(중복도 상쇄도 없음)면 텔레그램 알림이 없어야 한다
    — 평시엔 로그만이라는 설계 그대로."""
    pos = Position(symbol="TQQQ", qty=10.0, avg_cost=70.0)
    pos.meta["lots"] = {"donchian": {"qty": 10.0}}
    broker = FakeBroker(positions={"TQQQ": pos})
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=broker)
    settings = make_settings(tmp_path, {"position_report_minutes": 0})
    notifier = FakeNotifier()

    asyncio.run(_drive_n_cycles(
        3, strategies=[FakeStrategy()], ctx=ctx, risk=FakeRisk(), sinks=FakeSink(),
        settings=settings, notifier=notifier,
        exposure_check=_make_exposure_check(),
    ))

    assert not any(m.startswith("⚠️ 전략 간 합산 노출 경고") for m in notifier.messages)


def test_exposure_check_none_is_a_complete_noop(tmp_path):
    """exposure_check를 안 주면(호출부가 주입하지 않음) 관련 코드가 한 줄도
    안 돈다 — 기존 호출부(테스트 다수)가 그대로 통과해야 한다는 회귀 가드."""
    broker = FakeBroker(positions={"TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=70.0)})
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=broker)
    settings = make_settings(tmp_path, {"position_report_minutes": 0})
    notifier = FakeNotifier()

    asyncio.run(_drive_n_cycles(
        3, strategies=[FakeStrategy()], ctx=ctx, risk=FakeRisk(), sinks=FakeSink(),
        settings=settings, notifier=notifier,
    ))

    assert not any("합산 노출" in m for m in notifier.messages)
