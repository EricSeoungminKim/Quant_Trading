"""KiwoomRealtimeSource — MarketDataService가 라우팅 대상으로 삼는 실시간 웹소켓 소스.

KiwoomRealtimeFeed 자체는 벤더 프로토콜(LOGIN/REG/REAL)만 알고 quote()/health()로
최소한의 표면만 노출한다. 여기서 그 표면을 quant.adapters.data.service.MarketDataSource
Protocol(quote()/history())로 감싸고, **웹소켓이 죽었거나 틱이 오래됐을 때 stale 값을
조용히 반환하지 않고 DataSourceError를 던져 MarketDataService가 다음 라우트(toss REST)로
폴백하게 만드는** 안전장치를 여기 둔다. 이 파일이 이 기능 전체의 핵심 안전 경계다 —
죽은 웹소켓의 마지막 틱으로 거래 판단을 내리면 안 된다.
"""
from __future__ import annotations

import pandas as pd

from quant.core.ports import Clock, DataSourceError
from quant.core.models import Quote

from .websocket import KiwoomRealtimeFeed


def is_kr_symbol(symbol: str) -> bool:
    """국내 종목코드는 6자리 숫자(거래소 접미사가 붙어도 앞부분은 숫자, 문서 5.3
    item 필드: "005930_NX"/"005930_AL" 등) — TQQQ 같은 해외 티커는 여기 해당하지
    않는다. 접미사가 실제로 이 형태로 오는지는 [미확인]이지만, 접미사 유무와
    무관하게 숫자 6자리 여부만으로 KR/US를 가르면 충분하다."""
    base = symbol.split("_", 1)[0]
    return base.isdigit() and len(base) == 6


class KiwoomRealtimeSource:
    """domain 쪽 MarketDataSource 표면(quote()/history())을 구현하는 얇은 어댑터.

    history()는 지원하지 않는다 — 이 소스는 항상 Capability.QUOTE로만 라우트에
    등록되므로 MarketDataService가 실제로 부르는 일은 없다. 그래도 값을 조작해
    빈 프레임을 돌려주기보다는 명시적으로 실패시켜 둔다(방어적 방침).
    """

    def __init__(self, feed: KiwoomRealtimeFeed, clock: Clock, stale_seconds: float = 30.0) -> None:
        self._feed = feed
        self._clock = clock
        self._stale_seconds = float(stale_seconds)

    def quote(self, symbol: str) -> Quote | None:
        health = self._feed.health()
        if not health.connected:
            raise DataSourceError(f"kiwoom_rt 연결 끊김 (symbol={symbol})")

        quote = self._feed.quote(symbol)
        if quote is None:
            # 구독은 됐어도 체결 틱이 아직 한 번도 안 왔을 수 있다(장 시작 직후,
            # 또는 해외주식처럼 실시간 지원 여부가 [미확인]인 심볼). 값을 지어내지
            # 않고 폴백시킨다 — toss REST가 대신 응답한다.
            raise DataSourceError(f"kiwoom_rt {symbol} 아직 수신된 틱 없음")

        age = (self._clock.now() - quote.ts).total_seconds()
        if age > self._stale_seconds:
            raise DataSourceError(
                f"kiwoom_rt {symbol} 시세가 stale함 ({age:.0f}s > {self._stale_seconds:.0f}s) — "
                "웹소켓은 연결돼 있지만 최근 틱이 없다"
            )
        return quote

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        raise DataSourceError("kiwoom_rt는 과거 봉을 제공하지 않는다(QUOTE 캐퍼빌리티만 등록됨)")
