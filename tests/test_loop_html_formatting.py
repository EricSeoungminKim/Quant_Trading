"""quant/trade/loop.py 엔진 알림의 HTML 유효성 (2026-09-04, L1 서식).

텔레그램 parse_mode=HTML은 태그가 안 맞으면 그 메시지 자체를 400으로 거부한다
(어댑터의 평문 폴백이 구제하지만, 애초에 태그가 맞아야 서식이 실제로 먹힌다).
여기서는 `quant.core.tgfmt`로 만든 대표적인 엔진 메시지들이 (1) 태그가 항상
짝이 맞고 (2) 4096자 이내인지만 고정한다 — 문구 자체는 다른 테스트
(`test_loop_fill_notification.py`, `test_loop_resilience.py`)가 이미 고정한다.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from quant.adapters.execution.paper import PaperBroker
from quant.core.fx import FixedFxProvider
from quant.core.models import Position, Quote, Signal, SignalAction
from quant.core.ports import Context
from quant.core.portfolio.portfolio import Portfolio
from quant.core import tgfmt
from quant.trade.control import TradingControl
from quant.trade.loop import (
    _execute_signal, _halt_notify_text, _heartbeat_text, _position_report_text,
)
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
SYMBOL = "TQQQ"
FX_RATE = 1500.0
PRICE = 100.0


def _assert_balanced_html(text: str) -> None:
    stack: list[str] = []
    for m in re.finditer(r"<(/?)([a-z]+)[^>]*>", text):
        closing, name = m.group(1), m.group(2)
        if not closing:
            stack.append(name)
        else:
            assert stack and stack[-1] == name, f"짝이 안 맞는 태그 </{name}> in: {text!r}"
            stack.pop()
    assert not stack, f"닫히지 않은 태그 {stack} in: {text!r}"
    assert len(text) <= tgfmt.MAX_CHARS


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=NOW, price=PRICE)

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

    def send(self, text: str) -> None:
        self.messages.append(text)


def _risk() -> RiskManagerImpl:
    cfg = dict(
        capital_mode="shared", sizing_mode="capital_fraction",
        max_position_pct=100, max_symbol_pct_total=0, daily_loss_limit_pct=100,
        max_orders_per_day=1000, cooldown_bars_after_stop=0, max_order_notional_pct=0,
        max_total_exposure_pct=0, max_concurrent_positions=0, min_order_notional_krw=0,
    )
    return RiskManagerImpl(
        {"risk": cfg}, capital_fraction={"a": 1.0},
        market_of={SYMBOL: "US"}, fx=FixedFxProvider(FX_RATE),
    )


def test_buy_fill_notification_is_valid_html(fake_clock_cls):
    data = _Data()
    fx = FixedFxProvider(FX_RATE)
    portfolio = Portfolio(cash=10_000_000.0, state_path=None)
    broker = PaperBroker(data=data, portfolio=portfolio, fee_bps=0.0,
                         market_of={SYMBOL: "US"}, fx=fx)
    ctx = Context(clock=fake_clock_cls(now=NOW), data=data, broker=broker)
    notifier = _Notifier()
    signal = Signal(
        strategy_id="a", symbol=SYMBOL, action=SignalAction.ENTER_LONG,
        target_weight=0.5, reason="테스트 진입 A&B <신호>", stop=95.0, target=110.0,
    )
    _execute_signal(signal, ctx, _risk(), _Sink(), notifier=notifier)
    assert len(notifier.messages) == 1
    _assert_balanced_html(notifier.messages[0])
    # reason의 &/< 도 이스케이프돼 리터럴 텍스트로 남아야 한다(태그로 오인 금지).
    assert "&amp;" in notifier.messages[0] or "A&B" not in notifier.messages[0]


def test_heartbeat_text_is_valid_html():
    class _Broker:
        def positions(self):
            return {"TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=100.0, meta={})}

    class _Ctx:
        broker = _Broker()
        data = None
        clock = None

    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as d:
        control = TradingControl(state_path=_P(d) / "control.json")
        normal = _heartbeat_text(100, _Ctx(), control, None, uptime_seconds=3600)
        control.halt("사유 A&B", by="test")
        halted = _heartbeat_text(101, _Ctx(), control, None, uptime_seconds=3700)

    _assert_balanced_html(normal)
    _assert_balanced_html(halted)


def test_halt_notify_text_is_valid_html_and_escapes_reason():
    text = _halt_notify_text("연속 3회 실패 <중단> & 재시도")
    _assert_balanced_html(text)
    assert "&lt;중단&gt;" in text
    assert "&amp;" in text


def test_position_report_text_is_valid_html():
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
    assert text is not None
    _assert_balanced_html(text)
