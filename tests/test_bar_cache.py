"""MarketDataService 봉(bars) 공유 캐시 테스트. 가짜 클록·가짜 소스만 — 네트워크 없음.

왜 이 캐시가 있는가: 스캘핑 전략을 병렬로 여러 개 돌리면 각 전략이 같은 종목의 같은
봉을 매 사이클 새로 요청한다. 전략 8개 × 20종목이면 사이클(5초)마다 160회 브로커
API를 때린다 — Toss MARKET_DATA는 10 TPS라 그 자체로 rate limit이고, 순수 껍질
(PureStrategyShell)이 정적 DataNeeds로 매 사이클 전량을 다시 요청하기 때문에 전략을
늘릴수록 선형으로 나빠진다. 캐시 유효성 기준을 TTL이 아니라 **봉 경계**로 잡으면
"1분봉은 1분에 한 번만 바뀐다"는 사실이 그대로 정확한 캐시 정책이 된다.

커버리지: 같은 경계 내 재사용, 경계 전환 시 재조회, n 처리(큰 요청은 재조회/작은
요청은 슬라이스), 실패 무캐시, 비활성화 시 기존 동작 보존, interval 격리, 상한 정리,
그리고 160→20 성능 회귀 고정.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from quant.adapters.data.service import Capability, MarketDataService, SourceRoute

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


# --------------------------------------------------------------------- helpers

class FakeClock:
    """now()를 테스트가 직접 밀어 넣는 클록. 봉 경계 계산의 유일한 입력이다."""

    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return 1.0

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


class CountingSource:
    """history() 호출을 전부 기록하는 소스. 봉은 요청 시점의 클록 기준으로 생성한다."""

    def __init__(self, clock: FakeClock, *, periods: int = 400, empty: bool = False,
                 error: Exception | None = None):
        self._clock = clock
        self._periods = periods
        self._empty = empty
        self._error = error
        self.history_calls: list[tuple[str, str, int]] = []

    def quote(self, symbol: str):
        return None

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        self.history_calls.append((symbol, interval, n))
        if self._error is not None:
            raise self._error
        if self._empty:
            return pd.DataFrame(columns=_OHLCV_COLUMNS)
        return _bars(self._clock.now(), min(n, self._periods), interval)


def _bars(now: datetime, periods: int, interval: str) -> pd.DataFrame:
    """now 직전에 끝난 봉까지 periods개. 마지막 봉은 이미 완성된 것으로 만든다 —
    완성봉 필터가 데이터를 통째로 잘라내면 캐시 동작이 아니라 필터를 테스트하게 된다."""
    minutes = 24 * 60 if interval == "1d" else int(interval.rstrip("m"))
    freq = f"{minutes}min"
    last_open = pd.Timestamp(now).floor(freq) - pd.Timedelta(minutes=minutes)
    idx = pd.date_range(end=last_open, periods=periods, freq=freq, tz="UTC")
    prices = [100.0 + i for i in range(periods)]
    return pd.DataFrame({
        "open": prices, "high": [p + 1 for p in prices], "low": [p - 1 for p in prices],
        "close": prices, "volume": [10.0] * periods,
    }, index=idx)


def _service(source, clock: FakeClock, **kwargs) -> MarketDataService:
    return MarketDataService(
        routes=[SourceRoute(name="fake", source=source,
                            capabilities=frozenset({Capability.QUOTE, Capability.BARS}))],
        clock=clock,
        **kwargs,
    )


# ------------------------------------------------------- ① 같은 경계 = 소스 1회

def test_two_calls_in_same_bar_boundary_hit_source_once():
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 20, tzinfo=timezone.utc))
    source = CountingSource(clock)
    svc = _service(source, clock)

    first = svc.history("TQQQ", "1m", 50)
    clock.advance(seconds=25)  # 같은 1분봉 안에서 두 번째 호출
    second = svc.history("TQQQ", "1m", 50)

    assert len(source.history_calls) == 1
    assert len(first) == 50 and len(second) == 50
    pd.testing.assert_frame_equal(first, second)
    assert svc.bar_cache_stats() == {"hits": 1, "misses": 1, "source_calls": 1}


# --------------------------------------------------------- ② 경계를 넘으면 재조회

def test_crossing_bar_boundary_refetches():
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 50, tzinfo=timezone.utc))
    source = CountingSource(clock)
    svc = _service(source, clock)

    svc.history("TQQQ", "1m", 50)
    clock.advance(seconds=20)  # 13:31:10 — 다음 봉
    svc.history("TQQQ", "1m", 50)

    assert len(source.history_calls) == 2
    assert svc.bar_cache_stats()["hits"] == 0
    assert svc.bar_cache_stats()["misses"] == 2


def test_boundary_is_interval_sized_not_wall_clock_ttl():
    """15분봉은 같은 15분 버킷 안이면 14분이 흘러도 캐시가 유효하다. 시간 기반 TTL이면
    벌써 만료됐을 구간 — 이게 경계 캐시를 쓰는 이유다."""
    clock = FakeClock(datetime(2026, 8, 28, 13, 0, 30, tzinfo=timezone.utc))
    source = CountingSource(clock)
    svc = _service(source, clock)

    svc.history("TQQQ", "15m", 30)
    clock.advance(minutes=14)  # 13:14:30 — 여전히 13:00 버킷
    svc.history("TQQQ", "15m", 30)
    assert len(source.history_calls) == 1

    clock.advance(minutes=1)  # 13:15:30 — 새 버킷
    svc.history("TQQQ", "15m", 30)
    assert len(source.history_calls) == 2


# ------------------------------------------------- ③ n 처리: 큰 요청만 재조회

def test_larger_n_refetches_and_smaller_n_slices_the_cached_frame():
    """작은 요청이 큰 요청을 무효화하면 캐시가 매 사이클 갈린다 — 전략마다 요구
    봉 수가 다른 이 시스템에서는 그게 곧 캐시 무용지물이다."""
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    source = CountingSource(clock)
    svc = _service(source, clock)

    big = svc.history("TQQQ", "1m", 200)
    assert len(source.history_calls) == 1

    small = svc.history("TQQQ", "1m", 20)  # 캐시 슬라이스 — 소스 재호출 없음
    assert len(source.history_calls) == 1
    assert len(small) == 20
    pd.testing.assert_frame_equal(small, big.tail(20))

    bigger = svc.history("TQQQ", "1m", 300)  # 더 크면 재조회
    assert len(source.history_calls) == 2
    assert len(bigger) == 300

    # 재조회 결과가 캐시를 교체했으므로 그 뒤의 200 요청은 다시 히트다.
    svc.history("TQQQ", "1m", 200)
    assert len(source.history_calls) == 2


def test_short_history_does_not_thrash_the_cache():
    """소스가 요청보다 적은 봉만 가진 경우(신규 상장·얕은 히스토리). 캐시 유효성을
    '행 수'로 판정하면 n=200으로 30개를 받은 뒤 n=50 요청이 영원히 miss가 난다."""
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    source = CountingSource(clock, periods=30)
    svc = _service(source, clock)

    assert len(svc.history("NEWCO", "1m", 200)) == 30
    assert len(svc.history("NEWCO", "1m", 50)) == 30
    assert len(source.history_calls) == 1


# ------------------------------------------------------------- ④ 실패는 무캐시

def test_empty_frame_is_not_cached():
    """빈 프레임을 캐시하면 그 봉 내내 모든 전략이 데이터 없이 돈다 — 손절 판정이
    조용히 멈춘다. 다음 호출은 반드시 소스를 다시 쳐야 한다."""
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    source = CountingSource(clock, empty=True)
    svc = _service(source, clock)

    assert svc.history("TQQQ", "1m", 50).empty
    assert svc.history("TQQQ", "1m", 50).empty
    assert len(source.history_calls) == 2
    assert svc.bar_cache_stats()["hits"] == 0


def test_source_exception_is_not_cached_and_recovers_within_same_boundary():
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    broken = CountingSource(clock, error=RuntimeError("network down"))
    svc = _service(broken, clock)

    assert svc.history("TQQQ", "1m", 50).empty
    assert svc.health().degraded

    # 같은 봉 경계 안에서 소스가 회복되면 즉시 데이터를 받아야 한다 —
    # 실패를 캐시했다면 이 분이 끝날 때까지 빈 프레임만 나온다.
    broken._error = None
    assert len(svc.history("TQQQ", "1m", 50)) == 50
    assert len(broken.history_calls) == 2


# ------------------------------------------------- ⑤ 비활성화 = 기존 동작 그대로

def test_disabled_cache_calls_source_every_time():
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    source = CountingSource(clock)
    svc = _service(source, clock, bar_cache_enabled=False)

    for _ in range(5):
        assert len(svc.history("TQQQ", "1m", 50)) == 50
    assert len(source.history_calls) == 5
    assert svc.bar_cache_stats() == {"hits": 0, "misses": 0, "source_calls": 5}


def test_disabled_cache_matches_enabled_cache_output():
    """캐시는 성능 최적화일 뿐 결과를 바꾸면 안 된다 — 같은 입력에 같은 프레임."""
    clock_a = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    clock_b = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    cached = _service(CountingSource(clock_a), clock_a)
    uncached = _service(CountingSource(clock_b), clock_b, bar_cache_enabled=False)

    for interval, n in (("1m", 50), ("15m", 30), ("1d", 10)):
        pd.testing.assert_frame_equal(
            cached.history("TQQQ", interval, n), uncached.history("TQQQ", interval, n)
        )


# ------------------------------------------------------------ ⑥ interval 격리

def test_different_intervals_are_cached_separately():
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    source = CountingSource(clock)
    svc = _service(source, clock)

    m1 = svc.history("TQQQ", "1m", 50)
    m15 = svc.history("TQQQ", "15m", 50)

    assert [c[1] for c in source.history_calls] == ["1m", "15m"]
    # 서로의 캐시를 밟지 않았는지 — 각각 다시 물으면 소스 재호출이 없어야 한다.
    pd.testing.assert_frame_equal(svc.history("TQQQ", "1m", 50), m1)
    pd.testing.assert_frame_equal(svc.history("TQQQ", "15m", 50), m15)
    assert len(source.history_calls) == 2
    # 봉 간격이 다르면 인덱스 간격도 달라야 한다(같은 캐시를 공유하지 않았다는 증거).
    assert m1.index[-1] - m1.index[-2] == pd.Timedelta(minutes=1)
    assert m15.index[-1] - m15.index[-2] == pd.Timedelta(minutes=15)


# ---------------------------------------------------------- ⑦ 메모리 상한 정리

def test_cache_evicts_oldest_when_over_max_entries():
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    source = CountingSource(clock)
    svc = _service(source, clock, bar_cache_max_entries=3)

    for sym in ("A", "B", "C", "D"):
        svc.history(sym, "1m", 20)
    assert len(source.history_calls) == 4

    # 최신 3개(B·C·D)는 남아 히트, 가장 오래된 A는 밀려나 재조회된다.
    for sym in ("B", "C", "D"):
        svc.history(sym, "1m", 20)
    assert len(source.history_calls) == 4

    svc.history("A", "1m", 20)
    assert len(source.history_calls) == 5


def test_stale_boundaries_do_not_accumulate():
    """키에 봉 경계가 들어 있으므로 정리하지 않으면 분마다 항목이 쌓인다 — 이 저장소는
    1.8GB EC2에서 무인으로 며칠씩 돌기 때문에 상한만으로는 부족하다."""
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    source = CountingSource(clock)
    svc = _service(source, clock)

    for _ in range(120):  # 2시간치 1분봉
        svc.history("TQQQ", "1m", 20)
        svc.history("SQQQ", "1m", 20)
        clock.advance(minutes=1)

    assert len(svc._bar_cache) == 2  # 심볼 2개 × interval 1개 — 경계 수와 무관


# ------------------------------------------- 성능 회귀 고정: 8전략 × 20종목


def test_eight_strategies_twenty_symbols_hit_source_twenty_times_not_160():
    """소유자 지시(2026-08-28)의 핵심 수치를 테스트로 고정한다. 전략을 늘려도 소스
    호출은 심볼 수에 묶여야 한다 — 여기가 깨지면 병렬 스캘핑 실험이 rate limit에
    막힌다."""
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    source = CountingSource(clock)
    svc = _service(source, clock)
    symbols = [f"SYM{i:02d}" for i in range(20)]

    for _strategy in range(8):
        for sym in symbols:
            assert len(svc.history(sym, "1m", 100)) == 100

    assert len(source.history_calls) == 20  # 캐시 없으면 160
    stats = svc.bar_cache_stats()
    assert stats == {"hits": 140, "misses": 20, "source_calls": 20}

    # 대조군: 캐시를 끄면 실제로 160회다(20이 캐시 덕분임을 증명한다).
    bare_clock = FakeClock(datetime(2026, 8, 28, 13, 30, 10, tzinfo=timezone.utc))
    bare_source = CountingSource(bare_clock)
    bare = _service(bare_source, bare_clock, bar_cache_enabled=False)
    for _strategy in range(8):
        for sym in symbols:
            bare.history(sym, "1m", 100)
    assert len(bare_source.history_calls) == 160


def test_next_cycle_within_same_bar_still_hits_cache():
    """1분봉 · poll 5초면 한 봉 안에 사이클이 12번 돈다. 캐시가 사이클 경계에서
    풀리면(=TTL 방식) 절감분의 대부분이 사라진다."""
    clock = FakeClock(datetime(2026, 8, 28, 13, 30, 0, tzinfo=timezone.utc))
    source = CountingSource(clock)
    svc = _service(source, clock)
    symbols = [f"SYM{i:02d}" for i in range(20)]

    for _cycle in range(12):  # 12 사이클 × 8 전략 × 20 종목 = 1,920 요청
        for _strategy in range(8):
            for sym in symbols:
                svc.history(sym, "1m", 100)
        clock.advance(seconds=5)

    assert len(source.history_calls) == 20  # 봉이 안 바뀌었으므로 여전히 20회
