"""1분봉 -> N분봉 리샘플 유틸. stock-algo-trade와 동일한 시맨틱을 공유한다."""
from __future__ import annotations

import pandas as pd


def resample_1m(df1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """1분 OHLCV -> N분 OHLCV.

    closed="left", label="left"로 세션 시작(09:30/09:00)에 정렬한다.
    마지막 bin은 형성 중일 수 있으므로 항상 버린다 — 완성봉만 반환한다.
    """
    if df1m.empty:
        return df1m
    bars = df1m.resample(f"{minutes}min", closed="left", label="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open"])
    return bars.iloc[:-1] if not bars.empty else bars
