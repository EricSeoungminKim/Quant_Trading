"""Alpaca Markets(data.alpaca.markets) 기반 CandleSource — 실계좌·입금 불필요,
API 키만 필요(.env.local의 ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY). 이 어댑터는
과거 시세 조회 전용이며 주문 기능이 없다.

기본 feed는 **sip**(consolidated tape)다. 2026-08-06 실측 결과 무료 계정에서도
과거 SIP 데이터에 접근되며, TQQQ는 2016-01-01, SQQQ는 2016-01-04부터 15분봉이
제공된다. 2018-02 볼마겟돈, 2018 Q4, 2020-03 COVID 폭락, 2022 약세장(TQQQ -81.7%),
2024-08 엔캐리 청산, 2025-04 관세 쇼크를 모두 커버한다.

feed="iex"로 바꾸면 안 되는 이유(실측): 같은 15분봉의 거래량이
IEX 537,128 vs SIP 44,520,830 — IEX는 전체 거래량의 약 1.2%만 잡는다. Donchian의
거래량 필터는 상대 비교라 비율은 어느 정도 보존되지만, 유동성이 몰리는 구간에서
IEX 체결이 희박해지면 왜곡이 커진다. SIP가 무료로 열려 있는 한 IEX를 쓸 이유가 없다.
또 IEX 피드는 2020-07-27 이전 데이터가 아예 없어 COVID 폭락 구간을 잃는다.

키가 없으면 조용히 저하(degrade)하지 않고 즉시 명확한 에러로 실패한다 — 잘못된
빈 데이터로 백테스트를 조용히 오염시키는 것보다 낫다.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

_BASE_URL = "https://data.alpaca.markets"
_COLUMNS = ["open", "high", "low", "close", "volume"]

# CandleSource/MultiIntervalCandleSource가 쓰는 interval 표기 -> Alpaca timeframe 표기.
# 정규장 필터가 마지막 봉의 시가 경계(16:00 - interval)를 구하는 데 쓴다.
_INTERVAL_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 24 * 60}

_TIMEFRAMES: dict[str, str] = {
    "1m": "1Min",
    "5m": "5Min",
    "15m": "15Min",
    "1h": "1Hour",
    "1d": "1Day",
}


def _regular_session_only(df: pd.DataFrame, interval_minutes: int) -> pd.DataFrame:
    """미국 정규장(09:30~16:00 ET)만 남긴다.

    경계는 봉 **시가** 기준이므로 마지막으로 남길 봉의 시가는 `16:00 - interval`이다.
    이 값을 "15:45"로 하드코딩해 두면 15분봉에서만 맞고 다른 간격에서는 세션 끝이
    조용히 잘린다 — 5분봉이면 15:50/15:55 두 봉(마감 직전 10분)이, 1분봉이면 14봉이
    사라진다. 하필 거래량이 가장 몰리고 EoD 청산이 일어나는 구간이라, 잘린 채로는
    종가 청산 전략의 체결가가 실제와 달라진다.

    Alpaca는 04:00~20:00 ET 시간외 거래까지 준다(2026-08-06 실측: 전체의 59%가
    시간외). 이 전략에는 그대로 쓰면 안 된다:
    - Donchian 진입 조건이 "거래량 > 배수 × 직전 N봉 평균"인데, 시간외 봉의
      거래량은 정규장의 6.6%에 불과하다. 평균이 끌려 내려가면서 필터가 사실상
      항상 참이 된다.
    - 신고가 돌파 판정에 유동성이 희박한 시간외 고가가 섞인다.
    - min_session_bars_before_entry 게이트가 세션 기준 봉 수를 전제한다.
    yfinance 소스는 정규장만 주므로(26봉/일), 필터하지 않으면 소스마다 데이터의
    의미가 달라진다 — 그게 더 위험하다.

    시간외를 쓰는 전략을 만들 때는 regular_session_only=False로 명시적으로 켠다.
    """
    if df.empty:
        return df
    et = df.tz_convert("America/New_York") if df.index.tz is not None else df.tz_localize("UTC").tz_convert("America/New_York")
    last_open = (datetime(2000, 1, 1, 16, 0) - timedelta(minutes=interval_minutes)).time()
    mask = et.index.indexer_between_time("09:30", last_open)
    return df.iloc[mask]


def _as_utc(ts: datetime) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class AlpacaCandleSource:
    """interval="1m"(기본)이면 CandleSource.fetch_1m()도 제공해 backfill()의 기본
    1분봉 경로에 그대로 꽂힌다 — Alpaca 무료 티어는 Yahoo와 달리 분봉을 실제로
    깊게(~2016년까지) 제공하므로 1분봉을 지어내는 게 아니라 정직하게 서빙할 수
    있다. 다른 interval(15m/1h/1d)로 구성하면 MultiIntervalCandleSource로서
    backfill(..., interval=...)에 꽂는다."""

    def __init__(self, interval: str = "1m", feed: str = "sip",
                 regular_session_only: bool = True) -> None:
        if interval not in _TIMEFRAMES:
            raise ValueError(
                f"AlpacaCandleSource가 지원하지 않는 interval: {interval!r} "
                f"(지원: {sorted(_TIMEFRAMES)})"
            )
        key_id = os.environ.get("ALPACA_API_KEY_ID", "")
        secret = os.environ.get("ALPACA_API_SECRET_KEY", "")
        if not key_id or not secret:
            raise RuntimeError(
                "Alpaca API 키가 없습니다 — .env.local에 ALPACA_API_KEY_ID / "
                "ALPACA_API_SECRET_KEY를 설정하세요(.env.example 참고, "
                "https://alpaca.markets에서 발급 — 계정 생성은 사용자 본인이 해야 "
                "하는 작업이라 이 코드가 대신 만들지 않는다)."
            )
        self.native_interval = interval
        self._feed = feed
        self._regular_session_only = regular_session_only
        self._timeframe = _TIMEFRAMES[interval]
        self._http = httpx.Client(
            base_url=_BASE_URL, timeout=10.0,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
        )

    def fetch(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        start_ts, end_ts = _as_utc(start), _as_utc(end)

        rows: list[dict] = []
        page_token: str | None = None
        while True:
            params: dict = {
                "timeframe": self._timeframe,
                "start": start_ts.isoformat(),
                "end": end_ts.isoformat(),
                "limit": 10000,
                "feed": self._feed,
                # 반드시 분할/배당 조정. raw로 받으면 액면분할이 폭락으로,
                # 액면병합이 폭등으로 보인다 — 2026-08-06 실측: TQQQ 2:1 분할
                # (2022-01-13)이 하루 -54% 갭으로 나타나 전략이 손절을 터뜨린다.
                # SQQQ는 액면병합이 잦아 반대 방향으로 같은 문제가 생긴다.
                "adjustment": "all",
            }
            if page_token:
                params["page_token"] = page_token
            resp = self._http.get(f"/v2/stocks/{symbol}/bars", params=params)
            if resp.status_code == 403 and "recent SIP" in resp.text:
                # 무료 구독은 과거 SIP는 되지만 최근 구간은 막혀 있다. 여기서
                # IEX로 갈아타면 안 된다 — IEX 거래량은 통합 거래량의 약 1.2%라
                # 한 데이터셋 안에 스케일이 다른 봉이 섞이고, 거래량 필터가
                # 조용히 망가진다. 받은 데이터까지만 쓰고 경계를 알린다.
                # **"영향 없음"이라고 쓰지 않는다.** 원래 주석은 "최근 시세는 실거래
                # 피드(Toss)가 담당하므로 백테스트에는 영향 없음"이었는데, 그 전제가
                # 틀렸다 — regime provider 가 data/history/QQQ/1d/ 를 직접 읽어
                # 라이브 사이징 배수를 계산한다(2026-08-13 실측: 봉이 13일 멈춰
                # 국면이 뒤집혀 있었다). 일봉 백필은 yfinance 를 쓴다.
                logger.warning(
                    "%s: 최근 구간 SIP 미허용(무료 구독) — 여기까지만 수집한다. "
                    "이 소스로는 최근 구간을 채울 수 없다(rc=0 이어도 파일은 그대로다). "
                    "일봉은 yfinance 로 받을 것 — server/scripts/backfill_us_daily.sh", symbol,
                )
                break
            resp.raise_for_status()
            body = resp.json()
            rows.extend(body.get("bars") or [])
            page_token = body.get("next_page_token")
            if not page_token:
                break

        if not rows:
            return pd.DataFrame(columns=_COLUMNS)
        df = pd.DataFrame([{
            "ts": pd.Timestamp(r["t"]),
            "open": float(r["o"]), "high": float(r["h"]),
            "low": float(r["l"]), "close": float(r["c"]),
            "volume": float(r["v"]),
        } for r in rows]).set_index("ts").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        df = df.loc[(df.index >= start_ts) & (df.index <= end_ts)]
        return (
            _regular_session_only(df, _INTERVAL_MINUTES[self.native_interval])
            if self._regular_session_only else df
        )

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        if self.native_interval != "1m":
            raise NotImplementedError(
                f"이 AlpacaCandleSource 인스턴스는 native_interval={self.native_interval!r}로 "
                "구성됐습니다 — 1분봉이 필요하면 AlpacaCandleSource(interval='1m')를 쓰세요."
            )
        return self.fetch(symbol, start, end)
