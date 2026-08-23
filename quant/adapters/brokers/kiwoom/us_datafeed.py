"""KiwoomUSDataFeed — domain.interfaces.DataFeed 구현체, 미국 심볼 전용.

키움 해외증권(global) 키로 `KiwoomClient.usa_chart_1m`(usa06010, POST /api/us/chart)을
불러 Quote/과거봉을 만든다. 2026-08-12 실전 서버 실호출로 검증됨(NVDA, stex_tp=ND) —
응답 필드 상세는 `client.py: usa_chart_1m` docstring 참고.

이 소스는 **US 심볼만** 다룬다 — KR 심볼이 들어오면 값을 지어내지 않고 즉시
DataSourceError를 던진다(호출부가 실수로 KR 심볼을 이 라우트에 물리는 조립 실수를
여기서도 한 번 더 막는다).

history()는 usa06010이 실측으로 준 최근 ~100행(1분봉)만 다룰 수 있다 — 그보다 깊은
페이지네이션 지원 여부는 [미확인]이라 시도하지 않는다. 이 얕은 룩백 때문에
`app/assembly.py: build_kiwoom_us_route`는 이 소스를 Capability.BARS로는 라우팅에
등록하지 않는다(QUOTE만) — donchian(lookback_bars=40, 15분봉 → 600분 필요) 같은
전략에 이 얕은 소스를 BARS로 물리면 MarketDataService가 짧은 DataFrame을 정상
응답으로 받아들여 폴백하지 않고 룩백이 조용히 얕아진다. 자세한 이유는 그 함수의
docstring 참고. history()는 완전히 구현·테스트돼 있으니 usa06010의 페이지네이션이
[확인됨]으로 바뀌면 라우팅 쪽만 고치면 된다.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.adapters.data.resample import resample_1m
from quant.core.ports import Clock, DataSourceError
from quant.core.models import Quote

from .client import KiwoomClient
from .datafeed import is_kr_symbol

logger = logging.getLogger(__name__)

# 해외증권 글로벌 키는 2026-08-12 기준 실전 서버(api.kiwoom.com)에서만 검증됐다 —
# 모의서버(mockapi) 지원 여부는 [미확인].
KIWOOM_GLOBAL_BASE_URL = "https://api.kiwoom.com"

# usa06010 cntr_tm(YYYYMMDDHHMMSS)의 시간대 — 2026-08-11 실측(20260811114600 ≈
# 11:46 ET)으로 미국 동부시간으로 보이나 [미확인]. 확인 전까지는 America/New_York으로
# 가정한다(DST는 zoneinfo가 처리하므로 서머타임 경계에서도 안전하다).
_US_EASTERN = ZoneInfo("America/New_York")

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


def _interval_minutes(interval: str) -> int:
    """"15m" -> 15. resample_1m은 문자열이 아니라 정수 분을 받는다(toss/datafeed.py와 동일)."""
    return int(interval.rstrip("m"))


def _parse_signed_number(raw: object) -> float | None:
    """"+304.3400"/" -1.20"처럼 부호·공백이 붙은 문자열 가격/수량을 절대값으로
    파싱한다(kiwoom 국내 FID 관례와 동일 — websocket.py의 `_parse_price` 참고).
    파싱 실패는 None(호출부가 그 행을 버린다) — 값을 지어내지 않는다."""
    if raw is None:
        return None
    try:
        return abs(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def _parse_cntr_tm(raw: object) -> pd.Timestamp | None:
    """"20260811114600"(YYYYMMDDHHMMSS) -> tz-aware(America/New_York) Timestamp.
    파싱 실패는 None(호출부가 그 행을 버린다)."""
    if not raw:
        return None
    try:
        dt = datetime.strptime(str(raw), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return pd.Timestamp(dt, tz=_US_EASTERN)


def _rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    """usa06010 raw rows -> OHLCV DataFrame(오름차순, tz-aware 인덱스).

    open/high/low가 파싱 실패하면(필드 부재 등) close(cur_prc)로 대체한다 — 결측을
    0으로 지어내는 것보다 안전하다(캔들의 형태만 납작해질 뿐 가격 자체는 실측값).
    ts(cntr_tm) 또는 close(cur_prc)가 없는 행은 통째로 버린다."""
    records = []
    for row in rows:
        ts = _parse_cntr_tm(row.get("cntr_tm"))
        close = _parse_signed_number(row.get("cur_prc"))
        if ts is None or close is None:
            continue
        open_ = _parse_signed_number(row.get("open_pric"))
        high = _parse_signed_number(row.get("high_pric"))
        low = _parse_signed_number(row.get("low_pric"))
        volume = _parse_signed_number(row.get("trde_qty"))
        records.append({
            "ts": ts,
            "open": open_ if open_ is not None else close,
            "high": high if high is not None else close,
            "low": low if low is not None else close,
            "close": close,
            "volume": volume if volume is not None else 0.0,
        })
    if not records:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)
    df = pd.DataFrame(records).set_index("ts").sort_index()
    return df[~df.index.duplicated(keep="last")][_OHLCV_COLUMNS]


class KiwoomUSDataFeed:
    def __init__(self, client: KiwoomClient, clock: Clock, exchange: str = "ND") -> None:
        self._client = client
        self._clock = clock
        self._exchange = exchange

    def quote(self, symbol: str) -> Quote | None:
        if is_kr_symbol(symbol):
            raise DataSourceError(f"kiwoom_us는 KR 심볼을 다루지 않는다: {symbol}")
        df = self._fetch(symbol)
        if df.empty:
            return None  # 조회는 성공했고 데이터가 없다 — 실패가 아니다
        return Quote(symbol=symbol, ts=df.index[-1].to_pydatetime(), price=float(df.iloc[-1]["close"]))

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        if is_kr_symbol(symbol):
            raise DataSourceError(f"kiwoom_us는 KR 심볼을 다루지 않는다: {symbol}")
        if interval == "1d":
            # usa06010은 분봉 전용이다 — 일봉을 지어내지 않는다.
            raise DataSourceError("kiwoom_us(usa06010)는 일봉을 제공하지 않는다 — 분봉 전용")
        df1m = self._completed_1m(symbol)
        if interval == "1m":
            return df1m.tail(n)
        return resample_1m(df1m, _interval_minutes(interval)).tail(n)

    def _fetch(self, symbol: str) -> pd.DataFrame:
        try:
            rows = self._client.usa_chart_1m(symbol, exchange=self._exchange)
        except Exception as e:
            # 벤더 예외를 도메인 예외로 변환한다 — None/빈 프레임으로 위장하면
            # MarketDataService가 정상 응답으로 오인해 폴백하지 않는다.
            raise DataSourceError(f"kiwoom_us {symbol} 조회 실패: {type(e).__name__}: {e}") from e
        return _rows_to_frame(rows)

    def _completed_1m(self, symbol: str) -> pd.DataFrame:
        """완성봉만 반환한다(look-ahead 금지 — domain/interfaces.py 계약). usa06010은
        진행 중인 분의 중간 체결가도 그 분의 cntr_tm으로 줄 수 있으므로, 클록 기준
        "분이 실제로 끝났는가"로 한 번 더 걸러낸다(MarketDataService의
        `_filter_completed_bars`와 같은 원칙을 소스 레벨에서도 지킨다)."""
        df = self._fetch(symbol)
        if df.empty:
            return df
        bar_close = df.index + pd.Timedelta(minutes=1)
        return df[bar_close <= self._clock.now()]
