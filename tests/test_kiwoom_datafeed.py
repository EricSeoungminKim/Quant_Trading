"""KiwoomRealtimeSource 테스트 — 전부 페이크 feed만 사용(네트워크/실 웹소켓 없음).

핵심 커버리지: 라우트 우선순위(kiwoom_rt가 살아 있으면 toss보다 먼저 응답),
**stale/연결끊김 시 DataSourceError로 toss 폴백**(가장 중요 — 죽은 웹소켓의
마지막 가격으로 거래하면 안 된다), 심볼 형식(KR/US) 분류.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quant.adapters.brokers.kiwoom.datafeed import KiwoomRealtimeSource, is_kr_symbol
from quant.adapters.data.service import Capability, MarketDataService, SourceRoute
from quant.core.ports import DataSourceError
from quant.core.models import Quote


class FakeHealth:
    def __init__(self, connected: bool):
        self.connected = connected


class FakeFeed:
    """KiwoomRealtimeFeed가 노출하는 quote()/health() 표면만 흉내낸다."""

    def __init__(self, connected: bool = True, quotes: dict[str, Quote] | None = None):
        self.connected = connected
        self._quotes = quotes or {}

    def health(self) -> FakeHealth:
        return FakeHealth(self.connected)

    def quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)


class FakeClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeTossSource:
    """MarketDataService 폴백 대상. 호출 여부/횟수를 기록한다."""

    def __init__(self, price: float = 99.0):
        self.price = price
        self.calls: list[str] = []

    def quote(self, symbol: str) -> Quote | None:
        self.calls.append(symbol)
        return Quote(symbol=symbol, ts=datetime.now(timezone.utc), price=self.price)

    def history(self, symbol: str, interval: str, n: int):  # pragma: no cover - 이 테스트들에서 안 씀
        raise NotImplementedError


# ------------------------------------------------------------ is_kr_symbol

def test_is_kr_symbol_classifies_six_digit_codes():
    assert is_kr_symbol("005930") is True
    assert is_kr_symbol("005930_NX") is True  # 거래소 접미사가 붙어도 앞 6자리로 판정
    assert is_kr_symbol("TQQQ") is False
    assert is_kr_symbol("SQQQ") is False
    assert is_kr_symbol("12345") is False  # 5자리는 KR 코드가 아님
    assert is_kr_symbol("") is False


# ------------------------------------------------------------- quote() 안전장치

def test_quote_returns_fresh_tick_when_connected_and_recent():
    now = datetime(2024, 6, 3, 5, 0, tzinfo=timezone.utc)
    tick = Quote(symbol="005930", ts=now - timedelta(seconds=5), price=71500.0)
    feed = FakeFeed(connected=True, quotes={"005930": tick})
    source = KiwoomRealtimeSource(feed, FakeClock(now), stale_seconds=30.0)

    q = source.quote("005930")

    assert q is tick


def test_quote_raises_when_disconnected():
    """웹소켓이 끊겨 있으면 마지막 값을 조용히 주지 않고 DataSourceError를 던진다."""
    now = datetime(2024, 6, 3, 5, 0, tzinfo=timezone.utc)
    tick = Quote(symbol="005930", ts=now - timedelta(seconds=1), price=71500.0)
    feed = FakeFeed(connected=False, quotes={"005930": tick})
    source = KiwoomRealtimeSource(feed, FakeClock(now), stale_seconds=30.0)

    with pytest.raises(DataSourceError):
        source.quote("005930")


def test_quote_raises_when_no_tick_received_yet():
    """구독은 됐지만 아직 틱이 한 번도 안 왔으면(quote()가 None) 지어내지 않고 실패시킨다."""
    now = datetime(2024, 6, 3, 5, 0, tzinfo=timezone.utc)
    feed = FakeFeed(connected=True, quotes={})
    source = KiwoomRealtimeSource(feed, FakeClock(now), stale_seconds=30.0)

    with pytest.raises(DataSourceError):
        source.quote("TQQQ")


def test_quote_raises_when_tick_is_stale():
    """연결은 돼 있어도 최신 틱이 임계보다 오래됐으면 죽은 걸로 보고 폴백시킨다."""
    now = datetime(2024, 6, 3, 5, 0, tzinfo=timezone.utc)
    stale_tick = Quote(symbol="005930", ts=now - timedelta(seconds=45), price=71500.0)
    feed = FakeFeed(connected=True, quotes={"005930": stale_tick})
    source = KiwoomRealtimeSource(feed, FakeClock(now), stale_seconds=30.0)

    with pytest.raises(DataSourceError):
        source.quote("005930")


def test_quote_accepts_tick_just_under_stale_threshold():
    now = datetime(2024, 6, 3, 5, 0, tzinfo=timezone.utc)
    fresh_tick = Quote(symbol="005930", ts=now - timedelta(seconds=29), price=71500.0)
    feed = FakeFeed(connected=True, quotes={"005930": fresh_tick})
    source = KiwoomRealtimeSource(feed, FakeClock(now), stale_seconds=30.0)

    assert source.quote("005930") is fresh_tick


def test_history_is_not_supported():
    feed = FakeFeed(connected=True)
    source = KiwoomRealtimeSource(feed, FakeClock(datetime.now(timezone.utc)))

    with pytest.raises(DataSourceError):
        source.history("005930", "15m", 10)


# ---------------------------------------------- MarketDataService 라우팅 통합

def test_kiwoom_rt_route_serves_before_toss_when_alive():
    now = datetime(2024, 6, 3, 5, 0, tzinfo=timezone.utc)
    tick = Quote(symbol="TQQQ", ts=now - timedelta(seconds=2), price=51.0)
    feed = FakeFeed(connected=True, quotes={"TQQQ": tick})
    kiwoom_source = KiwoomRealtimeSource(feed, FakeClock(now), stale_seconds=30.0)
    toss = FakeTossSource(price=999.0)

    svc = MarketDataService(
        routes=[
            SourceRoute(name="kiwoom_rt", source=kiwoom_source, capabilities=frozenset({Capability.QUOTE})),
            SourceRoute(name="toss", source=toss, capabilities=frozenset({Capability.QUOTE, Capability.BARS})),
        ],
        clock=FakeClock(now),
    )

    q = svc.quote("TQQQ")

    assert q is not None
    assert q.price == 51.0  # kiwoom_rt 응답이 사용됨
    assert toss.calls == []  # toss는 불리지 않음


def test_stale_kiwoom_rt_falls_back_to_toss_and_marks_degraded(caplog):
    """이 테스트가 이 기능의 핵심이다: 죽은 웹소켓의 마지막 가격으로 거래하면 안 된다."""
    now = datetime(2024, 6, 3, 5, 0, tzinfo=timezone.utc)
    stale_tick = Quote(symbol="TQQQ", ts=now - timedelta(seconds=90), price=51.0)
    feed = FakeFeed(connected=True, quotes={"TQQQ": stale_tick})
    kiwoom_source = KiwoomRealtimeSource(feed, FakeClock(now), stale_seconds=30.0)
    toss = FakeTossSource(price=52.5)

    svc = MarketDataService(
        routes=[
            SourceRoute(name="kiwoom_rt", source=kiwoom_source, capabilities=frozenset({Capability.QUOTE})),
            SourceRoute(name="toss", source=toss, capabilities=frozenset({Capability.QUOTE, Capability.BARS})),
        ],
        clock=FakeClock(now),
    )

    with caplog.at_level("WARNING"):
        q = svc.quote("TQQQ")

    assert q is not None
    assert q.price == 52.5  # toss로 폴백됨
    assert toss.calls == ["TQQQ"]

    health = svc.health()
    # 폴백이 성공했으므로 degraded는 False(2026-08-12 정의 변경). kiwoom_rt의 실패
    # 자체는 아래 소스별 상태로 그대로 드러난다 — 이 테스트가 지키려는 것은
    # "stale한 kiwoom_rt가 조용히 넘어가지 않는다"이고 그건 유지된다.
    assert health.degraded is False
    assert health.sources["kiwoom_rt"].healthy is False


def test_disconnected_kiwoom_rt_falls_back_to_toss():
    now = datetime(2024, 6, 3, 5, 0, tzinfo=timezone.utc)
    feed = FakeFeed(connected=False, quotes={})
    kiwoom_source = KiwoomRealtimeSource(feed, FakeClock(now), stale_seconds=30.0)
    toss = FakeTossSource(price=52.5)

    svc = MarketDataService(
        routes=[
            SourceRoute(name="kiwoom_rt", source=kiwoom_source, capabilities=frozenset({Capability.QUOTE})),
            SourceRoute(name="toss", source=toss, capabilities=frozenset({Capability.QUOTE, Capability.BARS})),
        ],
        clock=FakeClock(now),
    )

    q = svc.quote("TQQQ")

    assert q is not None
    assert q.price == 52.5
