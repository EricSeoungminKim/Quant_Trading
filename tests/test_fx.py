"""FxProvider 구현체 테스트: FixedFxProvider(결정론적), DailyFxProvider(일 1회 캐시/
재조회, 실패 시 마지막 값 또는 fallback으로 대체, 예외를 던지지 않음)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from quant.core.fx import DailyFxProvider, FixedFxProvider

NY = ZoneInfo("America/New_York")


class FakeClock:
    def __init__(self, now: datetime, cadence_minutes: float = 15.0):
        self._now = now
        self._cadence = cadence_minutes

    def set(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return self._cadence

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        mtc = self.minutes_to_close(market)
        return mtc is not None and mtc - self._cadence < flatten_minutes


def test_fixed_fx_provider_returns_rate():
    fx = FixedFxProvider(1_300.0)
    assert fx.usd_krw() == 1_300.0
    assert fx.usd_krw() == 1_300.0  # 항상 동일 — 결정론적


def test_daily_fx_provider_fetches_once_per_day():
    clock = FakeClock(datetime(2026, 1, 5, 9, 30, tzinfo=NY))
    calls = []

    def fetch() -> float:
        calls.append(1)
        return 1_350.0

    fx = DailyFxProvider(fetch=fetch, clock=clock)
    assert fx.usd_krw() == 1_350.0
    assert fx.usd_krw() == 1_350.0  # 같은 날 — 재조회 없음
    assert len(calls) == 1


def test_daily_fx_provider_refetches_on_date_roll():
    clock = FakeClock(datetime(2026, 1, 5, 9, 30, tzinfo=NY))
    rates = iter([1_350.0, 1_360.0])

    def fetch() -> float:
        return next(rates)

    fx = DailyFxProvider(fetch=fetch, clock=clock)
    assert fx.usd_krw() == 1_350.0

    clock.set(datetime(2026, 1, 6, 9, 30, tzinfo=NY))  # 다음 거래일 첫 호출
    assert fx.usd_krw() == 1_360.0


def test_daily_fx_provider_falls_back_to_last_known_rate_on_fetch_failure():
    clock = FakeClock(datetime(2026, 1, 5, 9, 30, tzinfo=NY))
    should_fail = [False]

    def fetch() -> float:
        if should_fail[0]:
            raise RuntimeError("network down")
        return 1_350.0

    fx = DailyFxProvider(fetch=fetch, clock=clock)
    assert fx.usd_krw() == 1_350.0

    clock.set(datetime(2026, 1, 6, 9, 30, tzinfo=NY))
    should_fail[0] = True
    assert fx.usd_krw() == 1_350.0  # 조회 실패 — 마지막 성공 값 유지, 예외 없음


def test_daily_fx_provider_falls_back_to_fallback_when_never_fetched():
    clock = FakeClock(datetime(2026, 1, 5, 9, 30, tzinfo=NY))

    def fetch() -> float:
        raise RuntimeError("network down")

    fx = DailyFxProvider(fetch=fetch, clock=clock, fallback=1_500.0)
    assert fx.usd_krw() == 1_500.0  # 첫 조회부터 실패 — fallback
