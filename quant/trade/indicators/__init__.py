"""보조지표 순수 함수 모음. stdlib + pandas/numpy만 — 네트워크·config·브로커
임포트 금지(domain/과 같은 등급).

모든 함수는 완성봉 시계열(pandas Series/DataFrame, `DataFeed.history()`가 주는
전제와 동일 — look-ahead 없음)을 받아 같은 인덱스의 Series(또는 Series 튜플)를
반환한다. 워밍업 구간(계산에 필요한 만큼의 과거 데이터가 아직 없는 앞부분)은
0이나 임의값으로 채우지 않고 NaN 그대로 둔다 — 호출부(전략)가 `pd.notna()`로
직접 판단한다.

파라미터 기본값은 전부 문헌 표준값이다(RSI 14, MACD 12/26/9, 볼린저 20·2σ,
SMA 20/50, EMA 20) — 이 저장소 파라미터를 여러 조합으로 시도해 고른 값이 아니다.
"""
from __future__ import annotations

import pandas as pd


def sma(close: pd.Series, period: int) -> pd.Series:
    """단순이동평균. 앞의 `period - 1`개는 NaN."""
    return close.rolling(period).mean()


def ema(close: pd.Series, period: int) -> pd.Series:
    """지수이동평균 — pandas `ewm(adjust=False)` 재귀식(표준 EMA 재귀).
    `min_periods=period`를 줘서 SMA와 동일하게 워밍업 구간을 NaN으로 남긴다
    (pandas 기본은 첫 데이터부터 바로 값을 내므로 명시적으로 막아야 한다)."""
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI — Wilder 평활(단순 이동평균이 아니다).

    표준 정의: 첫 `period`개의 상승분/하락분을 단순평균으로 시드(seed)한 뒤
    (`평균_t = (평균_{t-1} x (period-1) + 현재값) / period`) 재귀적으로 평활한다.
    이 재귀는 pandas의 `ewm(alpha=1/period)`와 점근적으로는 같지만 시드 방식이
    달라(ewm은 첫 원본값에서 바로 재귀를 시작) 초반 값이 어긋난다 — 그래서 시드
    단계를 명시적으로 구현한다(첫 값 처리).

    첫 유효값은 인덱스 `period`(0-base, 종가 `period+1`개 필요)에서 나온다. 그
    전은 NaN. 상승분/하락분이 모두 0(가격 불변)이면 50, 하락분만 0이면 100.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = pd.Series(index=close.index, dtype=float)
    avg_loss = pd.Series(index=close.index, dtype=float)

    if len(close) > period:
        avg_gain.iloc[period] = gain.iloc[1:period + 1].mean()
        avg_loss.iloc[period] = loss.iloc[1:period + 1].mean()
        for i in range(period + 1, len(close)):
            avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
            avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))
    result = result.mask(avg_loss == 0, 100.0)
    result = result.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return result


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD — EMA 기반. `(macd_line, signal_line, histogram)`.

    macd_line = EMA(fast) - EMA(slow). signal_line = EMA(macd_line, signal).
    histogram = macd_line - signal_line. macd_line 워밍업 구간(NaN)은 그대로
    signal_line의 ewm 계산에 들어가는데, pandas ewm은 선행 NaN을 건너뛰고 첫
    유효값부터 재귀를 시작하므로(그리고 `min_periods=signal`이 그 뒤로 signal개
    유효 관측치를 더 요구하므로) 워밍업이 자연히 누적된다 — 별도 처리 불필요.
    """
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger(
    close: pd.Series, period: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    """볼린저 밴드. `(mid, upper, lower, bandwidth, percent_b)`.

    mid = SMA(period). std는 모집단 표준편차(ddof=0) — 밴드 자체가 그 구간의
    기술통계이지 표본에서 모집단을 추정하는 게 아니므로(원저자 정의 관례),
    pandas 기본(ddof=1, 표본표준편차)이 아니라 명시적으로 ddof=0을 쓴다.
    bandwidth = (upper - lower) / mid. percent_b = (close - lower) / (upper - lower)
    — close가 lower면 0, upper면 1.
    """
    mid = sma(close, period)
    std = close.rolling(period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    bandwidth = (upper - lower) / mid
    percent_b = (close - lower) / (upper - lower)
    return mid, upper, lower, bandwidth, percent_b


def squeeze(
    close: pd.Series, period: int = 20, num_std: float = 2.0, lookback: int = 20
) -> pd.Series:
    """스퀴즈 — 밴드폭(bandwidth)이 최근 `lookback` 구간 중 최저일 때 True
    (문헌 표준 정의: 볼린저밴드 스퀴즈는 변동성 수축의 저점을 찾는 것).

    반환은 평범한 bool Series다. `lookback` 미충족 구간(rolling min이 NaN)은
    rsi/macd/bollinger 같은 수치 지표와 달리 NaN이 아니라 **False**를 반환한다
    — 스퀴즈는 "그렇다/아니다" 판정이라 NaN(미확정)을 bool로 표현할 방법이
    없고, 데이터가 부족하면 스퀴즈를 확인할 수 없다는 뜻이므로 False가 맞는
    의미다(수치 지표의 "NaN 그대로 둔다" 규칙과는 의도적으로 다르다).
    """
    _, _, _, bandwidth, _ = bollinger(close, period, num_std)
    rolling_min = bandwidth.rolling(lookback).min()
    return (bandwidth <= rolling_min).fillna(False)


def detect_box(
    high: pd.Series, low: pd.Series, lookback: int = 20, tolerance_pct: float = 1.5
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """박스(횡보) 구간 판정. `(is_box, box_high, box_low)`.

    최근 `lookback`봉의 고가들이 서로 `tolerance_pct`% 안에 모여 있고(그 구간
    최고 고가 대비 최저 고가의 괴리가 tolerance 이내) 저가들도 그러하면(그 구간
    최저 저가 대비 최고 저가의 괴리가 tolerance 이내) 박스로 판정한다 — 추세
    구간은 고가/저가가 계속 갈아치워져 괴리가 커지므로 False가 된다.

    box_high/box_low는 그 구간의 롤링 최고가/최저가(수치 지표라 워밍업 NaN
    유지). is_box는 squeeze와 같은 이유로 워밍업 구간에 False를 쓴다.
    """
    box_high = high.rolling(lookback).max()
    box_low = low.rolling(lookback).min()
    high_low_in_window = high.rolling(lookback).min()
    low_high_in_window = low.rolling(lookback).max()

    high_spread_pct = (box_high - high_low_in_window) / box_high * 100
    low_spread_pct = (low_high_in_window - box_low) / box_low * 100

    is_box = ((high_spread_pct <= tolerance_pct) & (low_spread_pct <= tolerance_pct)).fillna(False)
    return is_box, box_high, box_low
