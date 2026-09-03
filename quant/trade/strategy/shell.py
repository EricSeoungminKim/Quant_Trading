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

        # 2026-09-02: 개장 여부를 **fetch 보다 먼저** 판정한다.
        # 예전에는 bars/quotes 를 전부 당긴 뒤 마지막에 market_open 을 채웠는데,
        # 순수 전략 대부분은 decide() 첫머리에서 `snap.market_open[market]` 이
        # False 면 진입도 보유관리도 하지 않고 빠져나간다(각 전략의 `_tradable`
        # 또는 시장 루프 첫 줄). 즉 닫힌 시장 심볼의 조회는 낭비였다 — KR 장중에
        # US 폐장분으로 사이클당 history 184 + quote 114 회(≈ 하루 6천 회,
        # Kiwoom 429 9,148건/24h). 진짜 피해는 요청 수가 아니라
        # `cold_fetch_budget_per_cycle`(8)을 이 낭비분이 먼저 다 써서,
        # **열려 있는 시장에서 포지션을 든 전략의 on_cycle 이 통째로 스킵되는 것**
        # 이었다(loop.py 가 ColdFetchBudgetExceeded 를 조용히 삼킨다 → 그 사이클에
        # 손절 판정이 없다).
        #
        # 데이터 평면(MarketDataService)이 아니라 여기서 막는 이유: 프리마켓을
        # 의도적으로 거래하는 전략(scalp_1m, `risk.extended_sessions`)이 있고
        # 백테스트 SimClock 경로도 같은 서비스를 탄다 — 데이터 평면에 일괄
        # 게이트를 걸면 그 둘이 죽는다. 게이트는 전략 선언에 따라 여기서만 건다.
        market_open = {m: ctx.clock.is_market_open(m) for m in self._markets}

        # 이 전략 몫만: pos.lot()은 순수 조회다(쓰기 의도가 있는 ensure_lot과 달리
        # 이행/마이그레이션을 트리거하지 않는다) — 껍질이 스냅샷을 만드는 과정에서
        # 실제 Position 객체를 건드리지 않는다. 아래 현재가 게이트가 "보유 중인가"
        # 를 봐야 하므로 bars/quotes 보다 **먼저** 채운다(브로커 포지션 조회는
        # 로컬 원장 읽기라 소스 호출이 아니다).
        lots: dict[str, dict] = {}
        if needs.needs_positions:
            positions = ctx.broker.positions()
            for symbol in self.symbols:
                pos = positions.get(symbol)
                if pos is not None and pos.is_open:
                    lot = pos.lot(self.id)
                    lots[symbol] = dict(lot) if lot is not None else {}

        def closed(symbol: str) -> bool:
            """이 심볼의 시장이 **닫혀 있다고 확실히 아는가**.

            `market_open` 에 없는 시장(이 전략의 `symbols` 밖 심볼을
            `requirements()` 가 요구하는 경우)은 판단 근거가 없으므로 "열림"으로
            본다 — 데이터가 빠지는 쪽보다 헛조회 쪽이 안전하다.
            """
            if needs.fetch_when_closed:
                return False
            return not market_open.get(market_of_symbol(symbol), True)

        bars = {
            (symbol, interval): ctx.data.history(symbol, interval, count)
            for symbol, interval, count in needs.bars
            if not closed(symbol)
        }

        quotes = {}
        for symbol in needs.quotes:
            # 닫힌 시장이라도 **이 전략이 보유 중인 심볼의 현재가는 남긴다**.
            # 보유 관리(_manage)를 market_open 으로 감싸지 않는 전략
            # (`donchian.py`)이 있어서, 현재가가 사라지면 방어선 판정이 조용히
            # 멈춘다 — 폐장 중엔 어차피 risk 가 주문을 막지만(MARKET_CLOSED_MARKER),
            # 판정 자체를 없애는 것은 이 수정의 범위가 아니다. 보유 심볼은 보통
            # 0~3개라 낭비의 실질은 그대로 사라진다.
            if closed(symbol) and symbol not in lots:
                continue
            quote = ctx.data.quote(symbol)
            if quote is not None:
                quotes[symbol] = quote

        return StrategySnapshot(
            now=ctx.clock.now(),
            market_open=market_open,
            minutes_to_close={m: ctx.clock.minutes_to_close(m) for m in self._markets},
            cadence_minutes=ctx.clock.cadence_minutes(),
            bars=bars,
            quotes=quotes,
            lots=lots,
        )
