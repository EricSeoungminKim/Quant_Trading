"""전체 파이프라인 통합 테스트: strategy.on_cycle -> risk.approve -> broker.place_order
-> sinks.on_fill을 real RiskManagerImpl/PaperBroker/Portfolio/MultiSink로 실행한다.

이 테스트가 지키는 불변식: 각 레이어가 mock으로 대체되면 "제대로 배선됐는지"를
검증할 수 없다 — 유닛 테스트는 각 레이어를 따로 mock으로 격리해서 통과했지만,
실제로는 조립(assembly)이 깨져서 라이브에서 신호가 0건 체결됐다(이 스위트가
생기게 된 배경, tests/e2e/conftest 상단 및 CLAUDE.md 참고). 여기서는 mock 호출
여부가 아니라 real cash/position 숫자가 실제로 기대한 만큼 바뀌었는지를 본다.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from quant.trade.loop import run_cycle
from quant.core.fx import FixedFxProvider
from quant.core.ports import Context
from quant.core.models import Quote, Side
from quant.adapters.execution.paper import PaperBroker
from quant.adapters.persistence.sink import MultiSink
from quant.core.portfolio.portfolio import Portfolio, to_krw
from quant.trade.risk.manager import RiskManagerImpl
from quant.trade.strategy.donchian import DonchianStrategy


class _CapturingSink:
    """실제 EventSink 구현 — on_signal/on_fill 호출 결과를 그대로 보관한다
    (call-was-made가 아니라 실제 페이로드를 검증하기 위함)."""

    def __init__(self):
        self.signals = []
        self.fills = []

    def on_signal(self, signal) -> None:
        self.signals.append(signal)

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


class _ScriptedFeed:
    """quote()는 현재 지정된 가격을, history()는 고정된 봉을 반환하는 최소 DataFeed.
    스크립트대로 가격을 바꿔가며(진입가 -> 손절가) 실제 체결/청산을 유도한다."""

    def __init__(self, symbol: str, bars, price: float):
        self.symbol = symbol
        self._bars = bars
        self.price = price

    def quote(self, symbol: str) -> Quote | None:
        if symbol != self.symbol:
            return None
        return Quote(symbol=symbol, ts=datetime.now(self._bars.index[-1].tzinfo), price=self.price)

    def history(self, symbol: str, interval: str, n: int):
        if symbol != self.symbol:
            import pandas as pd
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return self._bars.tail(n)


def _build_pipeline(bars, entry_price: float, tmp_path, start_cash: float = 10_000_000.0):
    symbol = "TQQQ"
    market_of = {symbol: "US"}
    fx = FixedFxProvider(1_000.0)  # 라운드 넘버 환율 — 수기 계산 검증을 쉽게 하려는 목적
    data = _ScriptedFeed(symbol, bars, entry_price)
    portfolio = Portfolio(cash=start_cash, state_path=tmp_path / "portfolio.json")
    broker = PaperBroker(data=data, portfolio=portfolio, fee_bps=7, market_of=market_of, fx=fx)
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 50, "max_symbol_pct_total": 0, "daily_loss_limit_pct": 3}},
        capital_fraction={"donchian": 1.0}, market_of=market_of, fx=fx,
    )
    strategy = DonchianStrategy(symbols=[symbol], params={
        "interval_minutes": 15, "lookback_bars": 40, "volume_mult": 1.5, "stop_fallback_pct": 1.5,
        "risk_reward": 2.0, "max_concurrent_names": 1, "flatten_before_close_minutes": 10,
        "min_session_bars_before_entry": 0,
    }, market="US", id="donchian")

    class _AlwaysOpenClock:
        def now(self):
            return datetime(2026, 1, 5, 20, 0, tzinfo=bars.index[-1].tzinfo)

        def is_market_open(self, market: str) -> bool:
            return True

        def minutes_to_close(self, market: str) -> float | None:
            return 120.0

        def cadence_minutes(self) -> float:
            return 15.0

        def should_flatten(self, market: str, flatten_minutes: float) -> bool:
            return False

    ctx = Context(clock=_AlwaysOpenClock(), data=data, broker=broker)
    sink = _CapturingSink()
    return strategy, ctx, risk, sink, portfolio, fx, market_of


def test_breakout_produces_a_real_fill_that_moves_cash_and_position(make_breakout_bars, tmp_path):
    bars = make_breakout_bars(41, breakout=True)
    entry_price = float(bars.iloc[-1]["close"])
    strategy, ctx, risk, sink, portfolio, fx, market_of = _build_pipeline(bars, entry_price, tmp_path)
    cash_before = portfolio.cash

    run_cycle([strategy], ctx, risk, MultiSink([sink]))

    assert len(sink.signals) == 1
    assert len(sink.fills) == 1
    fill = sink.fills[0]
    assert fill.symbol == "TQQQ"
    assert fill.side is Side.BUY
    assert fill.price == pytest.approx(entry_price)

    # risk.approve가 실제로 계산한 qty(=fill.qty)로 cash/position이 정확히 변했는지 검증.
    # (order sizing 공식 자체가 아니라, "그 사이징이 브로커/포트폴리오에 실제로 반영됐는가"를 본다)
    expected_fee = fill.qty * entry_price * 7 / 10_000
    expected_notional = fill.qty * entry_price + expected_fee
    assert portfolio.cash == pytest.approx(cash_before - to_krw(expected_notional, "US", fx))

    pos = portfolio.positions["TQQQ"]
    assert pos.is_open
    assert pos.qty == pytest.approx(fill.qty)
    assert pos.avg_cost == pytest.approx(entry_price)

    # 사이징 하드레일 자체도 실제로 걸렸는지 sanity-check: max_position_pct=50%이므로
    # 예산이 전략자본의 절반을 넘지 않는다.
    assert to_krw(fill.qty * entry_price, "US", fx) <= 0.5 * cash_before * (1 + 1e-9)


def test_stop_hit_produces_exit_fill_with_realized_loss(make_breakout_bars, tmp_path):
    bars = make_breakout_bars(41, breakout=True)
    entry_price = float(bars.iloc[-1]["close"])
    strategy, ctx, risk, sink, portfolio, fx, market_of = _build_pipeline(bars, entry_price, tmp_path)

    # 1) 진입
    run_cycle([strategy], ctx, risk, MultiSink([sink]))
    entry_fill = sink.fills[0]
    cash_after_entry = portfolio.cash
    stop = float(bars.iloc[-1]["low"])  # _params(stop_fallback_pct=1.5)에서 low가 stop을 결정(see test_donchian.py)
    assert stop < entry_price

    # 2) 가격을 손절가 아래로 스크립트 -> 다음 사이클에서 손절 청산
    exit_price = stop - 0.5
    ctx.data.price = exit_price

    run_cycle([strategy], ctx, risk, MultiSink([sink]))

    assert len(sink.fills) == 2
    exit_fill = sink.fills[1]
    assert exit_fill.symbol == "TQQQ"
    assert exit_fill.side is Side.SELL
    assert exit_fill.price == pytest.approx(exit_price)
    assert exit_fill.qty == pytest.approx(entry_fill.qty)  # 전량 청산(exit_fraction=1.0)

    # 포지션은 완전히 닫혀야 한다
    pos = portfolio.positions["TQQQ"]
    assert not pos.is_open
    assert pos.qty == 0.0

    # 실현 손익 = (청산가-진입가)*qty - 청산수수료 만큼 cash가 늘어야 한다(손실이므로 순유입은 감소분보다 작음)
    exit_fee = exit_fill.qty * exit_price * 7 / 10_000
    expected_cash_delta = to_krw(exit_fill.qty * exit_price - exit_fee, "US", fx)
    assert portfolio.cash == pytest.approx(cash_after_entry + expected_cash_delta)
    assert exit_price < entry_price  # 손절이므로 진입가보다 낮은 가격에 청산됐어야 함
    realized_pnl = (exit_price - entry_price) * exit_fill.qty - exit_fee
    assert realized_pnl < 0  # 손절 시나리오이므로 실현손익은 음수
