"""벤더 중립 과거 데이터 소스 Protocol. Toss가 오늘의 구현체이고, 다른 벤더는
같은 계약으로 꽂힌다."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class CandleSource(Protocol):
    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """[start, end] 구간의 1분봉 원본. columns=[open,high,low,close,volume],
        index=DatetimeIndex(tz-aware), 오름차순. 종목의 원래 통화 그대로 반환한다 —
        리샘플/FX 변환은 이 계층의 책임이 아니다(순수성 원칙, docs/data-availability.md)."""
        ...


@runtime_checkable
class MultiIntervalCandleSource(Protocol):
    """1분봉을 (충분히 깊게) 제공하지 못하는 벤더용 확장 계약이다(예: Yahoo Finance —
    1분봉은 최근 며칠뿐이지만 15분봉은 60일, 일봉은 상장 이후 전체를 제공한다,
    docs/data-availability.md 실측 참고).

    이 소스가 실제로 반환하는 봉 간격을 `native_interval`로 명시하고 그 간격 그대로의
    봉만 돌려준다 — 갖고 있지 않은 더 촘촘한 간격을 리샘플/보간으로 지어내지 않는다
    (HONESTY CONSTRAINT, docs/data-availability.md). `backfill()`/`HistoryDataFeed`는
    `native_interval`이 요청한 interval과 정확히 일치할 때만 이 소스/데이터를 쓴다 —
    다른 간격으로의 업샘플/리샘플은 거부한다."""

    native_interval: str  # 예: "15m", "1h", "1d" — fetch()가 실제로 반환하는 간격

    def fetch(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """[start, end] 구간, native_interval 그대로의 OHLCV. columns/index 계약은
        CandleSource.fetch_1m과 동일 (open,high,low,close,volume / tz-aware
        DatetimeIndex, 오름차순, UTC)."""
        ...
