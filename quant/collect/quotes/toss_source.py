"""TossClient 위의 CandleSource 구현체.

TossClient.candles()는 "지금부터 count개"만 페이징하고 임의의 시작 커서를 받지
않는다 — backfill의 [start, end] 구간 계약을 만족하려면 end를 upper-bound
`before` 커서로 삼아 start를 지날 때까지 backward paging해야 하므로, client의
private _request를 candles()와 동일한 방식으로 직접 호출한다(TossClient는
수정하지 않는다 — read-only 사용). rate limiter/재시도/429 Retry-After 처리는
_request 내부에서 항상 적용되므로 별도 처리가 필요 없다.

일봉(1d)은 `fetch()`(MultiIntervalCandleSource 계약, native_interval="1d")로
같은 backward-paging 로직을 재사용한다. **`adjusted=true`를 항상 명시적으로
보낸다** — 액면분할·병합이 반영되지 않은 가격으로 모멘텀/팩터를 계산하면 가짜
급등락이 생긴다(docs/data-availability.md, KR 개별종목 일봉 백필 배경).
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from quant.adapters.brokers.toss.client import TossClient

_PAGE_SIZE = 200
_COLUMNS = ["open", "high", "low", "close", "volume"]


class TossCandleSource:
    # fetch()(MultiIntervalCandleSource 계약)가 실제로 서빙하는 유일한 non-1m
    # interval. Toss candles API가 interval={"1m","1d"} 둘만 지원하므로 고정값이다.
    native_interval = "1d"

    def __init__(self, client: TossClient) -> None:
        self._client = client

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        return self._fetch(symbol, start, end, interval="1m")

    def fetch(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """MultiIntervalCandleSource 계약 — 일봉(1d)만 지원. adjusted=true(수정주가)
        적용."""
        return self._fetch(symbol, start, end, interval="1d")

    def _fetch(self, symbol: str, start: datetime, end: datetime, *, interval: str) -> pd.DataFrame:
        rows: list[dict] = []
        before: str | None = pd.Timestamp(end).isoformat()
        while True:
            result = self._client._request(
                "GET", "/api/v1/candles", "MARKET_DATA_CHART",
                params={
                    "symbol": symbol, "interval": interval, "count": _PAGE_SIZE,
                    "before": before, "adjusted": "true",
                },
            )
            page = result["candles"]
            if not page:
                break
            rows.extend(page)
            oldest_ts = pd.Timestamp(page[-1]["timestamp"])
            if oldest_ts <= pd.Timestamp(start):
                break
            next_before = result.get("nextBefore")
            if not next_before:
                break
            before = next_before

        if not rows:
            return pd.DataFrame(columns=_COLUMNS)
        df = pd.DataFrame([{
            "ts": pd.Timestamp(c["timestamp"]),
            "open": float(c["openPrice"]),
            "high": float(c["highPrice"]),
            "low": float(c["lowPrice"]),
            "close": float(c["closePrice"]),
            "volume": float(c["volume"]),
        } for c in rows]).set_index("ts").sort_index()
        # Toss는 timestamp를 고정 오프셋(예: +09:00)으로 준다 — 그대로 두면 다른
        # 벤더(yfinance 등, 항상 UTC로 정규화)가 이미 채워둔 파티션과 concat될 때
        # (예: 069500은 backfill_kr_daily.sh가 yfinance로도 채운다) pandas가 tz
        # 불일치를 object dtype Index로 흘려버려 DatetimeIndex를 잃는다(실측:
        # `_find_gaps`의 `.tz_convert`가 'Index' object has no attribute 'tz_convert'로
        # 죽음). 벤더 경계에서 UTC로 통일해 이 충돌을 원천 차단한다.
        df.index = df.index.tz_convert("UTC")
        df = df[~df.index.duplicated(keep="last")]
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        if start_ts.tzinfo is None:
            start_ts = start_ts.tz_localize("UTC")
        if end_ts.tzinfo is None:
            end_ts = end_ts.tz_localize("UTC")
        return df.loc[(df.index >= start_ts) & (df.index <= end_ts)]
