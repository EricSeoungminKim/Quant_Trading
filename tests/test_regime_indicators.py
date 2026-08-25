"""quant.adapters.regime_indicators — TossIndicatorClient(KOSPI/KOSDAQ ETF 프록시,
국채 미구현) + UpbitBitcoinAdapter(Upbit 공개 API) 계약 검증. 네트워크 호출 없음 —
전부 페이크 클라이언트/팩토리."""
from __future__ import annotations

import httpx
import pandas as pd
import pytest

from quant.adapters.regime_indicators import TossIndicatorClient, UpbitBitcoinAdapter


# --------------------------------------------------------------------- TossIndicatorClient


class _FakeTossClient:
    def __init__(self, *, closes: dict[str, list[float]] | None = None, raise_on_call: bool = False):
        self._closes = closes or {}
        self.raise_on_call = raise_on_call
        self.calls: list[tuple[str, str, int]] = []

    def candles(self, symbol: str, interval: str = "day", count: int = 2) -> pd.DataFrame:
        self.calls.append((symbol, interval, count))
        if self.raise_on_call:
            raise RuntimeError("network down")
        closes = self._closes.get(symbol)
        if closes is None:
            return pd.DataFrame(columns=["close"])
        return pd.DataFrame({"close": closes})


def test_kospi_proxy_returns_last_and_prev_close():
    fake = _FakeTossClient(closes={"069500": [400.0, 410.0]})
    c = TossIndicatorClient(fake)

    assert c.indicator_price("KOSPI") == 410.0
    assert c.indicator_prev_close("KOSPI") == 400.0
    # 두 조회 모두 069500 프록시 심볼로 나갔는지 확인
    assert all(call[0] == "069500" for call in fake.calls)


def test_kosdaq_proxy_returns_last_and_prev_close():
    fake = _FakeTossClient(closes={"229200": [1200.0, 1180.0]})
    c = TossIndicatorClient(fake)

    assert c.indicator_price("KOSDAQ") == 1180.0
    assert c.indicator_prev_close("KOSDAQ") == 1200.0


def test_proxy_network_failure_returns_none():
    fake = _FakeTossClient(raise_on_call=True)
    c = TossIndicatorClient(fake)

    assert c.indicator_price("KOSPI") is None
    assert c.indicator_prev_close("KOSPI") is None


def test_proxy_insufficient_candles_returns_none():
    fake = _FakeTossClient(closes={"069500": [410.0]})  # 1개뿐 — prev 계산 불가
    c = TossIndicatorClient(fake)

    assert c.indicator_price("KOSPI") is None
    assert c.indicator_prev_close("KOSPI") is None


def test_bond_symbol_returns_none_without_calling_client():
    fake = _FakeTossClient()
    c = TossIndicatorClient(fake)

    assert c.indicator_price("KR_BOND_10Y") is None
    assert c.indicator_prev_close("KR_BOND_10Y") is None
    assert fake.calls == []  # 미구현 심볼은 애초에 candles를 부르지 않는다


def test_bond_symbol_logs_unimplemented_warning_once(caplog):
    c = TossIndicatorClient(_FakeTossClient())

    with caplog.at_level("WARNING"):
        c.indicator_price("KR_BOND_10Y")
        c.indicator_price("KR_BOND_2Y")
        c.indicator_prev_close("KR_BOND_30Y")

    warnings = [r for r in caplog.records if "국채 지표" in r.getMessage()]
    assert len(warnings) == 1  # 최초 1회만


# --------------------------------------------------------------------- UpbitBitcoinAdapter


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self, payload=None, raise_on_get=False):
        self._payload = payload
        self._raise = raise_on_get

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        if self._raise:
            raise httpx.ConnectError("down")
        return _FakeResp(self._payload)


def test_upbit_price_change_pct_computes_from_ticker():
    payload = [{"trade_price": 110_000_000.0, "prev_closing_price": 100_000_000.0}]
    factory = lambda timeout=10.0: _FakeHttpClient(payload)
    adapter = UpbitBitcoinAdapter(client_factory=factory)

    assert adapter.price_change_pct() == pytest.approx(10.0)


def test_upbit_network_failure_returns_none():
    factory = lambda timeout=10.0: _FakeHttpClient(raise_on_get=True)
    adapter = UpbitBitcoinAdapter(client_factory=factory)

    assert adapter.price_change_pct() is None


def test_upbit_malformed_response_returns_none():
    factory = lambda timeout=10.0: _FakeHttpClient(payload=[{"unexpected": "shape"}])
    adapter = UpbitBitcoinAdapter(client_factory=factory)

    assert adapter.price_change_pct() is None


def test_upbit_empty_response_returns_none():
    factory = lambda timeout=10.0: _FakeHttpClient(payload=[])
    adapter = UpbitBitcoinAdapter(client_factory=factory)

    assert adapter.price_change_pct() is None
