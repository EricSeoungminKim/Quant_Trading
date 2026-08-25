"""국면(regime) 지표 어댑터 — `quant/trade/regime/interfaces.py`의
MarketIndicatorClient / BitcoinPriceAdapter 구현체.

2026-08-24 E2E 감사: 이 두 계약이 인터페이스만 있고 구현체가 아예 없어서
US 국면이 5개 지표 중 2개(QQQ 추세/변동성)로만 돌고 있었다 — 국채·코스피·
비트코인은 항상 None 이라 매 세션 "지표 제외"로 빠졌다. 여기서 코스피/코스닥
(ETF 프록시)과 비트코인(Upbit)을 살린다. 국채는 아직 무료·안정 소스가 없어
정직하게 None 을 유지한다(아래 TossIndicatorClient docstring 참고).

두 클래스 모두 네트워크 예외를 내부에서 삼키고 None 을 반환한다(계약,
interfaces.py 참고) — raw 예외를 quant/trade/ 로 올리지 않는다. 값은 캐시하지
않는다: RegimeProvider 가 이미 세션 시작 전 1회만 refresh() 하므로 어댑터
쪽에서 캐시를 얹으면 이중 캐시로 신선도 버그의 원인이 된다.
"""
from __future__ import annotations

import logging
from typing import Callable

import httpx

from quant.adapters.http import client as http_client

logger = logging.getLogger(__name__)

# Toss market-indicators API는 지수 심볼(KOSPI/KOSDAQ) 자체를 지원하지 않는다 —
# 가장 가까운 대체로 추종 ETF 일봉을 쓴다. **지수 그 자체가 아니라 프록시다** —
# 배당·리밸런싱 등으로 ETF NAV가 지수 등락률과 완전히 일치하지 않는 괴리가 있을
# 수 있다는 점을 감안할 것.
_PROXY_SYMBOL = {"KOSPI": "069500", "KOSDAQ": "229200"}  # KODEX200 / KODEX코스닥150


class TossIndicatorClient:
    """MarketIndicatorClient 구현체.

    KOSPI/KOSDAQ는 위 `_PROXY_SYMBOL` ETF의 toss 일봉 최근 2개(최신가/전일종가)로
    답한다. **국채(KR_BOND_2Y/3Y/5Y/10Y/20Y/30Y)는 아직 구현하지 않았다** — 무료·
    안정적인 일간 국채수익률 소스를 아직 확보하지 못했다(후보: 네이버 시장지표
    스크랩, KOFIA 채권정보). 항상 None을 반환하고, 프로세스당 최초 1회만 그 사실을
    로그로 남긴다(매 조회마다 로그가 쌓여 노이즈가 되는 것을 피하기 위해).
    """

    def __init__(self, client: object):
        """client: toss candles(symbol, interval, count) 를 가진 클라이언트
        (quant.adapters.brokers.toss.client.TossClient duck-type 재사용 —
        toss/datafeed.py의 _load_1d 가 쓰는 것과 같은 호출 방식)."""
        self._client = client
        self._bond_warned = False

    def indicator_price(self, symbol: str) -> float | None:
        last, _prev = self._proxy_last_prev(symbol)
        return last

    def indicator_prev_close(self, symbol: str) -> float | None:
        _last, prev = self._proxy_last_prev(symbol)
        return prev

    def _proxy_last_prev(self, symbol: str) -> tuple[float | None, float | None]:
        proxy = _PROXY_SYMBOL.get(symbol)
        if proxy is None:
            self._warn_if_bond_unimplemented(symbol)
            return None, None
        try:
            daily = self._client.candles(proxy, interval="day", count=2)
        except Exception:
            logger.warning("regime: %s 프록시(%s) 일봉 조회 실패", symbol, proxy, exc_info=True)
            return None, None
        if daily is None or "close" not in getattr(daily, "columns", []) or len(daily) < 2:
            return None, None
        try:
            last = float(daily["close"].iloc[-1])
            prev = float(daily["close"].iloc[-2])
        except Exception:
            logger.warning("regime: %s 프록시(%s) 일봉 파싱 실패", symbol, proxy, exc_info=True)
            return None, None
        return last, prev

    def _warn_if_bond_unimplemented(self, symbol: str) -> None:
        if not symbol.startswith("KR_BOND_") or self._bond_warned:
            return
        self._bond_warned = True
        logger.warning(
            "regime: 국채 지표(%s) 소스 미구현 — 무료·안정 일간 소스 확보 시 구현. "
            "후보: 네이버 시장지표 스크랩, KOFIA 채권정보",
            symbol,
        )


class UpbitBitcoinAdapter:
    """BitcoinPriceAdapter 구현체 — Upbit 공개 API(GET /v1/ticker, 인증키 불필요)로
    KRW-BTC 전일 대비 등락률(%)을 조회한다. 타임아웃 10초, 실패 시 None."""

    _URL = "https://api.upbit.com/v1/ticker"

    def __init__(self, client_factory: Callable[..., httpx.Client] = http_client):
        self._client_factory = client_factory

    def price_change_pct(self) -> float | None:
        try:
            with self._client_factory(timeout=10.0) as c:
                resp = c.get(self._URL, params={"markets": "KRW-BTC"})
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.warning("regime: 비트코인(Upbit) 조회 실패", exc_info=True)
            return None
        try:
            row = data[0]
            trade_price = float(row["trade_price"])
            prev_close = float(row["prev_closing_price"])
        except Exception:
            logger.warning("regime: 비트코인(Upbit) 응답 파싱 실패", exc_info=True)
            return None
        if prev_close == 0:
            return None
        return (trade_price - prev_close) / prev_close * 100
