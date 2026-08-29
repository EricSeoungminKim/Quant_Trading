"""지수별 전망(코스피/코스닥, S&P500/나스닥) — 스탠스 요인 일반화 + 경험적 상승확률.

`briefing.stance()`는 시장당 지수 1개(KR=^KS11, US=^GSPC)만 본다. 소유자 요청
(2026-08-29): "한국장 리포트면 코스피 상승 확률/코스닥 상승 확률을 나누면
좋겠다. 미국장도 비슷하게." — 신뢰가 생명이므로 확률을 지어내지 않고 계산
근거를 함께 싣는다.

이 모듈은 순수 함수만 담는다(quant.adapters 임포트 없음, 네트워크·디스크 I/O
없음). 두 갈래로 나뉜다:

1. `factor_outlook()` — `stance()`의 5요인(지수 모멘텀·수급·VIX·앵커·임박
   이벤트) 가감 로직을 지수 단위로 일반화한 것. 지수마다 관측 가능한 요인
   수가 다르므로(코스닥엔 앵커가 없고, S&P엔 수급이 없다), **span(분모)은 그
   호출에서 실제로 값이 주어진 요인 수로 계산한다** — 없는 요인을 0으로 채워
   분모만 그대로 두면 근거가 얇은 지수가 억지로 중립(50점)에 눌린다.
2. `empirical_probability()` — "지어내지 않는" 상승확률. 종가 리스트 하나로
   "전일 등락 부호 x 5일 추세 부호" 버킷을 만들고, 오늘과 같은 버킷이었던
   과거 날들의 다음날 상승 빈도를 센다. 표본이 `MIN_SAMPLES` 미만이면
   `prob=None`을 정직하게 반환한다 — 신뢰도 없는 확률을 보여주느니 안
   보여주는 게 낫다.

호출부(`quant.report.collect.index_outlook`)가 스냅샷/로컬 parquet에서 값을
뽑아 이 함수들에 주입한다 — 이 모듈은 그 값들을 계산만 한다.
"""
from __future__ import annotations

from typing import Sequence

from quant.analyze.scoring import label_100, to_100

# 버킷 하나에 이만큼 과거 관측치가 쌓이지 않으면 확률을 내지 않는다 — 표본
# 부족을 얼버무려 "그럴듯한 숫자"로 포장하지 않기 위한 정직성 하한선.
MIN_SAMPLES = 100

# 5거래일 추세(대략 1주일)로 "요즘 흐름"을 판정한다 — briefing.py의 다른
# 임계(예: FLOW_BIG)처럼 특정 실측에서 역산한 값이 아니라, 짧은 모멘텀(1일)과
# 구분되는 가장 단순한 중기 창이라 골랐다.
DEFAULT_TREND_DAYS = 5


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def factor_outlook(
    *,
    index_label: str,
    index_change_pct: float | None,
    index_strong_pct: float = 1.0,
    flow_row: dict | None = None,
    flow_actors: Sequence[str] = ("외국인", "기관계"),
    flow_big: float = 3000.0,
    vix: float | None = None,
    vix_calm: float = 15.0,
    vix_stress: float = 22.0,
    anchor_avg_pct: float | None = None,
    anchor_label: str | None = None,
    anchor_strong_pct: float = 3.0,
    imminent_event_text: str | None = None,
) -> dict:
    """지수 하나의 가감점 전망. 값이 `None`인 요인은 계산에서도 span에서도 빠진다.

    `flow_row`가 주어지면 `flow_actors`에 열거된 주체 각각을 독립적으로
    채점한다(`stance()`의 외국인/기관계 루프와 동일 — 두 주체가 동시에 큰 값을
    내면 최대 ±len(flow_actors)까지 움직인다). `imminent_event_text`는 방향과
    무관하게 -1(관망 압력)만 준다 — `stance()`와 동일하게 span 계산에는
    넣지 않는다(비대칭 요인이라 "이론상 최대 가점"에 기여하지 않는다).
    """
    plus: list[str] = []
    minus: list[str] = []
    score = 0
    span = 0

    if index_change_pct is not None:
        span += 1
        if index_change_pct >= index_strong_pct:
            score += 1
            plus.append(f"{index_label} {index_change_pct:+.2f}%")
        elif index_change_pct <= -index_strong_pct:
            score -= 1
            minus.append(f"{index_label} {index_change_pct:+.2f}%")

    if flow_row is not None:
        span += len(flow_actors)
        for actor in flow_actors:
            v = flow_row.get(actor)
            if v is None or abs(v) < flow_big:
                continue
            if v > 0:
                score += 1
                plus.append(f"{actor} {v:+,}억 순매수")
            else:
                score -= 1
                minus.append(f"{actor} {v:+,}억 순매도")

    if vix is not None:
        span += 1
        if vix <= vix_calm:
            score += 1
            plus.append(f"VIX {vix:.1f}")
        elif vix >= vix_stress:
            score -= 1
            minus.append(f"VIX {vix:.1f}")

    if anchor_avg_pct is not None:
        span += 1
        label = anchor_label or "앵커"
        if anchor_avg_pct >= anchor_strong_pct:
            score += 1
            plus.append(f"{label} 평균 {anchor_avg_pct:+.1f}%")
        elif anchor_avg_pct <= -anchor_strong_pct:
            score -= 1
            minus.append(f"{label} 평균 {anchor_avg_pct:+.1f}%")

    if imminent_event_text:
        score -= 1
        minus.append(imminent_event_text)

    if span <= 0:
        score100 = None
        label = None
    else:
        score100 = to_100(score, span)
        label = label_100(score100, "상승 신호", "하락 신호")

    return {
        "score": score,
        "span": span,
        "score100": score100,
        "label": label,
        "positives": plus,
        "negatives": minus,
    }


def empirical_probability(
    closes: Sequence[float], trend_days: int = DEFAULT_TREND_DAYS,
) -> dict:
    """"전일 등락 부호 x N일 추세 부호" 버킷의 경험적 다음날 상승 확률.

    `closes`는 과거→최근 오름차순 종가. 각 과거일 `t`(인덱스 `trend_days`부터
    `len(closes)-2`까지)에 대해 버킷 키 `(sign(close[t]/close[t-1]-1),
    sign(close[t]/close[t-trend_days]-1))`를 만들고, `close[t+1] > close[t]`면
    그날을 "다음날 상승"으로 센다. 오늘(마지막 종가)과 같은 버킷의 과거
    관측치 중 상승 비율이 확률이다 — 오늘 자신의 다음날(아직 모르는 값)은
    당연히 표본에 없다(lookahead 없음).

    표본(`n`)이 `MIN_SAMPLES` 미만이면 `prob=None`을 반환한다("표본 부족").
    데이터 자체가 버킷 하나 만들기에도 부족하면(`len(closes) < trend_days+2`)
    마찬가지로 `prob=None`("일봉 부족")이다. 둘 다 지어낸 확률보다 정직한
    결측이 낫다는 원칙(HONESTY CONSTRAINT, docs/data-availability.md)을 따른다.
    """
    closes = list(closes)
    n = len(closes)
    need = trend_days + 2  # t-trend_days..t(추세) + t+1(다음날 결과)
    if n < need:
        return {"prob": None, "n": 0, "method": None, "reason": "일봉 부족"}

    buckets: dict[tuple[int, int], list[int]] = {}
    for t in range(trend_days, n - 1):
        prev = closes[t - 1]
        base = closes[t - trend_days]
        if prev == 0 or base == 0:
            continue
        prev_sign = _sign(closes[t] / prev - 1)
        trend_sign = _sign(closes[t] / base - 1)
        outcome = 1 if closes[t + 1] > closes[t] else 0
        buckets.setdefault((prev_sign, trend_sign), []).append(outcome)

    if closes[-2] == 0 or closes[-1 - trend_days] == 0:
        return {"prob": None, "n": 0, "method": None, "reason": "일봉 부족"}
    today_key = (
        _sign(closes[-1] / closes[-2] - 1),
        _sign(closes[-1] / closes[-1 - trend_days] - 1),
    )
    outcomes = buckets.get(today_key, [])
    sample_n = len(outcomes)
    if sample_n < MIN_SAMPLES:
        return {"prob": None, "n": sample_n, "method": None, "reason": "표본 부족"}

    up = sum(outcomes)
    prob = up / sample_n
    years = n / 252
    method = f"최근 {years:.1f}년 유사 조건일 {sample_n}회 중 상승 {up}회"
    return {"prob": round(prob, 4), "n": sample_n, "method": method, "reason": None}
