"""소유자 지시(2026-08-31): "잔고 부족으로 못 산 경우, 시도 기록을 남겨 나중에
'안 산 게 아니라 못 샀던 것'이 되게." risk.approve()가 예산 부족으로 order를
만들지 못하면(order is None) 기존 코드는 로그만 남기고 return했다 — 정상 경로
(place_order 이후 _emit_order_state)를 전혀 타지 않으므로 orders.jsonl에 아무
흔적도 안 남는 구멍이었다. quant/trade/loop.py `_execute_signal`이 이제 그 구멍을
메운다: reason에 "자금 부족" 마커가 있으면 qty=0짜리 not_submitted OrderState를
만들어 sinks.on_order로 흘려보낸다.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.core.fx import FixedFxProvider
from quant.core.models import OrderStatus, Quote, Signal, SignalAction
from quant.core.ports import Context
from quant.trade.loop import _execute_signal
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
SYMBOL = "TQQQ"
MARKET_OF = {SYMBOL: "US"}
FX_RATE = 1500.0
PRICE = 100.0


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=NOW, price=PRICE)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class _Broker:
    """가용 현금이 1주 값(100달러×1,500원=150,000원)에 한참 못 미치는 100원 —
    "자산 0 이하" 게이트는 통과하되 배분 사이징에서 0주로 내림돼야 한다."""

    def positions(self):
        return {}

    def cash(self) -> float:
        return 100.0

    def place_order(self, order):
        raise AssertionError("자금 부족 거부는 place_order까지 가면 안 된다")


class _RecordingSink:
    def __init__(self):
        self.orders: list = []

    def on_signal(self, signal):
        pass

    def on_fill(self, fill):
        pass

    def on_order(self, state):
        self.orders.append(state)


def _ctx(fake_clock_cls, broker) -> Context:
    return Context(clock=fake_clock_cls(now=NOW), data=_Data(), broker=broker)


def _risk() -> RiskManagerImpl:
    cfg = dict(
        sizing_mode="cash_pct",
        max_position_pct=100, max_symbol_pct_total=0, daily_loss_limit_pct=100,
        max_orders_per_day=1000, cooldown_bars_after_stop=0, max_order_notional_pct=0,
        max_total_exposure_pct=0, max_concurrent_positions=0,
    )
    return RiskManagerImpl(
        {"risk": cfg}, capital_fraction={"s": 1.0}, market_of=MARKET_OF,
        fx=FixedFxProvider(FX_RATE),
    )


def _entry() -> Signal:
    return Signal(strategy_id="s", symbol=SYMBOL, action=SignalAction.ENTER_LONG,
                  target_weight=1.0, reason="테스트 진입")


def test_insufficient_funds_rejection_is_recorded_to_the_order_sink(fake_clock_cls):
    risk = _risk()
    sink = _RecordingSink()
    ctx = _ctx(fake_clock_cls, _Broker())

    _execute_signal(_entry(), ctx, risk, sink, notifier=None)

    assert "자금 부족" in risk.last_block  # 전제: risk 레이어가 실제로 이 사유로 막았다
    assert len(sink.orders) == 1
    state = sink.orders[0]
    assert state.status == OrderStatus.REJECTED
    assert state.broker_order_id is None  # 브로커에 닿지 못했다 — not_submitted
    assert "자금 부족" in state.reason
    assert state.order.symbol == SYMBOL
    assert state.order.strategy_id == "s"
    assert state.updated_at is not None  # 시각이 남는다


def test_other_rejection_reasons_are_not_recorded_to_the_sink(fake_clock_cls):
    """자금 부족이 아닌 다른 거부(예: 장 마감)는 기존 동작 그대로 — 로그만 남고
    sink에는 기록되지 않는다(이 변경의 영향 범위를 자금 부족 사유로 한정)."""
    risk = _risk()
    sink = _RecordingSink()
    ctx = Context(
        clock=fake_clock_cls(now=NOW, market_open=False), data=_Data(), broker=_Broker(),
    )

    _execute_signal(_entry(), ctx, risk, sink, notifier=None)

    assert "자금 부족" not in risk.last_block
    assert sink.orders == []
