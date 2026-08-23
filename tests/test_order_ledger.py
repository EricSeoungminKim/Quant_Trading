"""주문 생애를 원장에 남긴다 — Phase 6.4. 네트워크·sleep 없음(전부 페이크).

## 오늘 무엇이 사라지나

체결 원장(`trades.jsonl`)에는 **일어난 체결만** 있다. 그래서 사후에 이 질문들에
답할 수 없다:

- "20주를 시켰는데 8주만 채워졌나?" → 원장엔 8주 체결만 있고 12주를 못 받았다는
  사실이 없다. 토스 어댑터가 "미체결 잔량은 버린다"고 주석에 적어둔 그 잔량이다.
- "왜 안 샀지?" → 브로커가 거부했는지, 애초에 주문이 안 나갔는지 구분되지 않는다.
  둘 다 원장에 아무 줄도 남기지 않는다.

**부분 체결 정보는 이미 존재한다** — `fill.qty` 와 `order.qty` 를 비교하면 나온다.
아무도 그 둘을 비교하지 않았을 뿐이다. 그래서 이 단계는 Broker Protocol 을 바꾸지
않고도(그건 17곳을 동시에 건드린다) 잔량을 기록으로 남길 수 있다.

## 부기가 매매를 죽이면 안 된다

주문 기록은 사후 분석용이다. 기록이 실패하거나 상태기계가 예상 밖 값을 만나도
**체결 처리와 사이클은 계속돼야 한다** — 이 저장소의 기존 계약과 같다
("요약용 부기가 체결 처리를 막으면 안 된다", loop.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from quant.control.ledger import TradeLedgerSink
from quant.core.models import Fill, Order, OrderStatus, Position, Quote, Side, Signal, SignalAction
from quant.core.ports import Context
from quant.trade.loop import _execute_signal
from quant.core import oms
from quant.core.oms import accept, on_fill

T0 = datetime(2026, 8, 14, 0, 30, tzinfo=timezone.utc)


# ── 페이크 ────────────────────────────────────────────────────────────────

class _Clock:
    def now(self) -> datetime:
        return T0

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, price=100.0, ts=T0)


class _Broker:
    """요청 수량 중 `fill_qty` 만 채운다. None 이면 주문을 내지 못한 경우.

    Phase 6.3 계약대로 **OrderState** 를 돌려준다.
    """

    def __init__(self, fill_qty: float | None):
        self.fill_qty = fill_qty

    def place_order(self, order: Order):
        if self.fill_qty is None:
            return oms.not_submitted(order, "시세 없음 — 주문 생성 불가", at=T0)
        fill = Fill(symbol=order.symbol, side=order.side, qty=self.fill_qty,
                    price=100.0, ts=T0, strategy_id=order.strategy_id, fee=1.0,
                    realized_pnl=0.0)
        return oms.report_fill(order, fill, "A1", at=T0)

    def positions(self) -> dict[str, Position]:
        return {}

    def cash(self) -> float:
        return 1_000_000.0


class _Risk:
    def approve(self, signal, ctx, risk_multiplier=1.0, marks=None) -> Order:
        return Order(symbol=signal.symbol, side=Side.BUY, qty=20,
                     strategy_id=signal.strategy_id, reason=signal.reason)


class _PlainSink:
    """`on_order` 가 **없는** 싱크 — 기존 구현체·테스트 페이크가 전부 이 모양이다."""

    def __init__(self):
        self.fills = []

    def on_signal(self, signal) -> None: ...

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


class _OrderSink(_PlainSink):
    def __init__(self):
        super().__init__()
        self.orders = []

    def on_order(self, state) -> None:
        self.orders.append(state)


def _signal() -> Signal:
    return Signal(strategy_id="donchian", symbol="TQQQ",
                  action=SignalAction.ENTER_LONG, target_weight=0.1, reason="돌파")


def _run(broker: _Broker, sink) -> None:
    _execute_signal(_signal(), Context(_Clock(), _Data(), broker), _Risk(), sink, None)


# ── 루프가 주문 생애를 싱크로 흘려보낸다 ────────────────────────────────

def test_full_fill_is_recorded_as_filled():
    sink = _OrderSink()

    _run(_Broker(fill_qty=20), sink)

    (state,) = sink.orders
    assert state.status is OrderStatus.FILLED
    assert state.filled_qty == 20
    assert state.remaining_qty == 0


def test_partial_fill_records_the_remainder_that_is_lost_today():
    """**이 테스트가 6.4 의 존재 이유다.**

    20주를 시켰고 8주가 채워졌다. 체결 원장에는 8주 체결만 남고, 12주를 못 받았다는
    사실은 어디에도 없다 — 그 위에서 도는 청산 계산이 20주를 가정한다.
    """
    sink = _OrderSink()

    _run(_Broker(fill_qty=8), sink)

    (state,) = sink.orders
    assert state.status is OrderStatus.PARTIALLY_FILLED
    assert state.filled_qty == 8
    assert state.remaining_qty == 12


def test_unfilled_order_still_leaves_a_record():
    """예전엔 아무 줄도 안 남았다 — "주문이 안 나갔다"와 "나갔는데 못 채웠다"가
    구분되지 않았다. 이제 브로커가 **왜**까지 돌려준다(Phase 6.3)."""
    sink = _OrderSink()

    _run(_Broker(fill_qty=None), sink)

    (state,) = sink.orders
    assert state.status is OrderStatus.REJECTED
    assert state.broker_order_id is None      # 브로커에 닿지 못했다
    assert state.filled_qty == 0
    assert state.remaining_qty == 20
    assert "주문 생성 불가" in state.reason


def test_fill_handling_is_unchanged():
    """주문 기록을 붙였다고 체결 경로가 달라지면 안 된다."""
    sink = _OrderSink()

    _run(_Broker(fill_qty=8), sink)

    assert [f.qty for f in sink.fills] == [8]


# ── 하위 호환: on_order 가 없는 싱크 ─────────────────────────────────────

def test_sink_without_on_order_is_left_alone():
    """기존 싱크 구현체와 테스트 페이크 13개가 전부 `on_order` 가 없다.
    없는 메서드를 부르면 사이클이 죽는다 — 부기 때문에 매매가 멈추는 최악의 경로다."""
    sink = _PlainSink()

    _run(_Broker(fill_qty=8), sink)   # 예외 없이 통과해야 한다

    assert [f.qty for f in sink.fills] == [8]


def test_sink_raising_on_order_does_not_break_the_cycle():
    """부기가 매매를 죽이면 안 된다(loop.py 의 기존 계약과 같다)."""
    class _Exploding(_PlainSink):
        def on_order(self, state) -> None:
            raise RuntimeError("원장 디스크 꽉 참")

    sink = _Exploding()

    _run(_Broker(fill_qty=8), sink)

    assert [f.qty for f in sink.fills] == [8]   # 체결은 그대로 처리됐다


def test_overfill_keeps_the_fill_and_records_the_mismatch():
    """브로커가 요청보다 많이 채웠다고 보고했다.

    순수 상태기계는 여기서 예외를 던진다(장부가 틀렸다는 신호). 하지만 **어댑터는
    그 예외를 밖으로 올리면 안 된다** — 2026-08-14 실측에서 그 예외가 루프까지 올라가
    "주문 실행 실패"로 처리되며 **이미 일어난 체결이 통째로 버려졌다.** 돈은 이미
    움직였다는 사실은 변하지 않는다.

    그래서 `oms.report_fill` 이 체결을 지키고 불일치를 상태에 남긴다.
    """
    sink = _OrderSink()

    _run(_Broker(fill_qty=25), sink)   # 요청 20 < 체결 25

    assert [f.qty for f in sink.fills] == [25]   # 체결을 잃지 않는다
    (state,) = sink.orders
    assert state.filled_qty == 25
    assert "불일치" in state.reason               # 사실을 지우지도 않는다
    assert state.remaining_qty < 0                # 초과분이 드러난다


# ── 원장 기록 ────────────────────────────────────────────────────────────

def test_ledger_writes_order_lifecycle_row(tmp_path: Path):
    orders_path = tmp_path / "orders.jsonl"
    inner = _PlainSink()
    ledger = TradeLedgerSink(inner, path=tmp_path / "trades.jsonl",
                             orders_path=orders_path)
    state = on_fill(accept(Order(symbol="TQQQ", side=Side.BUY, qty=20,
                                 strategy_id="donchian"), "A1", T0),
                    qty=8, price=100.0, at=T0)

    ledger.on_order(state)

    row = json.loads(orders_path.read_text(encoding="utf-8").strip())
    assert row["status"] == "partially_filled"
    assert row["requested_qty"] == 20
    assert row["filled_qty"] == 8
    assert row["remaining_qty"] == 12
    assert row["symbol"] == "TQQQ"
    assert row["broker_order_id"] == "A1"
    assert row["market"] == "US"


def test_full_chain_from_loop_to_file_actually_writes(tmp_path: Path):
    """**배선이 진짜로 닿는지** 끝에서 끝까지 본다.

    루프는 `isinstance(sinks, OrderSink)` 로 물어보는데, 실제 조립에서는 세션 집계
    래퍼(_SessionTallySink)가 가장 바깥이다. 그 래퍼가 `on_order` 를 전달하지 않으면
    판정이 False 가 되어 주문 원장이 **조용히 비어 있게 된다** — 배선한 줄 알았는데
    아무 일도 안 하는, 이 저장소가 반복해서 다친 그 모양이다.
    """
    from quant.trade.loop import _SessionTallySink

    orders_path = tmp_path / "orders.jsonl"
    sinks = _SessionTallySink(
        TradeLedgerSink(_PlainSink(), path=tmp_path / "trades.jsonl",
                        orders_path=orders_path),
        market_of={"TQQQ": "US"}, fx=None,
    )

    _run(_Broker(fill_qty=8), sinks)

    row = json.loads(orders_path.read_text(encoding="utf-8").strip())
    assert row["status"] == "partially_filled"
    assert row["remaining_qty"] == 12


def test_ledger_write_failure_does_not_propagate(tmp_path: Path):
    """원장이 못 써져도 체결 처리는 계속된다(on_fill 의 기존 계약과 같다)."""
    ledger = TradeLedgerSink(_PlainSink(), path=tmp_path / "trades.jsonl",
                             orders_path=tmp_path / "없는디렉토리" / "x" / "orders.jsonl")
    ledger._orders_path = Path("/dev/null/불가능/orders.jsonl")
    state = accept(Order(symbol="TQQQ", side=Side.BUY, qty=20, strategy_id="d"), "A1", T0)

    ledger.on_order(state)   # 예외가 새어나오면 안 된다
