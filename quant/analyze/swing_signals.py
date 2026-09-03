"""스윙(멀티데이) 시그널 — 단기반전(short_term_reversal) / 거래량충격
(volume_shock_premium). analyze 평면, 순수 pandas만 쓴다 — 네트워크·파일I/O 없음
(quant/trade/ 임포트 없음, `quant/analyze/manual_recs.py`와 같은 평면 제약).

## 왜 이 파일이 있나 (2026-09-03)

quant-backtest 워크포워드(KR 일봉 2016→2026, 유니버스=시총≥3,000억+20일
중앙값 거래대금≥50억, 왕복비용 23bp)에서 OOS 엣지가 확인된 두 신호:

- `short_term_reversal_candidates` — 직전 5일 수익률 하위 10분위 매수, 5일 보유.
  기준선(전종목·전일 평균) 대비 +17.5bp/거래, t=3.2.
- `volume_shock_candidates` — 거래대금이 20일 중앙값의 2.5배 이상 + 상승마감,
  10일 보유. 기준선 대비 +51bp/거래, t=2.95.

둘 다 스윙(멀티데이 보유)이라 2026-09-03 소유자 결정("자동매매는 단타·스캘핑만")
아래서는 엔진이 자동으로 사지 않는다 — `quant/analyze/manual_recs.py`가 이
모듈의 후보를 "수동 계좌 추천"으로 텔레그램에 낸다(주문 없음).

## 이 모듈이 하지 않는 것

- 일봉을 읽지 않는다. `daily_bars_by_symbol`(심볼 → OHLCV DataFrame, 오름차순
  정렬 가정)을 호출부(manual_recs.py)가 만들어 넘긴다 — 파일 I/O는 그쪽 소관
  (`quant/analyze/opendays.py`와 같은 파티션 규칙으로 이미 읽고 있다).
- 시총·거래대금 조회를 하지 않는다. `largecap_universe()`는 이미 계산된
  시총/거래대금 dict를 받아 필터링만 한다 — 그 숫자를 누가 어떻게 구했는지는
  모른다(시총은 `quant/collect/kr_largecap_daily.py`가 Toss stock_info로 채운다).
- **각 DataFrame의 마지막 행을 "완성된 마지막 세션"으로 취급한다.** 당일
  장중(미완성) 봉을 넘기면 그 봉을 완성 세션으로 오판한다 — 호출부가 걸러야
  한다(manual_recs.py의 다른 생산자들과 같은 관례).
"""
from __future__ import annotations

from typing import Iterable

import pandas as pd

# 무효화 손절폭 — 두 신호 모두 과제 지시문 예시("−5% from ref")를 그대로 쓴다.
# 문헌 근거 자체가 종목 특이적 손절폭을 정하지 않으므로(리버설/거래량충격 둘 다
# "N일 보유 후 청산"이 핵심이지 촘촘한 스탑이 아니다), 다른 수동 추천 생산자
# (rsi2_dip -5%, overnight_drift -3%, close_bet -1%)와 자릿수를 맞춘 보수적 값.
_STOP_PCT = 5.0

_STR_HOLD_DAYS = 5
_VSP_HOLD_DAYS = 10


def _invalidation(stop_pct: float = _STOP_PCT) -> str:
    return f"기준가 대비 -{stop_pct:g}%"


def _ref_date(ts) -> str:
    return str(ts.date() if hasattr(ts, "date") else ts)


def short_term_reversal_candidates(
    daily_bars_by_symbol: dict[str, pd.DataFrame],
    *, lookback: int = 5, bottom_pct: float = 0.10, min_names: int = 3,
) -> list[dict]:
    """직전 `lookback`일 수익률 하위 `bottom_pct`(기본 10%, 최소 1종목) 매수 후보.

    각 DataFrame은 "close" 컬럼 + 최소 `lookback+1`개 완성 세션이 있어야 후보에
    들어간다(그 미만은 조용히 제외 — 데이터 부족을 신호 없음으로 위장하지
    않되, 에러도 던지지 않는다). 평가 대상 종목 수가 `min_names` 미만이면
    "하위 10분위"라는 개념 자체가 무의미하므로 빈 리스트를 돌려준다.

    반환은 수익률 오름차순(가장 많이 빠진 종목이 먼저 = 신호가 가장 강함)."""
    returns: dict[str, float] = {}
    ref_price: dict[str, float] = {}
    ref_date: dict[str, str] = {}

    for symbol, df in daily_bars_by_symbol.items():
        if df is None or df.empty or "close" not in df.columns:
            continue
        closes = df["close"].dropna()
        if len(closes) < lookback + 1:
            continue
        prior = float(closes.iloc[-1 - lookback])
        if prior <= 0:
            continue
        last = float(closes.iloc[-1])
        returns[symbol] = last / prior - 1
        ref_price[symbol] = last
        ref_date[symbol] = _ref_date(closes.index[-1])

    if len(returns) < min_names:
        return []

    k = max(1, int(len(returns) * bottom_pct))
    ranked = sorted(returns.items(), key=lambda kv: kv[1])[:k]

    return [
        {
            "symbol": symbol,
            "signal": "short_term_reversal",
            "value": ret,
            "ref_price": ref_price[symbol],
            "ref_date": ref_date[symbol],
            "hold_days": _STR_HOLD_DAYS,
            "invalidation": _invalidation(),
            "horizon": f"D+{_STR_HOLD_DAYS}",
        }
        for symbol, ret in ranked
    ]


def volume_shock_candidates(
    daily_bars_by_symbol: dict[str, pd.DataFrame],
    *, mult: float = 2.5, window: int = 20,
) -> list[dict]:
    """거래대금(종가×거래량)이 직전 `window`일 중앙값의 `mult`배 이상이면서
    상승마감(종가>시가)한 마지막 완성 세션 후보. 중앙값은 **오늘을 뺀** 직전
    `window`일로 계산한다 — 오늘 자체의 거래대금 폭증이 그 기준선을 같이
    끌어올리면 배율이 자기충족적으로 낮아져 진짜 충격을 놓친다.

    "close"/"open"/"volume" 세 컬럼이 모두 있고 `window+1`개 이상 완성 세션이
    있어야 후보에 들어간다. 반환은 배율 내림차순(가장 강한 충격이 먼저)."""
    needed = {"close", "open", "volume"}
    out: list[dict] = []

    for symbol, df in daily_bars_by_symbol.items():
        if df is None or df.empty or not needed.issubset(df.columns):
            continue
        d = df.dropna(subset=list(needed))
        if len(d) < window + 1:
            continue

        turnover = d["close"] * d["volume"]
        baseline = float(turnover.iloc[-1 - window:-1].median())
        if baseline <= 0:
            continue
        today_turnover = float(turnover.iloc[-1])
        multiple = today_turnover / baseline
        if multiple < mult:
            continue

        last_close = float(d["close"].iloc[-1])
        last_open = float(d["open"].iloc[-1])
        if not (last_close > last_open):
            continue

        out.append({
            "symbol": symbol,
            "signal": "volume_shock_premium",
            "value": multiple,
            "ref_price": last_close,
            "ref_date": _ref_date(d.index[-1]),
            "hold_days": _VSP_HOLD_DAYS,
            "invalidation": _invalidation(),
            "horizon": f"D+{_VSP_HOLD_DAYS}",
        })

    out.sort(key=lambda r: r["value"], reverse=True)
    return out


def largecap_universe(
    market_caps: dict[str, float],
    turnover_20d_median: dict[str, float],
    *, min_cap: float = 3e11, min_turnover: float = 5e9,
) -> list[str]:
    """시총 `min_cap`(기본 3,000억) 이상 **그리고** 20일 중앙값 거래대금
    `min_turnover`(기본 50억) 이상인 심볼만 남긴다. 두 dict 중 한쪽에라도
    없거나 값이 `None`이면 판정 불가로 취급해 제외한다(0으로 위장해 통과시키지
    않는다). 시총 내림차순으로 정렬해 돌려준다."""
    passed: Iterable[str] = (
        symbol for symbol, cap in market_caps.items()
        if cap is not None and cap >= min_cap
        and turnover_20d_median.get(symbol) is not None
        and turnover_20d_median[symbol] >= min_turnover
    )
    return sorted(passed, key=lambda s: market_caps[s], reverse=True)
