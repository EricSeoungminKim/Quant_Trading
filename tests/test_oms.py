"""OMS 주문 상태기계 — Phase 6.1/6.2. 순수 함수, I/O 없음.

## 이게 무엇을 막는가

지금은 "주문했다"와 "체결됐다" 사이가 **없다.** paper 는 즉시 체결이라 안 보이지만,
실계좌에서 20주 중 8주만 채워지면 원장은 20주로 안다. 토스 어댑터의 현재 주석이
그 사실을 이미 적어두고 있다 — 폴링 타임아웃 시 "체결된 만큼만 Fill 로 반영하고
**미체결 잔량은 버린다**", 거부·취소는 "부분 체결 유무와 무관하게 None".

그래서 이 상태기계가 지켜야 할 두 가지:

1. **부분 체결된 수량을 잃지 않는다.** 취소·거부가 와도 이미 채워진 건 실재한다.
2. **터미널 상태는 터미널이다.** 늦게 도착한 브로커 이벤트가 닫힌 주문을 되살리면
   원장이 두 번 세어진다.

## 지어낸 상태를 두지 않는다

상태 어휘는 브로커가 실제로 돌려주는 값에서 왔다(토스: FILLED / REJECTED /
CANCELED / PARTIAL_FILLED / PENDING...). **"우리가 모른다"를 상태로 만들지 않는다** —
폴링이 결론 없이 끝나면 주문은 그냥 **열린 채**로 남고, 그 잔량이 대사(6.5)가
집어야 할 대상이다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant.core.models import Fill, Order, OrderStatus, Side
from quant.core.oms import (
    InvalidTransition,
    accept,
    on_cancel,
    on_expire,
    on_fill,
    on_reject,
)

T0 = datetime(2026, 8, 14, 0, 30, tzinfo=timezone.utc)


def _order(qty: float = 20) -> Order:
    return Order(symbol="TQQQ", side=Side.BUY, qty=qty, strategy_id="donchian")


# ── 접수 ──────────────────────────────────────────────────────────────────

def test_accepted_order_is_open_with_nothing_filled():
    st = accept(_order(), broker_order_id="A1", at=T0)

    assert st.status is OrderStatus.ACCEPTED
    assert st.filled_qty == 0
    assert st.remaining_qty == 20
    assert st.is_open and not st.is_terminal
    assert st.broker_order_id == "A1"


# ── 부분 체결 ─────────────────────────────────────────────────────────────

def test_partial_fill_keeps_the_order_open_and_records_the_remainder():
    """**이 저장소가 오늘 잃고 있는 정보가 바로 remaining_qty 다.**"""
    st = on_fill(accept(_order(20), "A1", T0), qty=8, price=100.0, at=T0)

    assert st.status is OrderStatus.PARTIALLY_FILLED
    assert st.filled_qty == 8
    assert st.remaining_qty == 12
    assert st.is_open


def test_fills_accumulate_and_close_the_order_exactly_at_requested_qty():
    st = accept(_order(20), "A1", T0)
    st = on_fill(st, qty=8, price=100.0, at=T0)
    st = on_fill(st, qty=12, price=110.0, at=T0)

    assert st.status is OrderStatus.FILLED
    assert st.filled_qty == 20
    assert st.remaining_qty == 0
    assert st.is_terminal


def test_average_price_is_quantity_weighted_not_last_price():
    """마지막 체결가를 평균가로 쓰면 실현손익이 통째로 틀어진다."""
    st = accept(_order(10), "A1", T0)
    st = on_fill(st, qty=8, price=100.0, at=T0)
    st = on_fill(st, qty=2, price=200.0, at=T0)

    assert st.avg_price == pytest.approx((8 * 100.0 + 2 * 200.0) / 10)


def test_overfill_is_rejected_loudly():
    """요청보다 많이 채워지는 건 브로커 버그거나 이중 계산이다.

    조용히 받아들이면 원장이 없는 물량을 갖게 되고, 그 위에서 도는 청산 계산이
    전부 틀린다.
    """
    st = on_fill(accept(_order(10), "A1", T0), qty=6, price=100.0, at=T0)

    with pytest.raises(InvalidTransition, match="초과"):
        on_fill(st, qty=5, price=100.0, at=T0)


def test_zero_or_negative_fill_is_rejected():
    st = accept(_order(10), "A1", T0)
    for bad in (0, -1):
        with pytest.raises(InvalidTransition):
            on_fill(st, qty=bad, price=100.0, at=T0)


def test_fractional_share_fill_closes_within_tolerance():
    """미국 분할주는 소수점 6자리까지다 — 그 아래 잔량으로 주문이 영원히 열려 있으면
    대사가 매번 불일치를 외친다(reconcile._QTY_TOLERANCE 와 같은 기준)."""
    st = accept(_order(0.3), "A1", T0)
    st = on_fill(st, qty=0.1, price=100.0, at=T0)
    st = on_fill(st, qty=0.2, price=100.0, at=T0)

    assert st.status is OrderStatus.FILLED
    assert st.remaining_qty == 0


# ── 거부 / 취소 / 만료 ────────────────────────────────────────────────────

def test_rejected_order_records_the_reason():
    st = on_reject(accept(_order(), "A1", T0), reason="증거금 부족", at=T0)

    assert st.status is OrderStatus.REJECTED
    assert st.filled_qty == 0
    assert "증거금 부족" in st.reason
    assert st.is_terminal


def test_cancel_after_partial_fill_keeps_what_was_actually_filled():
    """**이미 채워진 8주는 실재한다.** 취소를 "아무 일도 없었음"으로 읽으면
    원장과 계좌가 8주만큼 어긋난 채로 계속 돈다 — 지금 토스 어댑터가 하는 일이다
    (REJECTED/CANCELED 는 부분 체결 유무와 무관하게 None).
    """
    st = on_fill(accept(_order(20), "A1", T0), qty=8, price=100.0, at=T0)

    st = on_cancel(st, at=T0)

    assert st.status is OrderStatus.CANCELED
    assert st.filled_qty == 8          # 잃지 않는다
    assert st.remaining_qty == 12      # 못 받은 잔량도 남는다
    assert st.is_terminal


def test_expired_day_order_keeps_partial_fill():
    """DAY 주문은 장 마감에 자동 만료된다 — 그때도 채워진 만큼은 실재한다."""
    st = on_fill(accept(_order(20), "A1", T0), qty=5, price=100.0, at=T0)

    st = on_expire(st, at=T0)

    assert st.status is OrderStatus.EXPIRED
    assert st.filled_qty == 5


# ── 터미널은 터미널이다 ──────────────────────────────────────────────────

@pytest.mark.parametrize("close", [
    lambda s: on_reject(s, reason="x", at=T0),
    lambda s: on_cancel(s, at=T0),
    lambda s: on_expire(s, at=T0),
])
def test_no_event_can_reopen_a_closed_order(close):
    """늦게 도착한 브로커 이벤트가 닫힌 주문을 되살리면 원장이 두 번 세어진다.

    폴링·재시작·재전송이 겹치는 실계좌에서 실제로 일어나는 순서다.
    """
    st = close(accept(_order(20), "A1", T0))

    with pytest.raises(InvalidTransition):
        on_fill(st, qty=1, price=100.0, at=T0)
    with pytest.raises(InvalidTransition):
        on_cancel(st, at=T0)


def test_fully_filled_order_rejects_further_fills():
    st = on_fill(accept(_order(10), "A1", T0), qty=10, price=100.0, at=T0)

    with pytest.raises(InvalidTransition):
        on_fill(st, qty=1, price=100.0, at=T0)


def test_transitions_do_not_mutate_the_previous_state():
    """상태는 불변이다 — 이전 상태를 들고 있는 코드가 조용히 바뀌면 안 된다."""
    accepted = accept(_order(20), "A1", T0)

    on_fill(accepted, qty=8, price=100.0, at=T0)

    assert accepted.filled_qty == 0
    assert accepted.status is OrderStatus.ACCEPTED


# ── 어댑터 전용 안전 변형 ────────────────────────────────────────────────

def test_report_fill_never_drops_a_fill_even_on_mismatch():
    """**돈은 이미 움직였다.**

    2026-08-14 실측: 토스 어댑터가 `filled_from` 의 예외를 밖으로 올려 루프가
    "주문 실행 실패"로 처리하고 **체결을 통째로 버렸다**. 순수 계층의 엄격함이
    어댑터로 새면 원장이 계좌와 어긋난다 — 이 저장소의 기존 계약("어댑터의 예외는
    어댑터 안에서 삼킨다")과도 어긋난다.
    """
    from quant.core.oms import report_fill

    order = _order(1)
    fill = Fill(symbol="TQQQ", side=Side.BUY, qty=5.0, price=100.0, ts=T0,
                strategy_id="donchian")

    st = report_fill(order, fill, "A1", at=T0)

    assert st.fill is fill                    # 체결을 잃지 않는다
    assert st.filled_qty == 5.0
    assert "불일치" in st.reason              # 사실을 지우지도 않는다
    assert st.remaining_qty < 0               # 초과분이 드러난다


def test_report_fill_matches_filled_from_when_consistent():
    from quant.core.oms import filled_from, report_fill

    order = _order(20)
    fill = Fill(symbol="TQQQ", side=Side.BUY, qty=8.0, price=100.0, ts=T0,
                strategy_id="donchian")

    assert report_fill(order, fill, "A1") == filled_from(order, fill, "A1")
