"""USD/KRW 환율 프로바이더. brokers/를 import하지 않는다 — 실 조회 함수(fetch)는
호출자가 주입한다(예: TossClient.usd_krw). data/ -> brokers/ 의존 금지 방향성 유지.

DailyFxProvider는 거래일 기준 하루 1회(그날 첫 호출 시점 = "장 시작") 환율을 재조회하고
캐시한다. 조회 실패 시 경고 로깅 후 마지막으로 성공한 값(없으면 fallback)을 반환한다 —
절대 예외를 던지지 않는다.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Protocol, runtime_checkable

from quant.core.ports import Clock

logger = logging.getLogger(__name__)

_DEFAULT_FALLBACK = 1500.0


@runtime_checkable
class FxProvider(Protocol):
    def usd_krw(self) -> float: ...


class FixedFxProvider:
    """결정론적 고정 환율 — 백테스트, 그리고 다른 프로바이더의 fallback으로 사용."""

    def __init__(self, rate: float = _DEFAULT_FALLBACK):
        self.rate = rate

    def usd_krw(self) -> float:
        return self.rate


class DailyFxProvider:
    """거래일이 바뀐 뒤 첫 호출에만 fetch()로 재조회하고, 같은 날 나머지 호출은 캐시된
    값을 반환한다. fetch 실패 시 마지막 성공 값(없으면 fallback)을 반환하며 날짜를
    갱신하지 않는다 — 다음 호출에서 다시 재조회를 시도한다."""

    def __init__(self, fetch: Callable[[], float], clock: Clock, fallback: float = _DEFAULT_FALLBACK):
        self._fetch = fetch
        self._clock = clock
        self._fallback = fallback
        self._day: date | None = None
        self._rate: float | None = None

    def usd_krw(self) -> float:
        today = self._clock.now().date()
        if today != self._day or self._rate is None:
            try:
                self._rate = self._fetch()
                self._day = today
            except Exception as e:
                logger.warning("USD/KRW 환율 조회 실패, 마지막 값으로 대체: %s: %s", type(e).__name__, e)
        return self._rate if self._rate is not None else self._fallback
