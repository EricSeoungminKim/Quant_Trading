"""`not_submitted` 경로의 `updated_at` — 2026-08-31 실측 결함.

EC2 `data/ledger/orders.jsonl`에 donchian SQQQ "매도 가능 수량 0" 거부가
`"ts": null`로 110건 쌓였다. 원인: `PaperBroker.place_order`가 `oms.not_submitted`를
부를 때 `at=`을 안 넘겨 `OrderState.updated_at`이 `None`으로 남았고,
`TradeLedgerSink.on_order`는 그 `None`을 그대로 `null`로 직렬화했다
(`quant/control/ledger.py`: `state.updated_at.isoformat() if state.updated_at else None`).

정상 체결 경로(`oms.report_fill(..., at=quote.ts)`)는 처음부터 `at`을 넘겼으므로
문제가 없었다 — `not_submitted` 네 갈래(시세 없음/매수 0/보유 없음/매도 가능 0)만
빠져 있었다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from quant.adapters.execution.paper import PaperBroker
from quant.core.models import Order, Quote, Position, Side
from quant.core.portfolio.portfolio import Portfolio


class _Feed:
    def __init__(self, price: float | None):
        self._price = price

    def quote(self, symbol: str):
        if self._price is None:
            return None
        return Quote(symbol=symbol, ts=datetime(2026, 8, 29, 0, 30, tzinfo=timezone.utc),
                     price=self._price)


def _broker(price: float | None = 100.0, positions: dict | None = None) -> PaperBroker:
    return PaperBroker(
        data=_Feed(price),
        portfolio=Portfolio(cash=10_000_000.0, positions=positions or {}),
    )


def test_no_quote_rejection_still_has_a_timestamp():
    broker = _broker(price=None)

    state = broker.place_order(Order(symbol="SQQQ", side=Side.SELL, qty=10, strategy_id="donchian"))

    assert state.updated_at is not None
    assert state.updated_at.tzinfo is not None


def test_buy_zero_qty_rejection_still_has_a_timestamp():
    broker = _broker()

    state = broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=0, strategy_id="donchian"))

    assert state.updated_at is not None


def test_no_position_sell_rejection_still_has_a_timestamp():
    broker = _broker()

    state = broker.place_order(Order(symbol="SQQQ", side=Side.SELL, qty=10, strategy_id="donchian"))

    assert state.updated_at is not None


def test_sellable_zero_rejection_still_has_a_timestamp():
    """실측 그 자체: 다른 전략의 lot만 있어 이 전략 몫(sellable)이 0인 매도 거부."""
    pos = Position(symbol="SQQQ", qty=5.0, avg_cost=10.0, opened_at=None)
    pos.ensure_lot("other_strategy")["qty"] = 5.0
    broker = _broker(positions={"SQQQ": pos})

    state = broker.place_order(Order(symbol="SQQQ", side=Side.SELL, qty=5, strategy_id="donchian"))

    assert state.updated_at is not None
    assert "매도 가능 수량 0" in state.reason
