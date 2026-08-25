"""Toss Securities Open API client — thin, spec-accurate HTTP wrapper.

Ported from stock-algo-trade/engine/broker/toss_client.py (battle-tested rate
limiter, token cache, 401 single-retry). Broker-agnostic of our domain models —
this module speaks raw dicts/DataFrames; quant.adapters.brokers.toss.broker maps
those onto domain.models.

Paper mode is the default: order/holdings-mutating endpoints refuse before any
HTTP request unless mode="live".
"""
from __future__ import annotations

import json
import random
import threading
import time
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

from quant.adapters.env import REPO_ROOT
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

BASE_URL = "https://openapi.tossinvest.com"
KST = ZoneInfo("Asia/Seoul")

# Rate-limit groups -> requests/sec (Toss Open API TPS table).
RATE_LIMITS = {
    "AUTH": 5,
    "ACCOUNT": 1,
    "ASSET": 5,
    "STOCK": 5,
    "MARKET_INFO": 3,
    "MARKET_DATA": 10,
    "MARKET_DATA_CHART": 5,
    "MARKET_INDICATOR": 10,
    "ORDER": 6,
    "ORDER_HISTORY": 5,
    "ORDER_INFO": 6,
    "RANKING": 5,
    "CONDITIONAL_ORDER": 5,
    "CONDITIONAL_ORDER_HISTORY": 10,
}
ORDER_PEAK_START = dtime(9, 0)
ORDER_PEAK_END = dtime(9, 10)
ORDER_PEAK_TPS = 3  # ORDER / ORDER_INFO groups, 09:00-09:10 KST

_INTERVAL_MAP = {"day": "1d", "1d": "1d", "minute": "1m", "1m": "1m"}

# 저장소 루트는 adapters.env.REPO_ROOT 한 곳에서만 센다 — parents[3] 은
# `quant/` 였고, 토큰 캐시가 quant/data/cache 에 따로 쌓여 갈렸다
# (토스는 client_credentials 당 토큰이 1개라 캐시가 둘이면 서로를 무효화한다).
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache"


def _mkt(market: str) -> str:
    return str(market).upper()


class TossAPIError(Exception):
    """Raised on any non-2xx response. Carries the full body for debugging."""

    def __init__(self, status: int, code: str, message: str, body: dict | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.body = body
        super().__init__(f"HTTP {status} [{code}] {message} body={body}")


class _RateLimiter:
    """Per-group min-interval limiter (token-bucket-of-1). Blocks the calling
    thread just long enough to stay under each group's TPS limit."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}

    def wait(self, group: str) -> None:
        tps = RATE_LIMITS[group]
        if group in ("ORDER", "ORDER_INFO"):
            now_kst = datetime.now(KST).time()
            if ORDER_PEAK_START <= now_kst <= ORDER_PEAK_END:
                tps = ORDER_PEAK_TPS
        min_interval = 1.0 / tps
        with self._lock:
            now = time.monotonic()
            wait_s = min_interval - (now - self._last_call.get(group, 0.0))
            if wait_s > 0:
                time.sleep(wait_s)
            self._last_call[group] = time.monotonic()


class TossClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        account_seq: str = "",
        mode: str = "paper",
        cache_dir: Path = DEFAULT_CACHE_DIR,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_seq = account_seq
        self.mode = mode
        self._cache_dir = Path(cache_dir)
        self._token_path = self._cache_dir / "toss_token.json"
        self._http = httpx.Client(base_url=BASE_URL, timeout=10.0)
        self._limiter = _RateLimiter()
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._trading_day_cache: dict[tuple[str, str], bool] = {}

    # ------------------------------------------------------------------ auth
    def _load_token_cache(self) -> dict | None:
        try:
            return json.loads(self._token_path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def _save_token_cache(self) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(json.dumps({
            "access_token": self._access_token,
            "expires_at": self._expires_at,
        }))

    def _fetch_token(self) -> str:
        if not self.client_id or not self.client_secret:
            raise TossAPIError(401, "missing-credentials",
                                "TOSS_CLIENT_ID/TOSS_CLIENT_SECRET not configured")
        self._limiter.wait("AUTH")
        resp = self._http.post("/oauth2/token", data={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        if resp.status_code != 200:
            self._raise_oauth_error(resp)
        body = resp.json()
        self._access_token = body["access_token"]
        self._expires_at = time.time() + body["expires_in"] - 30  # refresh 30s early
        self._save_token_cache()
        return self._access_token

    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        cached = self._load_token_cache()
        if cached and time.time() < cached.get("expires_at", 0):
            self._access_token = cached["access_token"]
            self._expires_at = cached["expires_at"]
            return self._access_token
        return self._fetch_token()

    # --------------------------------------------------------------- request
    def _raise_api_error(self, resp: httpx.Response) -> None:
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        err = body.get("error", {}) if isinstance(body, dict) else {}
        raise TossAPIError(resp.status_code, err.get("code", "unknown"),
                            err.get("message", resp.text), body)

    def _raise_oauth_error(self, resp: httpx.Response) -> None:
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        code = body.get("error", "unknown") if isinstance(body, dict) else "unknown"
        message = body.get("error_description", resp.text) if isinstance(body, dict) else resp.text
        raise TossAPIError(resp.status_code, code, message, body)

    def _request(
        self, method: str, path: str, group: str, *,
        params: dict | None = None, json_body: dict | None = None,
        account_scoped: bool = False,
    ):
        token = self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        if account_scoped:
            if not self.account_seq:
                raise TossAPIError(400, "account-header-required",
                                    "account_seq not configured on TossClient")
            headers["X-Tossinvest-Account"] = str(self.account_seq)

        idempotent = method == "GET"
        max_attempts = 3 if idempotent else 1  # only GETs get the bounded auto-retry
        token_refreshed = False  # 401 gets exactly one retry, for any method

        attempt = 0
        while True:
            attempt += 1
            self._limiter.wait(group)  # every attempt, not just the first, spends from the rate budget
            try:
                resp = self._http.request(method, path, params=params, json=json_body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError):
                # Transient network failure. GET is idempotent, so retrying is safe — nothing
                # was mutated. POST/DELETE (orders) are NOT retried here: whether the order
                # reached the server before the failure is ambiguous, and blindly resubmitting
                # risks a duplicate order.
                if idempotent and attempt < max_attempts:
                    time.sleep(0.5 * 2 ** (attempt - 1) + random.uniform(0, 0.3))
                    continue
                raise

            if resp.status_code == 401 and not token_refreshed:
                # A 401 means the request was rejected before processing (token expired/
                # revoked server-side despite our 30s-early refresh) — safe to retry once
                # for ANY method. MUST bypass _ensure_token(): the disk cache would hand
                # back the very same invalid token when its expires_at hasn't passed yet.
                token_refreshed = True
                token = self._fetch_token()
                headers["Authorization"] = f"Bearer {token}"
                continue

            if resp.status_code == 429:
                # A 429 is unambiguous — the request was never processed — so retrying is
                # always safe. GET gets this inside the bounded retry loop; non-GET keeps
                # its original single retry.
                retry_ok = attempt < max_attempts if idempotent else attempt < 2
                if retry_ok:
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                    time.sleep(retry_after + random.uniform(0, 0.3))
                    continue
                self._raise_api_error(resp)

            if resp.status_code >= 500 and idempotent and attempt < max_attempts:
                time.sleep(0.5 * 2 ** (attempt - 1) + random.uniform(0, 0.3))
                continue

            if resp.status_code >= 400:
                self._raise_api_error(resp)

            if resp.status_code == 204 or not resp.content:
                # 204 No Content (조건주문 취소) — 본문이 없으므로 .json()을 부르면
                # ValueError가 난다. 성공을 실패로 만들지 않기 위해 None을 돌려준다.
                return None
            return resp.json()["result"]

    def _require_live(self) -> None:
        """**계좌 상태를 바꾸는 호출**에만 붙인다 — 주문 생성·정정·취소.

        읽기 전용 조회(holdings/buying_power/order/orders/conditional_orders)에는
        붙이지 않는다. 2026-08-26 실측: 그전엔 읽기까지 막아 **읽기 전용 진단
        레이어인 Private Banker 가 이 박스에서 영영 돌 수 없었다**(MODE=paper 가
        정상 운영 상태다). 매일 07:00 크론이 18일 연속 죽어 일일 리스크 리포트가
        한 번도 발송되지 않았고, 로그에만 남아 아무도 보지 않았다. 잔고를 읽는
        것으로는 돈을 잃지 않는다 — 이 게이트가 막아야 할 것은 주문이다.

        주문 차단은 이 게이트 하나에 기대지 않는다: `TossBroker.place_order` 가
        MODE!=live 를 독립적으로 다시 확인한다(이중 방어).
        """
        if self.mode != "live":
            raise RuntimeError("live trading disabled in paper mode")

    # ---------------------------------------------------------- market data
    def prices(self, symbols: list[str]) -> list[dict]:
        """Current price snapshot: [{symbol, timestamp, lastPrice, currency}, ...]."""
        return self._request("GET", "/api/v1/prices", "MARKET_DATA",
                              params={"symbols": ",".join(symbols)})

    def candles(self, symbol: str, interval: str = "day", count: int = 120) -> pd.DataFrame:
        """Fetch up to `count` candles, paging past the API's 200-per-call cap
        with the `before` cursor (inclusive upper bound, ISO 8601; response
        `nextBefore` feeds the next request verbatim). Candles come back
        newest-first per page; pages are concatenated then sorted asc."""
        api_interval = _INTERVAL_MAP.get(interval, interval)
        raw_candles: list[dict] = []
        before: str | None = None
        remaining = count
        while remaining > 0:
            params = {
                "symbol": symbol, "interval": api_interval,
                "count": min(remaining, 200),
            }
            if before is not None:
                params["before"] = before
            result = self._request("GET", "/api/v1/candles", "MARKET_DATA_CHART", params=params)
            page = result["candles"]
            if not page:
                break
            raw_candles.extend(page)
            remaining -= len(page)
            before = result.get("nextBefore")
            if not before:
                break
        rows = [{
            "ts": pd.Timestamp(c["timestamp"]),
            "open": float(c["openPrice"]),
            "high": float(c["highPrice"]),
            "low": float(c["lowPrice"]),
            "close": float(c["closePrice"]),
            "volume": float(c["volume"]),
        } for c in raw_candles]
        cols = ["open", "high", "low", "close", "volume"]
        if not rows:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(rows).set_index("ts").sort_index()[cols]

    def orderbook(self, symbol: str) -> dict:
        return self._request("GET", "/api/v1/orderbook", "MARKET_DATA", params={"symbol": symbol})

    def stock_info(self, symbol: str) -> dict:
        result = self._request("GET", "/api/v1/stocks", "STOCK", params={"symbols": symbol})
        return result[0] if result else {}

    def stock_warnings(self, symbol: str) -> list[dict]:
        """GET /stocks/{symbol}/warnings — 활성 매수 유의사항 목록 (없으면 빈 리스트).

        openapi.json 실스키마: result[] = {warningType, startDate, endDate}.
        warningType enum: LIQUIDATION_TRADING(정리매매)/OVERHEATED(단기과열)/
        INVESTMENT_WARNING(투자경고)/INVESTMENT_RISK(투자위험)/VI_*(변동성완화)/
        STOCK_WARRANTS(신주인수권). unknown code 허용 필수(스펙 명시).
        종목 없음은 404 stock-not-found — 호출부에서 TossAPIError로 받는다."""
        result = self._request("GET", f"/api/v1/stocks/{symbol}/warnings", "STOCK")
        return (result or {}).get("result", []) if isinstance(result, dict) else []

    def investor_trading(self, symbol: str = "KOSPI", interval: str = "1d", count: int = 5) -> dict:
        """KRX 투자자별 매매대금 — **시장 전체**(KOSPI/KOSDAQ만, 개별 종목 불가).

        records[]: {date, foreigner: {buyAmount, sellAmount}, institution: {...},
        individual, otherCorporation} — 금액은 KRW 정수 문자열. 당일 기록은 장중
        잠정치(updatedAt 참고). watch-score의 시장 수급 조류 판정에 쓴다."""
        return self._request(
            "GET", f"/api/v1/market-indicators/{symbol}/investor-trading",
            "MARKET_INDICATOR", params={"interval": interval, "count": count},
        )

    def market_calendar(self, market: str, date_: date | str | None = None) -> dict:
        """{today, previousBusinessDay, nextBusinessDay}. The real API takes a
        single `date`, not a date_from/date_to range."""
        path = f"/api/v1/market-calendar/{_mkt(market)}"
        params = None
        if date_ is not None:
            params = {"date": date_.isoformat() if isinstance(date_, date) else date_}
        return self._request("GET", path, "MARKET_INFO", params=params)

    def is_trading_day(self, market: str, date_: date | str) -> bool:
        # memoized per (market, date): status is immutable for a given date,
        # and the run loop asks every cycle — without this it's +1 HTTP per 10s
        key = (_mkt(market), str(date_))
        cached = self._trading_day_cache.get(key)
        if cached is not None:
            return cached
        today = self.market_calendar(market, date_)["today"]
        if _mkt(market) == "KR":
            result = today.get("integrated") is not None
        else:
            result = any(today.get(k) is not None
                         for k in ("dayMarket", "preMarket", "regularMarket", "afterMarket"))
        self._trading_day_cache[key] = result
        return result

    def rankings(
        self, type: str, market_country: str, *,
        duration: str = "realtime", count: int = 20,
        exclude_investment_caution: bool = False,
    ) -> dict:
        """상위 랭킹 조회. type: MARKET_TRADING_AMOUNT | MARKET_TRADING_VOLUME |
        TOP_GAINERS | TOP_LOSERS | TOSS_SECURITIES_TRADING_AMOUNT |
        TOSS_SECURITIES_TRADING_VOLUME. market_country: "KR" | "US". duration:
        realtime | 1d | 1w | 1mo | 3mo | 6mo | 1y (TOP_GAINERS/TOP_LOSERS는
        realtime 미지원). count: 1~100.
        Result: {rankedAt, rankings: [{rank, symbol, currency, price: {lastPrice,
        basePrice, changeRate}, tradingVolume, tradingAmount}]}. 최대 100위까지만
        제공하며, 랭킹이 집계되지 않은 조합은 빈 rankings 배열(에러 아님)."""
        return self._request("GET", "/api/v1/rankings", "RANKING", params={
            "type": type,
            "marketCountry": market_country,
            "duration": duration,
            "count": count,
            "excludeInvestmentCaution": exclude_investment_caution,
        })

    def usd_krw(self) -> float:
        result = self._request("GET", "/api/v1/exchange-rate", "MARKET_INFO",
                                params={"baseCurrency": "USD", "quoteCurrency": "KRW"})
        return float(result["rate"])

    # --------------------------------------------------- account / orders
    def holdings(self, symbol: str | None = None) -> dict:
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/api/v1/holdings", "ASSET",
                              params=params, account_scoped=True)

    def buying_power(self, currency: str = "KRW") -> dict:
        """{currency, cashBuyingPower} — cash-only (excludes margin)."""
        return self._request("GET", "/api/v1/buying-power", "ORDER_INFO",
                              params={"currency": currency}, account_scoped=True)

    def place_order(
        self, symbol: str, side: str, order_type: str, *,
        quantity: float | str | None = None,
        order_amount: float | str | None = None,
        price: float | str | None = None,
        time_in_force: str = "DAY",
        client_order_id: str | None = None,
        confirm_high_value_order: bool = False,
    ) -> dict:
        self._require_live()
        if (quantity is None) == (order_amount is None):
            raise ValueError("place_order requires exactly one of quantity or order_amount")
        body: dict = {
            "symbol": symbol, "side": side, "orderType": order_type,
            "confirmHighValueOrder": confirm_high_value_order,
        }
        if time_in_force != "DAY":
            body["timeInForce"] = time_in_force
        if client_order_id:
            body["clientOrderId"] = client_order_id
        if quantity is not None:
            body["quantity"] = str(quantity)
        else:
            body["orderAmount"] = str(order_amount)
        if price is not None:
            body["price"] = str(price)
        return self._request("POST", "/api/v1/orders", "ORDER",
                              json_body=body, account_scoped=True)

    def modify_order(
        self, order_id: str, order_type: str, *,
        quantity: float | str | None = None,
        price: float | str | None = None,
        confirm_high_value_order: bool = False,
    ) -> dict:
        self._require_live()
        body: dict = {"orderType": order_type, "confirmHighValueOrder": confirm_high_value_order}
        if quantity is not None:
            body["quantity"] = str(quantity)
        if price is not None:
            body["price"] = str(price)
        return self._request("POST", f"/api/v1/orders/{order_id}/modify", "ORDER",
                              json_body=body, account_scoped=True)

    def cancel_order(self, order_id: str) -> dict:
        self._require_live()
        return self._request("POST", f"/api/v1/orders/{order_id}/cancel", "ORDER",
                              json_body={}, account_scoped=True)

    def order(self, order_id: str) -> dict:
        """주문 상세 조회 (상태 + 체결 결과). GET /api/v1/orders/{orderId}.
        Response `result`: {orderId, symbol, side, orderType, status, quantity,
        execution: {filledQuantity, averageFilledPrice, filledAmount, commission,
        tax, filledAt, settlementDate}, ...}. status enum: PENDING, PENDING_CANCEL,
        PENDING_REPLACE, PARTIAL_FILLED, FILLED, CANCELED, REJECTED,
        CANCEL_REJECTED, REPLACE_REJECTED, REPLACED."""
        return self._request("GET", f"/api/v1/orders/{order_id}", "ORDER_HISTORY",
                              account_scoped=True)

    def orders(
        self, status: str = "OPEN", *, symbol: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """주문 목록 조회. GET /api/v1/orders?status=OPEN|CLOSED.
        Response `result`: {orders: [...], nextCursor, hasNext}.

        주의: 응답의 order 객체에는 **clientOrderId가 없다**(openapi.json의 `Order`
        스키마 확인). 따라서 "내가 방금 보낸 clientOrderId의 주문이 이미 나갔는가"를
        이 목록으로 매칭할 수 없다 — 멱등성은 서버측 clientOrderId 키에 의존한다
        (broker.place_order 참고). 이 메서드는 사람이 하는 사후 확인/대사용이다."""
        params: dict = {"status": status}
        if symbol:
            params["symbol"] = symbol
        if limit is not None:
            params["limit"] = limit
        return self._request("GET", "/api/v1/orders", "ORDER_HISTORY",
                              params=params, account_scoped=True)

    # ------------------------------------------------------- 조건주문 (서버측 손절)
    def place_conditional_order(
        self, symbol: str, order_group_type: str, *,
        quantity: float | str,
        order_type: str,
        expire_date: str,
        first: dict,
        second: dict | None = None,
        client_order_id: str | None = None,
        confirm_high_value_order: bool = False,
    ) -> dict:
        """조건주문 생성. POST /api/v1/conditional-orders.

        [미검증] IP 화이트리스트 때문에 실호출로 확인할 수 없었다. 요청/응답 스키마는
        전부 docs/api/toss/openapi.json 기준이다.

        order_group_type: SINGLE | OCO | OTO
        - SINGLE: `first` 한 조건만 감시. MARKET 가능(그 경우 orderPrice 미지정).
        - OCO: first/second 둘 다 SELL, `first.triggerPrice > 현재가 > second.triggerPrice`,
          호가유형은 LIMIT만 지원.
        - OTO: first=BUY, second=SELL, LIMIT만.
        first/second dict: {orderSide, triggerPrice, orderPrice?} — 값은 문자열로 보낸다.
        expire_date: "YYYY-MM-DD" (필수). 만료일까지 미충족이면 자동 만료.
        Response `result`: {conditionalOrderId, clientOrderId}.
        """
        self._require_live()
        body: dict = {
            "symbol": symbol,
            "type": order_group_type,
            "quantity": str(quantity),
            "orderType": order_type,
            "expireDate": expire_date,
            "first": first,
            "confirmHighValueOrder": confirm_high_value_order,
        }
        if second is not None:
            body["second"] = second
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._request("POST", "/api/v1/conditional-orders", "CONDITIONAL_ORDER",
                              json_body=body, account_scoped=True)

    def conditional_orders(self, status: str = "OPEN", *, symbol: str | None = None) -> dict:
        """조건주문 목록. GET /api/v1/conditional-orders?status=OPEN|CLOSED.
        Response `result`: {conditionalOrders: [...], nextCursor, hasNext}. [미검증]"""
        params: dict = {"status": status}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/api/v1/conditional-orders", "CONDITIONAL_ORDER_HISTORY",
                              params=params, account_scoped=True)

    def cancel_conditional_order(self, conditional_order_id: str) -> dict:
        """조건주문 취소. DELETE /api/v1/conditional-orders/{conditionalOrderId}. [미검증]"""
        self._require_live()
        return self._request("DELETE", f"/api/v1/conditional-orders/{conditional_order_id}",
                              "CONDITIONAL_ORDER", account_scoped=True)
