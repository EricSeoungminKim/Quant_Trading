"""Yahoo Finance(yfinance) 기반 CandleSource — 인증 불필요, 즉시 사용 가능.

실측(2026-07-28, docs/data-availability.md 참고): Yahoo는 1분봉을 깊게 제공하지
않는다(최근 약 5~7거래일, 요청당 8일 상한). 반면 15분봉은 최근 60일, 1시간봉은
최근 730일, 일봉은 상장 이후 전체 히스토리를 서빙한다. 이 소스는
`MultiIntervalCandleSource` 계약(quant.collect.quotes.source)을 구현해
native_interval을 명시하고, 그 간격 그대로의 봉만 반환한다.

`fetch_1m()`은 의도적으로 구현하지 않는다 — Yahoo의 1분봉은 얕고(총 깊이 며칠) 또한
요청당 8일 상한이 있어 `CandleSource.fetch_1m()`이 기대하는 "[start, end] 구간의
1분봉 원본"을 정직하게 채울 수 없다. 얕은 1분봉을 그 계약인 것처럼 반환하는 건 이
저장소가 금지하는 조용한 저하(silent degrade)다.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_COLUMNS = ["open", "high", "low", "close", "volume"]

# Yahoo가 각 interval에 대해 "지금부터 며칠 전까지"만 서빙하는지의 하드 리밋(실측,
# docs/data-availability.md). 이 한도보다 과거인 start를 그대로 보내면 Yahoo는
# 에러 없이 빈 결과를 내려준다 — 그래서 호출 전에 clamp하고 얼마나 잘렸는지 로그로
# 남긴다(조용한 데이터 누락 방지).
_MAX_LOOKBACK_DAYS: dict[str, int | None] = {
    "15m": 60,
    "1h": 730,
    "1d": None,  # 상장 이후 전체 히스토리 — 리밋 없음
}


def _as_utc(ts: datetime) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class YFinanceCandleSource:
    """MultiIntervalCandleSource 구현체. 인스턴스 하나가 하나의 native interval만
    담당한다(예: YFinanceCandleSource("15m")) — Yahoo 자체가 interval마다 서로 다른
    과거 데이터 깊이를 갖고 있어, 하나의 소스가 여러 interval을 동시에 대표하면 그
    차이가 호출부에서 드러나지 않기 때문이다."""

    def __init__(self, interval: str = "15m") -> None:
        if interval not in _MAX_LOOKBACK_DAYS:
            raise ValueError(
                f"YFinanceCandleSource가 지원하지 않는 interval: {interval!r} "
                f"(지원: {sorted(_MAX_LOOKBACK_DAYS)})"
            )
        self.native_interval = interval

    def fetch(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        start_utc, end_utc = _as_utc(start), _as_utc(end)

        max_days = _MAX_LOOKBACK_DAYS[self.native_interval]
        if max_days is not None:
            # 정확히 max_days 전을 요청하면 Yahoo 서버 시계와의 미세한 차이로
            # 실제로는 그 경계를 넘겨 빈 결과가 나오는 경우가 실측됐다(60일 요청 시
            # 0봉, 59일 요청 시 정상) — 1일 안전 여유를 둔다.
            floor = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=max_days - 1)
            if start_utc < floor:
                logger.warning(
                    "%s %s: 요청 start=%s가 Yahoo 하드리밋(%d일)보다 과거 — %s로 clamp",
                    symbol, self.native_interval, start_utc, max_days, floor,
                )
                start_utc = floor

        # KR 앵커 일봉 백필(G): 6자리 숫자 심볼(예: "069500")은 Yahoo에서
        # "{symbol}.KS"로만 조회된다. 저장·반환 심볼은 원래 6자리를 유지해야
        # 개장일 판정 앵커 경로(data/history/069500/1d)와 일치하므로, 이 매핑은
        # yf.Ticker 질의에만 적용하고 symbol 변수 자체는 바꾸지 않는다.
        yahoo_symbol = f"{symbol}.KS" if symbol.isdigit() and len(symbol) == 6 else symbol
        df = yf.Ticker(yahoo_symbol).history(
            start=start_utc.to_pydatetime(), end=end_utc.to_pydatetime(),
            interval=self.native_interval,
        )
        if df.empty:
            return pd.DataFrame(columns=_COLUMNS)

        idx = df.index
        if idx.tz is None:
            # 일부 daily 응답이 tz-naive로 올 수 있음 — 거래소 시간대(NYSE)로 간주.
            idx = idx.tz_localize("America/New_York")
            idx = idx.tz_convert("UTC")
        elif self.native_interval == "1d" and str(idx.tz) != "UTC":
            # 일봉(1d)이면서 이미 tz-aware인 경우: KR(예: 069500.KS)은 Yahoo가
            # 비-UTC 로컬 자정(Asia/Seoul 00:00 등)으로 돌려준다. 이걸 그대로
            # tz_convert("UTC")하면 시간대 오프셋만큼(KST는 -9시간) 캘린더 날짜가
            # 전날로 밀린다 — 08-14 KST 자정 봉이 08-13 15:00 UTC로 저장되어
            # opendays.py의 개장일 판정이 하루 어긋난다(실측).
            # 일봉은 벽시계 시각이 아니라 "그 날의 거래일"을 나타내므로, 로컬
            # 캘린더 날짜를 그대로 UTC 자정으로 재해석한다(tz 정보만 버리고
            # UTC로 다시 태그 — tz_convert가 아니라 tz_localize).
            # 인트라데이(15m/1h)에는 적용하지 않는다 — 그쪽은 실제 벽시계 시각이
            # 의미를 가지므로 재해석하면 시각 자체가 왜곡된다.
            idx = idx.tz_localize(None).tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")

        out = pd.DataFrame({
            "open": df["Open"].to_numpy(dtype=float),
            "high": df["High"].to_numpy(dtype=float),
            "low": df["Low"].to_numpy(dtype=float),
            "close": df["Close"].to_numpy(dtype=float),
            "volume": df["Volume"].to_numpy(dtype=float),
        }, index=idx).sort_index()
        out = out[~out.index.duplicated(keep="last")]
        return out.loc[(out.index >= start_utc) & (out.index <= end_utc)]
