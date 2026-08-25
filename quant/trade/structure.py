"""시장 구조 층 — 지지/저항·전고/전저·이동평균·빗각(추세 기울기)·Williams %R.

2026-08-25 소유자 지시의 뼈대다:

> "일봉으로는 전체적인 빗각을 그리며 흐름을 보고, 1분봉으로는 지지선과 전 고점
> 돌파 혹은 전 저점 돌파를 했는지를 보면 좋을것같아. 그래프 + 거래량 + 외국인/
> 기관을 보고 그 뼈대를 기반으로 위에 우리의 전략을 올리는거야."

전략마다 이 계산을 제각각 구현하면 같은 개념이 세 벌 생기고 서로 다르게 낡는다
— 여기 한 벌만 두고 전략들이 **주입 없이 순수 호출**한다. 입력은 완성봉
DataFrame(open/high/low/close/volume, 시간 오름차순)뿐이고 네트워크·상태가 없다.

## 손절 철학 (소유자 지시)

> "손해를 볼 때는 잘못 들어갔다는 걸 감정을 제외한 냉정한 판단으로 손절하고
> 다음 포지션을 기다리는 거야. 잃을 땐 적게 잃고 벌 때는 많이."

임의의 고정 bp 손절은 노이즈 안에 걸린다(2026-08-24 실측: ±100bp 브래킷은
세션 내 67%가 양쪽 다 터치 — 동전던지기). **구조 기반 손절**은 "여기가 깨지면
내 진입 근거 자체가 틀린 것"인 가격 — 지지선(스윙 로우) 바로 아래 — 에 둔다.
근거가 살아 있는 동안의 흔들림은 견디고, 근거가 죽으면 즉시 나간다.

전부 순수 함수 — 거래 평면 규칙(네트워크·DB 금지) 그대로.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# 소유자 지정 이동평균 계단(5/10/20/50/100). 1분봉이면 분 단위, 일봉이면 일 단위 —
# 함수는 봉의 의미를 모르고 개수만 안다(호출부가 interval 을 안다).
MA_PERIODS = (5, 10, 20, 50, 100)


def moving_averages(close: pd.Series, periods: tuple[int, ...] = MA_PERIODS) -> dict[int, float | None]:
    """각 기간의 단순이동평균 마지막 값. **봉이 모자라면 None** — 짧은 표본으로
    계산한 값을 그 기간의 이평인 척 돌려주지 않는다(없는 것을 지어내지 않는다)."""
    out: dict[int, float | None] = {}
    for p in periods:
        out[p] = float(close.rolling(p).mean().iloc[-1]) if len(close) >= p else None
    return out


def ma_alignment(mas: dict[int, float | None]) -> str:
    """이평 배열 판정: "정배열"(단기>장기 순), "역배열", "혼조", "판정불가".

    계산 가능한 이평이 2개 미만이면 "판정불가" — 하나로는 배열이 없다."""
    vals = [(p, v) for p, v in sorted(mas.items()) if v is not None]
    if len(vals) < 2:
        return "판정불가"
    seq = [v for _, v in vals]  # 기간 오름차순(단기→장기)
    if all(a > b for a, b in zip(seq, seq[1:])):
        return "정배열"
    if all(a < b for a, b in zip(seq, seq[1:])):
        return "역배열"
    return "혼조"


def williams_r(bars: pd.DataFrame, period: int = 14) -> float | None:
    """Williams %R (-100~0). 관례: -20 위 = 과매수, -80 아래 = 과매도.

    봉이 period 미만이면 None. 고가==저가(레인지 0)면 None — 0으로 나눠
    극단값을 지어내지 않는다."""
    if len(bars) < period:
        return None
    window = bars.tail(period)
    hi = float(window["high"].max())
    lo = float(window["low"].min())
    if hi <= lo:
        return None
    close = float(bars["close"].iloc[-1])
    return (hi - close) / (hi - lo) * -100.0


def swing_points(bars: pd.DataFrame, wing: int = 3) -> tuple[list[float], list[float]]:
    """스윙 고점/저점 리스트 (전고·전저의 원천), 시간 오름차순.

    스윙 고점 = 양옆 `wing`개 봉보다 높은 고가(저점은 대칭). 마지막 `wing`개
    봉은 오른쪽 날개가 아직 없어 **판정 자체가 불가능하다** — 미래 봉을 기다리지
    않고는 스윙인지 알 수 없으므로 제외한다(look-ahead 의 쌍대: 미확정을 확정으로
    팔지 않는다)."""
    highs: list[float] = []
    lows: list[float] = []
    h = bars["high"].to_numpy()
    lo = bars["low"].to_numpy()
    for i in range(wing, len(bars) - wing):
        if h[i] == max(h[i - wing : i + wing + 1]):
            highs.append(float(h[i]))
        if lo[i] == min(lo[i - wing : i + wing + 1]):
            lows.append(float(lo[i]))
    return highs, lows


def nearest_support(price: float, lows: list[float], mas: dict[int, float | None] | None = None) -> float | None:
    """현재가 **아래** 가장 가까운 지지 후보 — 스윙 저점과 (선택) 이평 중 최댓값.

    후보가 하나도 없으면 None — 지지선이 안 보이는 자리에서 지지가 있는 척하지
    않는다(그 자리 진입이 위험하다는 정보 그 자체다)."""
    candidates = [v for v in lows if v < price]
    if mas:
        candidates += [v for v in mas.values() if v is not None and v < price]
    return max(candidates) if candidates else None


def nearest_resistance(price: float, highs: list[float]) -> float | None:
    """현재가 **위** 가장 가까운 저항(전고) — 부분 익절 1차 목표의 원천."""
    candidates = [v for v in highs if v > price]
    return min(candidates) if candidates else None


def broke_prior_high(bars: pd.DataFrame, wing: int = 3) -> bool:
    """마지막 종가가 직전 스윙 고점(전고)을 넘었는가 — 돌파 판정."""
    highs, _ = swing_points(bars, wing)
    if not highs:
        return False
    return float(bars["close"].iloc[-1]) > highs[-1]


def broke_prior_low(bars: pd.DataFrame, wing: int = 3) -> bool:
    """마지막 종가가 직전 스윙 저점(전저)을 깼는가 — 이탈 판정(청산 근거)."""
    _, lows = swing_points(bars, wing)
    if not lows:
        return False
    return float(bars["close"].iloc[-1]) < lows[-1]


def trend_slope(close: pd.Series, lookback: int = 20) -> float | None:
    """빗각 — 최근 `lookback`봉 종가의 선형회귀 기울기를 **봉당 %**로.

    +0.1 = 봉마다 평균 +0.1% 기울기. 소유자의 "일봉으로 전체적인 빗각" —
    스윙 두 점을 잇는 방식은 어느 두 점이냐에 따라 답이 달라지므로(자의성),
    전 구간을 쓰는 회귀로 잡는다. 봉이 lookback 미만이면 None."""
    if len(close) < lookback:
        return None
    y = close.tail(lookback).to_numpy(dtype=float)
    base = float(y.mean())
    if base <= 0:
        return None
    n = len(y)
    x = list(range(n))
    mx = (n - 1) / 2
    denom = sum((xi - mx) ** 2 for xi in x)
    slope = sum((xi - mx) * (yi - base) for xi, yi in zip(x, y)) / denom
    return slope / base * 100.0


@dataclass(frozen=True)
class StructureBracket:
    """구조 기반 진입 브래킷 — 손절은 지지 아래, 1차 익절은 전고."""

    stop: float
    partial_target: float | None  # 전고 — 없으면(신고가 영역) None
    stop_basis: str  # "swing_low" | "hard_cap" — 뭐가 손절을 정했는지


def structure_bracket(
    entry_price: float,
    bars: pd.DataFrame,
    *,
    wing: int = 3,
    stop_buffer_pct: float = 0.2,
    hard_cap_pct: float = 3.0,
) -> StructureBracket | None:
    """진입가에 대한 구조 브래킷. 지지(스윙 저점)가 안 보이면 **None** —
    "손절선을 정할 수 없는 자리"는 진입하지 말라는 신호이지, 임의의 선을
    그어줄 자리가 아니다.

    - stop: 최근 지지 × (1 − buffer). 단 진입가 대비 `hard_cap_pct` 이상
      벌어지면 hard cap 으로 자른다(3배 아래 지지를 "구조"라고 껴안으면
      한 번의 손절이 계좌를 문다 — 잃을 땐 적게).
    - partial_target: 가장 가까운 전고(없으면 None — 신고가 영역은 목표를
      지어내지 않고 트레일 대상).
    """
    highs, lows = swing_points(bars, wing)
    support = nearest_support(entry_price, lows)
    if support is None:
        return None
    stop = support * (1 - stop_buffer_pct / 100)
    basis = "swing_low"
    floor = entry_price * (1 - hard_cap_pct / 100)
    if stop < floor:
        stop = floor
        basis = "hard_cap"
    if stop >= entry_price:
        return None  # 지지가 진입가 위·동일 — 브래킷이 성립하지 않는다
    return StructureBracket(
        stop=stop,
        partial_target=nearest_resistance(entry_price, highs),
        stop_basis=basis,
    )
