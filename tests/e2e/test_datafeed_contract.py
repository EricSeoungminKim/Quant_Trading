"""DataFeed 계약 준수 테스트 — StubDataFeed/HistoryDataFeed/MarketDataService/
TossDataFeed(client는 페이크) 전부에 동일한 계약 스위트를 돌린다.

이 테스트가 직접 겨냥하는 결함(#2, WHY THIS EXISTS): `resample_1m(df, minutes: int)`가
문자열 `"15m"`로 호출되어 `"15mmin"`이라는 존재하지 않는 pandas frequency를 만들며
매 호출 raise됐다. 게다가 시그니처가 다른 "fallback shim"이 그 실패를 가려서 유닛
테스트는 통과했다. 개별 어댑터를 하나씩 골라 mock으로 테스트하면 이런 시그니처/포맷
불일치가 어댑터별로 다르게 숨을 수 있다 — 그래서 domain.interfaces.DataFeed 계약을
"모든" 구현체에 동일하게 강제하는 이 파일이 필요하다.

domain/interfaces.py의 DataFeed.history() 계약:
- interval: "1m" | "5m" | "15m" | "1d" — 전부 예외 없이 처리해야 한다.
- columns=[open,high,low,close,volume] (순서 고정), index=DatetimeIndex(tz-aware).
- 현재 형성 중인 봉은 절대 포함하지 않는다(look-ahead 금지).
- 데이터가 없으면 예외가 아니라 컬럼만 맞는 빈 프레임을 반환한다.

TossDataFeed는 Clock을 주입받지 않는다 — look-ahead 방지는 실 API가 완성봉만
내려준다는 전제(및 MarketDataService의 최종 방어선, tests/test_service.py의
test_lookahead_bars_from_sloppy_source_are_filtered)에 위임한다. 그래서 이 파일의
look-ahead 서브체크는 TossDataFeed 케이스에서만 건너뛴다(문서화된 설계, 결함 아님).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from quant.adapters.brokers.toss.datafeed import TossDataFeed
from quant.adapters.data.history import HistoryDataFeed
from quant.adapters.data.service import Capability, MarketDataService, SourceRoute
from quant.adapters.data.stub import StubDataFeed

_EXPECTED_COLUMNS = ["open", "high", "low", "close", "volume"]
_INTERVALS = ("1m", "5m", "15m", "1d")
_SYMBOL = "TQQQ"
_MISSING_SYMBOL = "NOSUCHSYMBOL"


def _make_1m_bars(start: str, n_minutes: int, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n_minutes, freq="1min", tz="UTC")
    prices = [price + i * 0.01 for i in range(n_minutes)]
    return pd.DataFrame({
        "open": prices, "high": [p + 0.5 for p in prices], "low": [p - 0.5 for p in prices],
        "close": prices, "volume": [10.0] * n_minutes,
    }, index=idx)


def _make_1d_bars(start: str, n_days: int, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n_days, freq="1D", tz="UTC")
    prices = [price + i for i in range(n_days)]
    return pd.DataFrame({
        "open": prices, "high": [p + 1 for p in prices], "low": [p - 1 for p in prices],
        "close": prices, "volume": [100.0] * n_days,
    }, index=idx)


class _FakeTossClient:
    """TossClient 대역 — 네트워크 없이 candles()/prices()만 흉내낸다."""

    def __init__(self, bars_1m: pd.DataFrame, bars_1d: pd.DataFrame):
        self._bars_1m = {_SYMBOL: bars_1m}
        self._bars_1d = {_SYMBOL: bars_1d}

    def prices(self, symbols: list[str]) -> list[dict]:
        return []

    def candles(self, symbol: str, interval: str = "day", count: int = 120) -> pd.DataFrame:
        source = self._bars_1d if interval == "1d" else self._bars_1m
        df = source.get(symbol)
        if df is None:
            return pd.DataFrame(columns=_EXPECTED_COLUMNS)
        return df.tail(count)


class _FixedClock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return 15.0

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


@dataclass
class DataFeedCase:
    name: str
    feed: Any
    now: pd.Timestamp
    enforce_lookahead: bool  # False = 이 구현체는 look-ahead 필터링을 자체적으로 하지 않는다(설계상)


def _stub_case() -> DataFeedCase:
    feed = StubDataFeed([_SYMBOL], days=5)
    now = feed.bars_1m[_SYMBOL].index[-1]
    feed.set_now(now)
    return DataFeedCase("stub", feed, now, enforce_lookahead=True)


def _history_case(history_dir: Path) -> DataFeedCase:
    bars = pd.concat([
        _make_1m_bars("2024-01-01T09:30:00Z", 390),
        _make_1m_bars("2024-01-02T09:30:00Z", 390),
        _make_1m_bars("2024-01-03T09:30:00Z", 390),
    ])
    part_path = history_dir / _SYMBOL / "2024" / "01.parquet"
    part_path.parent.mkdir(parents=True, exist_ok=True)
    bars.to_parquet(part_path)

    feed = HistoryDataFeed([_SYMBOL], history_dir=history_dir)
    now = feed.bars_1m[_SYMBOL].index[-1]
    feed.set_now(now)
    return DataFeedCase("history", feed, now, enforce_lookahead=True)


def _market_data_service_case() -> DataFeedCase:
    """MarketDataService 자체의 계약 준수를 검증한다 — 내부적으로는 StubDataFeed를
    감싸지만, ACL 레이어가 정규화/필터링을 다시 한번 거는지가 관심사다."""
    stub = StubDataFeed([_SYMBOL], days=5)
    now = stub.bars_1m[_SYMBOL].index[-1]
    stub.set_now(now)
    svc = MarketDataService(
        routes=[SourceRoute(
            name="stub", source=stub,
            capabilities=frozenset({Capability.QUOTE, Capability.BARS}),
        )],
        clock=_FixedClock(now),
    )
    return DataFeedCase("market_data_service", svc, now, enforce_lookahead=True)


def _toss_case(cache_dir: Path) -> DataFeedCase:
    bars_1m = _make_1m_bars("2024-01-02T09:30:00Z", 300)
    bars_1d = _make_1d_bars("2024-01-01", 5)
    client = _FakeTossClient(bars_1m, bars_1d)
    feed = TossDataFeed(client, cache_dir=cache_dir)
    now = bars_1m.index[-1]
    return DataFeedCase("toss", feed, now, enforce_lookahead=False)


@pytest.fixture(params=["stub", "history", "market_data_service", "toss"])
def datafeed_case(request, tmp_path) -> DataFeedCase:
    if request.param == "stub":
        return _stub_case()
    if request.param == "history":
        return _history_case(tmp_path / "history")
    if request.param == "market_data_service":
        return _market_data_service_case()
    return _toss_case(tmp_path / "toss_cache")


# --------------------------------------------------------------------- contract

def test_all_documented_intervals_return_without_raising(datafeed_case: DataFeedCase):
    for interval in _INTERVALS:
        datafeed_case.feed.history(_SYMBOL, interval, 500)  # 예외가 나면 여기서 바로 실패


def test_returns_documented_columns_in_order(datafeed_case: DataFeedCase):
    for interval in _INTERVALS:
        df = datafeed_case.feed.history(_SYMBOL, interval, 500)
        assert list(df.columns) == _EXPECTED_COLUMNS, f"{datafeed_case.name}/{interval}"


def test_index_is_tz_aware_and_monotonic_increasing(datafeed_case: DataFeedCase):
    for interval in _INTERVALS:
        df = datafeed_case.feed.history(_SYMBOL, interval, 500)
        if df.empty:
            continue
        assert isinstance(df.index, pd.DatetimeIndex), f"{datafeed_case.name}/{interval}"
        assert df.index.tz is not None, f"{datafeed_case.name}/{interval}: naive index"
        assert df.index.is_monotonic_increasing, f"{datafeed_case.name}/{interval}"


def test_never_returns_a_bar_whose_close_is_after_now(datafeed_case: DataFeedCase):
    if not datafeed_case.enforce_lookahead:
        pytest.skip(f"{datafeed_case.name}: look-ahead 필터링은 MarketDataService가 최종 방어선으로 담당(설계상)")
    for interval in _INTERVALS:
        df = datafeed_case.feed.history(_SYMBOL, interval, 500)
        if df.empty:
            continue
        minutes = 24 * 60 if interval == "1d" else int(interval.rstrip("m"))
        bar_close = df.index + pd.Timedelta(minutes=minutes)
        assert (bar_close <= datafeed_case.now).all(), f"{datafeed_case.name}/{interval}: look-ahead 유출"


def test_quote_never_comes_from_a_bar_that_has_not_closed_yet(datafeed_case: DataFeedCase):
    """look-ahead 금지는 history()뿐 아니라 quote()에도 적용된다.

    이 서브체크가 없어서 실제로 결함이 살아남았다: history()는 봉 **마감** 기준으로
    걸렀는데 quote()는 봉 **시가** 기준(`index <= now`)으로 걸러, 같은 피드가 두
    메서드에서 서로 다른 시각의 가격을 내놓았다. 15분봉 백테스트에서는 모든 체결가와
    손절/목표 판정에 15분치 미래 가격이 들어간다."""
    if not datafeed_case.enforce_lookahead:
        pytest.skip(f"{datafeed_case.name}: look-ahead 필터링은 MarketDataService가 최종 방어선으로 담당(설계상)")
    quote = datafeed_case.feed.quote(_SYMBOL)
    assert quote is not None, f"{datafeed_case.name}: quote 없음 — 검사가 공허하다"
    # 봉 시가 시각 ts의 봉이 마감된 뒤여야 그 종가를 현재가로 쓸 수 있다. 가장 촘촘한
    # 봉이 1분봉이므로 ts + 1분 <= now 이 최소 요건이다.
    assert quote.ts + pd.Timedelta(minutes=1) <= datafeed_case.now, (
        f"{datafeed_case.name}: 형성 중인 봉의 종가를 현재가로 쓰고 있다 "
        f"(quote.ts={quote.ts}, now={datafeed_case.now})"
    )


def test_no_data_returns_empty_frame_with_correct_columns_not_a_raise(datafeed_case: DataFeedCase):
    for interval in _INTERVALS:
        df = datafeed_case.feed.history(_MISSING_SYMBOL, interval, 500)  # raise하면 실패
        assert df.empty, f"{datafeed_case.name}/{interval}"
        assert list(df.columns) == _EXPECTED_COLUMNS, f"{datafeed_case.name}/{interval}"
