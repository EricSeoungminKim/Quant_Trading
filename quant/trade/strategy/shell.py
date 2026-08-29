"""`PureStrategy`(quant.core.strategy_api)를 감싸 기존 `Strategy` Protocol
(`on_cycle(ctx) -> list[Signal]`)을 만족시키는 껍질 — 엔진 분리 설계 Phase A.

`requirements()`를 읽어 필요한 것만 `ctx`에서 모아 `StrategySnapshot`을 만들고,
`decide()`를 부른 뒤 반환된 `next_state`를 다음 사이클까지 그대로 들고 있다가
넘긴다.

**이번 범위에서 아직 못 하는 것**: `next_state`는 체결 성공 여부와 무관하게 매
사이클 그대로 적용된다 — `risk.approve()`가 거부하거나 주문이 미체결이어도
반영된다. 기존 `Signal.state_update`(loop.py의 `_execute_signal`이 체결 확인
후에만 `Position.meta`에 적용)와 달리, 여기 `next_state`는 그 게이트를 거치지
않는다. "체결 후에만 상태를 적용한다"까지 하려면 `quant/trade/loop.py`의 루프
변경이 필요하다 — 그건 다음 단계다.

이 모듈은 `quant/trade/`에 있으므로 adapters/collect/analyze/control/apps를
임포트하지 않는다(`quant/trade/strategy/CLAUDE.md`).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Any, Mapping

from quant.core.models import Signal, market_of_symbol
from quant.core.ports import Context
from quant.core.strategy_api import PureStrategy, StrategySnapshot

logger = logging.getLogger(__name__)

_REJECT_SUMMARY_INTERVAL_SECONDS = 3600.0


class PureStrategyShell:
    """`PureStrategy` 하나를 감싸는 얇은 어댑터. `Strategy` Protocol을 만족한다."""

    def __init__(self, inner: PureStrategy) -> None:
        self.inner = inner
        self.id = inner.id
        self.symbols = inner.symbols
        self._needs = inner.requirements()
        self._state: Mapping[str, Any] = {}
        # Clock.is_market_open/minutes_to_close는 백테스트에서 데이터에 없는
        # 시장을 물으면 명시적으로 예외를 낸다("KR 세션을 알 수 없다" 등, 되는 척
        # 하지 않는 것이 의도 — engine.py 참고). 그래서 두 시장을 무조건 다 묻지
        # 않고, 이 전략이 실제로 거래하는 심볼의 시장만 묻는다.
        self._markets = sorted({market_of_symbol(s) for s in self.symbols})
        # 진단용: next_state["last_reject"](순수 전략이 남기는 거부 사유)는
        # 원래 self._state 안에만 있어 로그/리포트에 안 보였다 — "필터의 정당한
        # 침묵"과 "고장으로 전부 거부"를 구분하려면 로그가 필요하다. 사유가
        # 바뀐 심볼만 로그해 스팸을 막는다(직전 사유 기억).
        self._last_reject_reason: dict[str, str] = {}
        self._reject_counts: Counter[str] = Counter()
        self._reject_summary_since: datetime | None = None

    def on_cycle(self, ctx: Context) -> list[Signal]:
        snap = self._snapshot(ctx)
        decision = self.inner.decide(snap, self._state)
        self._state = decision.next_state
        self._log_rejects(decision.next_state, snap.now)
        return list(decision.signals)

    def _log_rejects(self, next_state: Mapping[str, Any], now: datetime) -> None:
        last_reject = next_state.get("last_reject")
        if not isinstance(last_reject, Mapping):
            return  # 이 전략은 거부 사유를 남기지 않는다 — 무동작

        for symbol, reason in last_reject.items():
            self._reject_counts[reason] += 1
            if self._last_reject_reason.get(symbol) != reason:
                logger.info("[%s] 진입 거부 %s: %s", self.id, symbol, reason)
                self._last_reject_reason[symbol] = reason

        if self._reject_summary_since is None:
            self._reject_summary_since = now
            return
        elapsed = (now - self._reject_summary_since).total_seconds()
        if elapsed < _REJECT_SUMMARY_INTERVAL_SECONDS:
            return
        if self._reject_counts:
            top5 = ", ".join(
                f"{reason}={count}" for reason, count in self._reject_counts.most_common(5)
            )
            logger.info("[%s] 거부 요약(최근 1시간): %s", self.id, top5)
            self._reject_counts.clear()
        self._reject_summary_since = now

    def _snapshot(self, ctx: Context) -> StrategySnapshot:
        needs = self._needs

        bars = {
            (symbol, interval): ctx.data.history(symbol, interval, count)
            for symbol, interval, count in needs.bars
        }

        quotes = {}
        for symbol in needs.quotes:
            quote = ctx.data.quote(symbol)
            if quote is not None:
                quotes[symbol] = quote

        # 이 전략 몫만: pos.lot()은 순수 조회다(쓰기 의도가 있는 ensure_lot과 달리
        # 이행/마이그레이션을 트리거하지 않는다) — 껍질이 스냅샷을 만드는 과정에서
        # 실제 Position 객체를 건드리지 않는다.
        lots: dict[str, dict] = {}
        if needs.needs_positions:
            positions = ctx.broker.positions()
            for symbol in self.symbols:
                pos = positions.get(symbol)
                if pos is not None and pos.is_open:
                    lot = pos.lot(self.id)
                    lots[symbol] = dict(lot) if lot is not None else {}

        return StrategySnapshot(
            now=ctx.clock.now(),
            market_open={m: ctx.clock.is_market_open(m) for m in self._markets},
            minutes_to_close={m: ctx.clock.minutes_to_close(m) for m in self._markets},
            cadence_minutes=ctx.clock.cadence_minutes(),
            bars=bars,
            quotes=quotes,
            lots=lots,
        )
