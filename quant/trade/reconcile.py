"""브로커 대사(reconciliation) — "엔진이 안다고 믿는 것"과 "브로커에 실제로 있는 것"을 맞춘다.

왜 필요한가: 사용자가 같은 계좌에서 손으로도 매매한다. 게다가 주문은 폴링
타임아웃·프로세스 재시작·부분체결로 얼마든지 어긋날 수 있다. 엔진 소유 원장이
실제와 다른 상태에서 새 포지션을 더 쌓으면, 그 다음 청산 계산이 전부 틀린 수량 위에서
돌아간다 — 최악의 경우 사용자 물량을 팔거나, 팔았다고 믿은 포지션을 방치한다.

정책:
- **불일치 = 신규 진입 halt + 알림.** 청산은 절대 막지 않는다(TradingControl.halt의
  기존 의미 그대로 — halt는 ENTER/SCALE_IN만 막는다). 불일치 상황에서 청산까지 막으면
  방어하려던 리스크보다 더 나쁜 상태가 된다.
- **사용자 수동 보유의 변화는 정보성 로그만.** 그건 사용자의 권한이지 이상 징후가 아니다.
- 자동 resume은 하지 않는다. 불일치가 사라져도 halt는 사람이 확인하고 푼다 — 원인을
  모른 채 자동 복구하면 같은 사고가 조용히 반복된다.

이 모듈은 브로커 어댑터를 직접 import하지 않는다. `engine_owned_qty`/`positions`를
노출하는 객체면 무엇이든 받는다(duck-typing) — 그 둘이 없으면 대사 자체를 하지 않는다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 수량 비교 허용 오차(주). 미국 분할주는 소수점 6자리까지라 그 아래는 반올림 잡음이다.
_QTY_TOLERANCE = 1e-6

_DEFAULT_INTERVAL_MINUTES = 5.0

# 미체결 주문 나이 감시 기본값(초) — 실계좌 방어선(2026-08-30). 시장가 위주 운용이라
# 평시엔 open_orders()가 거의 항상 빈 리스트지만, 브로커가 주문을 물고 있는 장애
# 시나리오(폴링 타임아웃 후 서버가 계속 PENDING을 돌려주는 등)에서 이 값을 넘긴
# 주문은 방치하지 않고 자동 취소를 시도한다.
_DEFAULT_STALE_ORDER_SECONDS = 120.0


@dataclass
class ReconcileReport:
    checked: bool = False
    mismatches: list[str] = field(default_factory=list)
    manual_changes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


class Reconciler:
    """기동 시 1회 + N분마다 브로커 실보유를 엔진 소유 원장과 대조한다."""

    def __init__(
        self, broker, control, notifier=None, *,
        interval_minutes: float = _DEFAULT_INTERVAL_MINUTES,
        clock=None,
        pending_qty=None,
        stale_order_seconds: float | None = _DEFAULT_STALE_ORDER_SECONDS,
    ) -> None:
        # `pending_qty(symbol) -> float` — 엔진이 낸 주문 중 아직 안 채워진 수량
        # (Phase 6.5). 없으면 기존 동작 그대로다. duck-typing 은 이 모듈의 관례.
        self._pending_qty = pending_qty
        self._broker = broker
        self._control = control
        self._notifier = notifier
        self._interval_seconds = float(interval_minutes) * 60
        self._clock = clock
        self._last_check_ts: float | None = None
        self._manual_snapshot: dict[str, float] = {}
        # 같은 불일치가 사이클마다 반복되므로 알림은 최초 1회만 보낸다(로그는 매번 남긴다).
        self._mismatch_notified = False
        # None/<=0 이면 stale 주문 감시 자체를 끈다.
        self._stale_order_seconds = stale_order_seconds

    @property
    def supported(self) -> bool:
        """엔진 소유 원장을 노출하는 브로커에서만 대사가 의미가 있다.
        PaperBroker는 portfolio.json이 곧 엔진 소유이므로 대사 대상이 아니다."""
        return callable(getattr(self._broker, "engine_owned_qty", None)) and callable(
            getattr(self._broker, "engine_owned_symbols", None)
        )

    def _now(self) -> float:
        """주기 판정용 초 단위 시각. clock을 주면 그 시계를 쓴다(테스트/리플레이),
        없으면 벽시계와 무관한 monotonic — 시스템 시간이 튀어도 주기가 깨지지 않는다."""
        if self._clock is not None:
            return self._clock.now().timestamp()
        return time.monotonic()

    def check(self, force: bool = False) -> ReconcileReport:
        """대조 1회. 주기가 안 됐으면 아무것도 하지 않고 checked=False를 돌려준다."""
        report = ReconcileReport()
        if not self.supported:
            return report
        now = self._now()
        if not force and self._last_check_ts is not None:
            if now - self._last_check_ts < self._interval_seconds:
                return report
        self._last_check_ts = now
        report.checked = True

        self._check_stale_orders()

        try:
            broker_positions = self._broker.positions()
        except Exception as e:  # noqa: BLE001 — 어댑터가 삼키지 못한 예외까지 방어
            logger.warning("대사 실패 — 브로커 보유 조회 불가: %s: %s", type(e).__name__, e)
            return report

        broker_qty = {sym: pos.qty for sym, pos in broker_positions.items()}
        engine_symbols = set(self._broker.engine_owned_symbols())

        for symbol in sorted(engine_symbols):
            owned = self._broker.engine_owned_qty(symbol)
            actual = broker_qty.get(symbol, 0.0)
            if actual <= _QTY_TOLERANCE:
                report.mismatches.append(
                    f"{symbol}: 엔진 원장 {owned:g}주인데 브로커 보유 없음"
                )
            elif actual + _QTY_TOLERANCE < owned:
                # 브로커 보유가 원장보다 **적을** 때만 불일치다. 많은 쪽은 사용자가
                # 같은 종목을 손으로 추가 매수한 정상 상황일 수 있다(아래 수동 보유
                # 로그에서 다룬다) — 그걸 halt 사유로 삼으면 오탐이 잦아진다.
                report.mismatches.append(
                    f"{symbol}: 엔진 원장 {owned:g}주 > 브로커 보유 {actual:g}주"
                )

        # 사용자 수동 보유 = 브로커에는 있는데 엔진 원장에 없는 물량. 정보성 로그만.
        #
        # **단, 그 잉여가 우리 미체결 주문으로 설명되면 정보성이 아니다**(Phase 6.5).
        # 폴링 타임아웃으로 결론을 못 본 주문이 뒤늦게 체결되면 브로커 보유는 늘지만
        # 엔진 원장에는 그 체결이 없다 — 엔진이 **자기 포지션을 모르는** 상태이고,
        # 청산 로직이 그 물량을 영원히 방치한다. 사용자가 손으로 산 물량과는 완전히
        # 다른 사건이라 같은 통에 넣으면 안 된다.
        manual: dict[str, float] = {}
        for symbol, qty in broker_qty.items():
            surplus = qty - self._broker.engine_owned_qty(symbol)
            if surplus <= _QTY_TOLERANCE:
                continue
            pending = self._pending_for(symbol)
            if pending > _QTY_TOLERANCE:
                report.mismatches.append(
                    f"{symbol}: 브로커 보유가 원장보다 {surplus:g}주 많고 엔진 미체결 "
                    f"잔량 {pending:g}주가 있다 — 우리 주문이 뒤늦게 체결된 것으로 보인다"
                    " (엔진이 이 포지션을 모른다)"
                )
                continue
            manual[symbol] = surplus
        for symbol in sorted(set(manual) | set(self._manual_snapshot)):
            before = self._manual_snapshot.get(symbol, 0.0)
            after = manual.get(symbol, 0.0)
            if abs(after - before) > _QTY_TOLERANCE:
                report.manual_changes.append(f"{symbol}: {before:g} → {after:g}")
        if report.manual_changes:
            logger.info(
                "사용자 수동 보유 변화(정보성 — 엔진 주문 대상 아님): %s",
                ", ".join(report.manual_changes),
            )
        self._manual_snapshot = manual

        if report.mismatches:
            self._on_mismatch(report)
        return report

    def _pending_for(self, symbol: str) -> float:
        """미체결 잔량. 조회 실패는 0으로 — 부가 정보가 대사 자체를 죽이면 안 된다."""
        if self._pending_qty is None:
            return 0.0
        try:
            return float(self._pending_qty(symbol) or 0.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("미체결 잔량 조회 실패(%s) — 0으로 본다: %s", symbol, e)
            return 0.0

    def _check_stale_orders(self) -> None:
        """미체결 주문 나이 감시 (2026-08-30, 실계좌 방어선).

        `Broker.open_orders()`/`cancel_order()`를 노출하는 브로커에서만 동작한다
        (duck-typing — 이 모듈의 관례, `supported`와 별개 판정이다: PaperBroker도
        이제 이 둘을 구현하지만 항상 빈 리스트/False라 여기선 사실상 no-op다).
        `stale_order_seconds`가 None/<=0이면 기능 자체를 끈다.

        평시엔 시장가 위주 운용이라 open_orders()가 거의 항상 비어 있다 — 이
        감시의 목적은 정상 경로가 아니라 브로커가 주문을 계속 물고 있는 장애
        시나리오(폴링 타임아웃 이후에도 서버가 결론을 못 내는 경우)의 방어선이다.
        """
        if not self._stale_order_seconds or self._stale_order_seconds <= 0:
            return
        open_orders_fn = getattr(self._broker, "open_orders", None)
        cancel_fn = getattr(self._broker, "cancel_order", None)
        if not callable(open_orders_fn) or not callable(cancel_fn):
            return
        try:
            orders = open_orders_fn()
        except Exception as e:  # noqa: BLE001 — 감시 실패가 대사 자체를 죽이면 안 된다
            logger.warning("미체결 주문 조회 실패 — stale 감시 스킵: %s: %s", type(e).__name__, e)
            return
        now = self._clock.now() if self._clock is not None else datetime.now(timezone.utc)
        for o in orders:
            try:
                age = (now - o.submitted_at).total_seconds()
            except TypeError:
                # tz-naive 등 비교 불가한 값 — 값을 지어내지 않고 건너뛴다.
                logger.warning("미체결 주문 나이 계산 불가 — 건너뜀 (orderId=%s)", o.order_id)
                continue
            if age < self._stale_order_seconds:
                continue
            logger.warning(
                "미체결 주문 나이 초과(%.0f초 ≥ %.0f초) — 자동 취소 시도 "
                "(orderId=%s symbol=%s side=%s qty=%s)",
                age, self._stale_order_seconds, o.order_id, o.symbol, o.side.value, o.qty,
            )
            try:
                canceled = cancel_fn(o.order_id)
            except Exception as e:  # noqa: BLE001 — 취소 실패가 감시 루프를 죽이면 안 된다
                logger.error("stale 주문 취소 중 예외 (orderId=%s): %s: %s",
                             o.order_id, type(e).__name__, e)
                canceled = False
            if self._notifier is None:
                continue
            status = "취소 요청 접수" if canceled else "취소 실패 — 토스 앱에서 직접 확인할 것"
            self._notifier.send(
                f"⚠️ 미체결 주문이 {age:.0f}초째 남아 있어 자동 취소를 시도했습니다 ({status}).\n"
                f"{o.symbol} {o.side.value} {o.qty:g}주 (orderId={o.order_id})"
            )

    def _on_mismatch(self, report: ReconcileReport) -> None:
        detail = "; ".join(report.mismatches)
        reason = f"브로커 대사 불일치 — 신규 진입 중단 ({detail})"
        logger.error(reason)
        if not self._control.is_halted():
            self._control.halt(reason, by="auto")
        if self._notifier is not None and not self._mismatch_notified:
            self._mismatch_notified = True
            self._notifier.send(
                "브로커 대사 불일치 — 신규 진입을 중단했다(청산은 계속 동작한다).\n"
                f"{detail}\n원인 확인 후 /resume 할 것."
            )


class OpenOrderBook:
    """엔진이 낸 주문 중 **아직 안 채워진 수량**을 종목별로 들고 있다 (Phase 6.5).

    `core.ports.OrderSink` 구현체다 — 루프가 주문 상태를 낼 때마다 갱신된다.

    왜 필요한가: 대사가 "브로커에 있는데 원장에 없는 물량"을 볼 때, 그게 **사용자가
    손으로 산 것**인지 **우리 주문이 뒤늦게 체결된 것**인지 가를 정보가 지금은 없다.
    후자는 엔진이 자기 포지션을 모르는 상태라 청산 로직이 그 물량을 방치한다.

    영속화하지 않는다. 재시작하면 비고, 그러면 대사는 잉여를 "수동 보유"로 본다 —
    **기존 동작으로 안전하게 떨어지는 쪽**이다(없는 잔량을 지어내지 않는다).
    """

    def __init__(self, ttl_minutes: float = 60.0, clock=None) -> None:
        # 주문 단위로 들고 있다가 합산한다. 같은 주문의 갱신이 누적되면 잔량이 두 배로
        # 세어지므로 **주문 키로 덮어쓴다**.
        self._by_order: dict[tuple, tuple[float, float]] = {}   # key -> (잔량, 기록시각)
        # **TTL 이 없으면 이 장부는 영구 halt 생성기가 된다.**
        # 루프는 주문을 낼 때 상태를 한 번만 준다 — 폴링 타임아웃으로 열린 채 남은
        # 주문은 그 뒤 갱신이 오지 않으므로 잔량이 영원히 남고, 대사가 매번 불일치로
        # 읽어 신규 진입을 계속 막는다. 60분인 이유: 이 장부의 쓸모는 "우리 주문이
        # **방금** 늦게 체결된 것 같다"를 설명하는 것이고, 그건 보통 몇 분 안에 끝난다.
        self._ttl_seconds = float(ttl_minutes) * 60
        self._clock = clock

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock.now().timestamp()
        return time.monotonic()

    def _prune(self) -> None:
        cutoff = self._now() - self._ttl_seconds
        for key in [k for k, (_, ts) in self._by_order.items() if ts < cutoff]:
            del self._by_order[key]

    # sink 체인 안에 놓이므로 나머지 이벤트는 조용히 흘려보낸다 — 주문만 본다.
    def on_signal(self, signal) -> None: ...

    def on_fill(self, fill) -> None: ...

    def on_order(self, state) -> None:
        key = (state.broker_order_id, state.order.symbol, state.order.strategy_id)
        # 브로커에 닿지 못한 주문은 서버에 남아 있을 수 없다 — 잔량이 0이다.
        if state.broker_order_id is None or state.is_terminal:
            self._by_order.pop(key, None)
            return
        remaining = max(state.remaining_qty, 0.0)
        if remaining <= _QTY_TOLERANCE:
            self._by_order.pop(key, None)
        else:
            self._by_order[key] = (remaining, self._now())
        self._prune()

    def pending_qty(self, symbol: str) -> float:
        self._prune()
        return sum(q for (_, sym, _), (q, _ts) in self._by_order.items() if sym == symbol)
