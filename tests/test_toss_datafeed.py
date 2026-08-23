"""TossDataFeed.history(interval="1d") 캐싱 — client는 페이크, 네트워크 없음.

일봉은 orb_scan이 심볼별 ATR 계산에 매 사이클(5s)마다 쓰는데, 캐시가 없으면
워치리스트가 커질수록 KR 09:05~09:10 진입 구간에서 5 TPS 차트 rate limit에
걸려 사이클이 밀리고 손절 평가까지 지연된다. 이 파일은 TTL 캐시(600s)와
실패 네거티브 캐시(60s)가 실제로 네트워크 호출 횟수를 줄이는지 검증한다.
"""
from __future__ import annotations

import pandas as pd
import pytest

import quant.adapters.brokers.toss.datafeed as datafeed_module
from quant.adapters.brokers.toss.datafeed import TossDataFeed
from quant.core.ports import DataSourceError

_SYMBOL = "TQQQ"


class _FakePricesClient:
    """prices()만 흉내내는 대역 — quote() 배치 조회 테스트용."""

    def __init__(self, rows_by_symbol: dict[str, dict] | None = None):
        self._rows = rows_by_symbol or {}
        self.calls: list[list[str]] = []
        self.fail_next: Exception | None = None

    def prices(self, symbols: list[str]) -> list[dict]:
        self.calls.append(list(symbols))
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        return [self._rows[s] for s in symbols if s in self._rows]


def _make_1d_bars(n_days: int, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n_days, freq="1D", tz="UTC")
    prices = [price + i for i in range(n_days)]
    return pd.DataFrame({
        "open": prices, "high": [p + 1 for p in prices], "low": [p - 1 for p in prices],
        "close": prices, "volume": [100.0] * n_days,
    }, index=idx)


class _FakeClient:
    """candles()만 흉내내는 대역. 실패를 흉내내려면 fail_next를 세팅한다."""

    def __init__(self, bars: pd.DataFrame):
        self._bars = bars
        self.call_count = 0
        self.fail_next: Exception | None = None

    def candles(self, symbol: str, interval: str = "day", count: int = 120) -> pd.DataFrame:
        self.call_count += 1
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        return self._bars.tail(count)


@pytest.fixture
def clock(monkeypatch):
    """time.monotonic()을 제어 가능한 값으로 고정한다."""
    state = {"now": 1000.0}

    def _monotonic():
        return state["now"]

    monkeypatch.setattr(datafeed_module.time, "monotonic", _monotonic)

    def _advance(seconds: float):
        state["now"] += seconds

    return _advance


def test_second_call_within_ttl_does_not_call_client_again(clock, tmp_path):
    client = _FakeClient(_make_1d_bars(120))
    feed = TossDataFeed(client, cache_dir=tmp_path)

    feed.history(_SYMBOL, "1d", 20)
    feed.history(_SYMBOL, "1d", 20)

    assert client.call_count == 1


def test_different_n_served_from_same_cached_fetch(clock, tmp_path):
    client = _FakeClient(_make_1d_bars(120))
    feed = TossDataFeed(client, cache_dir=tmp_path)

    first = feed.history(_SYMBOL, "1d", 10)
    second = feed.history(_SYMBOL, "1d", 30)

    assert client.call_count == 1
    assert len(first) == 10
    assert len(second) == 30


def test_failure_is_cached_for_60_seconds(clock, tmp_path):
    client = _FakeClient(_make_1d_bars(120))
    client.fail_next = RuntimeError("network down")
    feed = TossDataFeed(client, cache_dir=tmp_path)

    with pytest.raises(DataSourceError):
        feed.history(_SYMBOL, "1d", 20)
    assert client.call_count == 1

    # 캐시된 실패 구간 안 — client를 다시 부르지 않고 즉시 raise
    clock(30)
    with pytest.raises(DataSourceError):
        feed.history(_SYMBOL, "1d", 20)
    assert client.call_count == 1


def test_after_ttl_expiry_client_is_called_again(clock, tmp_path):
    client = _FakeClient(_make_1d_bars(120))
    feed = TossDataFeed(client, cache_dir=tmp_path)

    feed.history(_SYMBOL, "1d", 20)
    assert client.call_count == 1

    clock(601)
    feed.history(_SYMBOL, "1d", 20)
    assert client.call_count == 2


def test_failure_cache_expires_after_60_seconds(clock, tmp_path):
    client = _FakeClient(_make_1d_bars(120))
    client.fail_next = RuntimeError("network down")
    feed = TossDataFeed(client, cache_dir=tmp_path)

    with pytest.raises(DataSourceError):
        feed.history(_SYMBOL, "1d", 20)
    assert client.call_count == 1

    clock(61)
    result = feed.history(_SYMBOL, "1d", 20)  # 실패 캐시 만료 — 재시도, 이번엔 성공
    assert client.call_count == 2
    assert len(result) == 20


# ------------------------------------------------------ quote() 배치 조회

def _price_row(symbol: str, price: str) -> dict:
    return {"symbol": symbol, "timestamp": "2024-06-03T05:00:00Z", "lastPrice": price}


def test_quote_with_preloaded_symbols_costs_a_single_call(clock, tmp_path):
    """생성 시점에 symbols를 미리 주면, 그 심볼들을 각각 quote()해도 실제 HTTP
    호출은 배치 1회로 끝난다 — 관심종목이 커져도 사이클당 quote 호출이 상수다."""
    rows = {s: _price_row(s, "10.0") for s in ("TQQQ", "SQQQ", "005930")}
    client = _FakePricesClient(rows)
    feed = TossDataFeed(client, cache_dir=tmp_path, symbols=["TQQQ", "SQQQ", "005930"])

    feed.quote("TQQQ")
    feed.quote("SQQQ")
    feed.quote("005930")

    assert len(client.calls) == 1
    assert client.calls[0] == ["005930", "SQQQ", "TQQQ"]


def test_quote_grows_batch_as_new_symbols_are_requested(clock, tmp_path):
    """symbols를 안 주면(symbols=None) quote()가 불릴 때마다 그 심볼을 누적한다 —
    새 심볼은 아직 캐시가 없으므로 그 순간엔 다시 배치 호출이 나가지만, 그 다음부터는
    같은 TTL 안에서 함께 캐시된다."""
    rows = {"TQQQ": _price_row("TQQQ", "51.0"), "SQQQ": _price_row("SQQQ", "12.5")}
    client = _FakePricesClient(rows)
    feed = TossDataFeed(client, cache_dir=tmp_path)

    q1 = feed.quote("TQQQ")
    q2 = feed.quote("SQQQ")

    assert q1.price == 51.0
    assert q2.price == 12.5
    assert client.calls == [["TQQQ"], ["SQQQ", "TQQQ"]]


def test_quote_cache_ttl_forces_refetch(clock, tmp_path):
    client = _FakePricesClient({"TQQQ": _price_row("TQQQ", "51.0")})
    feed = TossDataFeed(client, cache_dir=tmp_path, symbols=["TQQQ"])
    ttl = datafeed_module._QUOTE_CACHE_FRESH_SECONDS

    feed.quote("TQQQ")
    assert len(client.calls) == 1

    clock(ttl - 0.5)  # TTL 이내 — 재조회하지 않음
    feed.quote("TQQQ")
    assert len(client.calls) == 1

    clock(1.0)  # 누적으로 TTL 초과 — 재조회
    feed.quote("TQQQ")
    assert len(client.calls) == 2


def test_quote_returns_none_for_symbol_missing_from_response(clock, tmp_path):
    """조회 자체는 성공했지만 그 심볼의 응답이 없다 — 실패가 아니라 None(위장 금지)."""
    client = _FakePricesClient({"TQQQ": _price_row("TQQQ", "51.0")})
    feed = TossDataFeed(client, cache_dir=tmp_path, symbols=["TQQQ", "GHOST"])

    assert feed.quote("GHOST") is None
    assert feed.quote("TQQQ").price == 51.0


def test_quote_skips_malformed_row_without_crashing(clock, tmp_path):
    rows = {
        "TQQQ": _price_row("TQQQ", "51.0"),
        "BAD": {"symbol": "BAD", "timestamp": "2024-06-03T05:00:00Z"},  # lastPrice 없음
    }
    client = _FakePricesClient(rows)
    feed = TossDataFeed(client, cache_dir=tmp_path, symbols=["TQQQ", "BAD"])

    assert feed.quote("TQQQ").price == 51.0
    assert feed.quote("BAD") is None


def test_quote_raises_data_source_error_on_client_failure(clock, tmp_path):
    client = _FakePricesClient({})
    client.fail_next = RuntimeError("network down")
    feed = TossDataFeed(client, cache_dir=tmp_path, symbols=["TQQQ"])

    with pytest.raises(DataSourceError):
        feed.quote("TQQQ")
