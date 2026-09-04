"""핵심 파이프라인: strategy.on_cycle -> risk.approve -> broker.place_order -> sinks.on_fill.

run_cycle은 순수 함수다 — 네트워크 호출도 sleep도 없다. 백테스트와 paper 양쪽이
동일한 이 함수를 공유한다(ADR-4). market data는 각 전략이 ctx.data에서 직접 읽는다.
run_paper_loop는 poll_seconds 간격으로 run_cycle을 반복하고 settings.yaml을 핫 리로드하는
얇은 async 래퍼다. 여기에 더해 사이클 예외 격리/자동 정지 에스컬레이션, 하트비트,
데이터 스테일 가드, 수동 킬 스위치(TradingControl) 소비, 신규 진입 승인 게이트
(ApprovalGate) 처리, 하트비트 상태 파일(외부 워치독용)과 세션 마감 요약을 담당한다.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
import logging
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pandas as pd

from quant.trade.approval import STATUS_REJECTED, ApprovalGate, ApprovalRequest
from quant.core import oms
from quant.core import strategy_ids
from quant.trade.control import TradingControl
from quant.core.ports import (
    ColdFetchBudgetExceeded, Context, EventSink, Notifier, OrderSink, RiskManager,
    Strategy, TickLogger,
)
from quant.core.models import (
    Fill, Order, OrderState, Quote, Side, Signal, SignalAction, market_of_symbol,
)
from quant.core.portfolio.portfolio import to_krw
from quant.core.session import in_continuous_session
from quant.trade.risk.books import StrategyBooks
from quant.trade.risk.manager import MARKET_CLOSED_MARKER
from quant.trade.watch_conditions import Rule as WatchRule
from quant.trade.watch_conditions import apply_cooldown as apply_watch_cooldown
from quant.trade.watch_conditions import evaluate as evaluate_watch_conditions
from quant.trade.watch_conditions import parse_rules as parse_watch_rules

class EngineSettings(Protocol):
    """루프가 설정에게 실제로 요구하는 것 전부 — 부채 상환(2026-08-24)으로
    `apps.config.Settings` 임포트를 이 구조적 계약으로 바꿨다(거래 평면이
    조립 평면을 알면 의존 방향이 뒤집힌다, tests/test_architecture.py 의
    KNOWN_DEBT 였던 엣지). `apps.config.Settings` 는 이 Protocol 을 코드
    변경 없이 구조적으로 만족한다."""

    raw: dict
    poll_seconds: float

    def reload_if_changed(self) -> bool: ...


logger = logging.getLogger(__name__)

_ENTRY_ACTIONS = (SignalAction.ENTER_LONG, SignalAction.SCALE_IN)

_DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
_DEFAULT_CYCLE_FAILURE_COOLDOWN_MINUTES = 10
_DEFAULT_MAX_CONSECUTIVE_STALE = 3
_DEFAULT_HEARTBEAT_MINUTES = 5
_DEFAULT_SLOW_CYCLE_WARN_MS = 1000
_DEFAULT_APPROVAL_EXPIRE_SECONDS = 300
_DEFAULT_MAX_PRICE_DRIFT_PCT = 0.5
_DEFAULT_PRUNE_AFTER_SECONDS = 86400
_CYCLE_LATENCY_HISTORY_SIZE = 20  # 하트비트 "최근 N회 최대" 계산에 쓰는 사이클 지연 이력 길이
# 봉 캐시 적중률 INFO 로그 주기(초). 캐시가 실제로 먹히는지 운영 로그에서 확인할 수
# 없으면 "넣었으니 되겠지"가 된다 — 히트율이 떨어지면 곧바로 rate limit으로 돌아온다.
_BAR_CACHE_LOG_INTERVAL_SEC = 300
# 워치 조건 rsi2/rsi14/change_pct 계산용 일봉 개수 — rsi14 워밍업(15개) + 여유.
_WATCH_DAILY_BAR_COUNT = 30
# 전략 간 합산 노출 경고(2026-08-30) 재발송 최소 간격 — 평시엔 로그만, 임계
# 초과/상쇄 쌍 존재가 계속돼도 이 간격보다 자주 텔레그램을 보내지 않는다.
_DEFAULT_EXPOSURE_ALERT_COOLDOWN_MINUTES = 60

DEFAULT_HEARTBEAT_PATH = Path("data/state/heartbeat.json")

# 심볼 → 표시명. run_paper_loop이 조립 단계에서 받은 이름표를 한 번 채운다.
# 텔레그램 메시지에서 "088350" 대신 "한화생명 (088350)"으로 보이게 하는 용도 —
# 표시 전용이라 거래 판단에는 어디서도 쓰이지 않는다.
_SYMBOL_NAMES: dict[str, str] = {}

_MARKET_LABELS = {"KR": "한국 시장", "US": "미국 시장"}

# 전략 표시명 — 어떤 신호로 들어갔는지가 보여야 전략별 성과를 사용자가 판단한다.
_STRATEGY_LABELS = {
    "donchian": "돈치안 채널 돌파",
    "orb_scan": "개장 5분 돌파",
    "intraday_scan": "장중 신고가 돌파",
    "orb": "개장 범위 돌파(논문판)",
    "news_momentum": "뉴스 개장 단타",
    "scalp_1m": "1분봉 스캘핑",
    "close_bet": "종가배팅",
    "frgn_accumulate": "외국인 추세 추종",
    "pullback_impulse": "눌림목 임펄스",
    "mr_vwap_quiet": "저거래 VWAP 회귀",
    "overnight_drift": "오버나이트 드리프트",
    # 대회 확장 2차(2026-08-29) — 소유자 "이름 항상 등록" 원칙은 종목만이 아니라
    # 전략 표시명도 같다: id 원문만 보이면 초보 유저가 판단할 수 없다.
    "vol_breakout": "변동성 돌파",
    "intraday_momentum": "일중 모멘텀(양방향)",
    "gap_fade": "갭하락 되돌림",
    "rsi2_dip": "RSI2 눌림 매수",
    "llm_trader": "LLM 트레이더(실험)",
    # 문헌 기반 일중 3종(2026-09-03) — 등록만, 번인 전까지 비활성.
    "orb_rvol": "개장 돌파(거래량 선별)",
    "eod_reversal": "장막판 역추세",
    "open_reversal": "전일 패자 개장 매수",
}


# A/B 촉매 갈래 접미사(2026-09-03) — `<id>_cat` 은 `<id>` 와 **같은 클래스**를
# 다른 유니버스로 돌리는 갈래다(config/settings.yaml `universe_filter`).
# 그래서 표시명·오버나이트 판정 같은 클래스 단위 처리는 전부 상속돼야 한다.
# 벗기는 규칙 자체는 `quant.core.strategy_ids`(의존 방향의 바닥) 단일 정의를
# 쓴다 — `quant/trade/`·`quant/control/`이 각자 베껴 쓰다 `ledger`쪽이 `_pure`를
# 벗기지 않는 채로 갈라졌던 버그(2026-09-03)가 재발하지 않게.
_CATALYST_ARM_SUFFIX = strategy_ids.CATALYST_ARM_SUFFIX
_base_strategy_id = strategy_ids.base_strategy_id


def _strategy_label(strategy_id: str | None) -> str:
    if not strategy_id:
        return "전략 미상"
    label = _STRATEGY_LABELS.get(strategy_id)
    if label is None:
        base = _base_strategy_id(strategy_id)
        # 촉매 갈래는 기준 전략의 표시명 + 꼬리표. id 원문만 보이면 소유자가
        # 텔레그램에서 "이건 또 뭐지"가 된다(표시명 등록 원칙, 2026-08-29).
        if base != strategy_id and base in _STRATEGY_LABELS:
            suffix = "촉매" if strategy_id.endswith(_CATALYST_ARM_SUFFIX) else "순수"
            label = f"{_STRATEGY_LABELS[base]}·{suffix}"
    return f"{label or strategy_id} ({strategy_id})"


def _is_kr(symbol: str) -> bool:
    return symbol.isdigit() and len(symbol) == 6


def _display(symbol: str) -> str:
    """'한화생명 (088350)' — 이름을 모르면 코드만."""
    name = _SYMBOL_NAMES.get(symbol)
    return f"{name} ({symbol})" if name else symbol


def _price(value: float, symbol: str) -> str:
    return f"{value:,.0f}원" if _is_kr(symbol) else f"${value:,.2f}"


def _amount(value: float, symbol: str) -> str:
    return f"{value:,.0f}원" if _is_kr(symbol) else f"${value:,.2f}"


def _signed_amount(value: float, symbol: str) -> str:
    """손익 표시 — 부호를 항상 붙인다(+15,918원 / -3,240원)."""
    return f"{value:+,.0f}원" if _is_kr(symbol) else f"{value:+,.2f}달러"


def _signed_amount_market(value: float, market: str) -> str:
    """_signed_amount와 같은 포맷이지만 심볼이 아니라 시장(KR/US) 기준 — 세션 요약은
    전략별 순손익을 여러 심볼에 걸쳐 합산하므로 심볼 하나로 통화를 판정할 수 없다."""
    return f"{value:+,.0f}원" if market == "KR" else f"{value:+,.2f}달러"


@dataclass
class CycleTimings:
    """사이클 1회의 단계별 소요시간(ms) 스냅샷 — run_cycle이 채우고 run_paper_loop가
    느린 사이클 경고/하트비트에 쓴다(risk.manager.RiskManagerImpl.breaker_state()와 같은
    "구조화된 값 노출" 패턴). strategy_ms는 전략 id별 on_cycle 소요시간, 나머지는 사이클
    안의 모든 시그널에 걸친 누적치다. 채우는 비용은 perf_counter 호출 몇 번뿐이고 문자열
    포매팅은 하지 않는다 — 그건 경고/하트비트 쪽에서 임계 초과 시에만 한다."""

    strategy_ms: dict[str, float] = field(default_factory=dict)
    # on_cycle이 예외로 죽은 전략 → 사유. **이게 비어 있지 않다는 것은 그 전략의
    # 손절·목표가·EoD청산 판정이 그 사이클에 아예 일어나지 않았다는 뜻이다** —
    # 포지션 관리가 전부 on_cycle 안에 있기 때문이다. 예전에는 WARNING 로그만 남고
    # 사이클은 "성공"으로 집계돼, 청산이 멈춘 상태를 시스템이 정상이라고 보고했다
    # (2026-08-12 감사에서 실행으로 확인).
    strategy_errors: dict[str, str] = field(default_factory=dict)
    risk_approve_ms: float = 0.0
    broker_place_order_ms: float = 0.0
    sinks_ms: float = 0.0
    total_ms: float = 0.0


class _SessionTallySink:
    """세션 마감 요약용 체결 집계기 — run_paper_loop이 기존 sinks를 감싼다.

    JsonlSink를 되읽어 세는 방법은 쓰지 않는다: 로그 파일은 재시작·로테이션·수동
    편집에 노출돼 있어 "그 세션의 체결 수"를 되묻는 순간 답이 흔들린다. 메모리
    카운터는 프로세스가 살아 있는 동안 정확하고, 죽으면 값이 사라진다는 사실 자체가
    솔직하다.

    수수료는 Fill.fee(표시 통화)를 종목 시장 기준으로 KRW 환산해 누적한다 — "수수료가
    엣지를 먹는가"를 매일 한 줄로 보이게 하는 것이 목적이라 통화가 섞이면 안 된다.
    집계를 내부 sink 호출보다 **먼저** 한다: 로그 쓰기가 실패해도 체결은 일어난
    것이고, 요약에서 빠지면 안 된다.

    전략별 라운드트립 성적(`strategy_stats`)도 여기서 집계한다. `quant/control/ledger.py`의
    `round_trips()`가 같은 판정(같은 (전략,종목) 누적 수량이 0으로 돌아오면 트립 종결)을
    이미 하지만, `quant/trade/`는 `quant/control/`을 임포트할 수 없다(FORBIDDEN,
    tests/test_architecture.py) — "장중에 MySQL이 딸꾹질해도 매매가 멈추면 안 된다"는
    평면 경계다. 그래서 원장을 다시 읽지 않고, 이미 지나가는 fill 스트림에서 같은
    판정을 실시간으로 재현한다. 트립 진행 상태(`_trip_*`)는 세션 경계를 넘어 살아
    있어야 오버나이트 전략(진입 세션과 청산 세션이 다르다)의 트립이 청산 세션에
    올바르게 잡힌다 — `reset()`은 `strategy_stats`(그 세션에 종결된 트립)만 비운다."""

    def __init__(self, inner: EventSink, market_of: dict[str, str], fx: object | None):
        self._inner = inner
        self._market_of = market_of
        self._fx = fx
        self.fills = 0
        self.fee_krw = 0.0
        self.notional_krw = 0.0
        # 배관 점검(세션 마감 요약)용 카운터 — run_paper_loop이 매 사이클 갱신한다.
        self.cycles = 0
        self.error_cycles = 0
        self.halted_seen = False
        self.degraded_seen = False
        self.strategy_stats: dict[str, dict] = {}
        self._trip_qty: dict[tuple[str, str], float] = {}
        self._trip_pnl: dict[tuple[str, str], float] = {}
        self._trip_fee: dict[tuple[str, str], float] = {}
        self._trip_unknown: dict[tuple[str, str], bool] = {}

    def on_signal(self, signal: Signal) -> None:
        self._inner.on_signal(signal)

    def on_fill(self, fill: Fill) -> None:
        self.fills += 1
        market = self._market_of.get(fill.symbol) or market_of_symbol(fill.symbol)
        try:
            self.fee_krw += to_krw(fill.fee, market, self._fx)
            self.notional_krw += to_krw(fill.qty * fill.price, market, self._fx)
        except Exception:
            # 요약용 부기가 체결 처리를 막으면 안 된다(리포팅이 거래를 죽이는 경로 금지).
            # 환산에 실패하면 합계를 통화 혼합으로 오염시키느니 그 건을 빼고 경고한다.
            logger.warning("수수료/거래대금 KRW 환산 실패 — 세션 합계에서 제외: %s", fill.symbol)
        self._tally_strategy(fill)
        self._inner.on_fill(fill)

    def _tally_strategy(self, fill: Fill) -> None:
        """전략별 라운드트립 완주 판정 — 클래스 docstring 참고."""
        sid = fill.strategy_id or "?"
        key = (sid, fill.symbol)
        side = str(getattr(fill.side, "value", fill.side)).upper()
        signed = fill.qty if side == "BUY" else -fill.qty
        self._trip_qty[key] = self._trip_qty.get(key, 0.0) + signed
        self._trip_fee[key] = self._trip_fee.get(key, 0.0) + (fill.fee or 0.0)
        if side != "BUY":
            if fill.realized_pnl is None:
                self._trip_unknown[key] = True
            else:
                self._trip_pnl[key] = self._trip_pnl.get(key, 0.0) + fill.realized_pnl
        if abs(self._trip_qty.get(key, 0.0)) >= 1e-9:
            return
        # 순수량이 0으로 돌아왔다 — 트립 종결. 진행 상태를 비우고 결과를 반영한다.
        gross = self._trip_pnl.pop(key, 0.0)
        fees = self._trip_fee.pop(key, 0.0)
        unknown = self._trip_unknown.pop(key, False)
        self._trip_qty.pop(key, None)
        stats = self.strategy_stats.setdefault(
            sid, {"trips": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "unknown": 0}
        )
        if unknown:
            # 매도 체결의 realized_pnl을 브로커가 모른다 — 0으로 위장하지 않고 뺀다.
            stats["unknown"] += 1
            return
        net = gross - fees
        stats["trips"] += 1
        stats["wins" if net > 0 else "losses"] += 1
        stats["net_pnl"] += net

    def on_order(self, state) -> None:
        """주문 생애를 그대로 흘려보낸다.

        **이 메서드가 없으면 주문 원장이 조용히 비어 있게 된다.** 루프는
        `isinstance(sinks, OrderSink)` 로 물어보는데, 세션 집계 래퍼가 가장 바깥이라
        여기 없으면 판정이 False 가 되고 안쪽 TradeLedgerSink 까지 닿지 못한다 —
        배선한 줄 알았는데 아무 일도 안 하는, 이 저장소가 반복해서 다친 그 모양이다.
        """
        inner = getattr(self._inner, "on_order", None)
        if inner is not None:
            inner(state)

    def reset(self) -> None:
        self.fills = 0
        self.fee_krw = 0.0
        self.notional_krw = 0.0
        self.cycles = 0
        self.error_cycles = 0
        self.halted_seen = False
        self.degraded_seen = False
        # _trip_* 는 비우지 않는다 — 세션을 넘는 진행 중 트립(오버나이트) 추적이다.
        self.strategy_stats = {}


def run_cycle(
    strategies: list[Strategy],
    ctx: Context,
    risk: RiskManager,
    sinks: EventSink,
    notifier: Notifier | None = None,
    control: TradingControl | None = None,
    timings: CycleTimings | None = None,
    approval: ApprovalGate | None = None,
    approval_notifier: object | None = None,
    approval_cfg: dict | None = None,
    risk_multiplier: float = 1.0,
    marks: dict[str, float] | None = None,
    mult_by_market: dict[str, float] | None = None,
    books: StrategyBooks | None = None,
) -> None:
    """approval이 None이면 승인 게이트는 존재하지 않는 것과 동일하게 동작한다 —
    백테스트/기존 호출부는 이 인자를 넘기지 않으므로 경로가 전혀 바뀌지 않는다.

    risk_multiplier는 그날의 국면(regime) 배수 — run_paper_loop가 RegimeProvider의
    캐시값(네트워크 없음)을 사이클마다 읽어 넘긴다. 백테스트는 넘기지 않으므로
    1.0(중립) — 과거 국면을 리플레이하려면 별도 작업이 필요하다(ADR-0009).

    marks는 보유 종목의 사이클 시세(run_paper_loop가 quote 캐시로 채운다) — risk.approve의
    daily_loss_limit_pct 평가금액(MTM) 계산에 쓰인다. 백테스트는 넘기지 않으므로 None
    (RiskManagerImpl은 None이면 기존처럼 평균단가로 근사한다)."""
    cycle_t0 = time.perf_counter()
    for strategy in strategies:
        try:
            strat_t0 = time.perf_counter()
            signals = strategy.on_cycle(ctx)
            if timings is not None:
                timings.strategy_ms[getattr(strategy, "id", "?")] = (time.perf_counter() - strat_t0) * 1000
        except ColdFetchBudgetExceeded as e:
            # 예산 스로틀은 장애가 아니다 — strategy_errors 에 넣지 않는다.
            # 넣으면 "포지션 있는 전략의 오류 3연속 → 자동 정지"가 오발한다
            # (2026-08-31 실사고: 분 경계 연속 스킵으로 KR 하루 무체결).
            # 다음 사이클에 예산이 리셋되며 자가 회복한다(ports.py 클래스 docstring).
            sid = getattr(strategy, "id", "?")
            logger.info("전략 %s 사이클 스킵(예산): %s", sid, e)
            continue
        except Exception as e:
            sid = getattr(strategy, "id", "?")
            logger.warning("전략 %s 사이클 스킵: %s", sid, e)
            if timings is not None:
                timings.strategy_errors[sid] = f"{type(e).__name__}: {e}"
            continue

        for signal in signals:
            sink_t0 = time.perf_counter()
            sinks.on_signal(signal)
            if timings is not None:
                timings.sinks_ms += (time.perf_counter() - sink_t0) * 1000
            if signal.action in _ENTRY_ACTIONS and control is not None and control.is_halted():
                # halt는 신규 진입(ENTER_LONG/SCALE_IN)만 막는다 — EXIT/SCALE_OUT은
                # 여기 걸리지 않고 그대로 _execute_signal로 흘러간다. 신규 주문이
                # 실제로 승인/집행되는 지점 바로 앞에서 막아야 시그널 생성 경로가
                # 달라도(전략마다 다른 방식으로 시그널을 내도) 우회할 수 없다.
                logger.info(
                    "거래 중단 상태 — 신규 진입 스킵 %s %s (%s)",
                    signal.strategy_id, signal.symbol, control.halt_reason(),
                )
                continue
            _execute_signal(
                signal, ctx, risk, sinks, notifier, timings,
                approval, approval_notifier, approval_cfg, risk_multiplier, marks,
                mult_by_market, books,
            )
    if timings is not None:
        timings.total_ms = (time.perf_counter() - cycle_t0) * 1000


def _entry_budget_text(orders: dict) -> str:
    """진입 예산을 **시장별로** 보여준다.

    한 줄로 합쳐 보여주던 시절, 밤사이 US 세션이 쓴 주문이 KR 아침 예산을 먹은 것을
    사람이 알 방법이 없었다 — "오늘 주문 135건 / 상한 30건 — 한도 도달"만 보였다
    (2026-08-14). 어느 시장이 소진했는지가 보여야 조치를 정할 수 있다.

    총 주문 수(청산 포함)도 함께 보인다. 진입 수와 크게 벌어지면 체결되지 않는 주문이
    반복 승인되고 있다는 뜻이다(그날 실측: 체결 29건인데 승인 135건).
    """
    limit = orders.get("limit", 0)
    by_market = orders.get("by_market") or {}
    # limit 0 = 상한 해제(2026-08-14). "3/0" 으로 찍으면 "한도 0"으로 읽힌다 —
    # 해제해 놓고 한도에 걸린 것처럼 보이는 게 원래 문제였다.
    cap = f"/{limit}" if limit else ""
    if not by_market:
        return "0건" + (f" / 상한 {limit}건 (시장별)" if limit else " (상한 없음)")
    parts = []
    for market in sorted(by_market):
        m = by_market[market]
        mark = " ⛔" if m.get("tripped") else ""
        parts.append(f"{market} {m.get('entries', 0)}{cap}{mark}")
    total_orders = sum(m.get("orders", 0) for m in by_market.values())
    return " · ".join(parts) + f" (총 주문 {total_orders}건)"


def _normalize_order_result(order: Order, result, ctx: Context):
    """브로커 반환값을 `(OrderState|None, Fill|None)` 로 정규화한다.

    Phase 6.3 에서 `place_order` 의 계약이 `Fill | None` → `OrderState` 로 바뀌었다.
    **전환 중에 사이클이 죽으면 안 되므로** 옛 형태(Fill 또는 None)도 그대로 받아준다 —
    외부 테스트 페이크나 아직 안 고친 구현이 있을 수 있다.
    """
    if result is None:
        return None, None
    if isinstance(result, OrderState):
        return result, result.fill
    if isinstance(result, Fill):
        try:
            return oms.filled_from(order, result, at=_safe_now(ctx)), result
        except Exception:  # noqa: BLE001 — 부기가 매매를 죽이지 않는다
            return None, result
    logger.warning("place_order 가 알 수 없는 타입을 돌려줬다: %s", type(result).__name__)
    return None, None


def _emit_order_state(state, sinks: EventSink) -> None:
    """"시킨 것"과 "일어난 일"의 차이를 싱크로 흘려보낸다 (Phase 6.4).

    **부분 체결 정보는 이미 있다** — `fill.qty` 와 `order.qty` 를 비교하면 나온다.
    아무도 비교하지 않았을 뿐이라, Broker Protocol 을 바꾸지 않고도(그건 17곳을
    동시에 건드린다) 못 받은 잔량을 기록으로 남길 수 있다.

    한계를 분명히 해둔다: 여기서 만드는 상태는 **체결로부터 역산한 것**이라
    "거부됐다"와 "주문이 안 나갔다"를 구분하지 못한다(둘 다 fill=None → ACCEPTED
    로 열린 채 남는다). 그 구분은 브로커가 상태를 돌려줘야 가능하고 그게 6.3 이다.

    부기가 매매를 죽이면 안 된다 — 여기서 나는 모든 예외를 삼킨다. 상태기계는
    잘못된 전이에 예외를 던지지만(장부가 틀렸다는 신호), 그 시끄러움은 순수
    계층에서만 유효하고 핫패스는 계속 돌아야 한다.
    """
    if state is None or not isinstance(sinks, OrderSink):
        return  # on_order 가 없는 싱크 — 기존 구현체·페이크가 전부 이 모양이다
    try:
        sinks.on_order(state)
    except Exception as e:  # noqa: BLE001
        logger.warning("주문 생애 기록 실패 %s: %s: %s",
                       state.order.symbol, type(e).__name__, e)


def _execute_signal(
    signal: Signal,
    ctx: Context,
    risk: RiskManager,
    sinks: EventSink,
    notifier: Notifier | None,
    timings: CycleTimings | None = None,
    approval: ApprovalGate | None = None,
    approval_notifier: object | None = None,
    approval_cfg: dict | None = None,
    risk_multiplier: float = 1.0,
    marks: dict[str, float] | None = None,
    mult_by_market: dict[str, float] | None = None,
    books: StrategyBooks | None = None,
) -> None:
    """승인 → 주문 → 체결 반영. 일반 시그널과 수동 청산(flatten) 시그널이 공유한다.

    approval이 붙어 있으면 **신규 진입만** 집행 직전에 사용자 승인 요청으로
    돌린다. 청산(EXIT_LONG/SCALE_OUT)은 어떤 경우에도 여기서 막히지 않는다 —
    _flatten_all이 만드는 kill-switch 청산도 approval=None으로 이 함수를 부른다.

    books(전략별 독립 명목계정, capital_mode: per_strategy)가 주입돼 있으면 체결이
    확정될 때만(fill is not None) 그 전략의 장부를 갱신하고 저장한다 — 체결이
    없으면(거부/미체결) 장부를 건드리지 않는다. `capital_mode: shared`(기본)에서는
    apps/assembly.py가 애초에 books를 만들지 않고 None을 넘긴다 — 이 기능은 꺼져
    있으면 흔적이 전혀 남지 않는다(장부 파일도 생기지 않는다)."""
    if mult_by_market:
        # 시장별 국면 배수(2026-08-10): KR 신호는 KR 국면으로 사이징 — 심볼에서
        # 시장을 추론(6자리=KR, 저장소 전역 규약). 없으면 기존 단일 배수 유지.
        _mkt = "KR" if (signal.symbol.isdigit() and len(signal.symbol) == 6) else "US"
        risk_multiplier = mult_by_market.get(_mkt, risk_multiplier)
    try:
        t0 = time.perf_counter()
        order = risk.approve(signal, ctx, risk_multiplier=risk_multiplier, marks=marks)
        if timings is not None:
            timings.risk_approve_ms += (time.perf_counter() - t0) * 1000
    except Exception as e:
        logger.warning("리스크 승인 실패 %s: %s", signal.symbol, e)
        return
    if order is None:
        reason = getattr(risk, "last_block", "") or "거부됨"
        logger.info("주문 거부 %s %s: %s", signal.strategy_id, signal.symbol, reason)
        if "자금 부족" in reason and signal.action in _ENTRY_ACTIONS:
            # 소유자 지시(2026-08-31): "잔고 부족으로 못 산 경우, 시도 기록을 남겨
            # 나중에 '안 산 게 아니라 못 샀던 것'이 되게." risk.approve()가 예산
            # 부족을 여기서 이미 걸러 order를 만들지 않으므로(위 order is None),
            # 평소 경로(place_order 이후 _emit_order_state)를 전혀 타지 않아
            # orders.jsonl에 아무 흔적도 안 남았다 — 그 구멍만 메운다(다른 거부
            # 사유는 기존 동작 그대로 로그만). qty=0은 "수량 산정 자체가 안 됐다"는
            # 뜻이고, 실제 필요/가용 금액은 reason 문자열에 있다(symbol/전략/시각은
            # 아래 OrderState/on_order가 별도 컬럼으로 남긴다).
            try:
                pseudo_order = Order(
                    symbol=signal.symbol, side=Side.BUY, qty=0.0,
                    strategy_id=signal.strategy_id, reason=signal.reason,
                )
                _emit_order_state(oms.not_submitted(pseudo_order, reason, at=_safe_now(ctx)), sinks)
            except Exception as e:  # noqa: BLE001 — 기록 실패가 매매 흐름을 막으면 안 된다
                logger.warning("자금 부족 거부 기록 실패 %s: %s: %s",
                               signal.symbol, type(e).__name__, e)
        return
    if approval is not None and signal.action in _ENTRY_ACTIONS:
        # 리스크 통과까지만 하고 집행하지 않는다. 여기서 만든 order는 버린다 —
        # 실제 주문은 승인 시점에 최신 시세로 다시 계산한다(불변식 5).
        _request_approval(signal, order, ctx, approval, approval_notifier, approval_cfg, notifier)
        return
    # 손절 매도 알림용 — 이 종목이 재진입 대기(쿨다운)에 들어가는지 사람이 알게 한다.
    cooldown_bars = getattr(risk, "cooldown_bars_after_stop", 0)
    cooldown_minutes = cooldown_bars * getattr(risk, "cooldown_bar_interval_minutes", 15)
    cooldown_note = (
        f"손절 후 재진입 대기: 약 {cooldown_minutes}분 ({cooldown_bars}봉)" if cooldown_bars else ""
    )
    try:
        t0 = time.perf_counter()
        state = ctx.broker.place_order(order)
        if timings is not None:
            timings.broker_place_order_ms += (time.perf_counter() - t0) * 1000
        # 브로커가 상태를 돌려준다(Phase 6.3). 구현이 아직 Fill 을 주는 경우도
        # 받아준다 — 계약 전환 중에 사이클이 죽으면 안 된다.
        state, fill = _normalize_order_result(order, state, ctx)
    except Exception as e:
        logger.warning("주문 실행 실패 %s: %s", order.symbol, e)
        if notifier is not None:
            notifier.send(f"⚠️ 주문 실행 실패\n\n📌 종목 {_display(order.symbol)}\n📕 사유 {e}")
        return
    _emit_order_state(state, sinks)
    if fill is not None:
        t0 = time.perf_counter()
        sinks.on_fill(fill)
        if timings is not None:
            timings.sinks_ms += (time.perf_counter() - t0) * 1000
        if books is not None:
            # 전략별 독립 명목계정(2026-08-19) 갱신. 기존 원장 기록(sinks.on_fill,
            # 위 줄) 경로는 건드리지 않는다 — books는 그 위에 얹는 별도 회계
            # 레이어다. fx는 ctx에 없으므로(Context는 clock/data/broker뿐)
            # PaperBroker가 들고 있는 것을 duck-typing으로 재사용한다
            # (_position_report_text 등 기존 코드와 같은 패턴).
            try:
                broker_market_of = getattr(ctx.broker, "market_of", None) or {}
                fill_market = broker_market_of.get(fill.symbol) or market_of_symbol(fill.symbol)
                fx = getattr(ctx.broker, "fx", None)
                if fx is None:
                    # 여기 오면 조립 배선 버그다 — 정상 기동이면 assembly가
                    # 브로커에 fx를 붙였거나(paper/live 양쪽) 아예 부팅을
                    # 거부했어야 한다(2026-09-02 C1). 최후 방어선으로만 남긴다.
                    logger.warning(
                        "전략별 장부 갱신 생략 — 브로커에 fx 없음(조립 배선 버그, "
                        "assembly.build_paper_runtime 확인 필요): %s", fill.symbol,
                    )
                else:
                    books.apply_fill(
                        fill.strategy_id, fill.symbol, fill.side, fill.qty, fill.price,
                        fill.fee, fill_market, fx,
                    )
                    books.save()
            except Exception:  # noqa: BLE001 — 장부 갱신 실패가 거래를 죽이면 안 된다
                logger.warning("전략별 장부 갱신 실패(거래는 계속): %s", fill.symbol, exc_info=True)
        if signal.state_update:
            # 체결 확인 후에만 전략 상태(meta)를 적용한다 — signal 생성 시점에
            # 미리 적용하면 risk 거부/미체결 시 상태가 오염된다. 포지션이 이번
            # 체결로 완전 청산됐다면(pos.is_open=False) 적용하지 않는다 —
            # PaperBroker는 전량 청산 시 meta를 비우므로, 그 위에 다시 쓰면
            # 다음 진입 때 잔존 meta가 새 _pending 상태를 가리게 된다.
            # 이 신호를 낸 전략의 **lot에만** 적용한다 — 심볼 합산 meta를 직접
            # 건드리면 같은 심볼의 다른 전략 lot을 덮어쓴다(2026-08-11 랏 도입).
            pos = ctx.broker.positions().get(signal.symbol)
            if pos is not None and pos.is_open:
                pos.ensure_lot(signal.strategy_id).update(signal.state_update)
        if notifier is not None:
            is_buy = fill.side is Side.BUY
            lines = [
                "🟢 매수 체결" if is_buy else "🔴 매도 체결",
                "",
                f"📌 종목    {_display(fill.symbol)}",
                f"🧠 전략    {_strategy_label(fill.strategy_id)}",
                f"🔢 수량    {fill.qty:g}주",
                f"💵 체결가  {_price(fill.price, fill.symbol)}",
                f"💰 총 금액 {_amount(fill.qty * fill.price, fill.symbol)}",
            ]
            if is_buy:
                # 손절/익절 가격 — 체결 직후 lot에는 아직 반영 전인 경우가 많으므로
                # (state_update는 이 블록보다 먼저 적용되지만 대부분의 전략은 진입
                # Signal에 state_update를 싣지 않고 다음 사이클에 lot을 채운다),
                # signal.stop/target(전략이 진입 시 직접 실어보낸 값)으로 폴백한다.
                # 둘 다 없으면 줄 자체를 만들지 않는다(0/"없음"으로 위장 금지).
                lot = None
                pos = ctx.broker.positions().get(fill.symbol)
                if pos is not None:
                    lot = pos.lot(fill.strategy_id)
                stop = (lot or {}).get("stop")
                if stop is None:
                    stop = signal.stop
                target = (lot or {}).get("target")
                if target is None:
                    target = signal.target
                if stop is not None:
                    pct = (float(stop) / fill.price - 1) * 100
                    lines.append(f"🛡 손절    {_price(float(stop), fill.symbol)} ({pct:+.2f}%)")
                if target is not None:
                    pct = (float(target) / fill.price - 1) * 100
                    lines.append(f"🎯 익절    {_price(float(target), fill.symbol)} ({pct:+.2f}%)")
            if not is_buy:
                # 매도에도 **진입가와 그때 걸어둔 손절선**을 보여준다(2026-08-26
                # 소유자: "매수랑 매도 메세지에서 해결해줘"). 청산가만 보면
                # "얼마에 샀고 어디서 끊기로 했었나"를 사유 문자열에서 눈으로
                # 파내야 한다. 전량 청산이면 lot 이 이미 비었을 수 있으므로
                # 없으면 그 줄을 만들지 않는다(지어내지 않는다).
                exit_lot = None
                exit_pos = ctx.broker.positions().get(fill.symbol)
                if exit_pos is not None:
                    exit_lot = exit_pos.lot(fill.strategy_id)
                entry_px = (exit_lot or {}).get("entry")
                exit_stop = (exit_lot or {}).get("stop")
                if entry_px:
                    gap = (fill.price / float(entry_px) - 1) * 100
                    lines.append(
                        f"💵 진입가  {_price(float(entry_px), fill.symbol)} "
                        f"→ 청산 {_price(fill.price, fill.symbol)} ({gap:+.2f}%)"
                    )
                if exit_stop:
                    lines.append(f"🛡 손절선  {_price(float(exit_stop), fill.symbol)}")
                # 매도는 결과가 곧 성적이다 — 실현 손익(수수료 차감 후)을 바로 보여준다.
                if fill.realized_pnl is not None:
                    net = fill.realized_pnl - fill.fee
                    notional = fill.qty * fill.price
                    pct = (net / (notional - net) * 100) if notional > net else 0.0
                    mark = "🔺 이익" if net > 0 else ("🔻 손실" if net < 0 else "➖ 본전")
                    lines.append(
                        f"{mark}    {_signed_amount(net, fill.symbol)}"
                        f" ({pct:+.2f}%, 수수료 {_amount(fill.fee, fill.symbol)} 차감 후)"
                    )
                else:
                    lines.append("❔ 실현 손익 미상 (브로커가 원가를 제공하지 않음)")
                if "손절" in fill.reason and cooldown_note:
                    lines.append(f"⏳ {cooldown_note}")
            lines += ["", f"📝 {fill.reason}"]
            notifier.send("\n".join(lines))


def _approval_setting(approval_cfg: dict | None, key: str, default: float) -> float:
    try:
        return float((approval_cfg or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _quote_price(ctx: Context, symbol: str) -> float | None:
    """현재가. 소스 실패/무효값은 None — 승인 경로에서는 None이면 진입하지 않는다."""
    try:
        quote = ctx.data.quote(symbol)
    except Exception as e:
        logger.warning("승인 처리 중 시세 조회 실패 %s: %s", symbol, e)
        return None
    if quote is None or quote.price <= 0:
        return None
    return quote.price


def _duplicate_of_recent(approval: ApprovalGate, signal: Signal, expire_seconds: float) -> str | None:
    """같은 (전략, 종목, 방향)에 대해 이미 대기 중이거나 방금 거절된 요청이 있으면 그 사유.

    전략은 조건이 유지되는 한 매 사이클(paper 기준 10초) 같은 진입 신호를 다시
    낸다. 중복 방지가 없으면 15분봉 하나에 승인 요청이 수십 건 날아가고, 사용자가
    그중 둘을 각각 수락하면 두 번 산다. 그래서 (1) 대기 중인 건이 있으면 새 요청을
    만들지 않고, (2) 거절당한 건은 그 요청의 유효시간만큼 다시 묻지 않는다 —
    방금 아니라고 한 것을 10초 뒤에 또 묻는 것은 승인 UI를 무의미하게 만든다."""
    now = time.time()
    for req in approval.all():
        if (req.strategy_id, req.symbol, req.action) != (
            signal.strategy_id, signal.symbol, signal.action.value
        ):
            continue
        if req.is_pending:
            return f"이미 승인 대기 중 (id={req.id})"
        if (
            req.status == STATUS_REJECTED
            and req.decided_at is not None
            and now - req.decided_at < expire_seconds
        ):
            return f"직전에 거절됨 (id={req.id})"
    return None


def _request_approval(
    signal: Signal,
    order: Order,
    ctx: Context,
    approval: ApprovalGate,
    approval_notifier: object | None,
    approval_cfg: dict | None,
    notifier: Notifier | None,
) -> None:
    """신규 진입을 집행하지 않고 사용자 승인 요청으로 돌린다.

    fail-closed: 시세를 못 읽거나 텔레그램 전송이 실패하면 요청을 즉시 만료시키고
    진입하지 않는다. "승인 없이 새 포지션이 생기는 경로"는 존재하면 안 된다."""
    expire_seconds = _approval_setting(approval_cfg, "expire_seconds", _DEFAULT_APPROVAL_EXPIRE_SECONDS)
    duplicate = _duplicate_of_recent(approval, signal, expire_seconds)
    if duplicate is not None:
        logger.debug("승인 요청 생략 %s %s: %s", signal.strategy_id, signal.symbol, duplicate)
        return
    price = _quote_price(ctx, order.symbol)
    if price is None:
        logger.warning("승인 요청 생략(현재가 없음) — 신규 진입하지 않는다: %s", order.symbol)
        return

    req = approval.create(
        strategy_id=signal.strategy_id,
        symbol=order.symbol,
        action=signal.action.value,
        qty=order.qty,
        price=price,
        reason=signal.reason or order.reason,
        target_weight=signal.target_weight,
        stop=signal.stop,
        target=signal.target,
        state_update=signal.state_update,
    )
    message_id = None
    if approval_notifier is not None:
        message_id = approval_notifier.send_request(req, expire_seconds)
    if message_id is None:
        # 사용자가 볼 수 없는 요청은 유령 pending으로 남기지 않는다.
        approval.expire(req.id)
        logger.warning("승인 요청 전송 실패 — 신규 진입 취소(fail-closed): %s %s", order.symbol, req.id)
        if notifier is not None:
            notifier.send(f"⚠️ 승인 요청 전송 실패 — 신규 진입을 취소했습니다\n\n📌 종목 {_display(order.symbol)}")
        return
    approval.set_message_id(req.id, message_id)
    logger.info("승인 대기 %s %s %g@%.2f (id=%s)", req.strategy_id, req.symbol, req.qty, req.price, req.id)


def _decision_text(req: ApprovalRequest, approved: bool) -> str:
    head = "✅ 수락됨" if approved else "❌ 거절됨"
    return f"{head} — {req.symbol} {req.action} {req.qty:g}@{req.price:,.2f}\n요청 ID: {req.id}"


def _process_approvals(
    ctx: Context,
    risk: RiskManager,
    sinks: EventSink,
    notifier: Notifier | None,
    control: TradingControl | None,
    approval: ApprovalGate,
    approval_notifier: object | None,
    approval_cfg: dict | None,
    marks: dict[str, float] | None = None,
    books: StrategyBooks | None = None,
) -> None:
    """매 사이클: 만료 처리 → 사용자 결정 수집 → 승인분 재검증 후 집행 → 정리.

    청산은 이 함수를 거치지 않는다. 여기서 다루는 것은 전부 신규 진입뿐이다."""
    expire_seconds = _approval_setting(approval_cfg, "expire_seconds", _DEFAULT_APPROVAL_EXPIRE_SECONDS)
    max_drift_pct = _approval_setting(approval_cfg, "max_price_drift_pct", _DEFAULT_MAX_PRICE_DRIFT_PCT)
    prune_after = _approval_setting(approval_cfg, "prune_after_seconds", _DEFAULT_PRUNE_AFTER_SECONDS)

    # (1) 만료를 결정 수집보다 **먼저** 한다. 신호는 시간에 민감하다 — 09:35 진입
    # 신호를 10:30에 승인해 실행하면 그건 다른 거래다. 순서를 뒤집으면 만료 시점을
    # 지나 도착한 콜백이 만료 판정 직전에 승인으로 확정돼 버린다.
    for req in approval.expire_stale(expire_seconds):
        logger.info("승인 요청 만료 %s %s (id=%s)", req.strategy_id, req.symbol, req.id)
        if approval_notifier is not None:
            approval_notifier.finalize(
                req.message_id, f"⏱ 만료됨 — {req.symbol} {req.action} 미실행\n요청 ID: {req.id}"
            )
        if notifier is not None:
            notifier.send(
            f"⏳ 승인 요청 만료 — 진입하지 않았습니다\n\n📌 종목 {_display(req.symbol)}\n⏱ 대기 시간 {expire_seconds:.0f}초"
        )

    # (2) 버튼 결정 수집. decide()가 None이면 이미 종결된 요청 — 멱등하게 무시한다
    # (버튼 연타, 재전송된 콜백, 만료 후 승인 전부 여기로 떨어진다).
    if approval_notifier is not None:
        for req_id, approved in approval_notifier.poll_decisions():
            req = approval.decide(req_id, approved)
            if req is None:
                logger.info("승인 콜백 무시 — 이미 처리/만료된 요청: %s", req_id)
                continue
            approval_notifier.finalize(req.message_id, _decision_text(req, approved))

    # (3) 승인분 집행. take_approved()는 one-shot이라 이 루프에 들어온 요청은
    # 이미 executed로 표시돼 있다 — 취소하더라도 다음 사이클에 되살아나지 않는다.
    for req in approval.take_approved():
        if control is not None and control.is_halted():
            _cancel_approved(req, f"거래 중단 상태({control.halt_reason()})", notifier)
            continue
        price = _quote_price(ctx, req.symbol)
        if price is None:
            _cancel_approved(req, "현재가 없음", notifier)
            continue
        drift_pct = abs(price - req.price) / req.price * 100
        if drift_pct > max_drift_pct:
            _cancel_approved(
                req,
                f"가격 {drift_pct:.2f}% 변동 (한도 {max_drift_pct:g}%): "
                f"승인 시 {req.price:,.2f} → 현재 {price:,.2f}",
                notifier,
            )
            continue
        # 낡은 Order를 집행하지 않는다 — Signal을 복원해 risk.approve를 다시 돌린다.
        # 가격이 조금이라도 움직였으면 사이징도 달라야 하고, 그 사이 회로차단기가
        # 걸렸다면 여기서 걸려야 한다.
        signal = Signal(
            strategy_id=req.strategy_id,
            symbol=req.symbol,
            action=SignalAction(req.action),
            target_weight=req.target_weight,
            reason=f"{req.reason} [승인됨 {req.id}]",
            stop=req.stop,
            target=req.target,
            state_update=dict(req.state_update),
        )
        sinks.on_signal(signal)
        _execute_signal(signal, ctx, risk, sinks, notifier, marks=marks, books=books)

    if prune_after > 0:
        approval.prune(prune_after)


def _cancel_approved(req: ApprovalRequest, why: str, notifier: Notifier | None) -> None:
    logger.warning("승인된 진입 취소 %s %s (id=%s): %s", req.strategy_id, req.symbol, req.id, why)
    if notifier is not None:
        notifier.send(f"🚫 승인된 진입을 취소했습니다\n\n📌 종목 {_display(req.symbol)}\n📕 사유 {why}")


def _cancel_open_orders_before_flatten(broker) -> None:
    """flatten 직전 미체결 주문을 먼저 취소한다 (2026-08-30, 실계좌 방어선).

    미체결 주문이 남은 채로 전량 매도를 내면 서버에 먼저 가 있던 매수 주문이
    뒤늦게 체결돼 방금 청산한 포지션이 되살아날 수 있다. `Broker.open_orders()`/
    `cancel_order()`가 없는 브로커(PaperBroker 등)는 duck-typing으로 조용히
    건너뛴다. **취소 실패해도 flatten 자체는 막지 않는다** — 청산이 취소보다
    우선한다는 이 함수의 원칙과 동일하다."""
    open_orders_fn = getattr(broker, "open_orders", None)
    cancel_fn = getattr(broker, "cancel_order", None)
    if not callable(open_orders_fn) or not callable(cancel_fn):
        return
    try:
        orders = open_orders_fn()
    except Exception as e:  # noqa: BLE001 — flatten을 막으면 안 된다
        logger.warning("flatten 전 미체결 주문 조회 실패 — 취소 없이 진행: %s: %s", type(e).__name__, e)
        return
    for o in orders:
        try:
            cancel_fn(o.order_id)
        except Exception as e:  # noqa: BLE001 — flatten을 막으면 안 된다
            logger.warning("flatten 전 미체결 주문 취소 실패 (orderId=%s) — 진행: %s: %s",
                            o.order_id, type(e).__name__, e)


def _flatten_all(
    ctx: Context, risk: RiskManager, sinks: EventSink, notifier: Notifier | None,
    books: StrategyBooks | None = None, scope: str = "all",
    symbols: list[str] | None = None, control: TradingControl | None = None,
) -> set[str]:
    """열린 포지션 전부(또는 scope="day"면 단타 전략 보유분만)를 즉시 청산한다 —
    TradingControl.request_flatten()의 실행부. 반환값은 이번 호출에서 장 마감으로
    청산하지 못한 종목 집합(빈 집합이면 전부 처리됨).

    `symbols`를 주면(장 마감으로 보류됐던 종목의 개장 후 재시도 경로,
    `_retry_pending_flatten` 참고) 그 종목들만 대상으로 삼는다 — None이면(기본,
    사용자의 최초 /flatten 요청) 엔진 소유 열린 포지션 전부가 대상이다.

    halt와 달리 여기서 만드는 Signal은 항상 EXIT_LONG이라 run_cycle의 halt 체크
    (_ENTRY_ACTIONS)에 걸리지 않는다: halt 중에도 flatten은 반드시 동작해야 한다.

    **장이 닫힌 종목은 risk.approve의 물리적 제약(A-5)에 그대로 걸린다** — flatten만
    이 게이트를 우회하지 않는다(닫힌 시장에 체결 가능한 가격이 없다는 사실은 flatten이라고
    바뀌지 않는다). 대신 여기서 우회하지 않는 대가로 **조용히 무시하지 않는다**: 게이트에
    막힌 종목은 `control`(주어졌으면)에 pending_flatten으로 영속화해 개장 시 엔진이
    스스로 재시도하게 하고, 사용자에게도 한 번에 모아 알린다 (2026-09-04 — 이전에는
    로그만 남기고 요청을 소비해버려 소유자가 개장 후 수동으로 재요청해야 했다).

    `scope="day"`: 오버나이트 캐리가 설계인 전략(`_OVERNIGHT_STRATEGIES`/`_is_overnight`
    — EoD 강제청산 여부를 가르는 데 이미 쓰던 같은 분류를 재사용한다)의 lot은
    건드리지 않는다. 소유자가 없는 잔여 수량("flatten" 경로로 잡히는 고아분)도
    day scope에서는 건드리지 않는다 — 스윙 전략 몫일 수 있어 단타 한정 청산의
    범위 밖이다."""
    _cancel_open_orders_before_flatten(ctx.broker)
    positions = ctx.broker.positions()
    # 엔진 소유 원장을 노출하는 브로커면 **엔진이 진입한 종목만** 청산한다. 사용자가
    # 같은 계좌에서 손으로 산 물량은 kill switch의 대상이 아니다 — risk.approve와
    # 브로커 어댑터에도 같은 상한이 있지만, 여기서 거르지 않으면 사용자 보유가
    # "청산 대상"으로 로그에 찍혀 운영자가 오해한다.
    engine_owned_qty = getattr(ctx.broker, "engine_owned_qty", None)
    open_symbols = [
        sym for sym, pos in positions.items()
        if pos.is_open and (not callable(engine_owned_qty) or engine_owned_qty(sym) > 0)
    ]
    if symbols is not None:
        # 재시도 경로: 이번에 시장이 열린 것으로 확인된 종목만 처리한다. 이미
        # 포지션이 사라진 종목(다른 경로로 이미 청산됨)은 조용히 걸러진다 —
        # 호출자가 pending에서 지울 때 "실행됨"으로 표시해도 실제 주문은 없다.
        wanted = set(symbols)
        open_symbols = [sym for sym in open_symbols if sym in wanted]
    if not open_symbols:
        return set()
    scope_label = "단타 보유분" if scope == "day" else "전량"
    logger.warning("수동 청산 요청 처리 — %s 청산: %s", scope_label, ", ".join(open_symbols))
    market_closed_symbols: set[str] = set()

    def _flatten_signal(strategy_id: str, symbol: str) -> None:
        signal = Signal(
            strategy_id=strategy_id,
            symbol=symbol,
            action=SignalAction.EXIT_LONG,
            target_weight=0.0,
            exit_fraction=1.0,
            reason="수동 청산(kill switch)",
        )
        sinks.on_signal(signal)
        _execute_signal(signal, ctx, risk, sinks, notifier, books=books)
        if MARKET_CLOSED_MARKER in (getattr(risk, "last_block", "") or ""):
            market_closed_symbols.add(symbol)

    for symbol in open_symbols:
        # 2026-08-11 랏(lot) 도입: 한 심볼에 여러 전략의 lot이 같이 있을 수 있다.
        # risk/execution 레이어는 이제 청산 수량을 "그 전략의 lot" 기준으로 계산
        # 하므로(다른 전략 몫을 건드리지 않기 위함), 단일 "flatten" 신호 하나로는
        # 첫 lot만 팔고 나머지 lot은 그대로 남는다 — kill switch가 절반만 도는
        # 사고가 된다. 그래서 **lot마다 개별 청산 신호**를 낸다. lot 밖 잔량(고아분,
        # 예: 순수 레거시로 아직 어떤 전략도 추적하지 않는 몫)은 마지막에 범용
        # "flatten" 신호로 마저 정리한다 — kill switch는 전략 소유권과 무관하게
        # **전량** 청산해야 한다(사용자가 손으로 산 물량은 위 engine_owned_qty
        # 필터가 이미 걸러냈다).
        pos = positions.get(symbol)
        lots = (pos.meta.get("lots") if pos is not None else None) or {}
        active_strategy_ids = [sid for sid, lot in lots.items() if lot.get("qty", 0.0) > 0]
        # day scope: 오버나이트 캐리가 설계인 전략의 lot은 건드리지 않는다 —
        # 그 몫은 스윙 포지션이라 단타 한정 청산의 대상이 아니다.
        target_strategy_ids = (
            [sid for sid in active_strategy_ids if not _is_overnight(sid)]
            if scope == "day" else active_strategy_ids
        )
        for strategy_id in target_strategy_ids:
            _flatten_signal(strategy_id, symbol)
        if scope == "day":
            # 소유자 없는 잔여 수량("flatten" 경로)은 스윙 몫일 수 있으므로
            # day scope에서는 건드리지 않는다.
            continue
        tracked_qty = sum(lot.get("qty", 0.0) for lot in lots.values())
        residual = (pos.qty if pos is not None else 0.0) - tracked_qty
        if not active_strategy_ids or residual > 1e-9:
            _flatten_signal("flatten", symbol)

    if market_closed_symbols:
        logger.warning(
            "수동 청산 보류 — 개장 시 자동 재시도: %s", ", ".join(sorted(market_closed_symbols)),
        )
        if control is not None:
            control.set_pending_flatten(scope, sorted(market_closed_symbols))
        if notifier is not None:
            notifier.send(
                "⏳ 청산 보류 — 장 마감\n\n"
                f"🏢 {', '.join(sorted(market_closed_symbols))}\n"
                "닫힌 시장에는 체결 가능한 가격이 없어 개장 시 엔진이 자동으로 재시도합니다."
            )
    return market_closed_symbols


def _retry_pending_flatten(
    ctx: Context, risk: RiskManager, sinks: EventSink, notifier: Notifier | None,
    control: TradingControl, books: StrategyBooks | None,
) -> None:
    """장 마감으로 보류된 flatten을 개장 시 자동 재시도한다 (2026-09-04).

    `_flatten_all`이 장이 닫혀 청산하지 못한 종목은 `control.pending_flatten()`에
    남는다. 매 사이클(일반 flatten 요청이 없는 사이클) 여기서 그중 시장이 열려
    **연속 거래 구간**에 들어간 종목만 골라 다시 청산을 시도한다 — risk.approve가
    실제로 쓰는 게이트(is_market_open)와 동일한 판정을 미리 걸러, 동시호가처럼
    체결 가능한 가격이 아직 없는 구간에 헛되이 주문을 내지 않는다.

    대기 중인 게 없거나 아직 열린 종목이 하나도 없으면 아무 것도 하지 않는다 —
    매 사이클 로그를 남기면 대기 기간 내내 스팸이 된다(모듈 상단 원칙)."""
    pending = control.pending_flatten()
    if pending is None:
        return
    ready = [
        sym for sym in pending["symbols"]
        if ctx.clock.is_market_open(market_of_symbol(sym))
        and in_continuous_session(market_of_symbol(sym), ctx.clock.now())
    ]
    if not ready:
        return
    still_blocked = _flatten_all(
        ctx, risk, sinks, notifier, books, scope=pending["scope"], symbols=ready, control=control,
    )
    executed = [sym for sym in ready if sym not in still_blocked]
    if executed:
        control.clear_pending_flatten(executed)
        done = ", ".join(sorted(executed))
        logger.warning("보류됐던 청산 실행 완료: %s", done)
        if notifier is not None:
            notifier.send(f"🧹 보류됐던 청산 실행: {done}")


def _strategies_with_open_lots(ctx: Context, strategy_errors: dict[str, str]) -> list[str]:
    """예외로 죽은 전략 중 **열린 포지션을 가진** 것만 골라낸다.

    포지션이 없는 전략의 실패는 진입 기회를 놓치는 것이라 다음 사이클에 회복되지만,
    포지션을 든 전략의 실패는 **손절이 작동하지 않는 상태**다. 둘을 같은 무게로
    다루면 알림 피로가 생기고, 다르게 다루지 않으면 진짜 위험을 놓친다.

    랏(`meta["lots"]`)이 있으면 그 키로 판정하고, 없으면(레거시/무태그) 판정할 수
    없으므로 **열린 포지션이 하나라도 있으면 위험 쪽으로** 센다 — 모르는 것을
    안전하다고 가정하지 않는다.
    """
    if not strategy_errors:
        return []
    try:
        positions = ctx.broker.positions()
    except Exception:  # noqa: BLE001 — 판정 실패가 알림 경로를 죽이면 안 된다
        return sorted(strategy_errors)
    open_positions = [p for p in positions.values() if p.is_open]
    if not open_positions:
        return []
    owners: set[str] = set()
    untagged = False
    for pos in open_positions:
        lots = (pos.meta or {}).get("lots")
        if lots:
            owners.update(lots)
        else:
            untagged = True
    return sorted(
        sid for sid in strategy_errors if untagged or sid in owners
    )


def _build_marks_and_unpriced(ctx: Context) -> tuple[dict[str, float], list[str]]:
    """`_build_marks`에 더해, **시세를 못 받은 보유 종목**을 함께 돌려준다.

    왜 따로 돌려주나: 전략의 `_manage_position`은 `quote is None`이면 조용히
    `return None`한다 — 즉 **시세가 끊기면 손절 판정 자체가 일어나지 않는다.**
    사람이 보고 있으면 알아채지만 이 시스템은 무인 운용이 전제다(2026-08-11
    사용자: "내가 프로그램을 보고 있지 않아도"). 조용한 미발동은 곧 무방비다.
    Toss 시세는 실측으로 3.3% 429가 나고 있어 가상의 위험이 아니다.
    """
    marks = _build_marks(ctx)
    # 시장이 닫힌 심볼은 애초에 quote()를 시도하지 않는다(_build_marks 참고) —
    # 그래서 marks에 없는 것과 "시세가 끊긴 것"을 여기서 구분해야 한다. 장이
    # 닫혀 있어서 marks에 없는 심볼까지 unpriced로 보고하면 US 세션 8시간 동안
    # KR 종목이 계속 "시세 끊김"으로 잡히는 오탐이 난다(2026-08-12 실측 695건) —
    # 장 마감은 장애가 아니다.
    unpriced = [
        symbol for symbol, pos in ctx.broker.positions().items()
        if pos.is_open and symbol not in marks
        and ctx.clock.is_market_open(market_of_symbol(symbol))
    ]
    return marks, unpriced


def _find_orphan_lots(ctx: Context, active_strategy_ids: set[str]) -> list[tuple[str, str]]:
    """열려 있는데 **lots의 소유 전략이 현재 활성 전략 목록에 없는** (symbol, strategy_id) 쌍.

    `_owns`(strategies/orb_scan.py 등) 규칙: lots 구조가 있는데 내 lot이 없으면 이미
    다른 전략이 추적 중인 것으로 보고 입양하지 않는다. 그 lot의 주인 전략이
    `enabled: false`로 꺼지거나(설정) 유니버스 롤로 전략 목록에서 빠지면, 어떤
    전략도 그 lot의 `_owns`를 통과시키지 않는다 — 손절·EoD청산 판정이 그 lot에
    대해서만 조용히 멈춘다(2026-08-12 감사 A-6). 레거시 평평한 meta(`lots` 키
    자체가 없는 경우)는 범위 밖이다 — `_owns`가 그건 이미 "미상 → 유니버스 소속
    입양"으로 처리하므로 고아가 아니다."""
    orphans: list[tuple[str, str]] = []
    try:
        positions = ctx.broker.positions()
    except Exception:  # noqa: BLE001 — 감시가 거래 루프를 죽이면 안 된다
        return orphans
    for symbol, pos in positions.items():
        if not pos.is_open:
            continue
        lots = (pos.meta or {}).get("lots")
        if not lots:
            continue
        for strategy_id, lot in lots.items():
            if float(lot.get("qty", 0.0)) > 0 and strategy_id not in active_strategy_ids:
                orphans.append((symbol, strategy_id))
    return orphans


def _build_marks(ctx: Context) -> dict[str, float]:
    """열린 포지션 종목의 사이클 시세 스냅샷 — risk.approve()의 daily_loss_limit_pct
    평가금액(MTM) 계산에 쓰인다. ctx.data.quote()는 MarketDataService의 quote 캐시
    (engine.quote_cache_seconds)를 그대로 타므로, 사이클마다 종목 수만큼 다시 불러도
    실제 네트워크 조회는 캐시 만료 시에만 일어난다. 종목 수는 보유 포지션 수로
    자연히 제한된다(관심종목 전체가 아니라 max_concurrent_positions 이내).
    조회 실패/무효 시세는 그냥 빠진다 — risk.approve가 종목별로 평균단가로 저하한다.

    시장이 닫힌 심볼(market_of_symbol + ctx.clock.is_market_open)은 quote() 호출
    자체를 건너뛴다 — US 세션 내내 장 마감된 KR 보유 종목을 계속 찔러 실패
    경고를 쌓는 것은 낭비이자 오탐이다(2026-08-12)."""
    marks: dict[str, float] = {}
    for symbol, pos in ctx.broker.positions().items():
        if not pos.is_open:
            continue
        if not ctx.clock.is_market_open(market_of_symbol(symbol)):
            continue
        try:
            quote = ctx.data.quote(symbol)
        except Exception:
            continue
        if quote is not None and math.isfinite(quote.price) and quote.price > 0:
            marks[symbol] = quote.price
    return marks


def _record_ticks(
    ctx: Context, tick_logger: TickLogger, strategies: list[Strategy], marks: dict[str, float],
) -> None:
    """워치리스트 전 심볼(활성 전략들이 다루는 심볼의 합집합)의 이번 사이클 시세를
    틱 로거에 남긴다(2026-08-28) — Toss 1분봉이 4거래일 롤링이라 소급이 안 되므로
    엔진이 읽는 시세를 우리가 직접 쌓는다(TickLogger 모듈 docstring 참고).

    marks(보유 종목 스냅샷, `_build_marks`)에 이미 있는 값은 재사용해 quote() 호출을
    줄인다. 시장이 닫힌 심볼은 건너뛴다 — `_build_marks`와 같은 이유로, 닫힌 시장을
    계속 찔러 실패를 쌓는 것은 낭비이자 오탐이다. quote() 실패는 조용히 건너뛴다 —
    틱 로깅이 사이클을 막으면 안 된다(TickLogger.record 자체도 예외를 삼키지만, 여기서
    quote() 실패까지 한 번 더 막는다)."""
    now = ctx.clock.now()
    symbols = {s for st in strategies for s in st.symbols}
    for symbol in symbols:
        price = marks.get(symbol)
        if price is None:
            if not ctx.clock.is_market_open(market_of_symbol(symbol)):
                continue
            try:
                quote = ctx.data.quote(symbol)
            except Exception:
                continue
            if quote is None or not math.isfinite(quote.price) or quote.price <= 0:
                continue
            price = quote.price
        tick_logger.record(symbol, price, now)


def _estimate_equity(ctx: Context) -> float | None:
    """PaperBroker처럼 portfolio.equity()를 노출하는 브로커면 KRW 환산 총자산을 계산한다.

    Broker Protocol엔 없는 부가 속성이라 duck-typing으로 있을 때만 사용하고, 실패하면
    None을 반환한다 — 하트비트에 부정확한 숫자를 보여주느니 생략하는 편이 낫다."""
    portfolio = getattr(ctx.broker, "portfolio", None)
    if portfolio is None:
        return None
    try:
        prices = {}
        for symbol in portfolio.positions:
            quote = ctx.data.quote(symbol)
            if quote is not None:
                prices[symbol] = quote.price
        market_of = getattr(ctx.broker, "market_of", None)
        fx = getattr(ctx.broker, "fx", None)
        return portfolio.equity(prices, market_of, fx)
    except Exception:
        return None


def _uptime_text(seconds: float) -> str:
    """가동 시간을 사람이 읽는 형태로. 재시작을 알아채는 것이 목적이다."""
    total = int(max(seconds, 0))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}일 {hours}시간"
    if hours:
        return f"{hours}시간 {minutes}분"
    return f"{minutes}분"


def _heartbeat_text(
    cycle_count: int,
    ctx: Context,
    control: TradingControl,
    market_data: object | None,
    last_cycle_ms: float | None = None,
    recent_max_cycle_ms: float | None = None,
    approval: ApprovalGate | None = None,
    risk: RiskManager | None = None,
    uptime_seconds: float | None = None,
) -> str:
    positions = ctx.broker.positions()
    open_positions = {sym: pos.qty for sym, pos in positions.items() if pos.is_open}
    halted = control.is_halted()

    held_text = (
        ", ".join(f"{_display(s)} {q:g}주" for s, q in open_positions.items())
        if open_positions else "없음"
    )
    # "N번째 확인"이라고 쓰던 자리다. cycle_count는 **엔진이 한 바퀴 돈 횟수**인데
    # (poll_seconds 5초 → 분당 약 10회) 하트비트는 30분마다 오므로 숫자가 300~400씩
    # 점프한다 — "확인"이라는 말이 알림 횟수처럼 읽혀 사용자가 무슨 뜻인지 물었다
    # (2026-08-12). 세는 단위를 정확히 쓰고, 가동 시간을 함께 보여 재시작을
    # 알아챌 수 있게 한다(사이클 수가 갑자기 작아지면 재시작이다).
    cycle_line = f"🔄 {cycle_count:,}번째 사이클"
    if uptime_seconds is not None:
        cycle_line += f" · 가동 {_uptime_text(uptime_seconds)}"
    if halted:
        # 정지는 하트비트의 부속 한 줄이 아니라 머리기사다(2026-08-31 실사고:
        # 금요일 밤 자동 정지가 "중단됨" 한 줄로만 표시돼 월요일 KR 세션 전체가
        # 무체결로 지나갔다 — 사유도 대처법도 없어 방치됐다).
        reason = getattr(control, "halt_reason", "") or "사유 미기록"
        lines = [
            "⛔ 거래 중단이 계속되고 있습니다",
            "",
            f"📕 사유: {reason}",
            "▶️ 신규 진입을 재개하려면 /resume (청산·손절은 계속 작동 중)",
            "",
            cycle_line,
            f"📦 보유 종목: {held_text}",
        ]
    else:
        lines = [
            "💓 엔진 상태 점검",
            "",
            cycle_line,
            "✅ 거래 상태: 정상",
            f"📦 보유 종목: {held_text}",
        ]

    equity = _estimate_equity(ctx)
    if equity is not None:
        lines.append(f"💰 추정 자산: {equity:,.0f}원")

    health_fn = getattr(market_data, "health", None)
    if callable(health_fn):
        try:
            health = health_fn()
            lines.append(f"📡 시세 연결: {'⚠️ 저하됨' if health.degraded else '정상'}")
        except Exception:
            pass

    if last_cycle_ms is not None:
        lines.append(
            f"⚡ 처리 속도: 직전 {last_cycle_ms:,.0f}밀리초"
            f" · 최근 최대 {recent_max_cycle_ms:,.0f}밀리초"
        )

    if approval is not None:
        # 승인 게이트가 켜져 있으면 대기 건수를 항상 보여준다 — 0건도 의미가 있다
        # ("승인 게이트가 살아 있다"는 신호).
        lines.append(f"🙋 승인 대기: {len(approval.pending())}건")

    # 회로차단기 — 세션 마감 요약과 동일한 한 줄. 이게 없으면 레일이 걸려도
    # 장 마감까지 아무도 모른다.
    if risk is not None:
        breaker = _breaker_snapshot(risk, _safe_now(ctx))
        if breaker is not None:
            breaker_line = _breaker_line(breaker)
            if breaker_line is not None:
                lines.append(breaker_line)

    if halted:
        lines.append(f"📕 중단 사유: {control.halt_reason()}")
    return "\n".join(lines)


def _persist_position_meta(ctx: Context, last_signature: str) -> str:
    """전략이 `Position.meta`에 쓴 손절/목표를 디스크에 반영한다(변경 시에만).

    2026-08-10 실측 버그: `PaperBroker.save()`는 **주문 시점에만** 불리는데, 손절은
    그 다음 사이클에 전략의 `_ensure_state`가 `_pending`에서 옮겨 담는다. 그 사이에
    프로세스가 재시작되면 디스크 meta가 `{}`라 전략이 "재시작 복구" 폴백(고정 2%)으로
    손절을 새로 만든다 — 진입 시 계산한 ATR 기반 손절이 조용히 사라진다. 실제로
    장중 보유 포지션 2건의 디스크 meta가 비어 있는 것을 확인하고 추가했다.

    시그니처가 바뀔 때만 쓴다 — 매 사이클(5초) tmp-replace 쓰기는 불필요한 I/O다."""
    portfolio = getattr(ctx.broker, "portfolio", None)
    if portfolio is None or not hasattr(portfolio, "save"):
        return last_signature  # 실거래 브로커 등 — 영속화 주체가 다르다
    try:
        signature = repr(sorted(
            (sym, sorted(pos.meta.items(), key=str))
            for sym, pos in ctx.broker.positions().items() if pos.is_open
        ))
    except Exception:  # noqa: BLE001 — 시그니처 계산 실패가 사이클을 죽이면 안 된다
        return last_signature
    if signature != last_signature:
        try:
            portfolio.save()
        except Exception:
            logger.warning("포지션 meta 영속화 실패 — 재시작 시 손절 폴백 위험", exc_info=True)
            return last_signature
    return signature


# 오버나이트 보유가 설계인 전략들 — EoD 강제청산(`Clock.should_flatten`)을 아예
# 호출하지 않는다. 목표가가 없다고 해서 "장 마감까지 보유"라고 쓰면 거짓말이 된다
# (2026-08-19: frgn_accumulate 가 KR 마감 후에도 들고 있는데 메시지가 "장 마감까지
# 보유"라고 해 소유자가 버그로 의심했다 — 동작은 정상, 문구가 틀렸다).
# 이 집합이 낡지 않도록 tests/test_position_report_wording.py 가 실제 소스에서
# should_flatten 호출 여부를 세어 대조한다.
# close_bet(2026-08-25): 수익 원천이 오버나이트 갭 그 자체 — EoD 청산에서 제외.
_OVERNIGHT_STRATEGIES = frozenset({
    "cross_momentum", "frgn_accumulate", "mean_reversion", "close_bet",
    # overnight_drift(2026-08-28): 종가 매수 → 익일 시가 매도가 전략 정의 자체다.
    # 누락하면 EoD 강제청산이 진입 당일 포지션을 털어 전략이 무효화된다.
    "overnight_drift",
    # rsi2_dip(2026-08-29): 마감 직전 매수 → 며칠 보유(RSI 회복/시간/하드레일
    # 청산)가 전략 정의다. overnight_drift 와 같은 이유로 누락 시 무효화된다.
    "rsi2_dip",
    # llm_trader는 여기 없다(2026-09-03 소유자 결정: 자동매매는 단타·스캘핑만).
    # 2026-08-30~2026-09-02에는 "LLM이 청산도 스스로 판단하는 실험"으로 오버나이트를
    # 허용했지만, 오버나이트/장기 아이디어는 이제 자동매매가 아니라
    # quant/analyze/manual_recs.py(텔레그램 추천) 레인으로 간다. llm_trader.py가
    # 마감 전 강제청산(should_flatten)을 스스로 호출하도록 바뀌었으므로 여기 남아
    # 있으면 안 된다 — 남기면 EoD 강제청산이 걸리지 않는 것과 소스 대조
    # (test_position_report_wording.py)가 즉시 어긋난다.
})


def _is_overnight(sid: str | None) -> bool:
    """오버나이트 보유 전략인가. `_pure` 접미사는 벗겨서 본다.

    순수 계약으로 전환해도 **보통은 id 가 그대로다** — settings.yaml 의 전략
    블록에서 `class:` 만 바꾸고 블록 키(=id)는 유지하기 때문이다. 하지만
    레거시와 순수를 나란히 돌려 비교하려면 별도 id(`close_bet_pure`)로 블록을
    하나 더 두게 되고, 그때 이 목록에 없으면 **EoD 강제청산이 오버나이트
    포지션을 당일 마감에 털어버려 전략 자체가 무효화된다**(2026-08-28 이관
    워커 경고). 접미사를 벗겨 판정하면 A/B 구성에서도 자동으로 맞는다.

    목록 자체에 `_pure` 를 넣지 않는 이유: `tests/test_position_report_wording.py`
    가 이 집합을 **전략 모듈 파일명**에서 유도해 대조한다(should_flatten 을
    호출하지 않는 모듈 = 오버나이트). 순수 구현은 같은 모듈 안에 있으므로
    목록에 새 이름을 넣으면 그 대조가 깨진다 — 대조를 약화시키는 대신
    판정 쪽을 관대하게 만든다.

    `_cat`(2026-09-03 A/B 촉매 갈래)도 같은 이유로 벗긴다 — `<id>_cat` 은
    `<id>` 와 같은 클래스라 보유 기한 성격이 같다. 지금 A/B 를 건 셋
    (scalp_1m/pullback_impulse/vol_breakout)은 전부 일중 전략이라 실제
    동작은 안 바뀌지만, 오버나이트 전략에 A/B 를 걸었을 때 EoD 강제청산이
    한쪽 갈래만 털어 비교를 무의미하게 만드는 사고를 미리 막는다."""
    # sid 가 None 일 수 있다 — 소유 전략을 모르는 포지션(수동 매수·구버전 lot).
    # 그때는 오버나이트로 단정하지 않는다(호출부가 기한 없는 문구를 쓴다).
    if not sid:
        return False
    return sid in _OVERNIGHT_STRATEGIES or _base_strategy_id(sid) in _OVERNIGHT_STRATEGIES


def _hard_rail_exit(
    strategy_id: str, symbol: str, ctx: Context, risk: RiskManager, sinks: EventSink,
    notifier: Notifier | None, books: StrategyBooks | None, reason: str,
) -> None:
    """`_intraday_hard_stop_check`가 만드는 강제 시장가 청산 — `_flatten_all`의
    `_flatten_signal`과 같은 패턴(전략 lot 단위 EXIT_LONG → `_execute_signal`).
    risk.approve()가 여전히 최종 승인자다(장 마감 게이트 등 물리적 제약은 여기서도
    그대로 적용된다) — 이 함수는 그 앞에 놓을 *신호*만 만든다."""
    signal = Signal(
        strategy_id=strategy_id,
        symbol=symbol,
        action=SignalAction.EXIT_LONG,
        target_weight=0.0,
        exit_fraction=1.0,
        reason=reason,
    )
    sinks.on_signal(signal)
    _execute_signal(signal, ctx, risk, sinks, notifier, books=books)


def _intraday_hard_stop_check(
    ctx: Context,
    risk: RiskManager,
    sinks: EventSink,
    notifier: Notifier | None,
    books: StrategyBooks | None,
    marks: dict[str, float],
) -> None:
    """엔진 레벨 장중 하드레일(2026-09-03, 소유자 지시: "장중 매매는 최대 손실
    −5%, 최대 목표 +10%를 유지한다"). `risk.manager.RiskManagerImpl.approve()`의
    진입 클램프(Order.stop/target + lot state_update 동기화)는 **진입 시점**에만
    걸린다 — 그 이후 전략의 `on_cycle` 자체가 멎으면(`ColdFetchBudgetExceeded`,
    또는 흔한 `quote is None -> return None` 패턴) 전략이 스스로 손절을 내지
    못한다. 그래서 이 레일은 **전략과 무관하게** 사이클마다 브로커 포지션(lot)을
    직접 보고 이번 사이클의 시세(marks — 이미 만든 quote 캐시, 추가 네트워크
    없음)와 대조한다.

    시세가 없는(marks에 없는) 종목은 건너뛴다 — 이미 `unpriced_since` 경보가
    "손절 판정이 멈춘 상태"를 알린다(이 함수까지 같은 경보를 중복으로 내지
    않는다). 오버나이트(캐리 설계) 전략은 `_is_overnight`으로 제외한다 — 이
    레일이 잡으려는 것은 장중 관리 실패이지, 정의상 밤을 넘기는 포지션이 아니다.

    익절(+10%) 강제 청산은 `risk.intraday_take_profit_cap_enabled`가 true일
    때만 낸다(기본 false, `RiskManagerImpl` 생성자 주석에 두 가지 해석과 기본값
    근거가 있다). 하드 손절 문자열은 리터럴로 고정한다 — 알림/원장에서 이 레일이
    낸 청산을 사유 문자열로 식별할 수 있어야 한다."""
    hard_stop_pct = getattr(risk, "intraday_hard_stop_pct", 0.0) or 0.0
    if hard_stop_pct <= 0:
        return
    take_profit_pct = getattr(risk, "intraday_max_target_pct", 0.0) or 0.0
    take_profit_enabled = bool(getattr(risk, "intraday_take_profit_cap_enabled", False))
    try:
        positions = ctx.broker.positions()
    except Exception:  # noqa: BLE001 — 조회 실패가 사이클을 죽이면 안 된다
        logger.warning("장중 하드레일 — 포지션 조회 실패, 이번 사이클 건너뜀", exc_info=True)
        return
    # list(...)로 스냅샷한다 — _hard_rail_exit이 부르는 _execute_signal이 체결 시
    # pos.meta["lots"]에서 청산된 전략의 lot을 pop한다(paper.py). 브로커가 돌려주는
    # positions()/lots는 살아있는 참조라(paper.py: `return self.portfolio.positions`),
    # 순회 중에 그 사전이 줄어들면 "dictionary changed size during iteration"으로
    # 이 함수 자체가 죽는다 — 뒤 종목/전략의 하드레일 판정이 함께 날아간다.
    for symbol, pos in list(positions.items()):
        if not pos.is_open:
            continue
        price = marks.get(symbol)
        if price is None:
            continue  # 시세 없음 — unpriced 경보가 이미 다룬다
        lots = pos.meta.get("lots") or {}
        for strategy_id, lot in list(lots.items()):
            qty = float(lot.get("qty", 0.0) or 0.0)
            if qty <= 0 or _is_overnight(strategy_id):
                continue
            entry = lot.get("entry", lot.get("avg_cost", pos.avg_cost))
            try:
                entry = float(entry)
            except (TypeError, ValueError):
                continue
            if entry <= 0:
                continue
            if price <= entry * (1 - hard_stop_pct):
                _hard_rail_exit(
                    strategy_id, symbol, ctx, risk, sinks, notifier, books,
                    "하드 손절 −5%(리스크 레일)",
                )
                continue
            if take_profit_enabled and take_profit_pct > 0 and price >= entry * (1 + take_profit_pct):
                _hard_rail_exit(
                    strategy_id, symbol, ctx, risk, sinks, notifier, books,
                    "하드 익절 +10%(리스크 레일)",
                )


def _position_report_text(ctx: Context, marks: dict[str, float], risk: RiskManager | None = None) -> str | None:
    """열린 포지션의 진입가 대비 현황 — 보유 중일 때만 1분 주기로 보낸다.

    하트비트(5분)는 "엔진이 살아 있는가"를 보는 것이고, 이건 "내 돈이 지금 어디에
    있는가"를 보는 것이다. 진입 직후가 가장 궁금한 구간이라 주기를 분리했다.
    시세는 사이클이 이미 만든 marks(quote 캐시 경유)를 그대로 쓴다 — 리포트 때문에
    추가 네트워크 조회를 하지 않는다."""
    positions = {s: p for s, p in ctx.broker.positions().items() if p.is_open}
    if not positions:
        return None

    now = ctx.clock.now()
    header = f"📊 보유 종목 현황 · 총 {len(positions)}종목"

    # 남은 현금 | 주식 투자금(평단가x수량, US는 KRW 환산) — 브로커가 portfolio를
    # 노출할 때만(paper). 실거래 브로커는 이 줄 없이 종목 현황만 나간다(강등).
    portfolio = getattr(ctx.broker, "portfolio", None)
    if portfolio is not None and hasattr(portfolio, "cash"):
        fx = getattr(ctx.broker, "fx", None)
        invested = 0.0
        for sym, pos in positions.items():
            try:
                invested += to_krw(pos.avg_cost * pos.qty, "KR" if _is_kr(sym) else "US", fx)
            except Exception:  # noqa: BLE001 — 환산 실패가 리포트를 죽이면 안 된다
                invested += pos.avg_cost * pos.qty
        header += f"\n🏦 남은 현금 {portfolio.cash:,.0f}원 | 📦 주식 투자금 {invested:,.0f}원"

    blocks: list[str] = [header]

    for symbol, pos in positions.items():
        cur = marks.get(symbol)
        avg = pos.avg_cost  # 여러 번 진입해도 표시는 평단가 하나 — 진입가들의 블렌딩(심볼 합산)
        cost = avg * pos.qty
        # 2026-08-11 랏(lot) 도입 — 여러 전략이 같은 심볼을 동시 보유할 수 있다.
        # 순수 조회만 한다(리포팅은 상태를 바꾸지 않는다 — ensure_lot의 이행을
        # 트리거하지 않는다).
        lots = pos.meta.get("lots") or {}
        active_lots = {sid: lot for sid, lot in lots.items() if lot.get("qty", 0.0) > 0}

        lines = [f"🏢 {_display(symbol)}"]

        if len(active_lots) >= 2:
            lines.append(
                f"   💵 평단가(합산) {_price(avg, symbol)}"
                f" · 총 매수금액 {_amount(cost, symbol)} ({pos.qty:g}주)"
            )
            for sid, lot in active_lots.items():
                lot_qty = lot.get("qty", 0.0)
                lot_avg = lot.get("avg_cost", avg)
                lot_entry = lot.get("entry", lot_avg)
                lot_stop = lot.get("stop")
                lot_target = lot.get("target")
                line = f"   🧠 {_strategy_label(sid)} · {lot_qty:g}주 @ {_price(lot_avg, symbol)}"
                if lot_stop:
                    gap = (lot_stop / lot_entry - 1) * 100 if lot_entry else 0.0
                    line += f" · 🛡 {_price(lot_stop, symbol)} ({gap:+.2f}%)"
                if lot_target:
                    line += f" · 🎯 {_price(lot_target, symbol)}"
                lines.append(line)
            entry, stop, target = avg, None, None
        else:
            sid = next(iter(active_lots), None)
            lot = active_lots.get(sid, {}) if sid else {}
            if not lot and "lots" not in pos.meta:
                # lots 구조 자체가 아직 없음(순수 레거시 — 예: 재시작 직후 어떤
                # 전략의 on_cycle도 아직 한 번도 안 돈 시점) — 평평한 meta를 그대로
                # 읽는다(리포팅은 상태를 바꾸지 않으므로 여기서 이행시키지 않는다).
                entry = pos.meta.get("entry", avg)  # R 계산용 리스크 프레임(손절 기준점)
                stop, target = pos.meta.get("stop"), pos.meta.get("target")
                sid = pos.meta.get("strategy")
            else:
                entry = lot.get("entry", avg)  # R 계산용 리스크 프레임(손절 기준점)
                stop, target = lot.get("stop"), lot.get("target")
            lines.append(f"   🧠 진입 전략 {_strategy_label(sid)}")
            lines.append(
                f"   💵 평단가 {_price(avg, symbol)}"
                f" · 총 매수금액 {_amount(cost, symbol)} ({pos.qty:g}주)"
            )

        if cur is None:
            # marks 는 사이클이 만든 quote 캐시다 — 장이 닫혀 있으면 애초에 조회를
            # 하지 않으므로 비어 있는 게 정상이다. 그걸 "조회 실패"라고 쓰면 장애처럼
            # 읽힌다(2026-08-19 소유자 지적).
            try:
                closed = not ctx.clock.is_market_open(market_of_symbol(symbol))
            except Exception:  # noqa: BLE001 — 캘린더 미지원 시장 등, 판단 불가면 기존 문구
                closed = False
            if closed:
                lines.append("   📈 장 마감 — 현재가 없음 (수익률은 다음 개장에)")
            else:
                lines.append("   📈 현재가 조회 실패 · 수익률 확인 불가")
        else:
            profit = (cur - avg) * pos.qty
            profit_pct = (cur / avg - 1) * 100 if avg else 0.0
            mark = "🔺" if profit > 0 else ("🔻" if profit < 0 else "➖")
            lines.append(
                f"   📈 현재가 {_price(cur, symbol)}"
                f" · 수익률 {mark} {profit_pct:+.2f}% ({_signed_amount(profit, symbol)})"
            )

        if len(active_lots) >= 2:
            # 랏별 목표가/손절가는 위에서 전략별 줄에 이미 보여줬다 — 합산 기준으로
            # 다시 중복 표시하지 않는다(회귀 없음 조건은 lot 1개 이하일 때만).
            if pos.opened_at is not None:
                try:
                    held = int((now - pos.opened_at).total_seconds() // 60)
                    hours, minutes = divmod(held, 60)
                    held_text = f"{hours}시간 {minutes}분" if hours else f"{minutes}분"
                    lines.append(f"   ⏱ 보유 {held_text}")
                except (TypeError, ValueError):
                    pass
        else:
            if target:
                lines.append(f"   🎯 목표가 {_price(target, symbol)}")
            elif _is_overnight(sid):
                lines.append("   🎯 목표가 없음 (오버나이트 보유 — 전략 청산 규칙에 따름)")
            elif sid:
                lines.append("   🎯 목표가 없음 (장 마감까지 보유)")
            else:
                # 소유 전략을 모르면 보유 기한을 단정하지 않는다.
                lines.append("   🎯 목표가 없음")

            rail: list[str] = []
            if stop:
                gap = (stop / entry - 1) * 100 if entry else 0.0
                rail.append(f"🛡 손절가 {_price(stop, symbol)} ({gap:+.2f}%)")
            if pos.opened_at is not None:
                try:
                    held = int((now - pos.opened_at).total_seconds() // 60)
                    hours, minutes = divmod(held, 60)
                    held_text = f"{hours}시간 {minutes}분" if hours else f"{minutes}분"
                    rail.append(f"⏱ 보유 {held_text}")
                except (TypeError, ValueError):
                    pass
            lines.append("   " + " · ".join(rail))
        blocks.append("\n".join(lines))

    # 금일 손익 푸터 — 오늘 시작 자산(리스크 매니저가 KST 거래일 단위로 고정) 대비
    # 지금 이 순간의 MTM 자산. 시작값이 아직 없으면(오늘 첫 승인 전) 조용히 생략.
    if risk is not None:
        try:
            snapshot = _breaker_snapshot(risk, _safe_now(ctx)) or {}
            day_start = snapshot.get("daily_loss_limit_pct", {}).get("day_start_equity")
            equity_now = _estimate_equity(ctx)
            if day_start and equity_now is not None:
                day_pnl = equity_now - day_start
                day_pct = day_pnl / day_start * 100
                mark = "🔺" if day_pnl > 0 else ("🔻" if day_pnl < 0 else "➖")
                blocks.append(f"📈 금일 손익 {mark} {day_pnl:+,.0f}원 ({day_pct:+.2f}%)")
        except Exception:  # noqa: BLE001 — 푸터 실패가 리포트를 죽이면 안 된다
            pass

    return "\n\n".join(blocks)


def _build_exposure_snapshot(
    ctx: Context, marks: dict[str, float],
) -> tuple[dict[str, dict[str, float]], dict[str, float], float | None]:
    """전략 간 합산 노출 감시(2026-08-30)에 넘길 스냅샷 —
    `(symbol -> {strategy_id: qty}, symbol -> 현재가, 계좌 총자본원)` 3튜플.

    `quant.control.exposure`(순수 계산)를 이 파일이 직접 임포트할 수 없으므로
    (아키텍처 규칙: `quant.trade → quant.control` 금지) 여기서는 재료만 눌러
    만들고, 실제 계산은 `run_paper_loop(exposure_check=...)`로 주입된 클로저
    (조립 시점에 `quant.apps.assembly`가 만든다)가 담당한다.

    시세는 이 사이클이 이미 만든 marks를 그대로 쓴다(추가 조회 없음) — 없으면
    평단가로 저하한다(`_position_report_text`와 동일 원칙). 전략 소유가 안
    잡힌 레거시 포지션(`meta["lots"]` 구조 이전)은 "?"로 담아 그래도 노출에
    넣는다 — 모르는 전략이라고 노출 자체를 숨기면 안 된다.

    총자본은 `portfolio.cash`를 노출하는 브로커(PaperBroker)에서만 계산한다
    (`_position_report_text`의 헤더 잔고 계산과 동일) — 없으면(실거래 브로커)
    `None`을 돌려주고, 호출부는 레버리지 비율(pct) 판단을 보류한다."""
    positions = {s: p for s, p in ctx.broker.positions().items() if p.is_open}
    lots: dict[str, dict[str, float]] = {}
    prices: dict[str, float] = {}
    for symbol, pos in positions.items():
        active = {
            sid: float(lot.get("qty", 0.0))
            for sid, lot in (pos.meta.get("lots") or {}).items()
            if float(lot.get("qty", 0.0)) > 0
        }
        if not active:
            sid = pos.meta.get("strategy") or "?"
            active = {sid: pos.qty}
        lots[symbol] = active
        price = marks.get(symbol)
        prices[symbol] = price if price is not None else pos.avg_cost

    portfolio = getattr(ctx.broker, "portfolio", None)
    capital_krw: float | None = None
    if portfolio is not None and hasattr(portfolio, "cash"):
        fx = getattr(ctx.broker, "fx", None)
        invested = 0.0
        for symbol, pos in positions.items():
            try:
                invested += to_krw(prices[symbol] * pos.qty, market_of_symbol(symbol), fx)
            except Exception:  # noqa: BLE001 — 환산 실패가 리포트를 죽이면 안 된다
                invested += prices[symbol] * pos.qty
        capital_krw = portfolio.cash + invested

    return lots, prices, capital_krw


def _write_heartbeat_file(path: Path, payload: dict) -> bool:
    """하트비트 상태 파일을 원자적으로 쓴다(control.py/Portfolio와 같은
    tmp-write-then-replace). 성공 여부를 bool로 돌려주고 **예외를 올리지 않는다**.

    이 파일이 존재하는 이유: 텔레그램 하트비트만으로는 "엔진이 멈춤"과 "알림이 안 옴"이
    구분되지 않는다. 크래시는 systemd Restart가 잡지만 행(hang)은 못 잡는다. 외부
    워치독이 이 파일의 ts만 보면 엔진이 실제로 사이클을 돌고 있는지 알 수 있다.
    반대로 이 파일을 못 쓴다고 거래가 멈춰선 안 된다 — 그래서 전부 삼킨다."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:
        return False


def _safe_now(ctx: Context):
    """표시용 현재 시각. 시계가 이상해도 리포트 한 줄 때문에 알림을 잃지 않는다."""
    try:
        return ctx.clock.now()
    except Exception:  # noqa: BLE001
        return None


def _breaker_snapshot(risk: RiskManager, now=None) -> dict | None:
    """risk.breaker_state() 스냅샷. Protocol에 없는 부가 메서드라 duck-typing으로 있을 때만
    쓰고, 실패하면 None — 요약 한 줄 때문에 마감 알림 전체를 잃지 않는다.

    `now` 를 넘기는 이유: 롤오버는 approve() 안에서만 일어나므로 오늘 신호가 아직
    없으면 **어제 카운트가 그대로 남아 있다.** 그걸 "오늘 주문 135건 — 한도 도달"로
    표시해 사용자가 진입이 막힌 줄 알았다(2026-08-14). now 를 주면 표시만 바로잡는다.
    구현이 now 를 안 받는 구버전일 수 있으므로 실패하면 인자 없이 한 번 더 부른다."""
    state_fn = getattr(risk, "breaker_state", None)
    if not callable(state_fn):
        return None
    try:
        return state_fn(now) if now is not None else state_fn()
    except TypeError:
        try:
            return state_fn()
        except Exception:
            return None
    except Exception:
        return None


def _breaker_line(state: dict) -> str | None:
    try:
        orders = state["max_orders_per_day"]
        loss = state["daily_loss_limit_pct"]
        cooldown = state["cooldown_bars_after_stop"]["symbols_in_cooldown"]
        cooldown_text = ", ".join(_display(s) for s in cooldown) if cooldown else "없음"
        # per_strategy 모드에서는 "어느 전략이" 막혔는지까지 보여준다 — 한도 도달
        # 사실만 알려주고 대상을 숨기면 운영자가 확인할 곳이 없다(2026-09-02 C2).
        tripped_ids = [
            sid for sid, v in (loss.get("per_strategy") or {}).items() if v["tripped"]
        ]
        loss_text = " — ⛔ 도달, 신규 진입 중단" if loss["tripped"] else " (아직 여유 있음)"
        if tripped_ids:
            loss_text += f" [{', '.join(_strategy_label(s) for s in tripped_ids)}]"
        return (
            f"🚦 안전장치\n"
            f"   • 오늘 진입 {_entry_budget_text(orders)}\n"
            f"   • 하루 손실 한도 -{loss['limit_pct']:.1f}%"
            f"{loss_text}\n"
            f"   • 손절 후 재진입 대기: {cooldown_text}"
        )
    except Exception:
        return None


def _open_position_owner_tag(pos) -> str:
    """열린 포지션 하나의 '소유 전략·보유기한' 태그 — "왜 안 팔았지?"에 메시지 안에서
    답하기 위함(2026-09 소유자 요청). `_position_report_text`와 같은 lots 파싱(레거시
    플랫 meta 폴백 포함)을 재사용하고, 보유기한 판정은 `_is_overnight`(기존
    `_OVERNIGHT_STRATEGIES` 목록)을 그대로 쓴다 — 새 목록을 만들지 않는다."""
    lots = pos.meta.get("lots") or {}
    active = {sid: lot for sid, lot in lots.items() if lot.get("qty", 0.0) > 0}
    if not active and "lots" not in pos.meta:
        sid = pos.meta.get("strategy")
        active = {sid: {}} if sid else {}
    if not active:
        return "전략 미상"
    return "+".join(
        f"{sid}·{'오버나이트' if _is_overnight(sid) else '단타'}" for sid in active
    )


def _session_summary_text(
    market: str,
    ctx: Context,
    risk: RiskManager,
    control: TradingControl,
    market_data: object | None,
    tally: _SessionTallySink,
    strategies: list[Strategy] | None = None,
) -> str:
    """세션 마감 성적표. 하루에 시장당 1회만 나가는 메시지라 하트비트보다 정보를 더 담는다."""
    positions = ctx.broker.positions()
    open_positions = {sym: pos for sym, pos in positions.items() if pos.is_open}

    breaker = _breaker_snapshot(risk, _safe_now(ctx))

    lines = [
        f"🔔 세션 마감 · {_MARKET_LABELS.get(market, market)}",
        "",
        f"{'⛔ 거래 상태: 중단됨' if control.is_halted() else '✅ 거래 상태: 정상'}",
    ]

    equity = _estimate_equity(ctx)
    if equity is not None:
        lines.append(f"💰 추정 자산: {equity:,.0f}원")

    loss_state = (breaker or {}).get("daily_loss_limit_pct", {})
    day_pnl = loss_state.get("day_pnl_pct")
    if day_pnl is not None:
        mark = "🔺" if day_pnl > 0 else ("🔻" if day_pnl < 0 else "➖")
        # per_strategy 모드의 day_pnl_pct 는 계좌 전체가 아니라 **가장 나쁜 전략**의
        # 값이다(2026-09-02 C2). 라벨을 붙이지 않으면 계좌 손익으로 오독된다.
        label = "오늘 손익(최악 전략)" if loss_state.get("scope") == "per_strategy" else "오늘 손익"
        lines.append(f"📊 {label}: {mark} {day_pnl:+.2f}%")

    # 비용 회계 한 줄 — 체결 수·수수료에 거래대금 대비 bp를 붙인다. 체결 0건이어도
    # 반드시 찍는다("오늘 아무것도 안 했다"는 것 자체가 매일 확인해야 할 신호다).
    bp_text = f" ({tally.fee_krw / tally.notional_krw * 1e4:.1f}bp)" if tally.notional_krw > 0 else ""
    lines.append(f"🧾 체결 {tally.fills}건 · 수수료 합계 {tally.fee_krw:,.0f}원{bp_text}")

    # 전략별 세션 성적 — 순손익(수수료 차감) 기준. 그 세션에 종결된 트립이 있었던
    # 전략만 보인다(소유자 지시: 거래 없는 전략까지 나열하면 소음).
    for sid in sorted(tally.strategy_stats):
        st = tally.strategy_stats[sid]
        if st["trips"] == 0:
            # 종결된 트립이 전부 손익미상(브로커가 realized_pnl을 모름) — 0으로
            # 위장하지 않고 건수만 알린다.
            lines.append(f"🧠 {_strategy_label(sid)} · 손익미상 {st['unknown']}건 (집계 제외)")
            continue
        unk = f" (손익미상 {st['unknown']}건 제외)" if st["unknown"] else ""
        lines.append(
            f"🧠 {_strategy_label(sid)} · {st['trips']}건 · {st['wins']}승 {st['losses']}패"
            f" · 순손익 {_signed_amount_market(st['net_pnl'], market)}{unk}"
        )

    # 미청산 포지션 — 소유 전략과 보유기한(오버나이트/단타)을 붙인다.
    held_text = (
        ", ".join(
            f"{_display(sym)} {pos.qty:g}주({_open_position_owner_tag(pos)})"
            for sym, pos in open_positions.items()
        )
        if open_positions else "없음 (전량 청산됨)"
    )
    lines.append(f"📦 보유 종목: {held_text}")

    if breaker is not None:
        breaker_line = _breaker_line(breaker)
        if breaker_line is not None:
            lines.append(breaker_line)

    health_fn = getattr(market_data, "health", None)
    if callable(health_fn):
        try:
            lines.append(f"📡 시세 연결: {'⚠️ 저하됨' if health_fn().degraded else '정상'}")
        except Exception:
            pass

    # 배관 점검 한 줄 — 워크플로우가 정상이었는지. 전부 엔진이 이미 들고 있는
    # 상태만 쓴다(거래 평면에서 파일을 새로 뒤지거나 네트워크를 타지 않는다).
    universe_n = len({sym for s in (strategies or []) for sym in getattr(s, "symbols", [])})
    lines.append(
        f"⚙️ 배관 점검: 유니버스 {universe_n}종목 · 사이클 {tally.cycles}회"
        f" · 에러 {tally.error_cycles}건 · 정지 {'발생' if tally.halted_seen else '없음'}"
        f" · 시세 폴백 {'발생' if tally.degraded_seen else '없음'}"
    )

    return "\n".join(lines)


def _market_states(ctx: Context, active_markets: frozenset[str] | None) -> dict[str, bool] | None:
    """active_markets의 각 시장별 개장 여부. None이면 "항상 장중" 취급(기존 동작).

    시장을 하나로 뭉치지 않고 개별로 들고 있는 이유: KR 마감(15:30 KST)과 US
    마감(05:00 KST)은 다른 사건이고, any()로 뭉치면 KR 마감이 US 세션에 가려 사라진다."""
    if active_markets is None:
        return None
    return {m: ctx.clock.is_market_open(m) for m in sorted(active_markets)}


def _kst_today() -> str:
    """국면 refresh의 거래일 경계. 세션 캘린더가 아니라 KST 달력일 기준인 이유:
    KR(09:00~15:30)과 US(22:30~05:00 KST) 세션을 모두 돌리는 프로세스에서 "하루
    1회"의 기준이 시장마다 다르면 refresh가 이틀에 걸쳐 두 번 튄다. KST 자정
    경계면 두 세션 모두 시작 전에 한 번씩만 갱신된다(RegimeProvider 캐시도 같은
    KST 기준이라 이중 계산은 캐시가 흡수한다)."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()


def _universe_roll_bucket(now: datetime | None = None) -> str:
    """유니버스 리로드 경계: KST 자정 + **08:27(KR 동시호가 직전)** +
    **14:00(KR 오후 마감 포지션 리포트 흡수)** + **22:10(US 개장 직전)**.

    각 경계는 "그 시장의 자동 편입이 그날 첫 세션부터 반영되게" 하는 것이 목적이다 —
    시장마다 편입 크론과 개장 시각이 다르므로 경계도 시장마다 하나씩 필요하다.

    - **14:53 (KR, 2026-08-25 종가배팅 체인)**: 장중 리포트(14:10 빌드 → 완료 즉시
      발행, `KR_close_engine.json`)의 종가배팅 후보(CLOSE_BET)가 14:52
      `own_brief.sh KR close` → 14:52 데드라인 직전 등록 → 이 경계로 흡수돼
      close_bet 의 진입 창(15:15~15:19)에 매매 대상이 된다(2026-08-26: 연속
      거래 마지막 구간으로 당겼다 — 15:20 부터는 동시호가라 '현재가'로 체결할 수
      없다, quant/trade/strategy/close_bet.py 참고). 원래 14:00 경계
      (13:50 리포트 → 14:00~15:30 마감 구간 매매)였는데, 그 소비자(intraday_scan
      마감 매매)가 전략 4종 체제에서 비활성이 되고 리포트가 종가배팅용 14:50
      발행으로 옮겨가며 경계도 함께 왔다.
    - **08:27 (KR)**: 2026-08-17 사용자 세션 표에 맞춰 체인 전체를 전진했다 —
      리포트 08:00 발행 → `watch-reset KR` 08:05 → `own_brief.sh`(자체 리포트 →
      확신도 엔진 → 자동 등록) 08:12 → 이 경계 08:27 → **08:30 동시호가**부터
      자동 편입분이 반영된다. 이전에는 08:40 브리핑 → 08:57 롤 → 09:00 개장이었는데,
      그러면 08:30~09:00 동시호가 구간(호가가 쌓여 개장가를 형성하는 창)에는
      전날 유니버스로 진입해야 했다 — 그날 새로 편입된 종목이 동시호가에 못 끼는
      비대칭이었다. 2026-08-13 회사 리포트 의존을 끊기 전에는 이 자리가 LLM 을
      타는 데일리 브리핑이었고 타임아웃 600s 를 감안해야 했다 — 지금 이 경로에
      LLM 은 없고 결정론적이라 여유가 더 크다(08:12 own_brief → 08:27 롤까지 15분).
      `orb_scan`(개장 5분 돌파)·`intraday_scan`(개장+30분~) 은 그대로 09:00
      정규장 개장을 기준으로 창을 잡으므로 이 경계가 더 이르게 지난다고 로직이
      바뀌지 않는다 — 유니버스가 더 일찍 반영될 뿐이다.
    - **22:10 (US)**: 21:50 `own_brief.sh US`(랭킹 발굴 → 확신도 엔진 → 자동
      등록)가 끝난 뒤이자 US 개장 전. 서머타임 무관하게 안전하다 — US 개장은
      22:30 KST(EDT) 또는 23:30 KST(EST)라 22:10은 두 체계 모두에서 개장 전이다.
      이 경계가 없으면 21:50에 편입된 US 종목이 **자정 롤에서야** 흡수돼, US 세션의
      앞 1.5~2.5시간(개장 5분 돌파 창을 포함!)을 통째로 놓친다 — KR이 08:27 경계
      덕분에 동시호가부터 반영되는 것과 대비되는 비대칭이었다(2026-08-11 사용자 지적:
      "한국장이랑 미국장 사이에 미국 종목 auto add 될 수 있게").

    - **장중 30분 경계 (2026-08-28 소유자 지시)**: "아침에 못 잡은 종목이라도
      장중에 거래대금이 쏠리면 발굴해 편입하고 시그널을 감시한다" — flow-scan
      크론(30분)이 확신도 게이트 통과분을 워치리스트에 넣으면, 세션 중 30분
      경계가 그것을 흡수한다(KR 09:30~14:30, US 23:00~04:30 KST). 발견→감시
      시작까지 최대 ~1시간. "장중 스냅샷 안정" 원칙은 이 지시로 개정됐다 —
      안전 근거는 14:53 장중 롤 선례와 동일하다: `_roll_universe`가 심볼 동일
      전략 인스턴스를 재사용하므로 패턴 사용 플래그·랏 상태가 흔들리지 않고,
      워치리스트는 장중에 늘기만 한다(리셋은 08:05/21:40 세션 밖). 한계
      정직히: 키움 웹소켓 구독은 기동 시 목록 고정이라 장중 편입 종목의
      시세는 다음 재시작까지 Toss REST 폴백으로 흐른다(느리지만 동작).

    자정 경계가 US 세션을 가로지르는 문제는 위와 같은 인스턴스 재사용으로 흡수한다.
    """
    from zoneinfo import ZoneInfo
    now = now or datetime.now(ZoneInfo("Asia/Seoul"))
    hm = (now.hour, now.minute)
    passed = [b for b in _ROLL_BOUNDARIES if hm >= b]
    suffix = "+%02d%02d" % passed[-1] if passed else ""
    return now.date().isoformat() + suffix


# 체인 경계 3개(08:27/14:53/22:10 — 각 사유는 _universe_roll_bucket docstring) +
# 장중 30분 경계(2026-08-28). (0,0)은 넣지 않는다 — 자정은 날짜 문자열 변경이
# 이미 경계다. 15:00~15:19 는 close_bet 진입 창이라 경계를 두지 않는다(14:53 이
# 마지막 KR 경계). US 는 EDT 개장(22:30) 기준 23:00 부터 — EST(23:30 개장)면
# 23:00 경계가 개장 전 한 번 더 도는 것뿐이라 무해하다.
_ROLL_BOUNDARIES: tuple[tuple[int, int], ...] = tuple(sorted(
    [(8, 27), (14, 53), (22, 10), (9, 30), (23, 0), (23, 30)]
    + [(h, m) for h in range(10, 15) for m in (0, 30)]      # KR 10:00~14:30
    + [(0, 30)] + [(h, m) for h in range(1, 5) for m in (0, 30)]  # US 00:30~04:30
))


def _refresh_symbol_names(universe: object) -> int:
    """유니버스(및 그 하위 소스)가 아는 표시명을 `_SYMBOL_NAMES`에 병합한다.

    `CompositeUniverse`는 `names()`를 갖지 않으므로 하위 소스까지 훑는다. 이름을
    제공하지 않는 소스(StaticUniverse/TossRankingUniverse)는 조용히 건너뛴다 —
    이름표가 없다고 거래가 멈출 이유는 없다. 반환값은 새로 알게 된 이름 수(로그용).
    """
    sources = [universe]
    inner = getattr(universe, "_sources", None)
    if isinstance(inner, list):
        sources.extend(inner)
    added = 0
    for src in sources:
        names_fn = getattr(src, "names", None)
        if not callable(names_fn):
            continue
        try:
            for sym, name in (names_fn() or {}).items():
                if name and _SYMBOL_NAMES.get(sym) != name:
                    _SYMBOL_NAMES[sym] = name
                    added += 1
        except Exception:  # noqa: BLE001 — 이름표 갱신 실패가 세션 롤을 막으면 안 된다
            logger.warning("표시명 갱신 실패(무시하고 계속): %s", type(src).__name__)
    if added:
        logger.info("표시명 %d건 갱신", added)
    return added


def _carry_session_state(old: object, fresh: object) -> None:
    """전략 교체 시 이전 인스턴스의 당일 상태를 새 인스턴스로 이월한다.

    규칙은 하나다: **이전 값이 비어 있지 않고, 새 인스턴스의 같은 이름 컨테이너
    (dict/set)가 아직 비어 있으면 이월한다.** 전략별 필드명을 여기 나열하지 않는
    이유는 결합 때문이다 — 전략이 상태 필드를 추가할 때마다 loop.py 를 같이
    고쳐야 하는 목록은 반드시 낡는다. "빈 컨테이너만 채운다"는 조건이 안전핀:
    새 인스턴스가 생성자에서 이미 채운 값은 설계된 초기 상태라 덮지 않고,
    dict/set 이 아닌 것(파라미터·파생 상수·주입 객체)은 건드리지 않는다.

    `symbols`/`tags_of`/params 는 이월하지 않는다 — 그건 새 인스턴스가 옳다
    (핫 리로드가 세션 롤에 반영된다는 기존 계약, cli._rebuild docstring)."""
    for name, old_val in vars(old).items():
        if not isinstance(old_val, (dict, set)) or not old_val:
            continue
        fresh_val = getattr(fresh, name, None)
        if type(fresh_val) is type(old_val) and not fresh_val:
            fresh_val.update(old_val)


def _roll_universe(
    universe: object,
    rebuild_strategies: Callable[[], list[Strategy]] | None,
    strategies: list[Strategy],
) -> list[Strategy]:
    """세션 롤: 유니버스를 다시 읽고 전략을 재조립한다. **항상 쓸 수 있는 전략 목록을
    돌려준다** — 어느 단계가 실패하든 기존 전략을 그대로 반환한다(거래가 멈추면 안 된다).

    심볼 목록이 그대로인 전략은 **기존 인스턴스를 유지**한다. KST 자정 경계는 US
    세션(22:30~05:00 KST) 한가운데라, 매일 전 전략을 새로 만들면 orb의
    `_entered_today` 같은 인메모리 세션 상태가 장중에 리셋되어 하루 1회 진입 제한이
    풀린다. 심볼이 실제로 바뀐 전략만 교체하므로, 유니버스를 소비하지 않는 고정 심볼
    쌍 전략들은 이 경로에서 아무 영향도 받지 않는다.
    """
    try:
        universe.refresh()
        symbols = list(universe.symbols())
    except Exception as e:
        logger.warning("유니버스 갱신 실패 — 기존 전략 유지: %s: %s", type(e).__name__, e)
        return strategies
    logger.info("유니버스 스냅샷 %d종목: %s", len(symbols), ", ".join(symbols) or "없음")

    # 표시명 이름표 갱신 — 부팅 시점 스냅샷만 쓰면 **그 뒤 편입된 종목이 코드로만
    # 표시된다**(2026-08-12 실측: 02:50 부팅, 08:41 자동 편입된 192820이 "192820"으로
    # 표시). 이름은 브리지가 /watch 시점에 이미 파일에 적어둔 값이라 네트워크 조회
    # 없이 읽어 쓴다. `market_of` 스테일과 같은 부류의 버그이고 같은 자리에서 고친다.
    _refresh_symbol_names(universe)

    if rebuild_strategies is None:
        return strategies
    try:
        rebuilt = rebuild_strategies()
    except Exception as e:
        logger.warning("전략 재조립 실패 — 기존 전략 유지: %s: %s", type(e).__name__, e)
        return strategies
    if not rebuilt:
        logger.warning("전략 재조립 결과가 비어 있음 — 기존 전략 유지")
        return strategies
    previous = {getattr(s, "id", None): s for s in strategies}
    merged: list[Strategy] = []
    for fresh in rebuilt:
        old = previous.get(getattr(fresh, "id", None))
        same_symbols = old is not None and list(getattr(old, "symbols", [])) == list(
            getattr(fresh, "symbols", [])
        )
        if old is not None and not same_symbols:
            # 심볼이 바뀌어 교체하는 경우: 새 인스턴스(핫 리로드된 params·tags_of)가
            # 이기되, 이전 인스턴스의 **당일 상태**는 이월한다. 유니버스는 하루 3번
            # (자정·08:27·22:10) + /watch 추가마다 리로드되는데, 관심종목이 하나만
            # 바뀌어도 watchlist 소비 전략 전부가 새로 만들어져 인메모리 상태가
            # 리셋됐다 — frgn_accumulate 의 `_evaluated_date`(오늘 이미 평가함)가
            # 지워지면 같은 날 같은 종목을 다시 산다(2026-08-21 결함).
            _carry_session_state(old, fresh)
        merged.append(old if same_symbols else fresh)
    logger.info("활성 전략/심볼: %s", {getattr(s, "id", "?"): list(getattr(s, "symbols", [])) for s in merged})
    return merged


async def run_paper_loop(
    strategies: list[Strategy],
    ctx: Context,
    risk: RiskManager,
    sinks: EventSink,
    settings: EngineSettings,
    notifier: Notifier | None = None,
    control: TradingControl | None = None,
    market_data: object | None = None,
    active_markets: frozenset[str] | None = None,
    approval: ApprovalGate | None = None,
    approval_notifier: object | None = None,
    approval_cfg: dict | None = None,
    reconciler: object | None = None,
    regime: object | None = None,
    universe: object | None = None,
    rebuild_strategies: Callable[[], list[Strategy]] | None = None,
    heartbeat_path: Path | str | None = None,
    name_of: dict[str, str] | None = None,
    books: StrategyBooks | None = None,
    tick_logger: TickLogger | None = None,
    exposure_check: Callable[[dict, dict, float | None], dict] | None = None,
) -> None:
    """paper 루프. control이 None이면 기본 경로(data/state/control.json)로 새로 만든다.
    regime(RegimeProvider)을 주면 KST 거래일이 바뀔 때 1회 refresh()로 국면을
    재계산하고(네트워크 I/O — 사이클 밖 경계에서만), 사이클마다 캐시값
    risk_multiplier()를 읽어 사이징에 전달한다. None이면 항상 1.0(중립).
    market_data는 health()를 노출하는 소스(보통 MarketDataService)면 스테일 가드/
    하트비트에 쓰인다 — 없으면 duck-typing으로 조용히 생략된다. active_markets는
    하트비트/스테일 가드를 "장중에만" 동작시키기 위한 기준 시장 집합(None이면 항상 장중
    취급). approval이 None이면 승인 게이트 관련 코드는 전혀 실행되지 않는다.
    reconciler(app/reconcile.Reconciler)를 주면 기동 시 1회 + 자체 주기마다 브로커
    실보유와 엔진 소유 원장을 대조한다 — None이면 대사 코드는 실행되지 않는다.
    universe(refresh()/symbols()를 노출하는 객체)를 주면 국면과 **같은 KST 거래일
    경계**에서 1회 다시 읽고, rebuild_strategies가 함께 주어졌으면 전략을 재조립한다.
    둘 다 None이면 관련 코드는 한 줄도 실행되지 않는다(기존 경로 그대로).
    heartbeat_path(None이면 DEFAULT_HEARTBEAT_PATH)에는 매 사이클(성공/실패 무관)
    외부 워치독용 상태 파일을 남긴다 — 쓰기 실패는 삼키고 경고 1회만 남긴다(파일이
    없어도 거래는 계속돼야 한다). books(전략별 독립 명목계정)를 주면 체결마다 그
    전략의 장부를 갱신·저장한다 — None이면(capital_mode: shared 기본값) 관련 코드는
    한 줄도 실행되지 않는다. tick_logger(TickLogger, 2026-08-28)를 주면 사이클마다
    워치리스트 전 심볼의 시세를 기록하고 사이클 끝에 flush_if_due를 호출한다 —
    None이면(호출부가 주입하지 않음) 관련 코드는 한 줄도 실행되지 않는다.
    engine.tick_log.enabled: false는 다른 경로다 — assembly.py가 TickLogger는
    항상 주입하되 그 인스턴스의 enabled 플래그를 끈다(record/flush가 즉시 반환).
    exposure_check(2026-08-30)를 주면 포지션 리포트와 같은 주기로 전략 간 합산
    노출(중복 보유/레버리지 가중 노출/상쇄 쌍)을 계산해 평시엔 로그만, 임계
    초과·상쇄 쌍 존재 시엔 쿨다운을 두고 텔레그램 1회 보낸다 — 자동 차단은
    하지 않는다(가시화+경고가 목적). 이 파일은 `quant.control.exposure`(순수
    계산)를 직접 임포트할 수 없어(아키텍처 규칙) 클로저로 주입받는다: 시그니처는
    `(lots, prices, capital_krw) -> dict`이고 `quant.apps.assembly`가 조립한다.
    None이면(호출부가 주입하지 않음) 관련 코드는 한 줄도 실행되지 않는다."""
    if control is None:
        control = TradingControl()
    if reconciler is not None:
        # 기동 대사는 첫 사이클보다 먼저. 재시작 직후가 원장과 실보유가 가장 어긋나기
        # 쉬운 시점이고, 그 상태로 신규 진입을 내보내면 안 된다.
        reconciler.check(force=True)

    engine_cfg = settings.raw.get("engine", {})
    max_failures = int(engine_cfg.get("max_consecutive_cycle_failures", _DEFAULT_MAX_CONSECUTIVE_FAILURES))
    failure_cooldown_sec = float(
        engine_cfg.get("cycle_failure_alert_cooldown_minutes", _DEFAULT_CYCLE_FAILURE_COOLDOWN_MINUTES)
    ) * 60
    max_stale = int(engine_cfg.get("max_consecutive_stale_data", _DEFAULT_MAX_CONSECUTIVE_STALE))
    heartbeat_sec = float(engine_cfg.get("heartbeat_minutes", _DEFAULT_HEARTBEAT_MINUTES)) * 60
    # 텔레그램 소음 다이어트(2026-08-25 소유자 지시: "보기가 어지러울 정도로 길어").
    # 주기 발송(엔진 하트비트·포지션 현황)은 기본 **off** — 채팅에는 리포트 발행과
    # 체결(무슨 전략인지)만 남긴다. 현재가·보유상태가 궁금하면 /status /balance
    # (tg_bridge, 온디맨드)로 물어본다. 하트비트 **파일**(외부 워치독용)과 로그는
    # 이 스위치와 무관하게 계속 쓴다 — 끄는 건 "채팅으로의 발송"뿐이다.
    telegram_heartbeat = bool(engine_cfg.get("telegram_heartbeat", False))
    telegram_position_report = bool(engine_cfg.get("telegram_position_report", False))
    slow_cycle_warn_ms = float(engine_cfg.get("slow_cycle_warn_ms", _DEFAULT_SLOW_CYCLE_WARN_MS))
    # 전략 간 합산 노출 감시(2026-08-30) — 계산 자체는 position_report_sec와 같은
    # 주기로 하되(위 telegram_position_report 스위치와는 무관하게 항상), 알림
    # 발송만 별도 쿨다운을 둔다.
    exposure_alert_cooldown_sec = float(
        engine_cfg.get("exposure_alert_cooldown_minutes", _DEFAULT_EXPOSURE_ALERT_COOLDOWN_MINUTES)
    ) * 60

    # 워치 조건(알림 전용, quant/trade/watch_conditions.py) — settings.yaml
    # watch_conditions 블록. 미지 metric/op는 parse_watch_rules가 여기서(부팅 시)
    # ValueError로 거부한다 — 오타가 조용히 무시되지 않는다.
    watch_cfg = settings.raw.get("watch_conditions", {})
    watch_rules: list[WatchRule] = (
        parse_watch_rules(watch_cfg.get("rules", [])) if watch_cfg.get("enabled", False) else []
    )
    watch_cooldown_sec = float(watch_cfg.get("cooldown_minutes", 60)) * 60
    watch_last_hit_mono: dict[str, float] = {}  # rule name -> 마지막 알림 시각(monotonic)

    cycle_count = 0
    loop_started_mono = time.monotonic()  # 하트비트 가동시간 표시용(재시작 감지)
    consecutive_failures = 0
    consecutive_stale = 0
    stale_alerted = False
    last_failure_alert = float("-inf")
    last_heartbeat = float("-inf")
    position_report_sec = float(engine_cfg.get("position_report_minutes", 1)) * 60
    last_position_report = float("-inf")
    last_exposure_check = float("-inf")  # 계산 자체(로그)의 cadence — position_report_sec 재사용
    last_exposure_alert = float("-inf")  # 알림(텔레그램) 발송의 별도 쿨다운
    meta_signature = ""  # 포지션 meta 영속화 트리거용 스냅샷
    # 표시명 이름표(코드 → "한화생명") — 텔레그램 메시지 전용, 거래 판단에 안 쓰인다.
    _SYMBOL_NAMES.update(name_of or {})
    cycle_latencies_ms: deque[float] = deque(maxlen=_CYCLE_LATENCY_HISTORY_SIZE)
    last_bar_cache_log = float("-inf")
    regime_day: str | None = None  # 마지막으로 refresh()한 KST 거래일
    universe_day: str | None = None  # 마지막 유니버스 리로드 경계 버킷(_universe_roll_bucket)
    heartbeat_path = Path(heartbeat_path or DEFAULT_HEARTBEAT_PATH)
    heartbeat_write_warned = False  # 쓰기 실패 경고는 1회만 — 매 사이클 스팸 금지
    # 보유 종목별 "시세를 못 받기 시작한 시각". 손절 판정이 조용히 멈춘 상태를
    # 감지하려는 것 — _build_marks_and_unpriced의 docstring 참고.
    unpriced_since: dict[str, float] = {}
    unpriced_alerted: set[str] = set()
    unpriced_alert_sec = float(engine_cfg.get("unpriced_position_alert_seconds", 60))
    # 고아 포지션(A-6): 이미 알림을 보낸 (symbol, strategy_id) — 같은 사이클 반복에도
    # 재알림하지 않고, 전략이 다시 켜지거나 청산되면(더 이상 orphan이 아니면) 제거해
    # 다음에 다시 orphan이 될 때 재알림한다.
    orphan_alerted: set[tuple[str, str]] = set()
    # 시장별 직전 개장 상태. 값이 없는 시장은 "아직 모름"이라 첫 사이클이 닫힘이어도
    # 마감으로 치지 않는다(기동 직후 유령 마감 요약 방지).
    prev_market_open: dict[str, bool] = {}
    # 체결 카운터/수수료는 sinks를 감싸서 센다 — 기존 sink 구성(MultiSink 등)을
    # 알 필요가 없고, run_cycle/_flatten_all/_process_approvals가 모두 이 래퍼를 쓴다.
    tally = _SessionTallySink(
        sinks,
        getattr(ctx.broker, "market_of", None) or {},
        getattr(ctx.broker, "fx", None),
    )
    sinks = tally

    while True:
        if settings.reload_if_changed():
            logger.info("settings.yaml 변경 감지 — 리로드")
        cycle_count += 1
        tally.cycles += 1  # 배관 점검용 — 세션 마감 요약이 "루프가 살아 있었나"의 증거로 쓴다
        if control.is_halted():
            tally.halted_seen = True
        # 콜드 페치 예산(service.py MarketDataService)은 사이클 단위 자원이라
        # 매 사이클 시작에 리셋한다 — 안 그러면 유니버스 롤 직후 몰린 캐시 미스가
        # 첫 사이클에 예산을 다 쓰고 남은 사이클에서도 계속 거부된다. market_data가
        # 이 메서드를 노출하지 않으면(스텁/테스트 더블) 조용히 건너뛴다.
        reset_budget_fn = getattr(market_data, "reset_cycle_budget", None)
        if callable(reset_budget_fn):
            reset_budget_fn()
        now_mono = time.monotonic()
        market_states = _market_states(ctx, active_markets)
        market_open = market_states is None or any(market_states.values())

        # 국면(regime): KST 거래일이 바뀌면 1회 재계산(네트워크 I/O — 여기는 사이클
        # "사이"의 경계이지 신호 판단/주문 경로가 아니다). 실패해도 provider가
        # 중립을 반환하므로 거래는 계속된다 — 단, 조용히 넘어가지 않고 알린다.
        risk_multiplier = 1.0
        mult_by_market: dict[str, float] | None = None
        if regime is not None:
            today_kst = _kst_today()
            if today_kst != regime_day:
                regime_day = today_kst
                try:
                    state = regime.refresh()
                    logger.info(
                        "국면 갱신: %s (배수 %.2f)%s — %s",
                        state.label, state.risk_multiplier,
                        " [판단불가→중립]" if state.degraded else "",
                        "; ".join(state.reasons) or "사유 없음",
                    )
                    if notifier is not None:
                        labels = {"defensive": "방어", "neutral": "중립", "aggressive": "공격"}
                        head = (
                            "⚠️ 시장 국면 판단 불가 — 중립으로 운행"
                            if state.degraded
                            else "🧭 오늘의 시장 국면"
                        )
                        parts = [
                            head,
                            "",
                            f"🇺🇸 미국 시장: {labels.get(state.label, state.label)}"
                            f" (투자 비중 {state.risk_multiplier:.2f}배)",
                        ]
                        kr = getattr(regime, "kr_state", lambda: None)()
                        if kr is not None:
                            parts.append(
                                f"🇰🇷 한국 시장: {labels.get(kr.label, kr.label)}"
                                f" (투자 비중 {kr.risk_multiplier:.2f}배)"
                            )
                        notifier.send("\n".join(parts))
                except Exception as e:
                    logger.warning("국면 refresh 실패 — 중립(1.0)으로 계속: %s: %s",
                                   type(e).__name__, e)
                    if notifier is not None:
                        notifier.send("⚠️ 시장 국면 계산 실패\n\n중립 배수(1.00배)로 운행합니다")
            risk_multiplier = regime.risk_multiplier()  # 캐시/메모리만 — 네트워크 없음
            # 시장별 배수(2026-08-10): KR 신호는 KR 국면으로 사이징한다. 구형/페이크
            # provider(무인자 시그니처)는 TypeError 폴백으로 기존 동작 유지.
            mult_by_market = {}
            for m in sorted(set(active_markets or ["US"])):
                try:
                    mult_by_market[m] = regime.risk_multiplier(m)
                except TypeError:
                    mult_by_market[m] = risk_multiplier

        # 유니버스: KST 자정 + 08:27(KR 동시호가 직전) 경계에서 다시 읽는다(디스크/네트워크
        # I/O — 사이클 "사이"의 경계이지 신호 판단 경로가 아니다). 세션 중에 관심종목이
        # 바뀌어도 그 세션의 유니버스는 흔들리지 않는다 — _universe_roll_bucket 참고.
        if universe is not None:
            bucket = _universe_roll_bucket()
            if bucket != universe_day:
                universe_day = bucket
                strategies = _roll_universe(universe, rebuild_strategies, strategies)

        timings = CycleTimings()
        # 보유 종목의 사이클 시세 스냅샷 — risk.approve()가 daily_loss_limit_pct 평가금액
        # (MTM)에 쓴다(quote 캐시를 타므로 사이클마다 다시 만들어도 네트워크 비용은
        # 캐시 만료 시에만 든다). run_cycle/_process_approvals가 만드는 approve() 호출
        # 전부가 이 스냅샷을 공유한다.
        marks, unpriced = _build_marks_and_unpriced(ctx)
        if tick_logger is not None:
            _record_ticks(ctx, tick_logger, strategies, marks)
        # 보유 중인데 시세가 없으면 손절/EoD 판정이 통째로 멈춘다(전략의
        # `quote is None -> return None`). 무인 운용에서 이 침묵이 가장 위험하다.
        _now_mono = time.time()
        for _sym in unpriced:
            unpriced_since.setdefault(_sym, _now_mono)
        for _sym in list(unpriced_since):
            if _sym in marks or _sym not in unpriced:
                unpriced_since.pop(_sym, None)
                unpriced_alerted.discard(_sym)
        for _sym, _since in unpriced_since.items():
            _age = _now_mono - _since
            if _age >= unpriced_alert_sec and _sym not in unpriced_alerted:
                unpriced_alerted.add(_sym)
                logger.error(
                    "보유 종목 시세 끊김 %s (%.0f초) — 손절 판정이 멈춘 상태", _sym, _age
                )
                if notifier is not None:
                    try:
                        notifier.send(
                            "🚨 시세 끊김 — 손절이 작동하지 않는 상태\n\n"
                            f"🏢 {_display(_sym)}\n"
                            f"   ⏱ {_age/60:.0f}분째 현재가를 받지 못했습니다\n\n"
                            "이 종목은 손절가·목표가 판정이 멈춰 있습니다.\n"
                            "즉시 조치가 필요하면 /flatten 으로 전량 청산하세요."
                        )
                    except Exception:
                        logger.exception("시세 끊김 알림 실패 — 거래는 계속한다")

        # 고아 포지션(A-6): lots의 소유 전략이 현재 활성 전략 목록에 없는 포지션.
        # 유니버스 롤로 strategies가 교체되므로 그 리스트에서 매번 활성 id를 뽑는다.
        active_strategy_ids = {s.id for s in strategies}
        orphan_now = set(_find_orphan_lots(ctx, active_strategy_ids))
        orphan_alerted &= orphan_now  # 회복된(더 이상 orphan이 아닌) 것은 재알림 대비 리셋
        for _osym, _osid in orphan_now - orphan_alerted:
            orphan_alerted.add((_osym, _osid))
            logger.error("고아 포지션 — 관리하는 전략 없음 %s (전략 %s 비활성)", _osym, _osid)
            if notifier is not None:
                try:
                    notifier.send(
                        "🚨 고아 포지션 — 관리하는 전략이 없습니다\n\n"
                        f"🏢 {_display(_osym)}\n"
                        f"🧠 소유 전략: {_osid} (현재 비활성)\n\n"
                        "손절가·목표가 판정이 멈춰 있습니다. /flatten 으로 청산하거나 전략을 다시 켜세요."
                    )
                except Exception:
                    logger.exception("고아 포지션 알림 실패 — 거래는 계속한다")
        try:
            if reconciler is not None:
                reconciler.check()  # 주기 미도달이면 내부에서 즉시 반환한다
            # 장중 하드레일(2026-09-03) — halt/flatten 여부와 무관하게 매 사이클
            # 돈다. 청산은 절대 막지 않는다는 이 레일 전체의 원칙(모듈 상단 참고)
            # 그대로, 하드 손절도 예외가 아니다.
            _intraday_hard_stop_check(ctx, risk, sinks, notifier, books, marks)
            flatten_scope = control.consume_flatten_scope()
            if flatten_scope:
                # flatten은 승인을 거치지 않는다 — kill switch가 승인 대기로 막히면
                # 안 된다. 대기 중이던 진입 요청은 만료시켜 청산 직후 되살아나지
                # 않게 한다(expire_seconds=0 = 지금 pending인 것 전부).
                _flatten_all(ctx, risk, sinks, notifier, books, scope=flatten_scope, control=control)
                if approval is not None:
                    approval.expire_stale(0)
                    for req in approval.take_approved():
                        _cancel_approved(req, "수동 청산 실행됨", notifier)
                if notifier is not None:
                    if flatten_scope == "day":
                        notifier.send("🧹 단타 청산 완료\n\n단타 전략 보유분을 청산 처리했습니다(오버나이트 전략 보유분은 유지)")
                    else:
                        notifier.send("🧹 수동 청산 완료\n\n열린 포지션을 전량 청산 처리했습니다")
            else:
                # 장 마감으로 보류됐던 flatten이 있으면 개장한 종목만 골라 재시도한다
                # (2026-09-04) — halt 여부와 무관하게, 일반 전략 사이클보다 먼저.
                _retry_pending_flatten(ctx, risk, sinks, notifier, control, books)
                if approval is not None:
                    _process_approvals(
                        ctx, risk, sinks, notifier, control,
                        approval, approval_notifier, approval_cfg, marks, books,
                    )
                run_cycle(
                    strategies, ctx, risk, sinks, notifier, control, timings,
                    approval, approval_notifier, approval_cfg, risk_multiplier, marks,
                    mult_by_market, books,
                )
        except Exception:
            consecutive_failures += 1
            tally.error_cycles += 1
            logger.exception("사이클 %d 실패 (연속 %d회)", cycle_count, consecutive_failures)
            if now_mono - last_failure_alert >= failure_cooldown_sec:
                last_failure_alert = now_mono
                if notifier is not None:
                    notifier.send(
                        f"⚠️ 엔진 사이클 실패 (연속 {consecutive_failures}회)\n\n서버 로그 확인이 필요합니다"
                    )
            if consecutive_failures >= max_failures and not control.is_halted():
                reason = f"연속 {consecutive_failures}회 사이클 실패 — 자동 정지"
                control.halt(reason, by="auto")
                logger.error(reason)
                if notifier is not None:
                    notifier.send(f"⛔ 거래 자동 중단\n\n📕 사유 {reason}")
        else:
            # 전략이 예외로 죽으면 그 전략의 **손절·목표가·EoD청산이 통째로 건너뛰어진다**
            # (포지션 관리가 전부 on_cycle 안에 있다). 사이클 자체는 성공했으므로 위
            # except 절에 걸리지 않아 예전에는 조용히 넘어갔다 — 청산이 멈춘 상태를
            # 시스템이 "정상"으로 보고했다. 열린 포지션을 가진 전략이 죽었다면
            # 사이클 실패와 동급으로 취급해 알림·자동정지 에스컬레이션에 태운다.
            if timings.strategy_errors:
                tally.error_cycles += 1
            broken_with_positions = _strategies_with_open_lots(ctx, timings.strategy_errors)
            if broken_with_positions:
                consecutive_failures += 1
                detail = ", ".join(
                    f"{sid}({timings.strategy_errors[sid]})" for sid in broken_with_positions
                )
                logger.error(
                    "전략 오류로 포지션 관리 중단 (연속 %d회): %s", consecutive_failures, detail
                )
                if now_mono - last_failure_alert >= failure_cooldown_sec:
                    last_failure_alert = now_mono
                    if notifier is not None:
                        names = ", ".join(_strategy_label(s) for s in broken_with_positions)
                        notifier.send(
                            "🚨 전략 오류 — 손절이 작동하지 않는 상태\n\n"
                            f"🧠 전략: {names}\n"
                            f"📕 사유: {detail}\n\n"
                            "이 전략이 보유한 종목은 손절가·목표가 판정이 멈춰 있습니다.\n"
                            "즉시 조치가 필요하면 /flatten 으로 전량 청산하세요."
                        )
                if consecutive_failures >= max_failures and not control.is_halted():
                    reason = f"전략 오류로 포지션 관리 중단 {consecutive_failures}회 — 자동 정지"
                    control.halt(reason, by="auto")
                    logger.error(reason)
                    if notifier is not None:
                        notifier.send(f"⛔ 거래 자동 중단\n\n📕 사유 {reason}")
            else:
                consecutive_failures = 0
            # timings.total_ms는 flatten 경로(run_cycle 미호출)면 0.0으로 남는다 — 그 경우는
            # 지연 이력/경고 집계에서 제외한다.
            if timings.total_ms > 0:
                cycle_latencies_ms.append(timings.total_ms)
                if timings.total_ms > slow_cycle_warn_ms:
                    logger.warning(
                        "느린 사이클 감지: 총 %.1fms (임계 %.0fms) | 전략=%s risk.approve=%.1fms "
                        "broker.place_order=%.1fms sinks=%.1fms",
                        timings.total_ms, slow_cycle_warn_ms, timings.strategy_ms,
                        timings.risk_approve_ms, timings.broker_place_order_ms, timings.sinks_ms,
                    )

        # 워치 조건(알림 전용, 2026-08-30) — 전략 평가 다음, 주문 경로와 무관한
        # 자리 1곳에서만 판정한다. quote/일봉 조회 실패는 그 심볼만 건너뛰고(evaluate가
        # None으로 스킵), 이 블록 전체 실패도 사이클을 죽이지 않는다 — 관찰용 알림이
        # 거래를 막으면 안 된다.
        if watch_rules and notifier is not None:
            try:
                watch_quotes: dict[str, Quote | None] = {}
                watch_bars: dict[str, pd.DataFrame | None] = {}
                watch_bar_symbols = {r.symbol for r in watch_rules if r.metric != "price"}
                for _wsym in {r.symbol for r in watch_rules}:
                    try:
                        watch_quotes[_wsym] = ctx.data.quote(_wsym)
                    except Exception:
                        watch_quotes[_wsym] = None
                    if _wsym in watch_bar_symbols:
                        try:
                            watch_bars[_wsym] = ctx.data.history(_wsym, "1d", _WATCH_DAILY_BAR_COUNT)
                        except Exception:
                            watch_bars[_wsym] = None
                watch_now = ctx.clock.now()
                watch_hits = evaluate_watch_conditions(watch_rules, watch_quotes, watch_bars, watch_now)
                for hit in apply_watch_cooldown(watch_hits, watch_last_hit_mono, watch_cooldown_sec, now_mono):
                    notifier.send(
                        f"👁 워치: {hit.rule_name} — {hit.value:.2f} {hit.op} {hit.threshold:g} "
                        f"({_display(hit.symbol)}, {watch_now:%Y-%m-%d %H:%M:%S})"
                    )
            except Exception:
                logger.exception("워치 조건 평가 실패 — 거래는 계속한다")

        # 봉 캐시 적중률 — 느린 사이클 경고와 같은 자리에서 본다. 사이클이 느려졌을 때
        # "소스를 몇 번이나 때리고 있나"가 바로 옆에 있어야 원인을 짚을 수 있다.
        stats_fn = getattr(market_data, "bar_cache_stats", None)
        if callable(stats_fn) and now_mono - last_bar_cache_log >= _BAR_CACHE_LOG_INTERVAL_SEC:
            last_bar_cache_log = now_mono
            try:
                stats = stats_fn()
                total = stats["hits"] + stats["misses"]
                logger.info(
                    "봉 캐시: 적중 %d / 요청 %d (%.0f%%) · 소스 호출 %d",
                    stats["hits"], total,
                    100.0 * stats["hits"] / total if total else 0.0,
                    stats["source_calls"],
                )
            except Exception as e:  # noqa: BLE001 — 계측이 매매를 멈추게 하면 안 된다
                logger.debug("봉 캐시 통계 조회 실패: %s: %s", type(e).__name__, e)

        # 데이터 스테일 가드 — 장중인데 소스가 계속 실패/빈값이면 예외 없이도 알림.
        # 장외 시간엔 카운트하지 않는다(정상적으로 시세가 없는 시간대라 오탐이 된다).
        if market_open and market_data is not None:
            health_fn = getattr(market_data, "health", None)
            if callable(health_fn):
                try:
                    degraded = health_fn().degraded
                except Exception:
                    degraded = False
                if degraded:
                    consecutive_stale += 1
                    tally.degraded_seen = True
                else:
                    consecutive_stale = 0
                    stale_alerted = False
                if consecutive_stale >= max_stale and not stale_alerted:
                    stale_alerted = True
                    msg = (
                        f"⚠️ 시세 조회 연속 {consecutive_stale}회 실패\n\n"
                        "시세 없이 동작 중일 수 있습니다 — 확인이 필요합니다"
                    )
                    logger.error(msg)
                    if notifier is not None:
                        notifier.send(msg)
        else:
            consecutive_stale = 0
            stale_alerted = False

        # 하트비트 — 장중에만, cadence는 engine.heartbeat_minutes.
        # 기본은 채팅 발송 off(위 telegram_heartbeat) — 파일/로그 하트비트는 별도.
        if telegram_heartbeat and market_open and notifier is not None and now_mono - last_heartbeat >= heartbeat_sec:
            last_heartbeat = now_mono
            last_cycle_ms = cycle_latencies_ms[-1] if cycle_latencies_ms else None
            recent_max_cycle_ms = max(cycle_latencies_ms) if cycle_latencies_ms else None
            notifier.send(
                _heartbeat_text(
                    cycle_count, ctx, control, market_data,
                    last_cycle_ms, recent_max_cycle_ms, approval, risk,
                    uptime_seconds=now_mono - loop_started_mono,
                )
            )

        # 전략이 이번 사이클에 쓴 손절/목표를 디스크에 반영(변경 시에만) — 재시작이
        # 손절을 2% 폴백으로 갈아치우는 것을 막는다.
        meta_signature = _persist_position_meta(ctx, meta_signature)

        # 포지션 현황 — 보유 중일 때만, 하트비트와 별도 주기(기본 1분). 진입 직후가
        # 가장 궁금한 구간이라 "엔진 생존"(하트비트)과 "내 돈의 위치"를 분리했다.
        # 전송 실패가 사이클을 죽이면 안 된다 — 리포팅은 거래보다 아래다.
        if telegram_position_report and market_open and notifier is not None and now_mono - last_position_report >= position_report_sec:
            try:
                report = _position_report_text(ctx, marks, risk)
            except Exception:
                logger.warning("포지션 현황 생성 실패 — 건너뜀", exc_info=True)
                report = None
            if report is not None:
                last_position_report = now_mono
                notifier.send(report)

        # 전략 간 합산 노출 감시(2026-08-30) — capital_mode: per_strategy에서
        # 리스크 상한이 전부 전략별 장부 기준이라, 다른 전략이 같은 심볼을
        # 이미 들고 있거나(중복 보유) 레버리지 ETF 롱 + 그 인버스 롱을 동시에
        # 들고 있어도(상쇄 쌍) 아무 상한도 그 사실을 모른다. 자동 차단은 하지
        # 않는다 — 평시엔 로그만, 임계 초과·상쇄 쌍 존재 시에만(쿨다운) 텔레그램.
        if exposure_check is not None and market_open and now_mono - last_exposure_check >= position_report_sec:
            last_exposure_check = now_mono
            try:
                lots, ex_prices, capital_krw = _build_exposure_snapshot(ctx, marks)
                exposure = exposure_check(lots, ex_prices, capital_krw)
            except Exception:
                logger.warning("합산 노출 계산 실패 — 건너뜀", exc_info=True)
                exposure = None
            if exposure is not None:
                logger.info("합산 노출 감시: %s", exposure.get("summary", ""))
                alert_text = exposure.get("alert_text")
                if (
                    alert_text and notifier is not None
                    and now_mono - last_exposure_alert >= exposure_alert_cooldown_sec
                ):
                    last_exposure_alert = now_mono
                    try:
                        notifier.send(alert_text)
                    except Exception:
                        logger.warning("합산 노출 경고 발송 실패", exc_info=True)

        # 세션 마감 요약 — 시장별로 개장→마감 전환에서 정확히 1회.
        # 상태 갱신을 발송보다 **먼저** 한다: 요약 생성/전송이 실패해도 같은 마감을
        # 다음 사이클에 또 시도하지 않는다(중복 발송 금지가 미발송보다 중요하다).
        if market_states is not None:
            for market, is_open in market_states.items():
                was_open = prev_market_open.get(market)
                prev_market_open[market] = is_open
                if was_open is not True or is_open:
                    continue
                try:
                    if notifier is not None:
                        notifier.send(
                            _session_summary_text(
                                market, ctx, risk, control, market_data, tally, strategies,
                            )
                        )
                except Exception:
                    logger.exception("세션 마감 요약 실패 %s — 다음 세션에는 영향 없음", market)
                # KR(15:30 KST)과 US(05:00 KST) 마감은 겹치지 않으므로 카운터를
                # 시장별로 쪼개지 않고 마감마다 통째로 리셋한다.
                tally.reset()

        # 하트비트 상태 파일 — 사이클 성공/실패와 무관하게 매번. 사이클 "밖"의 부기라
        # timings에는 넣지 않는다(계측값이 자기 자신의 기록 비용에 오염되면 안 된다).
        ok = _write_heartbeat_file(heartbeat_path, {
            "ts": time.time(),
            "cycle": cycle_count,
            "market_open": market_open,
            "halted": control.is_halted(),
            "last_cycle_ms": timings.total_ms,
        })
        if not ok and not heartbeat_write_warned:
            heartbeat_write_warned = True
            logger.warning("하트비트 파일 쓰기 실패 — 거래는 계속한다: %s", heartbeat_path)

        # 틱 로그 flush — 사이클 끝에서 한 번(버퍼링, N초마다만 실제 디스크 쓰기).
        if tick_logger is not None:
            tick_logger.flush_if_due(ctx.clock.now())

        await asyncio.sleep(settings.poll_seconds)
