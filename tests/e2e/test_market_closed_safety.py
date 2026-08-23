"""장 마감 시각 안전장치 — Clock이 시장이 닫혀 있다고 보고하면, 데이터가 아무리
그럴듯한 브레이크아웃을 보여줘도 신규 진입 신호가 나가면 안 된다.

WHY THIS EXISTS와 이어지는 관점: 조립/계약 결함이 다 고쳐져도, 전략이
Clock.is_market_open()을 무시하도록 배선되면 여전히 장외 시간에 주문이 나갈 수
있다. 여기서는 "데이터가 없어서" 못 사는 게 아니라(look-ahead류 결함과는 다른
경로) — 유효한 브레이크아웃 데이터가 있어도 Clock이 닫혀 있다고 하면 진입하지
않는지를 직접 본다. 마지막 테스트는 실제 SimClock으로 토요일 하루를 통째로
리플레이해서, 그 판단이 fake clock의 우연이 아니라 실제 세션 캘린더 로직에서
나온다는 것까지 확인한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.trade.loop import run_cycle
from quant.core.clock import SimClock
from quant.core.fx import FixedFxProvider
from quant.core.ports import Context
from quant.core.models import Quote
from quant.adapters.execution.paper import PaperBroker
from quant.adapters.persistence.sink import MultiSink
from quant.core.portfolio.portfolio import Portfolio
from quant.trade.risk.manager import RiskManagerImpl
from quant.trade.strategy.donchian import DonchianStrategy

NY = ZoneInfo("America/New_York")
_SYMBOL = "TQQQ"
_MARKET_OF = {_SYMBOL: "US"}
_RISK_CFG = {"risk": {"max_position_pct": 50, "max_symbol_pct_total": 0, "daily_loss_limit_pct": 3}}


class _ScriptedFeed:
    def __init__(self, symbol: str, bars: pd.DataFrame, price: float):
        self.symbol = symbol
        self._bars = bars
        self.price = price

    def quote(self, symbol: str) -> Quote | None:
        if symbol != self.symbol:
            return None
        return Quote(symbol=symbol, ts=self._bars.index[-1], price=self.price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        if symbol != self.symbol:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return self._bars.tail(n)


class _MarketClosedClock:
    """is_market_open이 항상 False — 그 외에는 데이터가 있으면 진입 조건이 충족되는
    시나리오로 세팅해서, "오직 Clock의 판단만으로" 진입이 막히는지를 격리해서 본다."""

    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, market: str) -> bool:
        return False

    def minutes_to_close(self, market: str) -> float | None:
        return None

    def cadence_minutes(self) -> float:
        return 15.0

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


class _CapturingSink:
    def __init__(self):
        self.signals = []
        self.fills = []

    def on_signal(self, signal) -> None:
        self.signals.append(signal)

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


def _build(bars, entry_price, tmp_path, donchian_params, clock):
    fx = FixedFxProvider(1000.0)
    data = _ScriptedFeed(_SYMBOL, bars, entry_price)
    portfolio = Portfolio(cash=10_000_000.0, state_path=tmp_path / "portfolio.json")
    broker = PaperBroker(data=data, portfolio=portfolio, fee_bps=7, market_of=_MARKET_OF, fx=fx)
    risk = RiskManagerImpl(_RISK_CFG, capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF, fx=fx)
    strategy = DonchianStrategy(symbols=[_SYMBOL], params=donchian_params(), market="US", id="donchian")
    ctx = Context(clock=clock, data=data, broker=broker)
    return strategy, ctx, risk, portfolio


def test_no_entry_signal_when_market_closed_despite_valid_breakout(make_breakout_bars, donchian_params, tmp_path):
    bars = make_breakout_bars(41, breakout=True)
    entry_price = float(bars.iloc[-1]["close"])
    clock = _MarketClosedClock(bars.index[-1] + timedelta(hours=6))  # 장 마감 이후 시각
    strategy, ctx, risk, portfolio = _build(bars, entry_price, tmp_path, donchian_params, clock)

    signals = strategy.on_cycle(ctx)

    assert signals == []


def test_run_cycle_places_no_orders_when_market_closed(make_breakout_bars, donchian_params, tmp_path):
    bars = make_breakout_bars(41, breakout=True)
    entry_price = float(bars.iloc[-1]["close"])
    clock = _MarketClosedClock(bars.index[-1] + timedelta(hours=6))
    strategy, ctx, risk, portfolio = _build(bars, entry_price, tmp_path, donchian_params, clock)
    capture = _CapturingSink()
    cash_before = portfolio.cash

    run_cycle([strategy], ctx, risk, MultiSink([capture]))

    assert capture.signals == []
    assert capture.fills == []
    assert portfolio.cash == pytest.approx(cash_before)
    assert _SYMBOL not in portfolio.positions or not portfolio.positions[_SYMBOL].is_open


def test_weekend_replay_produces_zero_signals_across_a_full_day(make_breakout_bars, donchian_params, tmp_path):
    """토요일 하루(15분 간격, 96스텝) 내내 같은 유효 브레이크아웃 데이터를 던져도
    단 한 번도 진입 신호가 나오지 않아야 한다. fake clock이 아니라 실제 SimClock을
    써서, 세션 캘린더(quant/core/clock.py의 _is_open) 자체가 주말을 올바르게
    닫힌 상태로 판정하고, 그 판정이 전략의 진입 게이트까지 실제로 전달되는지를
    함께 확인한다."""
    bars = make_breakout_bars(41, breakout=True)
    entry_price = float(bars.iloc[-1]["close"])
    saturday = datetime(2026, 1, 10, 0, 0, tzinfo=NY)
    assert saturday.weekday() == 5  # 2026-01-10은 실제로 토요일

    all_signals = []
    for minutes in range(0, 24 * 60, 15):
        now = saturday + timedelta(minutes=minutes)
        clock = SimClock(now=now, cadence_minutes=15.0)
        assert not clock.is_market_open("US")  # 전제 확인: 토요일은 하루 종일 닫혀 있다
        strategy, ctx, risk, portfolio = _build(bars, entry_price, tmp_path, donchian_params, clock)
        all_signals.extend(strategy.on_cycle(ctx))

    assert all_signals == []
