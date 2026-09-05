"""quant/trade/loop.py 매수 체결 텔레그램 알림 — 손절·익절 가격 표시.

소유자 지적(2026-08-26): "매수 할때 체결가, 총 금액, 전략, 종목, 수량 다
좋은데, 제일 중요한 손절 가격이랑 익절 가격들이 공식으로만 쓰여있어서 내가
읽을 수가 없잖아." 기존에는 전략의 `reason` 문자열(공학 표기 %.4g 포함)만
그대로 붙였다 — 이 파일은 매수 체결 알림에 **구조화된** 🛡 손절/🎯 익절 줄이
붙는지, 값이 없으면 그 줄 자체가 없는지(0/"없음"으로 위장 금지)를 고정한다.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.adapters.execution.paper import PaperBroker
from quant.core.fx import FixedFxProvider
from quant.core.models import Quote, Signal, SignalAction
from quant.core.ports import Context
from quant.core.portfolio.portfolio import Portfolio
from quant.trade.loop import _execute_signal
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
SYMBOL = "TQQQ"
KR_SYMBOL = "088350"
MARKET_OF = {SYMBOL: "US", KR_SYMBOL: "KR"}
FX_RATE = 1500.0
PRICE = 100.0
INITIAL_CASH = 10_000_000.0


class _Data:
    def __init__(self, price: float = PRICE, symbol: str = SYMBOL):
        self._price = price
        self._symbol = symbol

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=NOW, price=self._price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class _Sink:
    def on_signal(self, signal):
        pass

    def on_fill(self, fill):
        pass

    def on_order(self, state):
        pass


class _Notifier:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str, lane: str | None = None) -> None:
        self.messages.append(text)


def _ctx(fake_clock_cls, broker, data) -> Context:
    return Context(clock=fake_clock_cls(now=NOW), data=data, broker=broker)


def _broker(data, market_of=None) -> PaperBroker:
    fx = FixedFxProvider(FX_RATE)
    portfolio = Portfolio(cash=INITIAL_CASH, state_path=None)
    return PaperBroker(
        data=data, portfolio=portfolio, fee_bps=0.0, market_of=market_of or MARKET_OF, fx=fx,
    )


def _risk() -> RiskManagerImpl:
    cfg = dict(
        capital_mode="shared", sizing_mode="capital_fraction",
        max_position_pct=100, max_symbol_pct_total=0, daily_loss_limit_pct=100,
        max_orders_per_day=1000, cooldown_bars_after_stop=0, max_order_notional_pct=0,
        max_total_exposure_pct=0, max_concurrent_positions=0, min_order_notional_krw=0,
    )
    return RiskManagerImpl(
        {"risk": cfg}, capital_fraction={"a": 1.0}, market_of=MARKET_OF, fx=FixedFxProvider(FX_RATE),
    )


def _entry(symbol: str = SYMBOL, *, stop=None, target=None) -> Signal:
    return Signal(
        strategy_id="a", symbol=symbol, action=SignalAction.ENTER_LONG,
        target_weight=0.5, reason="테스트 진입", stop=stop, target=target,
    )


def test_buy_fill_shows_stop_and_target_lines(fake_clock_cls):
    data = _Data(price=PRICE)
    broker = _broker(data)
    ctx = _ctx(fake_clock_cls, broker, data)
    notifier = _Notifier()

    _execute_signal(_entry(stop=95.0, target=110.0), ctx, _risk(), _Sink(), notifier=notifier)

    assert len(notifier.messages) == 1
    msg = notifier.messages[0]
    assert "🛡 손절" in msg
    assert "95.00" in msg
    assert "-5.00%" in msg
    assert "🎯 익절" in msg
    assert "110.00" in msg
    assert "+10.00%" in msg


def test_buy_fill_omits_target_line_when_no_target(fake_clock_cls):
    """scalp_1m처럼 stop만 있고 고정 target이 없는 전략 — 익절 줄이 아예 없어야 한다."""
    data = _Data(price=PRICE)
    broker = _broker(data)
    ctx = _ctx(fake_clock_cls, broker, data)
    notifier = _Notifier()

    _execute_signal(_entry(stop=90.0, target=None), ctx, _risk(), _Sink(), notifier=notifier)

    msg = notifier.messages[0]
    assert "🛡 손절" in msg
    assert "🎯 익절" not in msg


def test_buy_fill_omits_both_lines_when_neither_set(fake_clock_cls):
    data = _Data(price=PRICE)
    broker = _broker(data)
    ctx = _ctx(fake_clock_cls, broker, data)
    notifier = _Notifier()

    _execute_signal(_entry(stop=None, target=None), ctx, _risk(), _Sink(), notifier=notifier)

    msg = notifier.messages[0]
    assert "🛡 손절" not in msg
    assert "🎯 익절" not in msg


def test_buy_fill_formats_kr_price_without_decimals(fake_clock_cls):
    data = _Data(price=100_000.0, symbol=KR_SYMBOL)
    broker = _broker(data)
    ctx = _ctx(fake_clock_cls, broker, data)
    notifier = _Notifier()

    _execute_signal(
        _entry(symbol=KR_SYMBOL, stop=95_000.0, target=110_000.0),
        ctx, _risk(), _Sink(), notifier=notifier,
    )

    msg = notifier.messages[0]
    assert "🛡 손절    95,000원 (-5.00%)" in msg
    assert "🎯 익절    110,000원 (+10.00%)" in msg


# ---------------------------------------------------------------- 매도 체결

def _exit_signal(symbol: str = SYMBOL) -> Signal:
    return Signal(
        strategy_id="a", symbol=symbol, action=SignalAction.EXIT_LONG,
        target_weight=0.0, exit_fraction=0.5, reason="테스트 청산",
    )


def test_sell_fill_shows_entry_and_stop(fake_clock_cls):
    """매도 메시지에도 진입가·손절선이 보인다(소유자: "매수랑 매도 메세지에서
    해결해줘"). 청산가만 보면 "얼마에 사서 어디서 끊기로 했었나"를 사유 문자열에서
    눈으로 파내야 한다."""
    data = _Data(price=PRICE)
    broker = _broker(data)
    ctx = _ctx(fake_clock_cls, broker, data)

    # 먼저 진입해 lot(entry/stop)을 만든다. 실제 전략은 진입 Signal 의
    # state_update 로 lot 에 진입가·손절선을 심는다(scalp_1m._build_entry 등).
    entry_sig = _entry(stop=95.0)
    entry_sig = Signal(
        strategy_id=entry_sig.strategy_id, symbol=entry_sig.symbol,
        action=entry_sig.action, target_weight=entry_sig.target_weight,
        reason=entry_sig.reason, stop=entry_sig.stop,
        state_update={"entry": PRICE, "stop": 95.0},
    )
    _execute_signal(entry_sig, ctx, _risk(), _Sink(), notifier=None)

    notifier = _Notifier()
    data._price = 97.0  # 청산가는 진입가보다 아래
    _execute_signal(_exit_signal(), ctx, _risk(), _Sink(), notifier=notifier)

    assert len(notifier.messages) == 1
    msg = notifier.messages[0]
    assert "🔴 매도 체결" in msg
    assert "💵 진입가" in msg and "100.00" in msg
    assert "97.00" in msg and "-3.00%" in msg
    assert "🛡 손절선" in msg and "95.00" in msg


def test_sell_fill_without_lot_omits_entry_line(fake_clock_cls):
    """lot 정보가 없으면(전량 청산으로 비었거나 레거시 경로) 줄을 만들지 않는다 —
    없는 값을 지어내지 않는다."""
    data = _Data(price=PRICE)
    broker = _broker(data)
    ctx = _ctx(fake_clock_cls, broker, data)
    _execute_signal(_entry(), ctx, _risk(), _Sink(), notifier=None)  # stop 없음

    notifier = _Notifier()
    _execute_signal(_exit_signal(), ctx, _risk(), _Sink(), notifier=notifier)

    msg = notifier.messages[0]
    assert "🛡 손절선" not in msg
