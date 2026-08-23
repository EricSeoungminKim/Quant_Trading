"""토스증권 Open API — 공개 시장 데이터만 읽는다.

이 리포트는 공개 호스팅된다. 토스 API 에는 보유종목(`/api/v1/holdings`),
매수여력(`/api/v1/buying-power`), 주문/예약주문 같은 **계좌 민감 엔드포인트**가
같은 API 안에 섞여 있다 — 이 파일에서 그런 경로가 실수로라도 호출되면 안 된다.

그래서 **허용목록을 코드에 박고, 그 밖의 경로는 호출 자체가 불가능하게 만든다**.
`_request()` 진입부에서 경로를 검사해 허용목록에 없으면 `PermissionError` 를
던진다 — 새 엔드포인트를 추가하려는 사람이 이 검사를 반드시 통과시켜야 하므로,
민감 경로가 조용히 섞여 들어올 수 없다.

## 실측 확인된 스키마 (2026-08-12, EC2 IP 화이트리스트 안에서 검증)

- **모든 응답이 `{"result": ...}` 로 감싸여 온다** — 사전에 문서화된 스펙은 이
  래핑 없이 평평한 구조를 가정했으나 실측에서 다르게 확인됐다. `_request()` 가
  `resp.json()["result"]` 로 한 번에 벗겨내므로, 이 파일의 나머지 함수는 벗겨진
  내부 구조만 다룬다.
- `rankings`: 모든 수치가 문자열로 온다. `changeRate` 는 비율(0.0638 = +6.38%).
- `stocks/{symbol}/warnings`: `result` 를 벗기면 바로 경고 리스트 — 없으면 `[]`,
  없는 종목은 404.
- `market-indicators/{KOSPI|KOSDAQ}/investor-trading`: 금액은 KRW 정수 문자열.
  당일 기록은 장중 잠정치라 `updatedAt` 을 같이 들고 다닌다.
- `exchange-rate`: `rate` 도 문자열로 온다.

## 로컬에서는 403 이 정상
토스 API 는 IP 화이트리스트라 로컬에서는 `403 ip-not-allowed` 가 뜬다. 이건
버그가 아니다 — 조용히 빈 값으로 감추지 않고 그대로 raise 해서
`quant.collect.snapshot` 가 `SourceResult(ok=False)` 로 남기게 한다.
"""
from __future__ import annotations

import time

from quant.adapters.env import get_key
from quant.adapters.http import client

BASE_URL = "https://openapi.tossinvest.com"

# 허용목록. 이 밖의 경로는 _check_path 에서 물리적으로 막힌다.
ALLOWED_PATHS = frozenset({
    "/api/v1/rankings",
    "/api/v1/exchange-rate",
})
ALLOWED_PATH_PREFIXES = ("/api/v1/stocks/", "/api/v1/market-indicators/")
FORBIDDEN = (
    "/api/v1/holdings",
    "/api/v1/buying-power",
    "/api/v1/orders",
    "/api/v1/conditional-orders",
)
_STOCKS_PREFIX, _MARKET_INDICATORS_PREFIX = ALLOWED_PATH_PREFIXES

WARNING_LABELS = {
    "LIQUIDATION_TRADING": "정리매매",
    "OVERHEATED": "단기과열",
    "INVESTMENT_WARNING": "투자경고",
    "INVESTMENT_RISK": "투자위험",
    "STOCK_WARRANTS": "신주인수권",
}

_TOKEN_REFRESH_MARGIN_S = 60.0

# 액세스 토큰 메모리 캐시. 파일 캐시는 만들지 않는다 — 거래 엔진의 캐시와
# 섞이면 안 된다.
_token_cache: dict[str, object] = {}

_RANKING_BOARDS = {
    # 보드 이름 -> (type, duration). TOP_GAINERS/LOSERS 는 realtime 을 지원하지
    # 않아 1d 를 쓴다(실측 확인).
    "거래대금": ("MARKET_TRADING_AMOUNT", "realtime"),
    "상승률": ("TOP_GAINERS", "1d"),
    "하락률": ("TOP_LOSERS", "1d"),
    "토스 사용자 거래대금": ("TOSS_SECURITIES_TRADING_AMOUNT", "realtime"),
}


def _warning_label(warning_type: str) -> str:
    """모르는 코드도 허용해야 한다(스펙 명시) — 매핑에 없으면 코드 문자열 그대로."""
    if warning_type.startswith("VI_"):
        return "변동성완화"
    return WARNING_LABELS.get(warning_type, warning_type)


def _check_path(path: str) -> None:
    """허용목록 밖 경로는 호출 자체를 막는다 — 이 파일에서 가장 중요한 검사.

    stocks/ 접두사는 /warnings 서브패스만 허용한다(종목 마스터 조회 등은
    이 리포트에 불필요하다).

    `..` 세그먼트는 문자열 검사 시점엔 stocks//market-indicators 접두사를
    통과하지만, httpx가 실제 요청을 만들 때 경로를 정규화해 `/api/v1/
    market-indicators/../holdings` 가 그대로 `/api/v1/holdings`(금지 엔드포인트)
    로 나간다(실측 확인) — 그래서 접두사 검사보다 먼저 통째로 막는다.
    """
    if any(segment == ".." for segment in path.split("/")):
        raise PermissionError(f"허용되지 않은 토스 API 경로 (경로 조작 시도): {path}")
    if path in ALLOWED_PATHS:
        return
    if path.startswith(_MARKET_INDICATORS_PREFIX):
        return
    if path.startswith(_STOCKS_PREFIX) and path.endswith("/warnings"):
        return
    raise PermissionError(f"허용되지 않은 토스 API 경로: {path}")


def _get_token(c, *, force: bool = False) -> str:
    """force=True는 캐시를 무시하고 새로 발급받는다 — 401(타 프로세스發 토큰 무효화)
    복구용. 이 client_id/secret은 거래 엔진(quant_trading_kiwoom)과 **공유**되는데,
    토스 OAuth2는 클라이언트당 활성 토큰이 하나뿐이라 새로 발급하는 순간 이전 토큰이
    즉시 무효화된다(재발급 자체엔 refresh 개념이 없다 — docs/api/toss/QUICKREF.md
    'No refresh token' 참고). 즉 이 프로세스가 시작하며 토큰을 발급받으면 그 순간
    거래 엔진이 쓰던 토큰이 끊기고, 반대로 엔진이 재발급하면 이 프로세스의 캐시도
    조용히 무효화될 수 있다 — 실측 확인(2026-08-13, EC2): market-report@KR 기동
    직후 거래 엔진 쪽 요청이 401 → 자동 재발급으로 회복되는 로그 확인. 그 반대
    방향(이 프로세스가 401을 맞는 경우)은 여기서 캐시 무시 재발급으로 흡수한다."""
    if not force:
        expires_at = _token_cache.get("expires_at", 0.0)
        if _token_cache.get("token") and time.monotonic() < expires_at:
            return _token_cache["token"]  # type: ignore[return-value]

    client_id = get_key("TOSS_CLIENT_ID")
    client_secret = get_key("TOSS_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 미설정")

    resp = c.post(
        f"{BASE_URL}/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    _token_cache["token"] = payload["access_token"]
    _token_cache["expires_at"] = (
        time.monotonic() + payload["expires_in"] - _TOKEN_REFRESH_MARGIN_S
    )
    return _token_cache["token"]  # type: ignore[return-value]


def _request(c, path: str, params: dict | None = None):
    _check_path(path)
    token = _get_token(c)
    resp = c.get(f"{BASE_URL}{path}", params=params, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        # 캐시가 아직 안 만료됐는데도 401 — 거래 엔진이 같은 client_id로 재발급해
        # 이 토큰을 무효화했을 가능성이 크다(_get_token 독스트링 참고). 캐시 무시하고
        # 한 번만 재발급 후 재시도한다. 그래도 실패하면 그대로 raise(호출부가
        # 보드/심볼 단위 스킵 또는 SourceResult(ok=False)로 흡수).
        token = _get_token(c, force=True)
        resp = c.get(f"{BASE_URL}{path}", params=params, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    # 모든 엔드포인트가 {"result": ...} 로 감싸여 온다(실측 확인) — 여기서 벗긴다.
    return resp.json()["result"]


def _parse_ranking_item(item: dict) -> dict:
    return {
        "rank": item["rank"],
        "symbol": item["symbol"],
        "price": float(item["price"]["lastPrice"]),
        "change_pct": round(float(item["price"]["changeRate"]) * 100, 2),
        "trading_amount": int(item["tradingAmount"]),
    }


def fetch_rankings(market: str) -> dict:
    """market: 'KR' | 'US'. 보드 하나가 실패해도 나머지는 살린다. 전부 실패하면 raise."""
    boards: dict[str, list[dict]] = {}
    ranked_at = None
    with client() as c:
        for name, (board_type, duration) in _RANKING_BOARDS.items():
            try:
                data = _request(c, "/api/v1/rankings", params={
                    "type": board_type,
                    "marketCountry": market,
                    "duration": duration,
                    "count": 10,
                    "excludeInvestmentCaution": True,
                })
            except Exception:
                continue
            boards[name] = [_parse_ranking_item(r) for r in data["rankings"]]
            ranked_at = data.get("rankedAt", ranked_at)
    if not boards:
        raise ValueError(f"토스 랭킹을 하나도 못 가져왔다: {market}")
    return {"ranked_at": ranked_at, "boards": boards}


def fetch_warnings(symbols: list[str]) -> dict[str, list[dict]]:
    """종목별 시장경고 조회. 경고 없는 종목은 빈 리스트. 심볼당 실패는 건너뛴다."""
    out: dict[str, list[dict]] = {}
    with client() as c:
        for symbol in symbols:
            try:
                warnings = _request(c, f"/api/v1/stocks/{symbol}/warnings")
            except Exception:
                out[symbol] = []
                continue
            out[symbol] = [
                {
                    "type": w["warningType"],
                    "label": _warning_label(w["warningType"]),
                    "start": w.get("startDate"),
                    "end": w.get("endDate"),
                }
                for w in warnings
            ]
    return out


def _investor_net(record: dict, key: str) -> int:
    return int(record[key]["buyAmount"]) - int(record[key]["sellAmount"])


def fetch_investor_trading() -> dict:
    """KOSPI/KOSDAQ 투자자별 순매수(원). 당일 기록은 장중 잠정치라 updated_at 을 남긴다."""
    out: dict[str, dict] = {}
    with client() as c:
        for index in ("KOSPI", "KOSDAQ"):
            data = _request(
                c,
                f"/api/v1/market-indicators/{index}/investor-trading",
                params={"interval": "1d", "count": 1},
            )
            record = data["records"][0]
            out[index] = {
                "date": record["date"],
                "updated_at": record["updatedAt"],
                "individual_net": _investor_net(record, "individual"),
                "foreigner_net": _investor_net(record, "foreigner"),
                "institution_net": _investor_net(record, "institution"),
            }
    return out


def fetch_usd_krw() -> dict:
    with client() as c:
        data = _request(c, "/api/v1/exchange-rate", params={
            "baseCurrency": "USD", "quoteCurrency": "KRW",
        })
    return {"rate": float(data["rate"])}
