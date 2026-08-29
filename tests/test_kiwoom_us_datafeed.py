"""KiwoomUSDataFeed 테스트 — 전부 페이크 클라이언트만 사용(네트워크 없음).

핵심 커버리지: quote()는 최신 행(형성 중이어도)을 그대로 쓰지만, history()는
클록 기준으로 완성되지 않은 마지막 분을 반드시 걸러낸다(look-ahead 금지 —
domain/interfaces.py 계약), 부호 붙은 가격 문자열 파싱, KR 심볼 거부, 벤더
예외의 DataSourceError 변환.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quant.adapters.brokers.kiwoom.client import KiwoomError
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


# ---------------------------------------------------------------- 실패 쿨다운
# 2026-08-29: usa06010이 폴링 사이클마다 모든 US 심볼에 재시도되며 429(rate
# limit)와 1903(종목 정보 없음)을 상시 유발했다(15~20분당 5,000~6,600건 실측).


def test_1903_permanently_excludes_symbol_without_further_network_calls():
    """1903(종목 정보 없음)은 같은 심볼을 다시 물어도 낫지 않는다 — 첫 실패 이후
    두 번째 호출부터는 네트워크를 아예 타지 않고 즉시 DataSourceError를 던진다."""
    client = FakeGlobalClient(_ROWS)
    feed = KiwoomUSDataFeed(client, FakeClock(_NOW))
    client.fail_next = KiwoomError(1903, "종목 정보 없음")

    with pytest.raises(DataSourceError):
        feed.quote("GLD")
    assert len(client.calls) == 1

    with pytest.raises(DataSourceError):
        feed.quote("GLD")
    assert len(client.calls) == 1, "1903 이후 재시도가 네트워크를 다시 탔다"

    # 다른 심볼은 영향받지 않는다 — 심볼별로 독립된 상태다.
    q = feed.quote("TQQQ")
    assert q is not None


def test_1903_logs_exclusion_once_at_info_level(caplog):
    client = FakeGlobalClient(_ROWS)
    feed = KiwoomUSDataFeed(client, FakeClock(_NOW))
    client.fail_next = KiwoomError(1903, "종목 정보 없음")

    with caplog.at_level(logging.INFO):
        with pytest.raises(DataSourceError):
            feed.quote("GLD")
        with pytest.raises(DataSourceError):
            feed.quote("GLD")

    info_msgs = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.INFO and "GLD" in r.getMessage()
    ]
    assert len(info_msgs) == 1, f"영구 제외 INFO 로그는 최초 1회여야 한다: {info_msgs}"


def test_other_errors_cooldown_after_threshold_then_retry_after_expiry(monkeypatch):
    """1903이 아닌 오류(429 등)는 연속 threshold회 실패해야 쿨다운에 들어가고,
    쿨다운 중에는 네트워크를 타지 않으며, 쿨다운 만료 후에는 다시 시도한다."""
    fake_now = {"t": 1_000.0}
    monkeypatch.setattr(
        "quant.adapters.brokers.kiwoom.us_datafeed.time.monotonic", lambda: fake_now["t"]
    )
    client = FakeGlobalClient(_ROWS)
    feed = KiwoomUSDataFeed(client, FakeClock(_NOW), failure_threshold=2, cooldown_seconds=100.0)
    err = KiwoomError(429, "rate limit exceeded")

    # 1회차 실패 — 아직 임계 미만, 네트워크는 탄다.
    client.fail_next = err
    with pytest.raises(DataSourceError):
        feed.quote("TQQQ")
    assert len(client.calls) == 1

    # 2회차 실패 — 임계(2) 도달, 쿨다운 진입.
    client.fail_next = err
    with pytest.raises(DataSourceError):
        feed.quote("TQQQ")
    assert len(client.calls) == 2

    # 쿨다운 중 — 네트워크를 타지 않고 즉시 실패.
    with pytest.raises(DataSourceError):
        feed.quote("TQQQ")
    assert len(client.calls) == 2, "쿨다운 중인데 네트워크를 다시 탔다"

    # 쿨다운 만료 후 — 다시 시도해 성공한다.
    fake_now["t"] += 100.1
    q = feed.quote("TQQQ")
    assert len(client.calls) == 3
    assert q is not None


def test_success_resets_consecutive_failure_count():
    """threshold 미만의 실패는 성공이 끼면 리셋된다 — 산발적 실패가 쌓여서
    쿨다운으로 이어지지 않는다."""
    client = FakeGlobalClient(_ROWS)
    feed = KiwoomUSDataFeed(client, FakeClock(_NOW), failure_threshold=2, cooldown_seconds=100.0)
    err = KiwoomError(429, "rate limit exceeded")

    client.fail_next = err
    with pytest.raises(DataSourceError):
        feed.quote("TQQQ")

    assert feed.quote("TQQQ") is not None  # 성공 — 카운터 리셋

    client.fail_next = err
    with pytest.raises(DataSourceError):
        feed.quote("TQQQ")  # 리셋됐으므로 이것만으로는 threshold(2) 미도달

    # 쿨다운에 들어가지 않았으므로 다음 호출은 네트워크를 정상적으로 탄다.
    q = feed.quote("TQQQ")
    assert q is not None
