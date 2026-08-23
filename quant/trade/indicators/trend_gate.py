"""QuantConnect 상위 전략(#407 Cross-Sectional Momentum with Trend Filter, #478
Quantum Walk BQP)에서 이식 가능한 두 필터 — **전략 통째 이식이 아니라 필터만**
(#407은 유니버스 1000종목·펀더멘털 데이터, #478은 scipy 기반 양자보행 최적화를
쓰는데 둘 다 이 저장소 환경에 없다).

- `adx_di`: #407의 ADX>25 + DI 확인(추세 존재/방향 필터)의 계산부.
- `atr_ratio`: #478의 변동성 억제기(과도한 변동성 구간 회피) 근사.
- `trend_ok`/`volatility_ok`: 위 계산을 boolean 판정으로 감싼 게이트 헬퍼.

전부 순수 함수 — stdlib + pandas/numpy만(**scipy 금지**, quant/trade/ 등급 규칙과
동일). 다른 지표 함수(`quant/trade/indicators/__init__.py`)와 달리 이 모듈은
"완성봉 시계열 → 같은 인덱스의 Series"가 아니라 "완성봉 시계열 → 최신 시점의
단일 값(또는 None)"을 반환한다 — 호출부(전략)가 매 사이클 최신 판정 하나만
필요로 하기 때문(전체 히스토리 재계산은 낭비).

**게이트 부재 = 기존 동작.** `trend_ok`/`volatility_ok`는 데이터가 부족하거나
계산이 불가능하면 (실패가 아니라) **True**를 반환한다 — 일봉 히스토리를 못
구했다고 진입을 막으면, 데이터 공급 장애가 조용히 전략을 무력화시키는 결과가
된다(이 저장소 원칙: 실패는 눈에 보여야 한다). 이 필터들은 "추세/변동성이
나쁘다고 확인됐을 때만" 진입을 막는 방어이지, "확인 못 했다"를 막을 이유로
쓰지 않는다.

이 값들(adx_min=25, max_atr_ratio=0.10)은 QuantConnect #407/#478에서 가져온
것으로 **이 저장소의 실데이터로 검증된 값이 아니다** — burn-in 전 상태.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def adx_di(bars: pd.DataFrame, period: int = 14) -> tuple[float, float, float] | None:
    """ADX/+DI/-DI (표준 Wilder 평활) — #407 근거.

    True Range와 방향성 움직임(+DM/-DM)을 각각 Wilder 평활(첫 `period`개 단순
    평균으로 시드 후 `평균_t = (평균_{t-1} x (period-1) + 현재값) / period`
    재귀 — `quant/trade/indicators/__init__.py`의 `rsi`와 동일 시드 관례)한 뒤
    DI를 구하고, DX(`100 x |+DI - -DI| / (+DI + -DI)`)를 같은 방식으로 다시
    평활해 ADX를 얻는다.

    최소 `2*period`개의 완성봉이 있어야 유효 ADX 1개가 나온다(TR/DM 평활 시드에
    `period`개 + DX 평활 시드에 다시 `period`개). 부족하거나 도중에 NaN이
    섞이면 None — 호출부는 이를 "판단 불가"로 다뤄야 한다(0/False로 임의
    대체 금지, 모듈 docstring "게이트 부재" 절 참고).

    반환은 (ADX, +DI, -DI) 최신값 하나뿐이다 — 전체 시계열이 필요 없어(호출부는
    매 사이클 최신 판정만 본다) 다른 지표 함수처럼 Series 전체를 반환하지 않는다.
    """
    if bars is None or period <= 0:
        return None
    required = 2 * period
    if len(bars) < required:
        return None

    high = bars["high"].astype(float).to_numpy()
    low = bars["low"].astype(float).to_numpy()
    close = bars["close"].astype(float).to_numpy()
    if np.isnan(high).any() or np.isnan(low).any() or np.isnan(close).any():
        return None
    n = len(high)

    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))

    def _wilder(values: np.ndarray) -> np.ndarray:
        smoothed = np.full(n, np.nan)
        smoothed[period] = values[1:period + 1].mean()
        for i in range(period + 1, n):
            smoothed[i] = (smoothed[i - 1] * (period - 1) + values[i]) / period
        return smoothed

    smoothed_tr = _wilder(tr)
    smoothed_plus_dm = _wilder(plus_dm)
    smoothed_minus_dm = _wilder(minus_dm)

    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    dx = np.full(n, np.nan)
    for i in range(period, n):
        if smoothed_tr[i] == 0 or np.isnan(smoothed_tr[i]):
            continue
        plus_di[i] = 100 * smoothed_plus_dm[i] / smoothed_tr[i]
        minus_di[i] = 100 * smoothed_minus_dm[i] / smoothed_tr[i]
        di_sum = plus_di[i] + minus_di[i]
        dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / di_sum if di_sum != 0 else 0.0

    seed_end = period + period  # exclusive — DX 시드 구간은 [period, 2*period)
    if seed_end > n or np.isnan(dx[period:seed_end]).any():
        return None
    adx = float(dx[period:seed_end].mean())
    for i in range(seed_end, n):
        if np.isnan(dx[i]):
            return None
        adx = (adx * (period - 1) + dx[i]) / period

    if np.isnan(plus_di[-1]) or np.isnan(minus_di[-1]) or np.isnan(adx):
        return None
    return adx, float(plus_di[-1]), float(minus_di[-1])


def atr_ratio(bars: pd.DataFrame, period: int = 14) -> float | None:
    """ATR(period, Wilder 평활) / 최근 완성봉 종가 — #478의 변동성 억제기 근사.

    True Range 정의·Wilder 평활 시드는 `adx_di`와 동일(첫 `period`개 단순평균
    시드 후 재귀 평활). 최소 `period + 1`개의 완성봉(TR 계산에 직전 종가 1개 +
    평활 시드 `period`개)이 필요 — 부족·NaN·종가<=0이면 None."""
    if bars is None or period <= 0:
        return None
    if len(bars) < period + 1:
        return None
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    if high.isna().any() or low.isna().any() or close.isna().any():
        return None
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    tr_values = tr.iloc[1:].to_numpy(dtype=float)  # 첫 값(직전 종가 없음) 제외
    if len(tr_values) < period:
        return None
    atr = tr_values[:period].mean()
    for v in tr_values[period:]:
        atr = (atr * (period - 1) + v) / period

    last_price = float(close.iloc[-1])
    if last_price <= 0 or pd.isna(atr):
        return None
    return float(atr / last_price)


def trend_ok(bars: pd.DataFrame, *, adx_min: float = 25.0, require_di: bool = True) -> bool:
    """일봉 추세 게이트(#407: ADX>=adx_min + 선택적 +DI>-DI 방향 확인).

    데이터 부족/계산 불가(`adx_di`가 None) 시 **True** — 게이트 부재=기존 동작
    (모듈 docstring 참고, 억지로 막지 않는다)."""
    result = adx_di(bars)
    if result is None:
        return True
    adx, plus_di, minus_di = result
    if adx < adx_min:
        return False
    if require_di and not (plus_di > minus_di):
        return False
    return True


def volatility_ok(bars: pd.DataFrame, *, max_atr_ratio: float = 0.10) -> bool:
    """변동성 억제기(#478: ATR/가격 상한). 데이터 부족/계산 불가(`atr_ratio`가
    None) 시 **True** — 게이트 부재=기존 동작(모듈 docstring 참고)."""
    ratio = atr_ratio(bars)
    if ratio is None:
        return True
    return ratio <= max_atr_ratio
