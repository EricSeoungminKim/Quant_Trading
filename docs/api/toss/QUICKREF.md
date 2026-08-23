# Toss Securities Open API — Quick Reference

Source: `openapi.json` (OpenAPI 3.1, downloaded 2026-07-14 from
`https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`), cross-checked
against `overview.md` and `api-reference.md` in this directory. This file is the
terse, load-bearing cheat sheet — read this instead of the raw spec.

Base URL: `https://openapi.tossinvest.com`

## Auth flow

1. Register client in Toss WTS app > Settings > Open API → get `client_id` / `client_secret`.
2. **IP allowlist is mandatory** — calls from unregistered IPs get `403`.
3. `POST /oauth2/token` (Client Credentials Grant) to get an access token.
4. Send `Authorization: Bearer {access_token}` on every call.
5. Account-scoped calls (Account/Asset/Order/Conditional-Order categories) ALSO require
   header `X-Tossinvest-Account: {accountSeq}` (int64, from `GET /api/v1/accounts`).

Market Data / Stock Info / Market Info / Ranking / Market Indicators categories need
ONLY the bearer token (no account header).

### `POST /oauth2/token`

- Body: `application/x-www-form-urlencoded` (NOT JSON).
- Request fields: `grant_type=client_credentials`, `client_id`, `client_secret`.
- Response (OAuth2 standard shape, NOT the `{result: ...}` envelope):
  ```json
  { "access_token": "eyJ...", "token_type": "Bearer", "expires_in": 86400 }
  ```
- No refresh token. Re-POST to reissue; **issuing a new token immediately invalidates
  the previous one** (only one live token per client at a time) — don't refresh
  speculatively from multiple processes.
- Errors: `400 invalid_request` / `400 unsupported_grant_type` / `401 invalid_client`
  (bad id/secret) / `403 access_denied` (IP not allowlisted) — these use the OAuth2
  `{error, error_description}` shape, not the normal `ApiError` envelope.
- Rate limit group: `AUTH`.

## Response envelope

Success (all endpoints except `/oauth2/token`):

```json
{"result": <payload>}
```

Error (4xx/5xx, all endpoints):

```json
{"error": {"requestId": "...", "code": "invalid-request", "message": "...", "data": {...}}}
```

`requestId` mirrors response header `X-Request-Id` (fallback: `x-amz-cf-id`).
`data` is optional and shape varies by `code`; common keys: `field`, `allowedValues`,
`constraint` (`min`/`max`/`integerOnly`), `tickSize`, `nearestPrices`,
`retryAfterSeconds`/`retryAfterAt`. Treat unknown `code` values as opaque strings.

All numeric fields in payloads are **strings** (`"quantity": "10"`, `"lastPrice": "72000"`)
— cast to `float`/`Decimal` yourself.

## Rate limits (token bucket per client × group; TPS = burst capacity)

| Group                     | TPS | Peak-hour TPS       |
| ------------------------- | --- | ------------------- |
| AUTH                      | 5   | —                   |
| ACCOUNT                   | 1   | —                   |
| ASSET                     | 5   | —                   |
| STOCK                     | 5   | —                   |
| MARKET_INFO               | 3   | —                   |
| MARKET_DATA               | 10  | —                   |
| MARKET_DATA_CHART         | 5   | —                   |
| RANKING                   | 5   | —                   |
| MARKET_INDICATOR_PRICE    | 10  | —                   |
| MARKET_INDICATOR          | 10  | —                   |
| MARKET_INDICATOR_CHART    | 5   | —                   |
| ORDER                     | 6   | 3 (09:00–09:10 KST) |
| ORDER_HISTORY             | 5   | —                   |
| ORDER_INFO                | 6   | 3 (09:00–09:10 KST) |
| CONDITIONAL_ORDER         | 5   | —                   |
| CONDITIONAL_ORDER_HISTORY | 10  | —                   |

Response headers on every call (incl. 429): `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
`X-RateLimit-Reset` (seconds to next token); `Retry-After` only on 429.
On 429: wait `Retry-After` seconds, then retry once; if it 429s again, raise. Recommended
(not yet implemented in `toss_client.py`): exponential backoff + jitter across retries.

## Error code reference (non-exhaustive, most relevant)

| HTTP | code                                                                                                    | meaning                                                        |
| ---- | ------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| 400  | `invalid-request`                                                                                       | generic validation failure (see `data.field`)                  |
| 400  | `confirm-high-value-required`                                                                           | order ≥ 1억원 (100M KRW) without `confirmHighValueOrder: true` |
| 400  | `account-header-required`                                                                               | missing `X-Tossinvest-Account`                                 |
| 401  | `invalid-token` / `expired-token` / `edge-blocked`                                                      | bad/missing/expired bearer token                               |
| 403  | `forbidden`                                                                                             | insufficient permission                                        |
| 404  | `stock-not-found` / `order-not-found` / `account-not-found` / `exchange-rate-not-found`                 |                                                                |
| 409  | `already-filled` / `already-canceled` / `already-modified` / `already-rejected` / `request-in-progress` | modify/cancel race                                             |
| 422  | `insufficient-buying-power`                                                                             | not enough cash                                                |
| 422  | `order-hours-closed`                                                                                    | outside order-acceptance window                                |
| 422  | `price-out-of-range`                                                                                    | outside limit-up/down band                                     |
| 422  | `amount-order-outside-regular-hours`                                                                    | `orderAmount` order outside US regular hours                   |
| 422  | `fractional-quantity-outside-regular-hours`                                                             | fractional US sell outside regular hours                       |
| 429  | `edge-rate-limit-exceeded` / `rate-limit-exceeded`                                                      | throttled                                                      |
| 500  | `internal-error` / `maintenance`                                                                        |                                                                |

## Endpoints used by `TossClient`

All GETs below need only the bearer token unless marked **[ACCOUNT HEADER]**.

### Prices — `GET /api/v1/prices` (group: MARKET_DATA)

- Query: `symbols` — comma-separated, max 200 (e.g. `005930,000660` or `AAPL,MSFT`).
- Result: array of `{symbol, timestamp, lastPrice, currency}`. No `prevClose`/`volume`
  here — use `candles` for volume, and prev close must be derived from yesterday's
  daily candle (there's no dedicated prev-close field on this endpoint).

### Candles — `GET /api/v1/candles` (group: MARKET_DATA_CHART)

- Query: `symbol` (single symbol only — NOT batch), `interval` (`"1m"` | `"1d"`,
  required — note: spec enum is `1m`/`1d`, not `day`/`minute`), `count` (1–200,
  default 100), `before` (ISO 8601 cursor, inclusive upper bound, for pagination —
  URL-encode `+` as `%2B`), `adjusted` (bool, default `true`).
- Result: `{candles: [{timestamp, openPrice, highPrice, lowPrice, closePrice, volume,
currency}], nextBefore}` — candles are returned **newest-first**; `nextBefore` is the
  cursor for the next (older) page, `null` on last page.
- No `market` param — the symbol alone determines KR vs US on the server side.

### Orderbook — `GET /api/v1/orderbook` (group: MARKET_DATA)

- Query: `symbol` (single).
- Result: `{timestamp, currency, asks: [{price, volume}, ...], bids: [{price, volume}, ...]}`
  (top-of-book first, typically 3 levels in examples but not documented as a fixed count).

### Stock info — `GET /api/v1/stocks` (group: STOCK)

- Query: `symbols` — comma-separated, max 200.
- Result: array of `StockInfo`: `{symbol, name, englishName, isinCode, market
("KOSPI"|"KOSDAQ"|"NASDAQ"|...), securityType ("STOCK"|"ETF"|...), isCommonShare,
status ("ACTIVE"|...), currency, listDate, delistDate, sharesOutstanding,
leverageFactor, koreanMarketDetail: {liquidationTrading, nxtSupported,
krxTradingSuspended, nxtTradingSuspended} | null}`.

### Exchange rate — `GET /api/v1/exchange-rate` (group: MARKET_INFO)

- Query: `baseCurrency` (required, e.g. `USD`), `quoteCurrency` (required, e.g. `KRW`),
  `dateTime` (optional ISO 8601, defaults to current valid rate).
- Result: `{baseCurrency, quoteCurrency, rate, midRate, basisPoint, rateChangeType
("UP"|"DOWN"), validFrom, validUntil}`. Refreshed ~1/min; reference only, not the
  exact execution rate.
- For `usd_krw()`: call with `baseCurrency=USD&quoteCurrency=KRW`, return `float(rate)`.

### KR market calendar — `GET /api/v1/market-calendar/KR` (group: MARKET_INFO)

- Query: `date` (optional, `YYYY-MM-DD`; defaults to today).
- Result: `{today, previousBusinessDay, nextBusinessDay}`, each
  `{date, integrated: {preMarket, regularMarket, afterMarket} | null}`.
  `integrated` is `null` on a full holiday; individual sessions inside `integrated`
  can independently be `null` (partial closure, e.g. NXT premarket-only holiday).
  Each session is `{startTime, singlePriceAuctionStartTime?, endTime}` in KST
  (`+09:00`).
- **Deviation from task spec**: the real API takes a single `date`, not a
  `date_from`/`date_to` range — there is no range-query endpoint. `market_calendar()`
  is implemented to match the real single-`date` signature (see toss_client.py).
- `is_trading_day("KR", date)`: call with that `date`, return
  `result["today"]["integrated"] is not None`.

### US market calendar — `GET /api/v1/market-calendar/US` (group: MARKET_INFO)

- Query: `date` (optional, `YYYY-MM-DD`, US local date).
- Result: `{today, previousBusinessDay, nextBusinessDay}`, each
  `{date, dayMarket, preMarket, regularMarket, afterMarket}` — 4 sessions, each
  `{startTime, endTime}` in KST or `null` (all 4 null = holiday).
- `is_trading_day("US", date)`: call with that `date`, return true if ANY of the 4
  sessions on `today` is non-null (using `regularMarket is not None` as the practical
  "market open" check; implemented as `any of the 4 session fields not None`).

### Accounts — `GET /api/v1/accounts` (group: ACCOUNT)

- No query params. Needs bearer token only (NOT the account header — this is how you
  discover `accountSeq` in the first place).
- Result: array of `{accountNo, accountSeq (int), accountType ("BROKERAGE" only,
for now)}`. Empty array if no accounts.

### Holdings — `GET /api/v1/holdings` **[ACCOUNT HEADER]** (group: ASSET)

- Query: `symbol` (optional filter).
- Result `HoldingsOverview`: summary totals (`totalPurchaseAmount`, `marketValue`,
  `profitLoss`, `dailyProfitLoss` — each split `{krw, usd}`) plus
  `items: [{symbol, name, marketCountry ("KR"|"US"), currency, quantity, lastPrice,
averagePurchasePrice, marketValue: {purchaseAmount, amount, amountAfterCost},
profitLoss: {...}, dailyProfitLoss: {...}, cost: {commission, tax}}]`.
- No holdings → summary fields are `"0"`/`null`, `items: []`.

### Buying power — `GET /api/v1/buying-power` **[ACCOUNT HEADER]** (group: ORDER_INFO)

- Query: `currency` (required, `"KRW"` | `"USD"`).
- Result: `{currency, cashBuyingPower}` — cash-only (excludes margin/미수).

### Create order — `POST /api/v1/orders` **[ACCOUNT HEADER]** (group: ORDER)

- Body (`OrderCreateRequest` = oneOf quantity-based / amount-based, all numeric
  fields are decimal **strings**):
  - Quantity-based (default; required: `symbol, side, orderType, quantity`):
    `{clientOrderId?, symbol, side: "BUY"|"SELL", orderType: "LIMIT"|"MARKET",
timeInForce?: "DAY"|"CLS" (default DAY; CLS = at-the-close, US LIMIT only),
quantity, price? (required iff LIMIT, forbidden iff MARKET),
confirmHighValueOrder?: bool (default false)}`.
    `quantity` must be a positive integer EXCEPT US `MARKET`+`SELL`, which alone
    allows fractional (up to 6 decimal places), and only during regular hours.
  - Amount-based (US MARKET only; required: `symbol, side, orderType="MARKET",
orderAmount`): `{clientOrderId?, symbol, side, orderType: "MARKET", orderAmount,
confirmHighValueOrder?}`. Regular-hours only (`422
amount-order-outside-regular-hours` otherwise).
  - `clientOrderId`: idempotency key, ≤36 chars `[a-zA-Z0-9_-]`, valid 10 minutes;
    reusing it replays the same order result.
  - `confirmHighValueOrder: true` required for orders ≥ 100,000,000 KRW-equivalent,
    else `400 confirm-high-value-required`.
- Result: `{orderId, clientOrderId}`.
- KR price must land on the correct tick-size grid or `400 invalid-request` with
  `data.tickSize`/`data.nearestPrices`.

### Modify order — `POST /api/v1/orders/{orderId}/modify` **[ACCOUNT HEADER]** (group: ORDER)

- Body `OrderModifyRequest`: `{orderType: "LIMIT"|"MARKET" (required),
quantity? (KR: required, positive integer; US: forbidden →
400 us-modify-quantity-not-supported), price? (required iff LIMIT),
confirmHighValueOrder?}`.
- Result: `{orderId}` — **a NEW orderId**, different from the original.
- 409 if the target order is already filled/canceled/modified/rejected/in-flight.

### Cancel order — `POST /api/v1/orders/{orderId}/cancel` **[ACCOUNT HEADER]** (group: ORDER)

- Body: `{}` (empty object; body itself optional).
- Result: `{orderId}` — new orderId for the cancel operation.
- 404 `order-not-found`; 409 same already-\* codes as modify.

## Symbol/market convention used across this codebase

KR symbols are 6-digit numeric strings (e.g. `"005930"`); anything else (tickers like
`"AAPL"`) is treated as US. This matches how the spec itself distinguishes KRX vs
other symbols (`^[A-Za-z0-9.\-]+$`, no explicit `market` query param on
prices/candles/orderbook/stocks — the symbol format alone disambiguates server-side).

## Known gaps / not implemented in `toss_client.py`

- Conditional orders (SINGLE/OCO/OTO), rankings, market indicators, trades,
  price-limits, sellable-quantity, commissions, order history (list/detail),
  stock warnings — all documented in the spec but out of scope for this worker's
  task list. Add methods following the same pattern if needed later.
- `websocket`/streaming: none exists — Toss Open API is REST-only (per `overview.md`).

---

## 실측 보강 (2026-07-28, 이 저장소에서 측정)

### 액세스 토큰의 실제 수명은 `expires_in`과 다르다

`POST /oauth2/token`은 `expires_in: 86399`(24시간)를 반환하지만, **실제로는
유휴 10초 안팎이면 무효화된다.** 재현 결과:

| 시나리오                              | 결과                               |
| ------------------------------------- | ---------------------------------- |
| 발급 후 연속 8회 호출(대기 없음)      | 8회 모두 200                       |
| 발급 후 12~15초 대기 뒤 1회 호출      | **401 `invalid-token`**            |
| httpx keepalive를 300초로 늘려 재시도 | 동일하게 401 (연결 유지 문제 아님) |

즉 호출 횟수가 아니라 **유휴 시간** 기준이며, TCP/TLS 연결 유지와도 무관하다
(서버 측 판단). `expires_in`을 믿고 토큰을 오래 재사용하도록 설계하면 안 된다.

실무적 함의:

- `_request`의 "401 → 토큰 1회 재발급 → 재시도"가 이 동작을 투명하게 흡수한다.
  이 로직이 없으면 폴링 루프가 매 사이클 실패한다.
- 그러나 재발급은 이전 토큰을 무효화하므로(위 Auth flow 참고), 폴링 주기가
  짧을수록 토큰 churn이 커진다. **근본 해결은 호출 빈도를 낮추는 것**이다 —
  `TossDataFeed._load_1m`이 1분봉을 증분 캐싱하는 이유가 이것이다.
- 여러 프로세스가 같은 client_id로 동시에 토큰을 발급받으면 서로를 무효화한다.
  엔진과 백필 스크립트를 동시에 돌릴 때 주의.

### 캔들 API의 실제 과거 깊이

| interval | 확보 가능 깊이                                |
| -------- | --------------------------------------------- |
| `1m`     | 약 4거래일 (`nextBefore`가 그 이전으로 안 감) |
| `1d`     | 2010-02-11 (TQQQ/SQQQ 상장일)까지 전량        |

측정 방법과 원자료는 `docs/data-availability.md` 참고.

### IP allowlist

전작 문서는 미등록 IP에서 403이 난다고 경고하지만, 2026-07-28 이 개발 환경
(가정용 회선)에서는 시세 조회 66회 전부 정상 200이었다. 서버 환경에서는
별도 확인 필요.
