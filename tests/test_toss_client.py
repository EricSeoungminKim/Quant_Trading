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
#
# paper 모드 게이트는 **계좌 상태를 바꾸는 호출**만 막는다(2026-08-26 수리).
#
# 그전엔 읽기 전용 조회(holdings/buying_power/order/orders/conditional_orders)도
# 같이 막았는데, 그래서 **읽기 전용 진단 레이어인 Private Banker 가 이 박스에서
# 영영 돌 수 없었다** — MODE=paper 가 정상 운영 상태이기 때문이다. 실측: 매일
# 07:00 크론이 18일 연속 "live trading disabled in paper mode" 로 죽어 일일 리스크
# 리포트가 한 번도 발송되지 않았다. 잔고를 **읽는 것**으로는 돈을 잃을 수 없다.
#
# 주문 차단은 이 게이트 하나에 기대지 않는다 — `TossBroker.place_order` 가
# MODE!=live 를 독립적으로 다시 확인한다(이중 방어).


def _paper(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 86400})
        return httpx.Response(200, json={"result": {"ok": request.url.path}})

    return _make_client(tmp_path, httpx.MockTransport(handler), mode="paper")


def test_read_only_account_queries_work_in_paper_mode(tmp_path):
    """읽기 전용 조회는 paper 에서도 된다 — Private Banker(읽기 전용 진단)가
    이것 없이는 존재할 수 없다."""
    client = _paper(tmp_path)
    # 반환값은 각 엔드포인트가 실제로 호출됐다는 증거다(게이트에 막히면 예외).
    assert client.holdings()["ok"] == "/api/v1/holdings"
    assert client.buying_power()["ok"] == "/api/v1/buying-power"
    assert client.order("order-123")["ok"] == "/api/v1/orders/order-123"
    assert client.orders()["ok"] == "/api/v1/orders"
    assert client.conditional_orders()["ok"] == "/api/v1/conditional-orders"


def test_state_changing_calls_still_refuse_in_paper_mode(tmp_path):
    """돈이 움직이는 호출은 그대로 막힌다 — 이 게이트의 진짜 목적."""
    client = _paper(tmp_path)
    for call in (
        lambda: client.place_order("005930", "BUY", "MARKET", quantity=1),
        lambda: client.modify_order("order-1", "MARKET", quantity=2),
        lambda: client.cancel_order("order-1"),
        lambda: client.cancel_conditional_order("cond-1"),
    ):
        with pytest.raises(RuntimeError, match="paper mode"):
            call()


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
