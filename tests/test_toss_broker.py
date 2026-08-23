"""TossBroker.place_order: real executed price/qty from order-status polling,
never a market-price guess. All HTTP is mocked via a fake TossClient — no real
network calls.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

import quant.adapters.brokers.toss.broker as broker_module
from quant.adapters.brokers.toss.broker import TossBroker
from quant.adapters.brokers.toss.client import TossAPIError
from quant.core.models import Order, Side, OrderStatus
from quant.core.portfolio.ownership import EngineOwnership


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """기본 상태 경로(data/state/...)가 상대경로라 cwd를 옮겨 실제 운영 상태 파일을
    건드리지 않게 한다 — 주문 의도 저널이 운영 파일에 섞이면 안 된다."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def ownership():
    """엔진 소유 원장(메모리 전용). 매도 테스트는 여기에 보유를 심어야 한다 —
    엔진이 자기가 산 적 없는 물량을 파는 경로는 존재하면 안 되기 때문."""
    return EngineOwnership(None)


def _order(qty: float = 10, side: Side = Side.BUY) -> Order:
    return Order(symbol="005930", side=side, qty=qty, strategy_id="s1", reason="test")


def _broker(client, **kw) -> TossBroker:
    """새 Phase 6.3 테스트용 — 소유 원장에 보유를 심어 매도 방어선에 걸리지 않게 한다."""
    own = EngineOwnership(None)
    own.add("005930", 1000.0)
    own.add("AAPL", 1000.0)
    return TossBroker(client, ownership=own, **kw)


def _us_order(qty: float, side: Side = Side.BUY) -> Order:
    return Order(symbol="AAPL", side=side, qty=qty, strategy_id="s1", reason="test")


def _execution(filled_qty: str, price: str | None = "70000",
               commission: str | None = "1400", tax: str | None = "0") -> dict:
    return {
        "filledQuantity": filled_qty,
        "averageFilledPrice": price,
        "filledAmount": None,
        "commission": commission,
        "tax": tax,
        "filledAt": "2026-03-28T09:31:15+09:00",
        "settlementDate": "2026-03-30",
    }


def test_place_order_refuses_before_any_http_call_in_paper_mode(monkeypatch):
    monkeypatch.setenv("MODE", "paper")
    client = MagicMock()
    broker = TossBroker(client)

    result = broker.place_order(_order()).fill

    assert result is None
    client.place_order.assert_not_called()
    client.order.assert_not_called()


def test_place_order_full_fill_uses_actual_executed_price_not_market_snapshot(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.place_order.return_value = {"orderId": "order-1", "clientOrderId": None}
    client.order.return_value = {
        "status": "FILLED",
        "execution": _execution("10", price="70500"),  # differs from any "market" price
    }
    broker = TossBroker(client, poll_max_attempts=5, poll_interval=0)

    fill = broker.place_order(_order(qty=10)).fill

    assert fill is not None
    assert fill.qty == 10
    assert fill.price == 70500.0  # actual executed price, not a snapshot
    assert fill.fee == 1400.0  # commission + tax
    client.prices.assert_not_called()  # no more market-price fallback


def test_place_order_partial_fill_at_poll_timeout_returns_only_executed_qty(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.place_order.return_value = {"orderId": "order-2", "clientOrderId": None}
    client.order.return_value = {
        "status": "PARTIAL_FILLED",
        "execution": _execution("4", price="70000"),
    }
    broker = TossBroker(client, poll_max_attempts=3, poll_interval=0)

    fill = broker.place_order(_order(qty=10)).fill

    assert fill is not None
    assert fill.qty == 4  # only the executed portion, never the full requested qty
    assert fill.price == 70000.0
    assert client.order.call_count == 3  # polled up to the bound


def test_place_order_rejected_returns_none(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.place_order.return_value = {"orderId": "order-3", "clientOrderId": None}
    client.order.return_value = {
        "status": "REJECTED",
        "execution": _execution("0", price=None, commission=None, tax=None),
    }
    broker = TossBroker(client, poll_max_attempts=5, poll_interval=0)

    fill = broker.place_order(_order()).fill

    assert fill is None


def test_place_order_poll_timeout_with_zero_fills_returns_none(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.place_order.return_value = {"orderId": "order-4", "clientOrderId": None}
    client.order.return_value = {
        "status": "PENDING",
        "execution": _execution("0", price=None, commission=None, tax=None),
    }
    broker = TossBroker(client, poll_max_attempts=3, poll_interval=0)

    fill = broker.place_order(_order()).fill

    assert fill is None
    assert client.order.call_count == 3


def test_place_order_swallows_client_errors_and_returns_none(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.place_order.side_effect = TossAPIError(500, "server-error", "boom")
    broker = TossBroker(client, poll_max_attempts=3, poll_interval=0)

    fill = broker.place_order(_order()).fill

    assert fill is None
    client.order.assert_not_called()


# ------------------------------------------- KR/US quantity vs amount resolution

def test_kr_fractional_quantity_floors_to_integer(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.place_order.return_value = {"orderId": "order-kr", "clientOrderId": None}
    client.order.return_value = {"status": "FILLED", "execution": _execution("10")}
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0)

    fill = broker.place_order(_order(qty=10.7)).fill

    assert fill is not None
    _, kwargs = client.place_order.call_args
    assert kwargs["quantity"] == 10.0  # floored, not 10.7
    assert kwargs["order_amount"] is None
    client.prices.assert_not_called()


def test_kr_quantity_floored_to_zero_skips_order_without_http_call(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()

    fill = TossBroker(client).place_order(_order(qty=0.4)).fill

    assert fill is None
    client.place_order.assert_not_called()


def test_us_market_buy_fractional_during_regular_hours_uses_amount_based_order(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setattr(broker_module, "_is_us_regular_hours", lambda now: True)
    client = MagicMock()
    client.prices.return_value = [{"symbol": "AAPL", "lastPrice": "150.00"}]
    client.place_order.return_value = {"orderId": "order-us-buy", "clientOrderId": None}
    client.order.return_value = {"status": "FILLED", "execution": _execution("2.5", price="150")}
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0)

    fill = broker.place_order(_us_order(qty=2.5, side=Side.BUY)).fill

    assert fill is not None
    _, kwargs = client.place_order.call_args
    assert kwargs["quantity"] is None
    assert kwargs["order_amount"] == pytest.approx(375.0)  # 2.5 * 150


def test_us_market_sell_fractional_during_regular_hours_uses_quantity_as_is(monkeypatch, ownership):
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setattr(broker_module, "_is_us_regular_hours", lambda now: True)
    client = MagicMock()
    client.place_order.return_value = {"orderId": "order-us-sell", "clientOrderId": None}
    client.order.return_value = {"status": "FILLED", "execution": _execution("2.5")}
    ownership.add("AAPL", 2.5)
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0, ownership=ownership)

    fill = broker.place_order(_us_order(qty=2.5, side=Side.SELL)).fill

    assert fill is not None
    _, kwargs = client.place_order.call_args
    assert kwargs["quantity"] == 2.5  # fractional SELL allowed as-is
    assert kwargs["order_amount"] is None
    client.prices.assert_not_called()  # no amount calc needed for SELL


def test_us_fractional_outside_regular_hours_floors_to_integer(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setattr(broker_module, "_is_us_regular_hours", lambda now: False)
    client = MagicMock()
    client.place_order.return_value = {"orderId": "order-us-afterhours", "clientOrderId": None}
    client.order.return_value = {"status": "FILLED", "execution": _execution("2")}
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0)

    fill = broker.place_order(_us_order(qty=2.7, side=Side.BUY)).fill

    assert fill is not None
    _, kwargs = client.place_order.call_args
    assert kwargs["quantity"] == 2.0  # floored, never sent fractional outside regular hours
    assert kwargs["order_amount"] is None
    client.prices.assert_not_called()


def test_us_fractional_outside_regular_hours_floored_to_zero_skips_order(monkeypatch, ownership):
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setattr(broker_module, "_is_us_regular_hours", lambda now: False)
    client = MagicMock()
    ownership.add("AAPL", 0.9)  # 소유는 하고 있으나 정수 내림 후 0이라 주문 불가

    fill = TossBroker(client, ownership=ownership).place_order(_us_order(qty=0.9, side=Side.SELL)).fill

    assert fill is None
    client.place_order.assert_not_called()


# --------------------------------------------------- clientOrderId idempotent retry

def test_ambiguous_network_failure_retries_with_same_client_order_id(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.place_order.side_effect = [
        httpx.TimeoutException("timed out"),
        {"orderId": "order-retry", "clientOrderId": None},
    ]
    client.order.return_value = {"status": "FILLED", "execution": _execution("10")}
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0)

    fill = broker.place_order(_order()).fill

    assert fill is not None
    assert client.place_order.call_count == 2
    first_client_order_id = client.place_order.call_args_list[0].kwargs["client_order_id"]
    second_client_order_id = client.place_order.call_args_list[1].kwargs["client_order_id"]
    assert first_client_order_id == second_client_order_id  # same idempotency key reused
    assert first_client_order_id is not None


def test_ambiguous_network_failure_after_retry_exhausted_returns_none(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.place_order.side_effect = httpx.TimeoutException("timed out")
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0)

    fill = broker.place_order(_order()).fill

    assert fill is None
    assert client.place_order.call_count == 2  # original + exactly one retry, no more
    client.order.assert_not_called()


# ------------------------------------------------------------- positions() meta 보존

def test_positions_returns_empty_meta_for_a_symbol_never_seen_before(tmp_path):
    client = MagicMock()
    client.holdings.return_value = {
        "items": [{"symbol": "AAPL", "quantity": "10", "averagePurchasePrice": "100"}]
    }
    broker = TossBroker(client, meta_state_path=tmp_path / "meta.json")

    positions = broker.positions()

    assert positions["AAPL"].qty == 10.0
    assert positions["AAPL"].avg_cost == 100.0
    assert positions["AAPL"].meta == {}  # 기록 없는 심볼의 meta를 지어내지 않는다


def test_positions_preserves_meta_across_calls(tmp_path):
    client = MagicMock()
    client.holdings.return_value = {
        "items": [{"symbol": "AAPL", "quantity": "10", "averagePurchasePrice": "100"}]
    }
    broker = TossBroker(client, meta_state_path=tmp_path / "meta.json")

    first = broker.positions()
    first["AAPL"].meta["scaled_out"] = True  # 전략이 체결 확인 후 붙이는 상태를 흉내낸다

    second = broker.positions()

    assert second["AAPL"].meta == {"scaled_out": True}
    assert second["AAPL"].qty == 10.0  # qty/avg_cost는 매번 holdings()가 최신값


def test_positions_meta_persists_across_broker_instances(tmp_path):
    """재시작을 흉내낸다 — 새 TossBroker 인스턴스도 디스크에 저장된 meta를 읽어야 한다."""
    state_path = tmp_path / "meta.json"
    client = MagicMock()
    client.holdings.return_value = {
        "items": [{"symbol": "AAPL", "quantity": "10", "averagePurchasePrice": "100"}]
    }
    broker1 = TossBroker(client, meta_state_path=state_path)
    broker1.positions()["AAPL"].meta["scaled_out"] = True
    broker1.positions()  # meta_store.save()를 트리거해 디스크에 반영

    broker2 = TossBroker(client, meta_state_path=state_path)
    assert broker2.positions()["AAPL"].meta == {"scaled_out": True}


def test_positions_drops_meta_when_position_closes(tmp_path):
    client = MagicMock()
    client.holdings.return_value = {
        "items": [{"symbol": "AAPL", "quantity": "10", "averagePurchasePrice": "100"}]
    }
    broker = TossBroker(client, meta_state_path=tmp_path / "meta.json")
    broker.positions()["AAPL"].meta["scaled_out"] = True

    client.holdings.return_value = {"items": []}  # 브로커가 더 이상 보유를 보고하지 않음
    assert broker.positions() == {}

    client.holdings.return_value = {
        "items": [{"symbol": "AAPL", "quantity": "5", "averagePurchasePrice": "90"}]
    }
    reopened = broker.positions()
    assert reopened["AAPL"].meta == {}  # 재진입 — 이전 meta가 새지 않는다


# ------------------------------------------- 멱등성: 같은 의도 → 서버 주문 정확히 1건

class _IdempotentOrderServer:
    """Toss의 clientOrderId 멱등 semantics를 흉내내는 페이크 서버.

    같은 clientOrderId로 다시 들어오면 새 주문을 만들지 않고 기존 주문 결과를
    그대로 돌려준다(문서상 10분 유효). `fail_first_n`번은 응답 직전에 타임아웃을
    던지지만 **주문은 이미 생성된 뒤**다 — 실전에서 가장 위험한 "모호한 실패"가
    정확히 이 모양이고, 여기서 중복 주문이 나면 돈이 두 배로 나간다.
    """

    def __init__(self, fail_first_n: int = 0):
        self.orders: dict[str, str] = {}  # clientOrderId -> orderId
        self.create_calls = 0
        self._fail_remaining = fail_first_n

    def place_order(self, symbol, side, order_type, *, quantity=None,
                    order_amount=None, client_order_id=None, **_):
        if client_order_id not in self.orders:
            self.create_calls += 1
            self.orders[client_order_id] = f"srv-{len(self.orders) + 1}"
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise httpx.TimeoutException("응답을 못 받았다 (주문은 이미 접수됨)")
        return {"orderId": self.orders[client_order_id], "clientOrderId": client_order_id}

    def order(self, order_id):
        return {"status": "FILLED", "execution": _execution("10")}


def test_ambiguous_timeout_after_the_order_reached_the_server_creates_exactly_one_order(
    monkeypatch, ownership, tmp_path,
):
    monkeypatch.setenv("MODE", "live")
    server = _IdempotentOrderServer(fail_first_n=1)
    broker = TossBroker(server, poll_max_attempts=1, poll_interval=0, ownership=ownership,
                        intents_path=tmp_path / "intents.jsonl")

    fill = broker.place_order(_order(qty=10)).fill

    assert fill is not None
    assert server.create_calls == 1  # 재시도했지만 서버에 남은 주문은 1건
    assert len(server.orders) == 1


def test_order_intent_is_journaled_before_the_request_goes_out(monkeypatch, ownership, tmp_path):
    """프로세스가 주문 직후 죽어도 "그 키로 주문을 시도했다"는 근거가 디스크에 남아야 한다."""
    monkeypatch.setenv("MODE", "live")
    intents = tmp_path / "intents.jsonl"
    client = MagicMock()
    client.place_order.side_effect = httpx.TimeoutException("boom")  # 결과를 영영 모른다
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0,
                        ownership=ownership, intents_path=intents)

    broker.place_order(_order(qty=10))

    records = [json.loads(line) for line in intents.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in records] == ["intent"]  # placed 기록은 없다 — 결과 미확인
    assert records[0]["symbol"] == "005930"
    assert records[0]["quantity"] == 10.0
    assert records[0]["client_order_id"]


def test_no_journal_entry_and_no_http_when_mode_is_not_live(monkeypatch, ownership, tmp_path):
    monkeypatch.setenv("MODE", "paper")
    intents = tmp_path / "intents.jsonl"
    client = MagicMock()
    broker = TossBroker(client, ownership=ownership, intents_path=intents)

    state = broker.place_order(_order())
    # 이제 "아무 일도 없었음"이 아니라 **왜 안 나갔는지**가 남는다(Phase 6.3):
    # 브로커에 닿지 못했으므로 주문번호가 없다.
    assert state.fill is None
    assert state.broker_order_id is None
    assert "MODE" in state.reason
    client.place_order.assert_not_called()
    client.place_conditional_order.assert_not_called()
    client.cancel_conditional_order.assert_not_called()
    assert not intents.exists()


# --------------------------------------------- 서버측 보호주문 (조건주문) [미검증 스키마]

def _filled_client(qty: str = "10", price: str = "100") -> MagicMock:
    client = MagicMock()
    client.place_order.return_value = {"orderId": "o1", "clientOrderId": None}
    client.order.return_value = {"status": "FILLED", "execution": _execution(qty, price=price)}
    client.place_conditional_order.return_value = {"conditionalOrderId": "co-1"}
    return client


def test_entry_with_stop_and_target_registers_an_oco(monkeypatch, ownership, tmp_path):
    monkeypatch.setenv("MODE", "live")
    client = _filled_client(price="100")
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0,
                        ownership=ownership, intents_path=tmp_path / "i.jsonl")

    broker.place_order(Order(symbol="005930", side=Side.BUY, qty=10, strategy_id="s",
                             stop=95.0, target=110.0))

    _, kwargs = client.place_conditional_order.call_args
    args, _ = client.place_conditional_order.call_args
    assert args[1] == "OCO"
    assert kwargs["order_type"] == "LIMIT"  # OCO는 지정가만 지원
    assert kwargs["first"]["triggerPrice"] == "110"   # 목표가가 현재가보다 위
    assert kwargs["second"]["triggerPrice"] == "95"   # 손절가가 현재가보다 아래
    assert kwargs["quantity"] == 10


def test_entry_with_stop_only_registers_a_market_stop(monkeypatch, ownership, tmp_path):
    """손절만 있으면 SINGLE + MARKET — 손절은 가격보다 체결 확실성이 중요하다."""
    monkeypatch.setenv("MODE", "live")
    client = _filled_client(price="100")
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0,
                        ownership=ownership, intents_path=tmp_path / "i.jsonl")

    broker.place_order(Order(symbol="005930", side=Side.BUY, qty=10, strategy_id="s", stop=95.0))

    args, kwargs = client.place_conditional_order.call_args
    assert args[1] == "SINGLE"
    assert kwargs["order_type"] == "MARKET"
    assert "orderPrice" not in kwargs["first"]
    assert kwargs["second"] is None


def test_entry_without_a_stop_registers_nothing(monkeypatch, ownership, tmp_path):
    """어댑터가 손절가를 지어내지 않는다."""
    monkeypatch.setenv("MODE", "live")
    client = _filled_client()
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0,
                        ownership=ownership, intents_path=tmp_path / "i.jsonl")

    broker.place_order(Order(symbol="005930", side=Side.BUY, qty=10, strategy_id="s"))

    client.place_conditional_order.assert_not_called()


def test_full_exit_cancels_the_outstanding_conditional_order(monkeypatch, ownership, tmp_path):
    """엔진이 스스로 청산했는데 서버측 손절이 남아 있으면, 사용자가 나중에 같은 종목을
    손으로 산 순간 그 물량이 엔진 손절에 팔린다 — 반드시 취소돼야 한다."""
    monkeypatch.setenv("MODE", "live")
    client = _filled_client(price="100")
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0,
                        ownership=ownership, intents_path=tmp_path / "i.jsonl")
    broker.place_order(Order(symbol="005930", side=Side.BUY, qty=10, strategy_id="s", stop=95.0))
    client.place_conditional_order.reset_mock()

    broker.place_order(Order(symbol="005930", side=Side.SELL, qty=10, strategy_id="s"))

    client.cancel_conditional_order.assert_called_once_with("co-1")
    client.place_conditional_order.assert_not_called()  # 남은 포지션이 없으니 재등록도 없다
    assert broker.engine_owned_qty("005930") == 0.0


def test_partial_exit_reregisters_the_stop_at_the_remaining_quantity(monkeypatch, ownership, tmp_path):
    """스케일아웃 후 옛 수량짜리 조건주문이 남으면 엔진 소유분보다 많이 팔게 된다."""
    monkeypatch.setenv("MODE", "live")
    client = _filled_client(price="100")
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0,
                        ownership=ownership, intents_path=tmp_path / "i.jsonl")
    broker.place_order(Order(symbol="005930", side=Side.BUY, qty=10, strategy_id="s", stop=95.0))
    client.place_conditional_order.reset_mock()
    client.order.return_value = {"status": "FILLED", "execution": _execution("4", price="100")}

    broker.place_order(Order(symbol="005930", side=Side.SELL, qty=4, strategy_id="s"))

    client.cancel_conditional_order.assert_called_once_with("co-1")
    _, kwargs = client.place_conditional_order.call_args
    assert kwargs["quantity"] == 6  # 남은 엔진 소유분
    assert kwargs["first"]["triggerPrice"] == "95"  # 손절가는 유지


def test_conditional_order_failure_does_not_lose_the_fill(monkeypatch, ownership, tmp_path):
    """조건주문 등록이 실패해도 체결 자체는 보고돼야 한다 — 삼키면 엔진이 포지션을
    갖고 있다는 사실을 모른 채 계속 돈다."""
    monkeypatch.setenv("MODE", "live")
    client = _filled_client()
    client.place_conditional_order.side_effect = TossAPIError(422, "rule", "안 됩니다")
    broker = TossBroker(client, poll_max_attempts=1, poll_interval=0,
                        ownership=ownership, intents_path=tmp_path / "i.jsonl")

    fill = broker.place_order(
        Order(symbol="005930", side=Side.BUY, qty=10, strategy_id="s", stop=95.0)).fill

    assert fill is not None
    assert fill.qty == 10
    assert broker.engine_owned_qty("005930") == 10.0


# ================================================ Phase 6.3: 네 결과를 구분한다
#
# 예전 계약(`Fill | None`)은 아래 넷을 전부 None 하나로 뭉갰다. 사후에 "왜 안 샀지"에
# 답할 수 없었고, 미체결 잔량은 그냥 사라졌다.

def test_broker_rejection_is_distinguishable_from_never_submitted(monkeypatch):
    """**거부됐다**(계좌·시장 문제)와 **안 나갔다**(우리 설정 문제)는 다른 사건이다.

    구분 기준은 주문번호다 — 있으면 실제로 서버에 닿았다는 뜻이다.
    """
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.place_order.return_value = {"orderId": "o1", "clientOrderId": None}
    client.order.return_value = {"status": "REJECTED", "execution": None}
    broker = _broker(client)

    rejected = broker.place_order(_order())

    assert rejected.status is OrderStatus.REJECTED
    assert rejected.broker_order_id == "o1"      # 서버에 닿았다
    assert "REJECTED" in rejected.reason

    monkeypatch.setenv("MODE", "paper")
    never_sent = _broker(MagicMock()).place_order(_order())

    assert never_sent.status is OrderStatus.REJECTED
    assert never_sent.broker_order_id is None    # 닿지 못했다


def test_poll_timeout_leaves_the_order_open_not_terminal(monkeypatch):
    """**"모른다"를 터미널로 만들지 않는다.** 주문은 서버에 남아 있을 수 있고,
    그 잔량이 대사(6.5)가 집어야 할 대상이다."""
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.place_order.return_value = {"orderId": "o1", "clientOrderId": None}
    client.order.return_value = {"status": "PENDING", "execution": None}
    broker = _broker(client, poll_max_attempts=1, poll_interval=0)

    state = broker.place_order(_order(qty=10))

    assert state.is_open and not state.is_terminal
    assert state.filled_qty == 0
    assert state.remaining_qty == 10
    assert state.broker_order_id == "o1"


def test_partial_fill_at_timeout_reports_the_remainder(monkeypatch):
    """오늘 버려지던 잔량이 상태에 남는다."""
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.place_order.return_value = {"orderId": "o1", "clientOrderId": None}
    client.order.return_value = {
        "status": "PENDING",
        "execution": {"filledQuantity": "4", "averageFilledPrice": "100",
                      "filledAmount": None, "commission": "0", "tax": "0",
                      "filledAt": "2026-01-05T20:00:00Z", "settlementDate": "2026-01-06"},
    }
    broker = _broker(client, poll_max_attempts=1, poll_interval=0)

    state = broker.place_order(_order(qty=10))

    assert state.status is OrderStatus.PARTIALLY_FILLED
    assert state.filled_qty == 4
    assert state.remaining_qty == 6      # 예전엔 이 6주가 그냥 사라졌다
    assert state.fill is not None and state.fill.qty == 4
