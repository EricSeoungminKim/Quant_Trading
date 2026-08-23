"""TossCandleSource: 1분봉(fetch_1m)과 신규 일봉(fetch, KR 개별종목 히스토리
백필용) backward-paging, adjusted=true 강제, nextBefore 종료 조건을 검증한다.
네트워크 호출 없음 — TossClient._request를 페이크로 교체.
"""
from __future__ import annotations

import pandas as pd
import pytest

from quant.collect.quotes.backfill import backfill
from quant.collect.quotes.toss_source import TossCandleSource


class FakeTossClient:
    """TossClient._request 스텁. 호출 순서대로 canned 응답을 반환하고 매 호출의
    params를 기록한다 — 실제 client와 달리 `before` 값으로 분기하지 않는다(테스트가
    호출 순서를 직접 통제)."""

    def __init__(self, pages: list[dict]) -> None:
        self._pages = list(pages)
        self.calls: list[dict] = []

    def _request(self, method, path, group, *, params=None, **kwargs):
        self.calls.append(dict(params or {}))
        if not self._pages:
            return {"candles": [], "nextBefore": None}
        return self._pages.pop(0)


def _candle(ts: str, price: float) -> dict:
    return {
        "timestamp": ts,
        "openPrice": str(price),
        "highPrice": str(price + 1),
        "lowPrice": str(price - 1),
        "closePrice": str(price + 0.5),
        "volume": "1000",
        "currency": "KRW",
    }


# --------------------------------------------------------------------- fetch_1m


def test_fetch_1m_sends_1m_interval_and_adjusted_true():
    client = FakeTossClient([
        {"candles": [_candle("2026-08-14T09:31:00+09:00", 100), _candle("2026-08-14T09:30:00+09:00", 99)],
         "nextBefore": None},
    ])
    source = TossCandleSource(client)

    out = source.fetch_1m("005930", pd.Timestamp("2026-08-14T09:30:00+09:00"), pd.Timestamp("2026-08-14T09:31:00+09:00"))

    assert len(out) == 2
    assert client.calls[0]["interval"] == "1m"
    assert client.calls[0]["adjusted"] == "true"


# ----------------------------------------------------------------------- fetch (1d)


def test_native_interval_is_1d():
    source = TossCandleSource(FakeTossClient([]))
    assert source.native_interval == "1d"


def test_fetch_daily_sends_1d_interval_and_adjusted_true():
    client = FakeTossClient([
        {"candles": [_candle("2026-08-14T09:00:00+09:00", 71600)], "nextBefore": None},
    ])
    source = TossCandleSource(client)

    out = source.fetch("069500", pd.Timestamp("2026-08-14", tz="UTC"), pd.Timestamp("2026-08-15", tz="UTC"))

    assert len(out) == 1
    assert client.calls[0]["interval"] == "1d"
    assert client.calls[0]["adjusted"] == "true"
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]


def test_fetch_daily_pages_backward_via_next_before_until_start_reached():
    """200봉 상한을 넘는 깊이는 nextBefore를 그대로 다음 요청에 넘겨 페이징해야
    한다 — 2년치 KR 개별종목 백필의 핵심 경로."""
    client = FakeTossClient([
        {
            "candles": [_candle("2026-08-03T09:00:00+09:00", 103), _candle("2026-08-02T09:00:00+09:00", 102)],
            "nextBefore": "2026-08-01T09:00:00+09:00",
        },
        {
            "candles": [_candle("2026-08-01T09:00:00+09:00", 101), _candle("2026-07-31T09:00:00+09:00", 100)],
            "nextBefore": "2026-07-30T09:00:00+09:00",
        },
    ])
    source = TossCandleSource(client)

    out = source.fetch("005930", pd.Timestamp("2026-07-31", tz="UTC"), pd.Timestamp("2026-08-03", tz="UTC"))

    assert len(client.calls) == 2
    # 두 번째 호출의 before는 첫 페이지의 nextBefore를 그대로 넘겨받아야 한다.
    assert client.calls[1]["before"] == "2026-08-01T09:00:00+09:00"
    assert len(out) == 4


def test_fetch_daily_stops_when_next_before_missing():
    client = FakeTossClient([
        {"candles": [_candle("2026-08-14T09:00:00+09:00", 100)], "nextBefore": None},
    ])
    source = TossCandleSource(client)

    out = source.fetch("005930", pd.Timestamp("2020-01-01", tz="UTC"), pd.Timestamp("2026-08-14", tz="UTC"))

    assert len(client.calls) == 1  # nextBefore가 없으니 start(2020)에 못 미쳐도 멈춘다
    assert len(out) == 1


def test_fetch_daily_stops_once_oldest_bar_reaches_start():
    client = FakeTossClient([
        {
            "candles": [_candle("2026-08-03T09:00:00+09:00", 100), _candle("2026-08-01T09:00:00+09:00", 99)],
            "nextBefore": "2026-07-31T09:00:00+09:00",
        },
        # start=2026-08-01이면 첫 페이지의 oldest(08-01)가 이미 <= start이므로
        # 두 번째 페이지는 요청되면 안 된다.
    ])
    source = TossCandleSource(client)

    out = source.fetch("005930", pd.Timestamp("2026-08-01", tz="UTC"), pd.Timestamp("2026-08-03", tz="UTC"))

    assert len(client.calls) == 1
    assert len(out) == 2


def test_fetch_daily_dedups_overlapping_pages():
    client = FakeTossClient([
        {"candles": [_candle("2026-08-02T09:00:00+09:00", 101), _candle("2026-08-01T09:00:00+09:00", 100)],
         "nextBefore": "2026-08-01T09:00:00+09:00"},
        {"candles": [_candle("2026-08-01T09:00:00+09:00", 100), _candle("2026-07-31T09:00:00+09:00", 99)],
         "nextBefore": None},
    ])
    source = TossCandleSource(client)

    out = source.fetch("005930", pd.Timestamp("2026-07-31", tz="UTC"), pd.Timestamp("2026-08-02", tz="UTC"))

    assert len(out) == 3  # 08-01이 두 페이지에 겹쳐 나와도 한 번만 남는다
    assert out.index.is_monotonic_increasing


# ---------------------------------------------------------------- backfill() 통합


def test_backfill_writes_toss_daily_to_1d_partition_path(tmp_path):
    client = FakeTossClient([
        {"candles": [_candle("2026-08-03T09:00:00+09:00", 103), _candle("2026-08-02T09:00:00+09:00", 102),
                     _candle("2026-08-01T09:00:00+09:00", 101)],
         "nextBefore": None},
    ])
    source = TossCandleSource(client)

    report = backfill(
        "005930", source,
        start=pd.Timestamp("2026-08-01T00:00:00Z"), end=pd.Timestamp("2026-08-03T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2026-08-15T00:00:00Z"), interval="1d",
    )

    assert report.partitions_written == ["2026-08"]
    path = tmp_path / "005930" / "1d" / "2026" / "08.parquet"
    assert path.exists()
    saved = pd.read_parquet(path)
    assert len(saved) == 3
    assert list(saved.columns) == ["open", "high", "low", "close", "volume"]


def test_backfill_rejects_requesting_non_1d_interval_from_toss_source(tmp_path):
    source = TossCandleSource(FakeTossClient([]))

    with pytest.raises(ValueError, match="native_interval"):
        backfill(
            "005930", source,
            start=pd.Timestamp("2026-08-01T00:00:00Z"), end=pd.Timestamp("2026-08-03T23:59:59Z"),
            history_dir=tmp_path, now=pd.Timestamp("2026-08-15T00:00:00Z"), interval="15m",
        )
