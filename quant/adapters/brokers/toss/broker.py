"""TossBroker — domain.interfaces.Broker 구현체. client.py(raw HTTP)를 도메인 모델에 매핑한다.

어댑터 규칙: 네트워크/API 예외는 여기서 로깅 후 삼킨다 — 코어로 raw 예외를 전파하지 않는다
(domain.interfaces 문서 규칙).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, time as dtime, timezone
from pathlib import Path

import httpx
from zoneinfo import ZoneInfo

from quant.core import oms
from quant.core.models import Fill, OpenOrder, Order, OrderState, Position, Side
from quant.core.portfolio.ownership import EngineOwnership
from quant.core.portfolio.portfolio import Portfolio

from .client import TossAPIError, TossClient

logger = logging.getLogger(__name__)

# 체결 완료로 취급하는 상태. REJECTED/CANCELED는 부분 체결 유무와 무관하게 미체결로 취급한다
# (owner 지시 — Toss 문서상으론 execution.filledQuantity로 부분 체결을 구분할 수 있다지만
# 여기선 단순하게 None을 반환하고, 다음 사이클의 positions() 조정을 안전망으로 둔다).
_REJECTED_STATUSES = {"REJECTED", "CANCELED"}

# cancel_order 호출이 이 코드로 실패하면 "취소를 못 했다"가 아니라 "취소할 게 이미
# 없었다"는 뜻이다(docs/api/toss/openapi.json cancelOrder 409 응답) — 미체결 위험이
# 남아 있지 않으므로 stale 주문 감시 입장에서는 성공(True)과 동치로 취급한다.
_CANCEL_ALREADY_RESOLVED_CODES = {"already-canceled", "already-filled"}

# KR 심볼은 6자리 숫자 문자열, 그 외는 US 티커로 취급한다 (Toss 스펙 자체가 심볼 형식만으로
# 시장을 구분함 — market 파라미터가 별도로 없음).
_KR_SYMBOL_RE = re.compile(r"^\d{6}$")

# US 정규장(America/New_York 09:30-16:00, 평일). quant/core/clock.py의 _is_open과
# 동일한 정의지만, data/**는 이 작업 범위 밖(동시 수정 중)이라 이 어댑터 내부에 독립적으로 둔다.
_US_TZ = ZoneInfo("America/New_York")
_US_REGULAR_OPEN = dtime(9, 30)
_US_REGULAR_CLOSE = dtime(16, 0)

# 조건주문 expireDate(YYYY-MM-DD)는 Toss 기준 시각(KST)으로 해석된다.
KST = ZoneInfo("Asia/Seoul")


def _is_kr_symbol(symbol: str) -> bool:
    return bool(_KR_SYMBOL_RE.match(symbol))


# KR 호가 단위 그리드 (2023년 개편표, 원). (미만 가격 상한, 그 구간의 틱) 오름차순.
# 이 표의 마지막 구간(500,000원 이상)은 표에 없으므로 _KR_TICK_ABOVE 로 별도 처리.
_KR_TICK_TABLE: tuple[tuple[float, int], ...] = (
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
)
_KR_TICK_ABOVE = 1_000  # 500,000원 이상


def snap_to_tick(price: float, side: str = "SELL") -> int:
    """가격을 KR 호가 단위 그리드에 맞춘다 (조건주문 서버측 보호주문 등록용).

    그냥 `round()`하면 2,000원 이상 종목에서 호가단위를 벗어난 가격이 나와
    서버가 400 invalid-request로 거부한다 — 서버측 보호주문(OCO 손절/익절)이
    조용히 빠지는 실계좌 차단급 결함이었다.

    방향은 **보수적으로** 고정한다: side="BUY"만 올림(지정 매수가 밀려서 체결
    안 되는 쪽을 피함), 그 외(기본값 "SELL" 포함, 즉 손절가·목표가 둘 다)는
    내림 — 손절은 더 일찍 발동하는 쪽이 안전하고 목표가도 보수적으로 낮춘다.

    **한계**: ETF는 실제로는 5원 고정 틱이 별도로 있지만(2023년 개편), 이 계층
    (브로커 어댑터)에는 종목이 ETF인지 판별할 정보가 없다 — 그래서 이 함수는
    적용하지 않는다. 2,000원 미만 ETF에서 최대 4원 오차가 날 수 있고, 그 경우
    서버가 nearestPrices 필드와 함께 거부하면 로그로 드러난다. 그 실패 시의
    기존 폴백(엔진 폴링 손절, 최대 poll_interval초 지연)은 그대로 안전망으로
    남는다 — 이 함수의 실패가 무방비 상태를 만들지 않는다.
    """
    tick = _KR_TICK_ABOVE
    for threshold, t in _KR_TICK_TABLE:
        if price < threshold:
            tick = t
            break
    if side == "BUY":
        return int(math.ceil(price / tick)) * tick
    return int(math.floor(price / tick)) * tick


def _is_us_regular_hours(now: datetime) -> bool:
    local = now.astimezone(_US_TZ)
    if local.weekday() >= 5:
        return False
    return _US_REGULAR_OPEN <= local.time() < _US_REGULAR_CLOSE


# 우리(전략)가 붙인 포지션 부가 상태(meta: entry/stop/target/adds_done/scaled_out 등)의
# 영속화 경로. holdings()는 qty/avg_cost의 소스 오브 트루스일 뿐 meta 개념이 없으므로,
# meta는 별도로 여기서 관리한다 — Portfolio의 기존 원자적 저장(tmp+replace) 메커니즘을
# 그대로 재사용한다(cash 필드는 쓰지 않고 0으로 둔다).
_DEFAULT_META_STATE_PATH = Path("data/state/toss_position_meta.json")

# 엔진 소유 포지션 원장 + 주문 의도 저널의 기본 경로.
_DEFAULT_OWNERSHIP_STATE_PATH = Path("data/state/engine_positions.json")
_DEFAULT_INTENTS_PATH = Path("data/state/order_intents.jsonl")


class TossBroker:
    def __init__(
        self, client: TossClient, *,
        poll_max_attempts: int = 5,
        poll_interval: float = 1.0,
        meta_state_path: Path = _DEFAULT_META_STATE_PATH,
        ownership: EngineOwnership | None = None,
        intents_path: Path | None = _DEFAULT_INTENTS_PATH,
        register_protective_stops: bool = True,
    ) -> None:
        self._client = client
        self._poll_max_attempts = poll_max_attempts
        self._poll_interval = poll_interval
        self._meta_state_path = Path(meta_state_path)
        self._meta_store: Portfolio | None = None  # positions() 첫 호출 시 지연 로드
        self._ownership = ownership or EngineOwnership(_DEFAULT_OWNERSHIP_STATE_PATH)
        self._intents_path = Path(intents_path) if intents_path is not None else None
        self._register_protective_stops = register_protective_stops

    def _get_meta_store(self) -> Portfolio:
        if self._meta_store is None:
            self._meta_store = Portfolio.load_or_init(start_cash=0.0, state_path=self._meta_state_path)
        return self._meta_store

    # ------------------------------------------------- 엔진 소유 포지션 (사용자 수동 보유와 분리)
    def engine_owned_qty(self, symbol: str) -> float:
        """엔진이 진입해서 아직 들고 있는 수량. holdings()에는 있지만 여기 0인 종목은
        **사용자가 손으로 산 물량**이므로 엔진의 주문 대상이 아니다."""
        return self._ownership.owned_qty(symbol)

    def engine_owned_symbols(self) -> set[str]:
        return self._ownership.symbols()

    def _append_intent(self, record: dict) -> None:
        """주문 의도/결과를 append-only 저널에 남긴다.

        clientOrderId가 서버측 멱등키라 중복 주문 방지는 이 파일이 아니라 그 키가
        한다(place_order docstring 참고). 이 저널의 용도는 **사후 복구**다: 프로세스가
        주문 직후 죽으면 "그 키로 주문이 나갔는지"를 사람이 확인할 근거가 로그밖에
        없다. 쓰기 실패는 절대 거래를 막지 않는다 — 삼키고 로그만 남긴다."""
        if self._intents_path is None:
            return
        try:
            self._intents_path.parent.mkdir(parents=True, exist_ok=True)
            with self._intents_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("주문 의도 저널 기록 실패 (%s: %s)", type(e).__name__, e)

    def place_order(self, order: Order) -> OrderState:
        """MODE 환경변수가 "live"가 아니면 실주문을 거부하고 경고만 남긴다.

        매도 상한: 수량은 항상 **엔진 소유 원장**(EngineOwnership)으로 클램프된다.
        사용자가 같은 계좌에서 손으로 산 물량은 브로커 holdings()에는 보이지만
        엔진 원장에는 없으므로 주문 수량에 포함되지 않는다 — 이 클램프가 "엔진이
        사용자 보유를 판다"는 사고를 막는 마지막 방어선이다(리스크 레이어에도 같은
        상한이 있지만, 그쪽을 우회하는 경로가 생겨도 여기서 걸린다).

        미체결/부분체결 방침:
        - 폴링 안에 FILLED가 오면 체결 수량·평균가·수수료(commission+tax)로 Fill 구성.
        - 폴링 타임아웃 시 filledQuantity>0이면 **체결된 만큼만** Fill로 반영하고
          미체결 잔량은 버린다(취소하지 않는다 — DAY 주문이라 장 마감에 자동 만료되고,
          그 사이 체결되면 다음 사이클 positions()/대사가 잡는다).
        - filledQuantity==0이면 None. 주문은 서버에 남아 있을 수 있으므로 엔진 원장은
          건드리지 않는다 — 대사(app/reconcile.py)가 불일치로 감지한다.
        - REJECTED/CANCELED는 부분 체결 유무와 무관하게 None.
        """
        if os.environ.get("MODE") != "live":
            logger.warning(
                "TossBroker.place_order 거부: MODE!=live (symbol=%s side=%s qty=%s)",
                order.symbol, order.side.value, order.qty,
            )
            return oms.not_submitted(order, "MODE!=live — 실주문 차단")
        side = "BUY" if order.side is Side.BUY else "SELL"

        qty_requested = order.qty
        if order.side is Side.SELL:
            owned = self._ownership.owned_qty(order.symbol)
            if owned <= 0:
                logger.warning(
                    "TossBroker.place_order 매도 거부: 엔진 소유 수량 0 (symbol=%s, 요청=%s) — "
                    "사용자 수동 보유는 엔진의 주문 대상이 아니다",
                    order.symbol, order.qty,
                )
                return oms.not_submitted(
                    order, "엔진 소유 수량 0 — 사용자 수동 보유는 주문 대상이 아니다")
            if qty_requested > owned:
                logger.warning(
                    "TossBroker.place_order 매도 수량을 엔진 소유분으로 축소 "
                    "(symbol=%s, 요청=%s → %s)", order.symbol, qty_requested, owned,
                )
                qty_requested = owned

        resolved = self._resolve_quantity_or_amount(order, side, qty_requested)
        if resolved is None:
            return oms.not_submitted(order, "수량/주문금액 계산 실패")
        qty, order_amount = resolved

        # clientOrderId는 Toss의 주문 생성 멱등키다 (≤36자, [a-zA-Z0-9_-], 10분간 유효 —
        # 동일 키 재사용 시 새 주문이 아니라 기존 주문 결과를 그대로 돌려준다). 이 호출
        # 내에서 발생하는 모호한 네트워크 실패 재시도는 그래서 안전하다: 이전 요청이 실제로
        # 서버에 닿았어도 중복 주문이 되지 않는다.
        client_order_id = uuid.uuid4().hex
        intent_record = {
            "event": "intent",
            "ts": datetime.now(timezone.utc).isoformat(),
            "client_order_id": client_order_id,
            "symbol": order.symbol,
            "side": side,
            "quantity": qty,
            "order_amount": order_amount,
            "strategy_id": order.strategy_id,
            "reason": order.reason,
        }
        # 결정 시점 시세 — TCA(quant/control/tca.py)가 의도가 vs 체결가로
        # 슬리피지를 재는 기준. 없으면 키 생략(값을 지어내지 않는다).
        if order.ref_price is not None:
            intent_record["price"] = order.ref_price
        self._append_intent(intent_record)

        result = None
        for attempt in range(1, 3):  # 원 시도 + 모호한 네트워크 실패 시 1회 재시도
            try:
                result = self._client.place_order(
                    order.symbol, side, "MARKET",
                    quantity=qty, order_amount=order_amount,
                    client_order_id=client_order_id,
                )
                break
            except (TossAPIError, RuntimeError, ValueError) as e:
                logger.warning("TossBroker.place_order 실패 (%s): %s: %s",
                                order.symbol, type(e).__name__, e)
                if isinstance(e, TossAPIError) and e.code == "insufficient-buying-power":
                    # 소유자 지시(2026-08-31): "잔고 부족으로 못 산 경우, 시도 기록을
                    # 남겨 나중에 '안 산 게 아니라 못 샀던 것'이 되게." 이 422 응답은
                    # 주문 자체가 서버에 생성되지 않아 사후 조회(order-status)로
                    # 재구성할 수 없다 — 이 except 블록이 사유를 남길 **유일한
                    # 기회**다. "자금 부족" 마커는 risk 레이어(quant/trade/risk/
                    # manager.py)의 예산 부족 사유 문구와 통일해 grep 한 번으로
                    # 리스크 레벨/브로커 레벨 거부를 함께 찾을 수 있게 한다.
                    needed = order_amount if order_amount is not None else (
                        qty * order.ref_price if qty is not None and order.ref_price else None
                    )
                    reason = (
                        f"자금 부족: 주문 생성 거절 (symbol={order.symbol}, "
                        f"필요금액={needed if needed is not None else '알 수 없음'}) — "
                        f"{type(e).__name__}: {e}"
                    )
                    return oms.not_submitted(order, reason)
                return oms.not_submitted(order, f"주문 생성 거절: {type(e).__name__}: {e}")
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < 2:
                    logger.warning(
                        "TossBroker.place_order 주문 생성 요청 실패(timeout/network) — "
                        "동일 clientOrderId=%s로 재시도 (symbol=%s): %s: %s",
                        client_order_id, order.symbol, type(e).__name__, e,
                    )
                    continue
                logger.error(
                    "TossBroker.place_order 재시도 후에도 실패 (clientOrderId=%s는 10분간 "
                    "유효 — 필요 시 동일 키로 수동 재시도 가능) (symbol=%s side=%s): %s: %s",
                    client_order_id, order.symbol, side, type(e).__name__, e,
                )
                # **"안 나갔다"가 아니다.** clientOrderId 멱등키 때문에 이전 요청이
                # 서버에 닿았을 수 있다 — 열린 채로 두고 대사가 집게 한다.
                return oms.open_without_fill(
                    order, None,
                    f"주문 생성 응답 못 받음(재시도 소진) — clientOrderId={client_order_id}")

        if result is None:
            return oms.open_without_fill(order, None, "주문 생성 결과 없음")

        order_id = result["orderId"]
        self._append_intent({
            "event": "placed",
            "ts": datetime.now(timezone.utc).isoformat(),
            "client_order_id": client_order_id,
            "order_id": order_id,
            "symbol": order.symbol,
            "side": side,
        })
        state = self._poll_for_fill(order_id, order)
        if state.fill is not None:
            self._on_engine_fill(order, state.fill)
        return state

    def _on_engine_fill(self, order: Order, fill: Fill) -> None:
        """엔진 체결을 원장에 반영하고 서버측 보호주문(손절/OCO)을 갱신한다.

        여기서 나는 예외는 전부 삼킨다 — 체결은 이미 일어났고, 그 사실을 보고하는
        것이 조건주문 등록보다 우선이다. 보호주문 실패는 로그로 크게 남긴다."""
        try:
            # 취소 → 원장 갱신 → 재등록 순서를 지킨다. 원장을 먼저 갱신하면 전량
            # 청산 시 보호주문 기록이 함께 지워져 **취소할 id를 잃는다** — 그러면
            # 옛 조건주문이 서버에 살아남고, 사용자가 나중에 같은 종목을 손으로 사는
            # 순간 그 물량이 엔진 손절에 팔린다.
            previous = self._ownership.protection(order.symbol)
            self._cancel_protective_orders(order.symbol, previous.get("ids", []))
            if fill.side is Side.BUY:
                self._ownership.add(order.symbol, fill.qty)
                stop, target = order.stop, order.target
            else:
                self._ownership.remove(order.symbol, fill.qty)
                # 매도 주문에는 보호가격이 실리지 않는다 — 직전에 걸어둔 값을 이어쓴다.
                stop, target = previous.get("stop"), previous.get("target")
            self._register_protective_order(order.symbol, stop, target, fill.price)
        except Exception as e:  # noqa: BLE001 — 체결 보고를 막지 않는다
            logger.error(
                "TossBroker 체결 후처리 실패 (symbol=%s side=%s qty=%s): %s: %s — "
                "엔진 소유 원장/조건주문이 실제와 어긋났을 수 있다. 대사 로그를 확인할 것",
                order.symbol, fill.side.value, fill.qty, type(e).__name__, e,
            )

    def _resolve_quantity_or_amount(
        self, order: Order, side: str, qty: float | None = None,
    ) -> tuple[float | None, float | None] | None:
        """symbol/side/현재 시각에 따라 quantity 기반 vs amount 기반 주문을 결정한다.
        반환값은 (quantity, order_amount) 중 정확히 하나만 not-None. 유효한 주문을
        만들 수 없으면 (예: 정수로 내림 후 0) None을 반환해 place_order가 스킵하게 한다.

        Toss 규칙 (docs/toss-api/QUICKREF.md 'Create order'):
        - KR: quantity는 항상 양의 정수.
        - US: quantity는 양의 정수가 기본. 예외는 MARKET+SELL만 분할 수량(소수점 6자리)
          허용, 그마저도 정규장 중에만 가능.
        - US MARKET+BUY 분할 수량은 quantity로 보낼 수 없어 amount 기반
          (orderAmount = qty * 현재가, 정규장 중에만) 주문으로 전환한다.
        - 정규장 밖에서는 분할 수량 자체가 불가능하므로 정수로 내림한다.

        qty를 넘기면 order.qty 대신 그 값을 쓴다 (매도 시 엔진 소유분으로 축소된 수량).
        """
        qty = order.qty if qty is None else qty

        if _is_kr_symbol(order.symbol):
            qty_int = int(qty)
            if qty_int <= 0:
                logger.warning(
                    "TossBroker.place_order KR 수량 정수 내림 후 0 — 주문 스킵 "
                    "(symbol=%s, qty=%s)",
                    order.symbol, order.qty,
                )
                return None
            return float(qty_int), None

        fractional = qty != int(qty)
        if fractional and not _is_us_regular_hours(datetime.now(timezone.utc)):
            qty_int = int(qty)
            if qty_int <= 0:
                logger.warning(
                    "TossBroker.place_order US 정규장 외 분할 수량 정수 내림 후 0 — "
                    "주문 스킵 (symbol=%s, qty=%s)",
                    order.symbol, order.qty,
                )
                return None
            return float(qty_int), None

        if fractional and side == "SELL":
            # US MARKET+SELL만 분할 수량을 quantity 그대로 허용 (정규장 확인됨).
            return qty, None

        if fractional and side == "BUY":
            try:
                price_result = self._client.prices([order.symbol])
                price = float(price_result[0]["lastPrice"]) if price_result else 0.0
            except (TossAPIError, IndexError, KeyError, ValueError) as e:
                logger.warning(
                    "TossBroker.place_order 분할매수 금액 산정용 시세 조회 실패 "
                    "(symbol=%s): %s: %s",
                    order.symbol, type(e).__name__, e,
                )
                return None
            if price <= 0:
                logger.warning(
                    "TossBroker.place_order 분할매수 금액 산정 실패, 시세 0 이하 "
                    "(symbol=%s)", order.symbol,
                )
                return None
            return None, round(qty * price, 2)

        # 정수 수량 (KR 아니고 분할도 아님) — quantity 그대로.
        return qty, None

    def _poll_for_fill(self, order_id: str, order: Order) -> OrderState:
        """체결 확인까지 order-status를 폴링한다 (bounded: poll_max_attempts회,
        각 시도 사이 poll_interval초 대기 — 기본값 기준 최대 약 5초)."""
        last_execution: dict | None = None
        for attempt in range(1, self._poll_max_attempts + 1):
            try:
                status_result = self._client.order(order_id)
            except (TossAPIError, RuntimeError, httpx.TimeoutException, httpx.TransportError) as e:
                logger.warning(
                    "TossBroker.place_order 상태 조회 실패 (orderId=%s, attempt=%d/%d): %s: %s",
                    order_id, attempt, self._poll_max_attempts, type(e).__name__, e,
                )
                if attempt < self._poll_max_attempts:
                    time.sleep(self._poll_interval)
                continue

            status = status_result["status"]
            last_execution = status_result["execution"]

            if status == "FILLED":
                return oms.report_fill(
                    order, self._build_fill(order, last_execution), order_id)
            if status in _REJECTED_STATUSES:
                logger.warning(
                    "TossBroker.place_order 주문 거부/취소 (orderId=%s, symbol=%s, status=%s)",
                    order_id, order.symbol, status,
                )
                # 부분 체결이 있었어도 잃지 않는다 — 채워진 만큼은 실재한다.
                filled = float(last_execution["filledQuantity"]) if last_execution else 0.0
                if filled > 0:
                    partial = oms.report_fill(
                        order, self._build_fill(order, last_execution, qty_override=filled),
                        order_id)
                    return oms.on_cancel(partial, reason=f"브로커 {status}")
                return oms.rejected_by_broker(order, order_id, f"브로커 {status}")
            # PENDING / PENDING_CANCEL / PENDING_REPLACE / PARTIAL_FILLED / REPLACED 등 —
            # 아직 최종 상태가 아니므로 계속 폴링한다.
            if attempt < self._poll_max_attempts:
                time.sleep(self._poll_interval)

        filled_qty = float(last_execution["filledQuantity"]) if last_execution else 0.0
        if filled_qty <= 0:
            logger.warning(
                "TossBroker.place_order 폴링 타임아웃, 체결 없음 (orderId=%s, symbol=%s) — "
                "다음 사이클 positions() 조정에 맡긴다",
                order_id, order.symbol,
            )
            return oms.open_without_fill(
                order, order_id, "폴링 타임아웃, 체결 0 — 서버에 남아 있을 수 있다")

        remainder = order.qty - filled_qty
        logger.warning(
            "TossBroker.place_order 폴링 타임아웃, 부분 체결만 반영 (orderId=%s, symbol=%s, "
            "filled=%s/%s, 미체결 잔량=%s)",
            order_id, order.symbol, filled_qty, order.qty, remainder,
        )
        return oms.report_fill(
            order, self._build_fill(order, last_execution, qty_override=filled_qty), order_id)

    def _build_fill(
        self, order: Order, execution: dict, qty_override: float | None = None,
    ) -> Fill:
        filled_qty = qty_override if qty_override is not None else float(execution["filledQuantity"])
        avg_price = execution.get("averageFilledPrice")
        commission = execution.get("commission")
        tax = execution.get("tax")
        fee = (float(commission) if commission is not None else 0.0) \
            + (float(tax) if tax is not None else 0.0)
        return Fill(
            symbol=order.symbol,
            side=order.side,
            qty=filled_qty,
            price=float(avg_price) if avg_price is not None else 0.0,
            ts=datetime.now(timezone.utc),
            strategy_id=order.strategy_id,
            fee=fee,
            reason=order.reason,
        )

    # ------------------------------------------------------ 서버측 보호주문 (조건주문)
    def _fmt_price(self, symbol: str, price: float, side: str = "SELL") -> str:
        """조건주문 body의 가격은 문자열이다.

        KR은 `snap_to_tick`으로 호가 단위 그리드에 스냅한 정수 — 단순
        `round()`는 2,000원 이상 종목에서 호가단위 밖 가격을 만들어 서버가
        400으로 거부한다(이 파일 상단 `snap_to_tick` 참고, 방향 기본값은
        내림). US는 소수점 2자리로 보낸다(실호출로 검증하지 못했다 —
        [미검증]. $1 미만 종목은 4자리 소수 규칙이 있다고 알려져 있으나
        우리 유니버스에 $1 미만 종목이 없어 미적용 — 여기 기록만 해둔다)."""
        if _is_kr_symbol(symbol):
            return str(snap_to_tick(price, side))
        return f"{price:.2f}"

    def _cancel_protective_orders(self, symbol: str, ids: list[str] | None = None) -> None:
        """ids를 명시하지 않으면 원장에 기록된 것을 취소한다."""
        if ids is None:
            ids = self._ownership.protection(symbol).get("ids", [])
        for conditional_order_id in ids:
            try:
                self._client.cancel_conditional_order(conditional_order_id)
            except (TossAPIError, RuntimeError, httpx.TimeoutException, httpx.TransportError) as e:
                # 취소 실패를 조용히 넘기면 옛 수량짜리 손절이 서버에 남는다 — 크게 남긴다.
                logger.error(
                    "TossBroker 조건주문 취소 실패 (symbol=%s, conditionalOrderId=%s): %s: %s — "
                    "토스 앱에서 직접 취소할 것",
                    symbol, conditional_order_id, type(e).__name__, e,
                )
        self._ownership.set_protection(symbol, [])

    def _register_protective_order(
        self, symbol: str, stop: float | None, target: float | None, last_price: float,
    ) -> None:
        """현재 엔진 소유 수량에 맞춰 서버측 손절(+목표가 OCO)을 건다.
        (기존 조건주문 취소는 호출자가 먼저 한다 — `_cancel_protective_orders`.)

        **왜 서버측인가**: 엔진 폴링은 10초 주기다. 급락은 10초를 기다려주지 않는다.
        조건주문은 거래소/브로커가 감시하므로 우리 프로세스가 죽어 있어도 동작한다 —
        실전 안전의 핵심이다.

        [미검증] IP 화이트리스트로 실호출 검증 불가. 요청 스키마는 openapi.json 기준.

        규칙(openapi.json `ConditionalOrderCreateRequest` 설명):
        - OCO는 first/second 둘 다 SELL이고 `first.trigger > 현재가 > second.trigger`,
          호가유형 LIMIT만. 그래서 first=목표가, second=손절가로 넣는다.
        - 목표가가 없거나 위 부등식을 못 채우면 손절만 SINGLE + MARKET으로 건다
          (손절은 체결 확실성이 가격보다 중요하다).
        - 수량은 정수만 받는다("주 단위"). 분할 보유분은 내림하며, 내림 후 0이면
          조건주문을 걸지 않는다 — 그 경우 엔진 폴링 손절이 유일한 방어선이다.
        """
        if not self._register_protective_stops or stop is None or stop <= 0:
            return
        qty = int(self._ownership.owned_qty(symbol))
        if qty <= 0:
            return

        use_oco = target is not None and target > last_price > stop
        if use_oco:
            group_type, order_type = "OCO", "LIMIT"
            first = {
                "orderSide": "SELL",
                "triggerPrice": self._fmt_price(symbol, target),
                "orderPrice": self._fmt_price(symbol, target),
            }
            second: dict | None = {
                "orderSide": "SELL",
                "triggerPrice": self._fmt_price(symbol, stop),
                "orderPrice": self._fmt_price(symbol, stop),
            }
        else:
            group_type, order_type = "SINGLE", "MARKET"
            first = {"orderSide": "SELL", "triggerPrice": self._fmt_price(symbol, stop)}
            second = None

        try:
            result = self._client.place_conditional_order(
                symbol, group_type,
                quantity=qty, order_type=order_type,
                # 당일 만료: 이 엔진은 오버나이트를 금지하고 마감 전 청산한다. 만료일을
                # 길게 잡으면 청산된 뒤에도 조건주문이 살아 있을 창이 생긴다.
                expire_date=datetime.now(KST).date().isoformat(),
                first=first, second=second,
                client_order_id=uuid.uuid4().hex,
            )
        except (TossAPIError, RuntimeError, ValueError,
                httpx.TimeoutException, httpx.TransportError) as e:
            logger.error(
                "TossBroker 서버측 손절 등록 실패 (symbol=%s stop=%s target=%s qty=%s): %s: %s — "
                "이 포지션은 엔진 폴링 손절에만 의존한다",
                symbol, stop, target, qty, type(e).__name__, e,
            )
            return

        conditional_order_id = (result or {}).get("conditionalOrderId")
        if conditional_order_id:
            self._ownership.set_protection(symbol, [conditional_order_id], stop, target)
        logger.info(
            "서버측 보호주문 등록 (symbol=%s type=%s qty=%s stop=%s target=%s id=%s)",
            symbol, group_type, qty, stop, target, conditional_order_id,
        )

    def positions(self) -> dict[str, Position]:
        """qty/avg_cost는 항상 holdings()가 소스 오브 트루스 — 매 호출 새로 반영한다.
        meta(entry/stop/target/adds_done/scaled_out 등)는 우리 전략이 붙인 부가 상태라
        holdings()엔 없으므로, 로컬 저장소에서 심볼별로 보존해 되돌려준다. 저장소에
        기록이 없는 심볼은 meta={}로 반환한다 — 여기서 값을 지어내지 않는다. 그 경우
        전략의 _ensure_state가 보수적 복구 로직으로 채운다. 브로커가 더 이상 보유하지
        않는다고 보고하는 심볼의 meta는 저장소에서 지운다.
        """
        try:
            data = self._client.holdings()
        except (TossAPIError, RuntimeError) as e:
            logger.warning("TossBroker.positions 조회 실패: %s: %s", type(e).__name__, e)
            return {}

        meta_store = self._get_meta_store()
        result: dict[str, Position] = {}
        seen: set[str] = set()
        for item in data.get("items", []):
            symbol = item["symbol"]
            seen.add(symbol)
            stored = meta_store.positions.get(symbol)
            if stored is None:
                stored = Position(symbol=symbol, qty=0.0, avg_cost=0.0)
                meta_store.positions[symbol] = stored
            result[symbol] = Position(
                symbol=symbol,
                qty=float(item["quantity"]),
                avg_cost=float(item["averagePurchasePrice"]),
                meta=stored.meta,
            )

        gone = set(meta_store.positions) - seen
        for symbol in gone:
            del meta_store.positions[symbol]

        meta_store.save()
        return result

    def cash(self) -> float:
        """기준 통화(KRW) 현금."""
        try:
            data = self._client.buying_power(currency="KRW")
        except (TossAPIError, RuntimeError) as e:
            logger.warning("TossBroker.cash 조회 실패: %s: %s", type(e).__name__, e)
            return 0.0
        return float(data["cashBuyingPower"])

    def cash_usd(self) -> float | None:
        """USD 매수가능금액(원화와 분리된 별도 예수금).

        Broker Protocol(`quant.core.ports.Broker`)에는 없는 부가 메서드다 —
        원화/달러 계좌가 분리돼 있고 자동 환전은 하지 않는다는 사용자 정책
        (2026-08-31) 때문에 US 사이징 경로(quant/trade/risk/manager.py)가
        `getattr`로 duck-typing 조회한다. PaperBroker 등 이 메서드가 없는
        구현체는 그 경로에서 조용히 건너뛰어진다 — 기존 동작을 바꾸지 않는다.

        `cash()`와 달리 조회 실패는 **0.0이 아니라 None**을 돌려준다 — "정말
        예수금이 0달러"와 "네트워크가 흔들려서 모른다"를 구분해야 한다. 0.0으로
        위장하면 일시적 API 오류 한 번에 모든 US 진입이 "USD 자금 부족"으로
        차단된다(실제로는 몰라서 막는 것뿐인데). 호출부(risk/manager.py)는
        None을 "게이트 건너뛰기"로 취급한다 — 모르면 막지 않고, 기존 KRW 기준
        사이징이 안전망으로 남는다. 이 어댑터 예외 삼킴 규칙은 그대로 지킨다
        (raw 예외를 코어로 올리지 않는다) — 실패를 None이라는 값으로 표현할 뿐,
        예외를 전파하지 않는다."""
        try:
            data = self._client.buying_power(currency="USD")
        except (TossAPIError, RuntimeError) as e:
            logger.warning("TossBroker.cash_usd 조회 실패: %s: %s", type(e).__name__, e)
            return None
        return float(data["cashBuyingPower"])

    # ------------------------------------------------------------- 미체결 주문 관리
    def cancel_order(self, order_id: str) -> bool:
        """주문 취소 (POST /api/v1/orders/{orderId}/cancel).

        **비동기다.** 200 응답은 "취소 요청이 접수됐다"는 뜻이지 "취소가 끝났다"는
        뜻이 아니다 — 응답 자체는 취소 오퍼레이션 자체의 새 orderId를 돌려줄 뿐,
        원 주문의 최종 상태(CANCELED)는 다음 폴링/대사가 확인한다
        (docs/api/toss/QUICKREF.md 'Cancel order': "Result: {orderId} — new orderId
        for the cancel operation"). 그래서 이 메서드의 True는 "취소를 접수시켰다"는
        뜻이지 "포지션이 이제 안전하다"는 보장이 아니다.

        이미 체결됐거나(already-filled) 이미 취소된(already-canceled) 주문에 대한
        취소 시도는 409로 실패하지만, 어느 쪽이든 더 이상 미체결 위험이 없으므로
        stale 주문 감시 관점에서는 성공과 동치로 True를 돌려준다.
        """
        if os.environ.get("MODE") != "live":
            logger.warning("TossBroker.cancel_order 거부: MODE!=live (orderId=%s)", order_id)
            return False
        try:
            self._client.cancel_order(order_id)
            return True
        except TossAPIError as e:
            if e.code in _CANCEL_ALREADY_RESOLVED_CODES:
                logger.info(
                    "TossBroker.cancel_order: 이미 종결된 주문이라 취소 불필요 "
                    "(orderId=%s, code=%s)", order_id, e.code,
                )
                return True
            logger.error("TossBroker.cancel_order 실패 (orderId=%s): %s: %s",
                          order_id, type(e).__name__, e)
            return False
        except (RuntimeError, httpx.TimeoutException, httpx.TransportError) as e:
            logger.error("TossBroker.cancel_order 실패 (orderId=%s): %s: %s",
                          order_id, type(e).__name__, e)
            return False

    def open_orders(self) -> list[OpenOrder]:
        """서버 기준 미체결 주문 전체 (GET /api/v1/orders?status=OPEN).

        우리 쪽에 별도 주문 레지스트리가 없다(place_order 폴링 타임아웃 시에도
        order_id는 append-only 저널에만 남고 인메모리/영속 추적 대상이 아니다) —
        그래서 서버가 유일한 소스 오브 트루스다. 조회 실패는 예외를 삼키고 빈
        리스트로 — 이 정보는 대사(reconcile)의 부가 감시용이라 실패가 매매를
        막으면 안 된다."""
        try:
            data = self._client.orders(status="OPEN")
        except (TossAPIError, RuntimeError, httpx.TimeoutException, httpx.TransportError) as e:
            logger.warning("TossBroker.open_orders 조회 실패: %s: %s", type(e).__name__, e)
            return []
        result: list[OpenOrder] = []
        for item in data.get("orders", []):
            try:
                submitted_at = datetime.fromisoformat(item["orderedAt"])
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(
                    "TossBroker.open_orders: orderedAt 파싱 실패, 항목 건너뜀 "
                    "(orderId=%s): %s: %s", item.get("orderId"), type(e).__name__, e,
                )
                continue
            result.append(OpenOrder(
                order_id=item["orderId"],
                symbol=item["symbol"],
                side=Side.BUY if item["side"] == "BUY" else Side.SELL,
                qty=float(item["quantity"]),
                submitted_at=submitted_at,
            ))
        return result
