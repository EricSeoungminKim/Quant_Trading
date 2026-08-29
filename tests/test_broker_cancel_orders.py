"""주문 취소 경로 (2026-08-30, 실계좌 방어선).

배경: TossClient 에는 modify_order/cancel_order/conditional_orders 가 있었지만
`Broker` Protocol 에 취소가 없어 엔진이 주문을 낼 줄만 알고 취소할 줄 몰랐다. 이
스위트는 그 계약(cancel_order/open_orders)이 실제 구현체(PaperBroker, TossBroker)
양쪽에서 정직하게 지켜지는지 고정한다:

- `Broker` Protocol 구현체 전수가 새 메서드를 갖는다(구조적 타이핑 확인).
- PaperBroker는 미체결 개념이 없으므로 항상 빈 리스트/False(가짜 성공 금지).
- TossBroker는 MODE!=live면 거부, live면 실제 취소를 시도하고 결과를 정직하게
  반환한다. 이미 종결된 주문(already-filled/already-canceled) 취소는 "더 이상
  위험 없음"으로 True 취급한다.
- open_orders()는 조회 실패 시 예외를 삼키고 빈 리스트를 돌려준다.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from quant.adapters.brokers.toss.broker import TossBroker
from quant.adapters.brokers.toss.client import TossAPIError
from quant.adapters.execution.paper import PaperBroker
from quant.core.fx import FixedFxProvider
from quant.core.models import OpenOrder, Quote, Side
from quant.core.ports import Broker
from quant.core.portfolio.ownership import EngineOwnership
from quant.core.portfolio.portfolio import Portfolio


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


class _Feed:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=datetime.now(), price=100.0)

    def history(self, symbol, interval, n):
        raise NotImplementedError


def _paper_broker() -> PaperBroker:
    return PaperBroker(
        data=_Feed(), portfolio=Portfolio(cash=1_000_000.0, state_path=None),
        fee_bps=0.0, fx=FixedFxProvider(),
    )


def _toss_broker(client) -> TossBroker:
    own = EngineOwnership(None)
    own.add("005930", 100.0)
    return TossBroker(client, ownership=own)


# ------------------------------------------------------------- Protocol 구조 확인

def test_paper_broker_satisfies_broker_protocol_with_cancel_surface():
    assert isinstance(_paper_broker(), Broker)


def test_toss_broker_satisfies_broker_protocol_with_cancel_surface():
    assert isinstance(_toss_broker(MagicMock()), Broker)


# --------------------------------------------------------------------- PaperBroker

def test_paper_broker_open_orders_is_always_empty():
    broker = _paper_broker()
    assert broker.open_orders() == []


def test_paper_broker_cancel_order_is_always_false():
    """미체결 주문이 있을 수 없으니 취소할 것도 없다 — 가짜 성공을 반환하지 않는다."""
    broker = _paper_broker()
    assert broker.cancel_order("anything") is False


# ---------------------------------------------------------------------- TossBroker

def test_toss_cancel_order_refused_when_not_live(monkeypatch):
    monkeypatch.setenv("MODE", "paper")
    client = MagicMock()
    broker = _toss_broker(client)

    result = broker.cancel_order("order-1")

    assert result is False
    client.cancel_order.assert_not_called()


def test_toss_cancel_order_succeeds_when_live(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.cancel_order.return_value = {"orderId": "cancel-op-1"}
    broker = _toss_broker(client)

    result = broker.cancel_order("order-1")

    assert result is True
    client.cancel_order.assert_called_once_with("order-1")


@pytest.mark.parametrize("code", ["already-filled", "already-canceled"])
def test_toss_cancel_order_treats_already_resolved_as_success(monkeypatch, code):
    """취소를 못 한 게 아니라 취소할 게 이미 없었다 — 미체결 위험이 없으므로 True."""
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.cancel_order.side_effect = TossAPIError(409, code, "already resolved")
    broker = _toss_broker(client)

    assert broker.cancel_order("order-1") is True


def test_toss_cancel_order_reports_real_failure_as_false(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.cancel_order.side_effect = TossAPIError(422, "cancel-restricted", "취소 불가")
    broker = _toss_broker(client)

    assert broker.cancel_order("order-1") is False


def test_toss_open_orders_maps_server_response(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.orders.return_value = {"orders": [
        {"orderId": "o1", "symbol": "005930", "side": "BUY", "quantity": "10",
         "orderedAt": "2026-08-30T09:00:00+09:00"},
        {"orderId": "o2", "symbol": "AAPL", "side": "SELL", "quantity": "3.5",
         "orderedAt": "2026-08-30T09:05:00+09:00"},
    ]}
    broker = _toss_broker(client)

    orders = broker.open_orders()

    client.orders.assert_called_once_with(status="OPEN")
    assert orders == [
        OpenOrder(order_id="o1", symbol="005930", side=Side.BUY, qty=10.0,
                  submitted_at=datetime.fromisoformat("2026-08-30T09:00:00+09:00")),
        OpenOrder(order_id="o2", symbol="AAPL", side=Side.SELL, qty=3.5,
                  submitted_at=datetime.fromisoformat("2026-08-30T09:05:00+09:00")),
    ]


def test_toss_open_orders_returns_empty_list_on_failure(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.orders.side_effect = TossAPIError(500, "internal-error", "boom")
    broker = _toss_broker(client)

    assert broker.open_orders() == []


def test_toss_open_orders_skips_items_with_unparseable_timestamp(monkeypatch):
    """값을 지어내지 않는다 — 나이를 모르면 그 항목만 건너뛰고 나머지는 살린다."""
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.orders.return_value = {"orders": [
        {"orderId": "bad", "symbol": "005930", "side": "BUY", "quantity": "10",
         "orderedAt": "not-a-timestamp"},
        {"orderId": "good", "symbol": "AAPL", "side": "SELL", "quantity": "1",
         "orderedAt": "2026-08-30T09:05:00+09:00"},
    ]}
    broker = _toss_broker(client)

    orders = broker.open_orders()

    assert [o.order_id for o in orders] == ["good"]
