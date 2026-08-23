"""KiwoomUSDataFeed 테스트 — 전부 페이크 클라이언트만 사용(네트워크 없음).

핵심 커버리지: quote()는 최신 행(형성 중이어도)을 그대로 쓰지만, history()는
클록 기준으로 완성되지 않은 마지막 분을 반드시 걸러낸다(look-ahead 금지 —
domain/interfaces.py 계약), 부호 붙은 가격 문자열 파싱, KR 심볼 거부, 벤더
예외의 DataSourceError 변환.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quant.adapters.brokers.kiwoom.us_datafeed import KiwoomUSDataFeed
from quant.core.ports import DataSourceError

_ET = ZoneInfo("America/New_York")


class FakeGlobalClient:
    """KiwoomClient.usa_chart_1m()만 흉내낸다."""

    def __init__(self, rows: list[dict] | None = None):
        self._rows = rows or []
        self.calls: list[tuple[str, str]] = []
        self.fail_next: Exception | None = None

    def usa_chart_1m(self, symbol: str, exchange: str = "ND") -> list[dict]:
        self.calls.append((symbol, exchange))
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        return self._rows


class FakeClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


def _row(cntr_tm: str, cur_prc: str, *, open_pric=None, high_pric=None, low_pric=None, trde_qty="100") -> dict:
    return {
        "cur_prc": cur_prc,
        "trde_qty": trde_qty,
        "open_pric": open_pric if open_pric is not None else cur_prc,
        "high_pric": high_pric if high_pric is not None else cur_prc,
        "low_pric": low_pric if low_pric is not None else cur_prc,
        "cntr_tm": cntr_tm,
        "bus_dt": cntr_tm[:8],
    }


# 세 개의 연속된 1분봉(11:44/11:45/11:46) — 마지막(11:46)은 "지금"(11:46:30) 기준
# 아직 형성 중이다(그 분이 끝나려면 11:47이 돼야 한다).
_ROWS = [
    _row("20240603114400", "100.00"),
    _row("20240603114500", "101.00"),
    _row("20240603114600", "+102.5000"),  # 실측처럼 부호가 붙을 수 있다
]
_NOW = datetime(2024, 6, 3, 11, 46, 30, tzinfo=_ET)


# --------------------------------------------------------------------- quote()

def test_quote_uses_latest_row_even_if_forming():
    """quote()는 look-ahead 금지 대상이 아니다 — 형성 중인 분이어도 최신 체결가를 쓴다."""
    client = FakeGlobalClient(_ROWS)
    feed = KiwoomUSDataFeed(client, FakeClock(_NOW))

    q = feed.quote("TQQQ")

    assert q is not None
    assert q.price == 102.5  # 부호가 벗겨짐
    assert q.ts.replace(tzinfo=None) == datetime(2024, 6, 3, 11, 46, 0)


def test_quote_returns_none_when_no_rows():
    client = FakeGlobalClient([])
    feed = KiwoomUSDataFeed(client, FakeClock(_NOW))

    assert feed.quote("TQQQ") is None


def test_quote_rejects_kr_symbol_without_calling_client():
    client = FakeGlobalClient(_ROWS)
    feed = KiwoomUSDataFeed(client, FakeClock(_NOW))

    with pytest.raises(DataSourceError):
        feed.quote("005930")
    assert client.calls == []


def test_quote_wraps_vendor_exception_as_data_source_error():
    client = FakeGlobalClient([])
    client.fail_next = RuntimeError("network down")
    feed = KiwoomUSDataFeed(client, FakeClock(_NOW))

    with pytest.raises(DataSourceError):
        feed.quote("TQQQ")


# ------------------------------------------------------------------- history()

def test_history_1m_excludes_the_forming_last_bar():
    client = FakeGlobalClient(_ROWS)
    feed = KiwoomUSDataFeed(client, FakeClock(_NOW))

    bars = feed.history("TQQQ", "1m", 10)

    assert len(bars) == 2  # 11:44, 11:45만 — 11:46은 아직 형성 중
    assert bars.iloc[-1]["close"] == 101.0


def test_history_1m_includes_bar_the_instant_it_closes():
    """마지막 분의 마감 시각(11:47:00) 정각이면 그 분도 완성봉으로 포함된다."""
    client = FakeGlobalClient(_ROWS)
    feed = KiwoomUSDataFeed(client, FakeClock(datetime(2024, 6, 3, 11, 47, 0, tzinfo=_ET)))

    bars = feed.history("TQQQ", "1m", 10)

    assert len(bars) == 3


def test_history_resamples_to_larger_interval():
    """closed="left"/label="left" 15분 구간 경계는 11:30/11:45/12:00... — 11:44는
    [11:30,11:45) bin, 11:45/11:46은 [11:45,12:00) bin에 들어간다. resample_1m이
    마지막(뒤) bin을 형성 중으로 간주해 버리므로 앞 bin(11:44 하나) 결과만 남는다."""
    client = FakeGlobalClient(_ROWS)
    feed = KiwoomUSDataFeed(client, FakeClock(datetime(2024, 6, 3, 11, 47, 0, tzinfo=_ET)))

    bars = feed.history("TQQQ", "15m", 5)

    assert len(bars) == 1
    assert bars.iloc[0]["close"] == 100.0


def test_history_rejects_daily_interval():
    """usa06010은 분봉 전용이다 — 일봉을 지어내지 않고 실패시킨다."""
    client = FakeGlobalClient(_ROWS)
    feed = KiwoomUSDataFeed(client, FakeClock(_NOW))

    with pytest.raises(DataSourceError):
        feed.history("TQQQ", "1d", 20)


def test_history_rejects_kr_symbol_without_calling_client():
    client = FakeGlobalClient(_ROWS)
    feed = KiwoomUSDataFeed(client, FakeClock(_NOW))

    with pytest.raises(DataSourceError):
        feed.history("005930", "1m", 10)
    assert client.calls == []


def test_history_malformed_row_is_dropped_not_crashing():
    rows = [
        _row("20240603114400", "100.00"),
        {"cntr_tm": "20240603114500"},  # cur_prc 없음 — 버려짐
    ]
    client = FakeGlobalClient(rows)
    feed = KiwoomUSDataFeed(client, FakeClock(datetime(2024, 6, 3, 11, 46, 0, tzinfo=_ET)))

    bars = feed.history("TQQQ", "1m", 10)

    assert len(bars) == 1
    assert bars.iloc[0]["close"] == 100.0
