"""엔진 소유 포지션 분리 — "사용자가 손으로 산 물량을 엔진이 절대 팔지 않는다".

배경: 사용자는 같은 토스 계좌에서 직접도 매매한다. 브로커 holdings()는 엔진
포지션과 수동 보유를 구분해 주지 않으므로, 그것만 보고 청산하면 사용자의 장기
보유가 엔진의 kill-switch 한 번에 사라진다. 이 스위트는 그 경로가 **세 겹 전부**
막혀 있는지 고정한다:

1. 루프(`_flatten_all`) — 엔진 소유가 아닌 종목은 청산 대상 목록에 들어가지 않는다
2. 리스크(`RiskManagerImpl.approve`) — 매도 수량을 엔진 소유분으로 상한
3. 브로커(`TossBroker.place_order`) — HTTP 요청 전에 다시 상한(마지막 방어선)

한 겹만으로 충분하다고 보지 않는다. 실수로 한 겹을 우회하는 코드가 생겨도 나머지
두 겹이 남아야 하는 종류의 사고다. 네트워크 호출은 전부 mock.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.trade.loop import _flatten_all
from quant.adapters.brokers.toss.broker import TossBroker
from quant.core.ports import Context
from quant.core.models import Order, Position, Quote, Side, Signal, SignalAction
from quant.core.portfolio.ownership import EngineOwnership
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)

ENGINE_SYMBOL = "TQQQ"   # 엔진이 진입한 종목
MANUAL_SYMBOL = "AAPL"   # 사용자가 손으로 산 종목


class _Clock:
    def now(self) -> datetime:
        return NOW

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return 15.0

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=NOW, price=100.0)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class _Sink:
    def __init__(self):
        self.signals: list[Signal] = []
        self.fills: list = []

    def on_signal(self, signal) -> None:
        self.signals.append(signal)

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


def _execution(qty: str, price: str = "100") -> dict:
    return {
        "filledQuantity": qty, "averageFilledPrice": price, "filledAmount": None,
        "commission": "0", "tax": "0", "filledAt": "2026-01-05T15:00:00Z",
        "settlementDate": "2026-01-07",
    }


@pytest.fixture
def mixed_account(tmp_path, monkeypatch):
    """엔진 보유 10주(TQQQ) + 사용자 수동 보유 7주(AAPL)인 계좌."""
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.holdings.return_value = {"items": [
        {"symbol": ENGINE_SYMBOL, "quantity": "10", "averagePurchasePrice": "90"},
        {"symbol": MANUAL_SYMBOL, "quantity": "7", "averagePurchasePrice": "150"},
    ]}
    client.place_order.return_value = {"orderId": "o1", "clientOrderId": None}
    client.order.return_value = {"status": "FILLED", "execution": _execution("10")}
    client.buying_power.return_value = {"currency": "KRW", "cashBuyingPower": "1000000"}
    ownership = EngineOwnership(None)
    ownership.add(ENGINE_SYMBOL, 10.0)
    broker = TossBroker(
        client, poll_max_attempts=1, poll_interval=0,
        meta_state_path=tmp_path / "meta.json", ownership=ownership,
        intents_path=tmp_path / "intents.jsonl",
        register_protective_stops=False,
    )
    return client, broker


def _risk() -> RiskManagerImpl:
    return RiskManagerImpl(
        {"risk": {"max_orders_per_day": 999, "cooldown_bars_after_stop": 0}},
        capital_fraction={"flatten": 1.0},
        market_of={ENGINE_SYMBOL: "US", MANUAL_SYMBOL: "US"},
    )


# ------------------------------------------------- 1겹: 루프의 flatten 대상 선정

def test_flatten_all_never_targets_user_manual_holdings(mixed_account):
    client, broker = mixed_account
    ctx = Context(clock=_Clock(), data=_Data(), broker=broker)
    sink = _Sink()

    _flatten_all(ctx, _risk(), sink, notifier=None)

    ordered_symbols = {c.args[0] for c in client.place_order.call_args_list}
    assert ordered_symbols == {ENGINE_SYMBOL}  # 사용자 보유 AAPL은 주문 자체가 없다
    assert {s.symbol for s in sink.signals} == {ENGINE_SYMBOL}


def test_flatten_cancels_open_orders_before_selling(mixed_account):
    """flatten 직전 미체결 주문을 먼저 취소한다(2026-08-30) — 늦게 체결된 매수
    주문이 방금 청산한 포지션을 되살리는 사고를 막는다."""
    client, broker = mixed_account
    client.orders.return_value = {"orders": [
        {"orderId": "stale-1", "symbol": ENGINE_SYMBOL, "side": "BUY", "quantity": "5",
         "orderedAt": "2026-01-05T09:00:00+09:00"},
    ]}
    client.cancel_order.return_value = {"orderId": "cancel-op-1"}
    ctx = Context(clock=_Clock(), data=_Data(), broker=broker)
    sink = _Sink()

    _flatten_all(ctx, _risk(), sink, notifier=None)

    client.cancel_order.assert_called_once_with("stale-1")


# ------------------------------------------------------ 2겹: 리스크 레이어 상한

def test_risk_blocks_exit_for_holding_the_engine_never_bought(mixed_account):
    _, broker = mixed_account
    ctx = Context(clock=_Clock(), data=_Data(), broker=broker)
    risk = _risk()

    order = risk.approve(
        Signal(strategy_id="s", symbol=MANUAL_SYMBOL, action=SignalAction.EXIT_LONG,
               target_weight=0.0, exit_fraction=1.0),
        ctx,
    )

    assert order is None
    assert "엔진 소유분 없음" in risk.last_block


def test_risk_clamps_exit_qty_to_engine_owned_portion(mixed_account):
    """같은 종목을 엔진도 사고 사용자도 산 경우 — 엔진 소유분만 판다."""
    client, broker = mixed_account
    client.holdings.return_value = {"items": [
        {"symbol": ENGINE_SYMBOL, "quantity": "25", "averagePurchasePrice": "90"},  # 엔진 10 + 사용자 15
    ]}
    ctx = Context(clock=_Clock(), data=_Data(), broker=broker)

    order = _risk().approve(
        Signal(strategy_id="s", symbol=ENGINE_SYMBOL, action=SignalAction.EXIT_LONG,
               target_weight=0.0, exit_fraction=1.0),
        ctx,
    )

    assert order is not None
    assert order.qty == 10.0  # 25가 아니다


# ------------------------------------------------------- 3겹: 브로커 마지막 방어선

def test_broker_refuses_sell_of_symbol_engine_never_bought(mixed_account):
    client, broker = mixed_account

    fill = broker.place_order(
        Order(symbol=MANUAL_SYMBOL, side=Side.SELL, qty=7.0, strategy_id="s")
    ).fill

    assert fill is None
    client.place_order.assert_not_called()  # HTTP 요청 자체가 나가지 않는다


def test_broker_clamps_sell_qty_even_if_caller_asks_for_more(mixed_account):
    """리스크 레이어를 우회하는 호출부가 생겨도 브로커가 다시 막는다."""
    client, broker = mixed_account
    client.order.return_value = {"status": "FILLED", "execution": _execution("10")}

    broker.place_order(Order(symbol=ENGINE_SYMBOL, side=Side.SELL, qty=25.0, strategy_id="s"))

    _, kwargs = client.place_order.call_args
    assert kwargs["quantity"] == 10.0  # 엔진 소유분까지만


# --------------------------------------------------------------- 원장 자체의 동작

def test_engine_fills_update_the_ownership_ledger(mixed_account):
    client, broker = mixed_account
    client.order.return_value = {"status": "FILLED", "execution": _execution("4")}

    broker.place_order(Order(symbol=ENGINE_SYMBOL, side=Side.BUY, qty=4.0, strategy_id="s"))
    assert broker.engine_owned_qty(ENGINE_SYMBOL) == 14.0

    broker.place_order(Order(symbol=ENGINE_SYMBOL, side=Side.SELL, qty=4.0, strategy_id="s"))
    assert broker.engine_owned_qty(ENGINE_SYMBOL) == 10.0


def test_ledger_survives_restart(tmp_path):
    path = tmp_path / "engine_positions.json"
    first = EngineOwnership(path)
    first.add(ENGINE_SYMBOL, 3.5)

    assert EngineOwnership(path).owned_qty(ENGINE_SYMBOL) == 3.5


def test_full_exit_drops_symbol_from_ledger(tmp_path):
    ledger = EngineOwnership(tmp_path / "engine_positions.json")
    ledger.add(ENGINE_SYMBOL, 3.0)
    ledger.remove(ENGINE_SYMBOL, 3.0)

    assert ledger.owned_qty(ENGINE_SYMBOL) == 0.0
    assert ledger.symbols() == set()


def test_manual_holdings_are_still_visible_in_positions(mixed_account):
    """조회는 하되 주문 대상으로 삼지 않는다 — positions()에는 그대로 보여야 한다
    (자산 평가·리포트가 사용자 보유를 빠뜨리면 그것도 틀린 그림이다)."""
    _, broker = mixed_account

    positions = broker.positions()

    assert set(positions) == {ENGINE_SYMBOL, MANUAL_SYMBOL}
    assert positions[MANUAL_SYMBOL].qty == 7.0
    assert broker.engine_owned_qty(MANUAL_SYMBOL) == 0.0
