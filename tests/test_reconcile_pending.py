"""대사가 **미체결 잔량**을 안다 — Phase 6.5. 페이크만 사용(네트워크 없음).

## 오늘의 구멍

주문이 결론 없이 끝나면(폴링 타임아웃) 그 잔량은 서버에 남아 있을 수 있고, 나중에
체결된다. 그러면 브로커 보유는 늘어나는데 **엔진 원장에는 그 체결이 없다.**

지금 대사는 그 잉여를 "사용자 수동 보유"로 읽고 **정보성 로그로 넘긴다.** 사용자가
손으로 산 물량과 우리 주문이 뒤늦게 체결된 것은 완전히 다른 사건이다:

- 사용자 수동 보유 → 엔진이 건드리면 안 되는 물량. 정상.
- 우리 주문의 늦은 체결 → **엔진이 자기 포지션을 모른다.** 청산 로직이 그 물량을
  영원히 방치한다.

둘을 가르는 정보는 하나뿐이다: 그 종목에 **엔진이 낸 미체결 잔량이 있었는가.**
Phase 6.3 이후 그 값이 `OrderState.remaining_qty` 로 존재한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quant.core.models import Order, Position, Side
from quant.core.oms import accept, on_fill, on_reject
from quant.trade.reconcile import OpenOrderBook, Reconciler

T0 = datetime(2026, 8, 14, 0, 30, tzinfo=timezone.utc)


class _Control:
    def __init__(self):
        self.halted = False
        self.reason = ""
        self.by = ""

    def is_halted(self) -> bool:
        return self.halted

    def halt(self, reason: str, by: str = "manual") -> None:
        self.halted = True
        self.reason = reason
        self.by = by


class _Broker:
    """엔진 소유 원장을 노출하는 브로커(대사 대상)."""

    def __init__(self, broker_qty: dict[str, float], engine_qty: dict[str, float]):
        self._broker_qty = broker_qty
        self._engine_qty = engine_qty

    def positions(self) -> dict[str, Position]:
        return {s: Position(symbol=s, qty=q, avg_cost=100.0)
                for s, q in self._broker_qty.items()}

    def engine_owned_qty(self, symbol: str) -> float:
        return self._engine_qty.get(symbol, 0.0)

    def engine_owned_symbols(self):
        return list(self._engine_qty)


def _order(qty: float = 20) -> Order:
    return Order(symbol="TQQQ", side=Side.BUY, qty=qty, strategy_id="donchian")


# ── 미체결 장부 ───────────────────────────────────────────────────────────

def test_open_order_remainder_is_tracked():
    book = OpenOrderBook()

    book.on_order(on_fill(accept(_order(20), "A1", T0), qty=8, price=100.0, at=T0))

    assert book.pending_qty("TQQQ") == 12


def test_terminal_order_leaves_no_pending():
    """닫힌 주문의 잔량은 더 이상 올 것이 아니다 — 남겨두면 영원히 오탐한다."""
    book = OpenOrderBook()

    book.on_order(on_reject(accept(_order(20), "A1", T0), reason="증거금", at=T0))

    assert book.pending_qty("TQQQ") == 0


def test_later_state_of_the_same_order_replaces_the_earlier_one():
    """같은 주문의 갱신이 누적되면 잔량이 두 배로 세어진다."""
    book = OpenOrderBook()
    partial = on_fill(accept(_order(20), "A1", T0), qty=8, price=100.0, at=T0)
    book.on_order(partial)

    book.on_order(on_fill(partial, qty=12, price=100.0, at=T0))   # 마저 체결

    assert book.pending_qty("TQQQ") == 0


def test_orders_without_broker_id_are_not_pending():
    """브로커에 닿지 못한 주문은 서버에 남아 있을 수 없다."""
    from quant.core.oms import not_submitted

    book = OpenOrderBook()
    book.on_order(not_submitted(_order(20), "MODE!=live", at=T0))

    assert book.pending_qty("TQQQ") == 0


# ── 대사가 그 값을 쓴다 ──────────────────────────────────────────────────

def _reconciler(broker, control, pending=None):
    return Reconciler(broker, control, interval_minutes=0, pending_qty=pending)


def test_surplus_explained_by_our_pending_order_is_a_mismatch_not_manual_holding():
    """**이 테스트가 6.5 의 존재 이유다.**

    엔진은 8주만 안다. 브로커엔 20주가 있고, 우리가 낸 주문의 미체결 잔량이 12주였다.
    이건 사용자가 손으로 산 게 아니라 **우리 주문이 뒤늦게 체결된 것**이고, 엔진은
    자기 포지션 12주를 모르는 상태다 — 청산 로직이 그 물량을 방치한다.
    """
    control = _Control()
    broker = _Broker(broker_qty={"TQQQ": 20.0}, engine_qty={"TQQQ": 8.0})
    book = OpenOrderBook()
    book.on_order(on_fill(accept(_order(20), "A1", T0), qty=8, price=100.0, at=T0))

    report = _reconciler(broker, control, book.pending_qty).check(force=True)

    assert report.mismatches, "잉여가 정보성 로그로 묻히면 안 된다"
    assert "미체결" in report.mismatches[0]
    assert report.manual_changes == []
    assert control.halted is True


def test_surplus_without_any_pending_order_is_still_manual_holding():
    """미체결 잔량이 없으면 그건 정말 사용자 물량이다 — 오탐을 만들지 않는다."""
    control = _Control()
    broker = _Broker(broker_qty={"TQQQ": 20.0}, engine_qty={"TQQQ": 8.0})

    report = _reconciler(broker, control, OpenOrderBook().pending_qty).check(force=True)

    assert report.mismatches == []
    assert report.manual_changes == ["TQQQ: 0 → 12"]
    assert control.halted is False


def test_surplus_larger_than_pending_is_reported_as_mismatch_too():
    """잔량 12주로 설명되지 않는 30주 잉여 — 설명 가능한 부분이 있으면 알린다."""
    control = _Control()
    broker = _Broker(broker_qty={"TQQQ": 38.0}, engine_qty={"TQQQ": 8.0})
    book = OpenOrderBook()
    book.on_order(on_fill(accept(_order(20), "A1", T0), qty=8, price=100.0, at=T0))

    report = _reconciler(broker, control, book.pending_qty).check(force=True)

    assert report.mismatches


def test_reconciler_without_pending_source_behaves_exactly_as_before():
    """`pending_qty` 를 안 주면 기존 동작 그대로 — 배선 전에도 회귀가 없다."""
    control = _Control()
    broker = _Broker(broker_qty={"TQQQ": 20.0}, engine_qty={"TQQQ": 8.0})

    report = Reconciler(broker, control, interval_minutes=0).check(force=True)

    assert report.mismatches == []
    assert report.manual_changes == ["TQQQ: 0 → 12"]


def test_pending_lookup_failure_does_not_break_reconciliation():
    """부가 정보 조회 실패가 대사 자체를 죽이면 안 된다."""
    control = _Control()
    broker = _Broker(broker_qty={"TQQQ": 20.0}, engine_qty={"TQQQ": 8.0})

    def boom(symbol):
        raise RuntimeError("장부 손상")

    report = _reconciler(broker, control, boom).check(force=True)

    assert report.checked is True


# ── 배선이 진짜로 닿는지 ─────────────────────────────────────────────────

def test_open_order_book_is_actually_fed_through_the_real_sink_chain(tmp_path):
    """**조립된 그대로** 루프→체인→장부까지 값이 흐르는지 본다.

    2026-08-14 배선 중 두 군데가 끊겨 있었다: `MultiSink` 에 `on_order` 가 없어
    체인 안쪽 소비자가 굶었고, `OpenOrderBook` 에 `on_fill` 이 없어 크래시했다.
    둘 다 "붙였는데 아무 일도 안 일어나는" 실패라 테스트로 고정한다.
    """
    from quant.adapters.persistence.sink import JsonlSink, MultiSink
    from quant.control.ledger import TradeLedgerSink
    from quant.trade.loop import _SessionTallySink

    book = OpenOrderBook()
    sinks = _SessionTallySink(
        TradeLedgerSink(
            MultiSink([JsonlSink(path=tmp_path / "events.jsonl"), book]),
            path=tmp_path / "trades.jsonl", orders_path=tmp_path / "orders.jsonl",
        ),
        market_of={"TQQQ": "US"}, fx=None,
    )

    sinks.on_order(on_fill(accept(_order(20), "A1", T0), qty=8, price=100.0, at=T0))

    assert book.pending_qty("TQQQ") == 12


def test_stale_pending_ages_out_so_it_cannot_halt_forever():
    """**TTL 이 없으면 이 장부는 영구 halt 생성기가 된다.**

    루프는 주문을 낼 때 상태를 한 번만 준다. 폴링 타임아웃으로 열린 채 남은 주문은
    그 뒤 갱신이 오지 않으므로, TTL 이 없으면 잔량이 영원히 남아 대사가 매번 불일치로
    읽고 신규 진입을 계속 막는다(구현 중 실제로 만든 결함).
    """
    class _Clock:
        def __init__(self):
            self.t = datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)

        def now(self):
            return self.t

    clock = _Clock()
    book = OpenOrderBook(ttl_minutes=60, clock=clock)
    book.on_order(on_fill(accept(_order(20), "A1", T0), qty=8, price=100.0, at=T0))
    assert book.pending_qty("TQQQ") == 12

    clock.t += timedelta(minutes=61)

    assert book.pending_qty("TQQQ") == 0
