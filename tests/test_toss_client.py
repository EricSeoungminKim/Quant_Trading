"""TossClient: rate limiter TPS math, token cache reuse/expiry, single 401 retry.

All HTTP is intercepted via httpx.MockTransport — no real network calls.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, time as dtime
from unittest.mock import patch

import httpx
import pytest

from quant.adapters.brokers.toss.client import (
    KST,
    TossAPIError,
    TossClient,
    _RateLimiter,
)


def _make_client(tmp_path, transport: httpx.MockTransport, **kwargs) -> TossClient:
    client = TossClient(
        client_id="cid", client_secret="secret", account_seq="123",
        mode=kwargs.pop("mode", "live"), cache_dir=tmp_path,
    )
    client._http = httpx.Client(base_url="https://openapi.tossinvest.com", transport=transport)
    return client


# --------------------------------------------------------------- rate limiter

def test_rate_limiter_enforces_min_interval_between_calls():
    limiter = _RateLimiter()
    start = time.monotonic()
    limiter.wait("MARKET_DATA")  # 10 TPS -> 0.1s min interval
    limiter.wait("MARKET_DATA")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.1


def test_rate_limiter_independent_groups_dont_block_each_other():
    limiter = _RateLimiter()
    limiter.wait("MARKET_DATA")
    start = time.monotonic()
    limiter.wait("STOCK")  # different group, should not wait for MARKET_DATA's budget
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


def test_rate_limiter_uses_peak_tps_for_order_group_during_peak_window():
    limiter = _RateLimiter()
    peak_time = datetime(2026, 7, 28, 9, 5, tzinfo=KST)
    with patch("quant.adapters.brokers.toss.client.datetime") as mock_dt:
        mock_dt.now.return_value = peak_time
        start = time.monotonic()
        limiter.wait("ORDER")  # peak TPS = 3 -> min interval 1/3s
        limiter.wait("ORDER")
        elapsed = time.monotonic() - start
    assert elapsed >= 1.0 / 3


# ------------------------------------------------------------------ token cache

def test_token_cache_reused_when_not_expired(tmp_path):
    (tmp_path / "toss_token.json").write_text(json.dumps({
        "access_token": "cached-token",
        "expires_at": time.time() + 3600,
    }))
    token_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            token_calls["n"] += 1
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 86400})
        assert request.headers["authorization"] == "Bearer cached-token"
        return httpx.Response(200, json={"result": [{"symbol": "AAPL", "lastPrice": "100"}]})

    client = _make_client(tmp_path, httpx.MockTransport(handler))
    result = client.prices(["AAPL"])

    assert token_calls["n"] == 0  # never hit /oauth2/token — cached token reused
    assert result[0]["lastPrice"] == "100"


def test_token_cache_expired_triggers_refetch(tmp_path):
    (tmp_path / "toss_token.json").write_text(json.dumps({
        "access_token": "stale-token",
        "expires_at": time.time() - 10,  # already expired
    }))
    token_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            token_calls["n"] += 1
            return httpx.Response(200, json={"access_token": "new-token", "expires_in": 86400})
        assert request.headers["authorization"] == "Bearer new-token"
        return httpx.Response(200, json={"result": [{"symbol": "AAPL", "lastPrice": "100"}]})

    client = _make_client(tmp_path, httpx.MockTransport(handler))
    client.prices(["AAPL"])

    assert token_calls["n"] == 1
    saved = json.loads((tmp_path / "toss_token.json").read_text())
    assert saved["access_token"] == "new-token"


# ------------------------------------------------------------------- 401 retry

def test_401_triggers_exactly_one_token_refetch_then_succeeds(tmp_path):
    token_calls = {"n": 0}
    price_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            token_calls["n"] += 1
            return httpx.Response(200, json={
                "access_token": f"token-{token_calls['n']}", "expires_in": 86400,
            })
        price_calls["n"] += 1
        if price_calls["n"] == 1:
            return httpx.Response(401, json={"error": {"code": "expired-token", "message": "x"}})
        return httpx.Response(200, json={"result": [{"symbol": "AAPL", "lastPrice": "150"}]})

    client = _make_client(tmp_path, httpx.MockTransport(handler))
    result = client.prices(["AAPL"])

    assert token_calls["n"] == 2  # initial fetch + exactly one 401-triggered refetch
    assert price_calls["n"] == 2
    assert result[0]["lastPrice"] == "150"


def test_401_retry_is_bounded_not_infinite(tmp_path):
    token_calls = {"n": 0}
    price_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            token_calls["n"] += 1
            return httpx.Response(200, json={
                "access_token": f"token-{token_calls['n']}", "expires_in": 86400,
            })
        price_calls["n"] += 1
        return httpx.Response(401, json={"error": {"code": "expired-token", "message": "still bad"}})

    client = _make_client(tmp_path, httpx.MockTransport(handler))
    with pytest.raises(TossAPIError):
        client.prices(["AAPL"])

    assert token_calls["n"] == 2  # initial fetch + exactly one retry, no more
    assert price_calls["n"] == 2  # original attempt + one retry, then raise


# ------------------------------------------------------------------- mode guard

def test_holdings_refuses_outside_live_mode(tmp_path):
    client = _make_client(tmp_path, httpx.MockTransport(lambda r: httpx.Response(200)), mode="paper")
    with pytest.raises(RuntimeError):
        client.holdings()


# --------------------------------------------------------------- order status

def test_order_status_refuses_outside_live_mode(tmp_path):
    client = _make_client(tmp_path, httpx.MockTransport(lambda r: httpx.Response(200)), mode="paper")
    with pytest.raises(RuntimeError):
        client.order("order-123")


# ------------------------------------------------------------------- rankings

def test_rankings_hits_correct_endpoint_with_params(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 86400})
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"result": {
            "rankedAt": "2026-06-10T14:30:00+09:00",
            "rankings": [
                {"rank": 1, "symbol": "005930", "currency": "KRW",
                 "price": {"lastPrice": "56500", "basePrice": "55800", "changeRate": "0.0125"},
                 "tradingVolume": "18432100", "tradingAmount": "1041436650000"},
            ],
        }})

    client = _make_client(tmp_path, httpx.MockTransport(handler), mode="paper")
    result = client.rankings(type="MARKET_TRADING_AMOUNT", market_country="KR", count=20)

    assert seen["path"] == "/api/v1/rankings"
    assert seen["params"] == {
        "type": "MARKET_TRADING_AMOUNT", "marketCountry": "KR",
        "duration": "realtime", "count": "20", "excludeInvestmentCaution": "false",
    }
    assert result["rankings"][0]["symbol"] == "005930"


def test_order_status_hits_correct_endpoint_and_returns_result(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 86400})
        seen["path"] = request.url.path
        seen["account_header"] = request.headers.get("x-tossinvest-account")
        return httpx.Response(200, json={"result": {
            "orderId": "order-123",
            "symbol": "005930",
            "side": "BUY",
            "status": "FILLED",
            "quantity": "10",
            "execution": {
                "filledQuantity": "10",
                "averageFilledPrice": "70000",
                "filledAmount": "700000",
                "commission": "1400",
                "tax": "0",
                "filledAt": "2026-03-28T09:31:15+09:00",
                "settlementDate": "2026-03-30",
            },
        }})

    client = _make_client(tmp_path, httpx.MockTransport(handler))
    result = client.order("order-123")

    assert seen["path"] == "/api/v1/orders/order-123"
    assert seen["account_header"] == "123"
    assert result["status"] == "FILLED"
    assert result["execution"]["filledQuantity"] == "10"
