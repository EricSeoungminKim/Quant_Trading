"""history(최근 1년치 종가, 최대 252거래일) 배열에서 파생 지표를 계산하는 순수 함수 모음.

네트워크 호출 없음 — `quant/collect/sources/market.py` 가 이미 채워둔 `history` 만 쓴다.
결측(데이터 부족, 0 나눗셈, 정의역 밖 입력)은 예외 대신 `None` 으로 표시한다 —
NaN 이 리포트 HTML 에 그대로 찍히는 걸 막기 위함이다.
"""
from __future__ import annotations

import math
import statistics


def sma(values: list[float], window: int) -> float | None:
    """마지막 `window` 개 종가의 단순평균. 데이터가 모자라면 None."""
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def disparity(close: float, ma: float | None) -> float | None:
    """이격도 (close/ma - 1) * 100 — 이동평균 대비 과열(양수)/과냉(음수) 정도."""
    if not ma:
        return None
    return round((close / ma - 1) * 100, 2)


def range_position(values: list[float]) -> float | None:
    """기간 내 최저~최고 구간에서 현재가의 상대 위치(0~100) — 클수록 고점권."""
    if len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    if hi == lo:
        return None
    close = values[-1]
    return round((close - lo) / (hi - lo) * 100, 1)


def cum_return(values: list[float], days: int) -> float | None:
    """`days` 거래일 전 대비 누적 등락률(%)."""
    if len(values) < days + 1:
        return None
    base = values[-(days + 1)]
    if not base:
        return None
    return round((values[-1] / base - 1) * 100, 2)


def realized_vol(values: list[float], days: int = 20) -> float | None:
    """최근 `days` 일 일간 로그수익률의 표본표준편차를 연율화(%)한 실현 변동성."""
    if len(values) < days + 1:
        return None
    window = values[-(days + 1):]
    log_returns = []
    for prev, cur in zip(window, window[1:]):
        if prev <= 0 or cur <= 0:
            return None
        log_returns.append(math.log(cur / prev))
    if len(log_returns) < 2:
        return None
    stdev = statistics.stdev(log_returns)
    return round(stdev * math.sqrt(252) * 100, 1)


def describe(quote: dict) -> dict:
    """quote 하나(history 포함)를 받아 파생 지표 묶음을 반환. history 없으면 전부 None."""
    history = quote.get("history") or []
    if not history:
        return {
            "ma20": None, "ma60": None, "ma120": None,
            "disparity20": None, "disparity60": None, "disparity120": None,
            "pos_52w": None,
            "ret_5d": None, "ret_20d": None, "ret_60d": None,
            "vol20": None,
            "above_ma20": None, "above_ma60": None, "above_ma120": None,
        }

    close = history[-1]
    ma20 = sma(history, 20)
    ma60 = sma(history, 60)
    ma120 = sma(history, 120)
    # 52주 위치: history 가 이제 최대 252개(1년치)라 그 구간에서 계산한다.
    # 상장 초기 등 252개 미만이면 있는 만큼으로 계산한다 — None 을 내지 않는다.
    return {
        "ma20": ma20,
        "ma60": ma60,
        "ma120": ma120,
        "disparity20": disparity(close, ma20),
        "disparity60": disparity(close, ma60),
        "disparity120": disparity(close, ma120),
        "pos_52w": range_position(history[-252:]),
        "ret_5d": cum_return(history, 5),
        "ret_20d": cum_return(history, 20),
        "ret_60d": cum_return(history, 60),
        "vol20": realized_vol(history, 20),
        "above_ma20": (close > ma20) if ma20 is not None else None,
        "above_ma60": (close > ma60) if ma60 is not None else None,
        "above_ma120": (close > ma120) if ma120 is not None else None,
    }
