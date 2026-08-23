"""주문 상태기계 — Phase 6.2. **순수 함수만. I/O 없음, 시각도 주입받는다.**

## 왜 core 인가 (계획서는 trade/ 라고 했다)

브로커 어댑터가 이걸 써야 하는데 `quant.adapters` 는 `quant.trade` 를 임포트할 수
없다(아키텍처 규칙 — 어댑터가 거래 로직을 알면 안 된다). 상태기계는 `core.models`
말고는 아무것도 의존하지 않는 순수 코드이고, **거래 평면과 어댑터 양쪽이 같은
전이 규칙을 써야** 조립이 갈리지 않는다. 그래서 의존 방향의 바닥인 core 에 둔다.

## 왜 이게 실거래 전환의 선결 조건인가

지금은 "주문했다"와 "체결됐다" 사이가 없다. `Broker.place_order()` 가 `Fill | None`
을 돌려주므로 표현할 수 있는 결과가 둘뿐이다 — 다 됐거나, 아무 일도 없었거나.
현실은 그렇지 않고, 토스 어댑터의 주석이 이미 그 사실을 적어두고 있다:

- 폴링 타임아웃 시 "체결된 만큼만 Fill 로 반영하고 **미체결 잔량은 버린다**"
- "REJECTED/CANCELED 는 부분 체결 유무와 무관하게 None"

paper 는 즉시 체결이라 이 구멍이 안 보인다. **실계좌에서 20주 중 8주만 채워지면
원장은 20주로 안다.** 그 위에서 도는 청산 계산이 전부 틀린 수량 위에 선다.

## 이 모듈이 지키는 두 가지

1. **부분 체결된 수량을 잃지 않는다.** 취소·거부·만료가 와도 이미 채워진 건 실재한다.
2. **터미널 상태는 터미널이다.** 늦게 도착한 이벤트가 닫힌 주문을 되살리면 원장이
   두 번 세어진다 — 폴링·재시작·재전송이 겹치는 실계좌에서 실제로 나는 순서다.

## 왜 예외인가 (None 이 아니라)

이 저장소의 어댑터들은 실패를 `None` 으로 삼킨다 — 네트워크가 흔들려도 매매가
멈추면 안 되기 때문이다. **여기는 반대다.** 잘못된 전이는 네트워크 사정이 아니라
*우리 장부가 틀렸다*는 뜻이고, 조용히 넘기면 그 순간부터 모든 수량 계산이 오염된다.
거래 평면에서 돈이 걸린 불변식은 시끄럽게 깨져야 한다.
"""
from __future__ import annotations

import logging
from datetime import datetime

from quant.core.models import QTY_TOLERANCE, Fill, Order, OrderState, OrderStatus


logger = logging.getLogger(__name__)


class InvalidTransition(ValueError):
    """일어날 수 없는 전이. **삼키지 않는다** — 장부가 틀렸다는 신호다."""


def accept(order: Order, broker_order_id: str | None = None,
           at: datetime | None = None) -> OrderState:
    """브로커가 주문을 받았다. 아직 아무것도 체결되지 않았다."""
    return OrderState(
        order=order,
        status=OrderStatus.ACCEPTED,
        broker_order_id=broker_order_id,
        updated_at=at,
    )


def on_fill(state: OrderState, qty: float, price: float,
            at: datetime | None = None) -> OrderState:
    """체결 한 건을 누적한다. 요청 수량을 채우면 FILLED 로 닫힌다.

    평균가는 **수량 가중**이다 — 마지막 체결가를 쓰면 실현손익이 통째로 틀어진다.
    """
    _require_open(state, "체결")
    if qty <= 0:
        raise InvalidTransition(f"체결 수량이 0 이하다: {qty}")

    filled = state.filled_qty + qty
    if filled - state.order.qty > QTY_TOLERANCE:
        # 요청보다 많이 채워지는 건 브로커 버그거나 우리가 이중으로 세고 있다는 뜻이다.
        # 조용히 받으면 원장이 없는 물량을 갖고, 그 위 청산 계산이 전부 틀린다.
        raise InvalidTransition(
            f"요청 수량을 초과하는 체결: 요청 {state.order.qty}, "
            f"누적 {state.filled_qty} + 신규 {qty} = {filled}"
        )

    prior_notional = (state.avg_price or 0.0) * state.filled_qty
    avg = (prior_notional + price * qty) / filled

    done = state.order.qty - filled <= QTY_TOLERANCE
    return _replace(
        state,
        status=OrderStatus.FILLED if done else OrderStatus.PARTIALLY_FILLED,
        filled_qty=filled,
        avg_price=avg,
        updated_at=at,
    )


def on_reject(state: OrderState, reason: str, at: datetime | None = None) -> OrderState:
    """브로커가 주문을 거부했다. 사유를 남긴다 — 사후에 "왜 안 샀지"에 답해야 한다."""
    _require_open(state, "거부")
    return _replace(state, status=OrderStatus.REJECTED, updated_at=at,
                    reason=reason or "사유 없음")


def on_cancel(state: OrderState, at: datetime | None = None,
              reason: str = "") -> OrderState:
    """취소됐다. **이미 채워진 수량은 그대로 둔다** — 그건 실재하는 체결이다."""
    _require_open(state, "취소")
    return _replace(state, status=OrderStatus.CANCELED, updated_at=at,
                    reason=reason or state.reason)


def on_expire(state: OrderState, at: datetime | None = None) -> OrderState:
    """DAY 주문이 장 마감에 만료됐다. 부분 체결분은 남는다."""
    _require_open(state, "만료")
    return _replace(state, status=OrderStatus.EXPIRED, updated_at=at)


# ── 내부 ──────────────────────────────────────────────────────────────────

def _require_open(state: OrderState, what: str) -> None:
    if state.is_terminal:
        raise InvalidTransition(
            f"이미 닫힌 주문({state.status.value})에 {what} 이벤트가 왔다 — "
            f"{state.order.symbol} {state.order.side.value} {state.order.qty}. "
            "늦게 도착한 브로커 이벤트일 수 있다(원장 이중 계산 위험)."
        )


def _replace(state: OrderState, **changes) -> OrderState:
    from dataclasses import replace

    return replace(state, **changes)


# ── 어댑터용 조립 헬퍼 ────────────────────────────────────────────────────
#
# 브로커 어댑터가 매번 accept→on_fill 을 손으로 엮으면 그 조립이 어댑터마다 조금씩
# 달라진다. 결과 종류는 사실 넷뿐이라 그걸 이름으로 못 박는다.

def not_submitted(order: Order, reason: str, at: datetime | None = None) -> OrderState:
    """**브로커에 닿지 못했다.** MODE!=live, 엔진 소유 수량 0, 수량 계산 실패 등.

    "브로커가 거부했다"와 다른 사건이라 구분돼야 한다 — 전자는 우리 설정·장부
    문제이고 후자는 시장·계좌 문제다. 상태를 하나 더 만들지 않고
    `broker_order_id is None` 으로 구분한다.
    """
    return _replace(accept(order, broker_order_id=None, at=at),
                    status=OrderStatus.REJECTED, reason=reason)


def rejected_by_broker(order: Order, broker_order_id: str | None, reason: str,
                       at: datetime | None = None) -> OrderState:
    """브로커가 거부·취소했다. 주문번호가 있으면 실제로 서버에 닿았다는 뜻이다."""
    return on_reject(accept(order, broker_order_id, at), reason=reason, at=at)


def filled_from(order: Order, fill: Fill, broker_order_id: str | None = None,
                at: datetime | None = None) -> OrderState:
    """체결 하나로 상태를 만든다. `fill.qty < order.qty` 면 자동으로 부분체결이 된다.

    **여기가 오늘 잃던 정보를 살리는 지점이다** — 잔량이 `remaining_qty` 로 남는다.
    """
    state = on_fill(accept(order, broker_order_id, at), fill.qty, fill.price,
                    at=getattr(fill, "ts", None) or at)
    return _replace(state, fill=fill)


def open_without_fill(order: Order, broker_order_id: str | None,
                      reason: str = "", at: datetime | None = None) -> OrderState:
    """주문은 나갔는데 결론을 못 봤다(폴링 타임아웃, 체결 0).

    **터미널이 아니다.** "모른다"를 상태로 만들지 않는다 — 주문은 서버에 남아 있을
    수 있고, 그 잔량이 대사(6.5)가 집어야 할 대상이다.
    """
    return _replace(accept(order, broker_order_id, at), reason=reason)


def report_fill(order: Order, fill: Fill, broker_order_id: str | None = None,
                at: datetime | None = None) -> OrderState:
    """**어댑터 전용** — 상태 조립에 실패해도 체결을 잃지 않는다.

    `filled_from` 은 요청 수량을 넘는 체결에 예외를 던진다(장부가 틀렸다는 신호).
    그 엄격함은 순수 계층에서만 유효하다 — **어댑터에서 그 예외가 밖으로 나가면
    이미 일어난 체결이 통째로 버려지고, 원장이 계좌와 어긋난다.** 돈은 이미
    움직였다는 사실은 변하지 않는다.

    (2026-08-14 실측: 토스 어댑터가 이 예외를 올려 루프가 "주문 실행 실패"로 처리하고
    체결을 버렸다. 이 저장소의 기존 계약 — "어댑터의 예외는 어댑터 안에서 삼킨다" —
    과도 어긋났다.)

    불일치는 **지우지 않고 상태에 남긴다**: 초과분은 `remaining_qty` 가 음수로,
    사유는 `reason` 에 남아 사후에 드러난다.
    """
    try:
        return filled_from(order, fill, broker_order_id, at)
    except InvalidTransition as e:
        logger.error(
            "브로커 보고가 요청과 어긋난다 — 체결은 반영하되 불일치를 기록한다 "
            "(%s %s 요청 %s / 보고 %s): %s",
            order.symbol, order.side.value, order.qty, fill.qty, e,
        )
        return _replace(
            accept(order, broker_order_id, at),
            status=OrderStatus.FILLED,
            filled_qty=fill.qty,
            avg_price=fill.price,
            reason=f"보고 불일치: {e}",
            fill=fill,
            updated_at=getattr(fill, "ts", None) or at,
        )
