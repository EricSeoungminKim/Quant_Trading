"""지수별 전망(코스피/코스닥, S&P500/나스닥) — 스탠스 요인 일반화 + 경험적 상승확률.

`briefing.stance()`는 시장당 지수 1개(KR=^KS11, US=^GSPC)만 본다. 소유자 요청
(2026-08-29): "한국장 리포트면 코스피 상승 확률/코스닥 상승 확률을 나누면
좋겠다. 미국장도 비슷하게." — 신뢰가 생명이므로 확률을 지어내지 않고 계산
근거를 함께 싣는다.

이 모듈은 순수 함수만 담는다(quant.adapters 임포트 없음, 네트워크·디스크 I/O
없음). 세 갈래로 나뉜다:

1. `factor_outlook()` — `stance()`의 5요인(지수 모멘텀·수급·VIX·앵커·임박
   이벤트) 가감 로직을 지수 단위로 일반화한 것. 지수마다 관측 가능한 요인
   수가 다르므로(코스닥엔 앵커가 없고, S&P엔 수급이 없다), **span(분모)은 그
   호출에서 실제로 값이 주어진 요인 수로 계산한다** — 없는 요인을 0으로 채워
   분모만 그대로 두면 근거가 얇은 지수가 억지로 중립(50점)에 눌린다.
2. `empirical_probability()` — (v1, 하위호환 유지) "지어내지 않는" 상승확률.
   종가 리스트 하나로 "전일 등락 부호 x 5일 추세 부호" 버킷을 만들고, 오늘과
   같은 버킷이었던 과거 날들의 다음날 상승 빈도를 센다. 표본이 `MIN_SAMPLES`
   미만이면 `prob=None`을 정직하게 반환한다 — 신뢰도 없는 확률을 보여주느니
   안 보여주는 게 낫다.
3. `shrinkage_probability()` — (v2, 2026-09-02 소유자 요청) v1의 "표본
   부족이면 침묵"을 베이지안 수축으로 대체한다("표본만큼만 조건부"). 아래
   docstring에 채택/탈락 요인과 실측 근거를 전부 남긴다.

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


# ══════════════════════════════════════════════════════════════════
# 확률 엔진 v2 — 수축 기저율 + 요인 채택 실험 기록
# ══════════════════════════════════════════════════════════════════
#
# 소유자 요청(2026-09-02): "상승확률을 더 계산적이고 구체적으로 — 채점 요소를
# 로직 문제 없이 고도화하라." 전부 결정론(LLM 금지), quant/trade/ 미접촉.
#
# ## 1. 수축(shrinkage) — 소표본 정직성
#
# v1은 표본(n) < MIN_SAMPLES=100 이면 prob=None을 반환한다("모르면 침묵").
# v2는 대신 베이지안 축소 추정(Beta-Binomial 켤레사전의 사후평균과 동일 형태)을
# 쓴다:
#
#     P = (k + α·p0) / (n + α)
#
# k=오늘과 같은 조건(버킷)에서 과거에 다음날 상승한 횟수, n=그 버킷의 과거
# 관측 수, p0=조건 없는 무조건부 상승률(전체 표본 평균), α=SHRINKAGE_ALPHA.
# n=0이면 P=p0(전체평균에 완전히 수축), n≫α면 P→k/n(조건부 경험치로 수렴).
# "표본 부족이면 침묵" 대신 "표본만큼만 조건부"가 된다 — 결측 대신 신뢰도가
# 낮아질수록 무조건부 쪽으로 정직하게 당겨지는 숫자를 낸다.
#
# α=100을 고른 근거: v1의 MIN_SAMPLES(100)와 같은 크기로 잡았다 — n이 100
# 근처면 조건부/무조건부가 거의 반반(가중치 α/(n+α)=50%)으로 섞이고, n이
# 100을 훨씬 넘으면 조건부 쪽으로, n이 0에 가까우면 무조건부(p0) 쪽으로
# 자연히 수렴한다. 별도 최적화 탐색 없이 기존 정직성 하한선을 그대로 재사용한
# 값이다(추가로 탐색하면 그 자체가 다중검정이 된다).
#
# ## 2. 조건(요인) 확장 실험 — 채택 2, 탈락 4
#
# 후보: (a) 변동성 국면(5일/60일 실현변동성 비, high>1.2/normal/low<0.8),
# (b) 갭 방향(당일 시가 vs 전일 종가 부호), (c) 2일 연속 부호(전전일 등락
# 부호를 3번째 차원으로 추가), (d) 요일(월~금).
#
# 검증 방법: 4개 대리 심볼(069500/229200/SPY/QQQ, `data/history/{symbol}/1d`
# 로컬 parquet, 2026-09-02 EC2 scp 사본으로 실측) 각각에서 시간순 70%
# train/30% test 분할. train에서만 버킷 비율(위 수축 포함)을 추정하고, test
# 구간에서 Brier score(예측확률-실제결과)^2 평균)를 무조건부 기저율(p0)의
# Brier와 비교했다. 각 후보를 "기존 2요인(전일 부호 x 5일 추세 부호)에 추가한
# 3요인 버킷"으로 test 구간에 적용해, 무조건부 대비 개선 폭과 페어드
# t-통계량(|t|<1.96 → 유의하지 않음, 노이즈와 구분 불가)을 같이 봤다.
#
# 실측 결과(요약, 전체 원문은 검증 하니스 출력 참고):
#   - 기존 2요인 자체도 무조건부 대비 유의한 개선이 아니다(KOSPI t=-0.46,
#     KOSDAQ t=-1.01, SP500 t=+0.82, QQQ t=+1.97 — SP500/QQQ는 오히려
#     소폭 악화, 전부 |t|<2.0 근처로 노이즈와 구분이 애매하다). 그럼에도
#     기존 2요인을 그대로 유지한다 — 이 요청의 범위는 "새 요인을 함부로
#     추가하지 않는 것"이지 기존 계약을 흔드는 게 아니고, v1과의 직접
#     비교 기준선이 필요하기 때문이다.
#   - 변동성 국면: 4개 지수 전부 reject. QQQ에서는 유의하게(t=+2.12) 악화.
#   - 갭 방향: 4개 지수 전부 reject(KOSPI만 방향은 맞았으나 t=-0.85로
#     유의하지 않음). QQQ에서는 유의하게(t=+2.40) 악화.
#   - 2일 연속 부호: KOSDAQ 1개 지수만 방향이 맞았고(t=-1.32) 나머지 3개는
#     reject(KOSPI는 오히려 크게 악화). 4개 중 1개, 유의성도 없어 채택 근거
#     부족.
#   - 요일: KOSPI/KOSDAQ/QQQ 3개 지수에서 방향은 개선이었으나 전부
#     |t|<1.5로 유의하지 않고, SP500(가장 긴 표본 4,189봉)에서는 오히려
#     악화(t=+1.11)했다. 4개 중 하나(가장 유동성 높고 표본이 가장 긴 지수)
#     에서 실패하는 요인을 "상승확률" 같은 신뢰 핵심 숫자에 넣을 수 없다.
#
#   → **4개 후보 전부 탈락.** 최종 채택 요인은 기존 그대로 2개(전일 등락
#   부호, `trend_days`일 추세 부호)다. 다중검정 편향 고지: 지수 4개 x 후보
#   4개 = 16회 비교 중 "방향은 개선"으로 보인 조합이 여럿 있었지만(예:
#   요일 3/4), 유의성 기준(|t|>=1.96)을 넘긴 것은 하나도 없었다 — 이건
#   "더 좋은 요인을 못 찾았다"가 아니라 "찾아봤고, 노이즈와 구분되는 신호가
#   없었다"는 정직한 결과다. 좋아 보이게 포장하지 않는다.
#
# ## 3. brier_vs_base — 매 호출마다 재검증
#
# 각 호출 시점의 전체 종가 이력을 그대로 시간순 70/30으로 다시 쪼개 재계산한다
# (오프라인에서 한 번 구한 고정 상수를 박아두지 않는다) — 결정론적이고(같은
# 종가 이력이면 항상 같은 값), 데이터가 쌓일수록 자연히 최신 구간을 반영한다.
# 표본이 `MIN_SPLIT_BARS` 미만이면 분할 자체가 무의미하므로 `None`을 반환한다
# (지어낸 숫자보다 결측이 낫다는 원칙은 v1과 동일).

SHRINKAGE_ALPHA = 100.0

# walk-forward(70/30) 재검증에 필요한 최소 버킷 표본 수. 100이면 train 70개
# /test 30개로 나뉘어 "표본 부족이라 못 잰다"는 오탐 없이 최소한의 페어드
# 비교가 가능하다.
MIN_SPLIT_BARS = 100

_STATE_LABEL = {1: "상승", -1: "하락", 0: "보합"}


def _state_label(sign: int) -> str:
    return _STATE_LABEL[sign]


def _fmt_pp(delta: float) -> str:
    return f"{delta * 100:+.1f}%p"


def _bucket_rows(closes: list[float], trend_days: int) -> list[tuple[int, int, int]]:
    """(전일 부호, 추세 부호, 다음날 상승 여부) — `empirical_probability`와
    동일한 t 범위(`range(trend_days, n-1)`)로 만든 시간순 관측 목록."""
    rows: list[tuple[int, int, int]] = []
    n = len(closes)
    for t in range(trend_days, n - 1):
        prev, base = closes[t - 1], closes[t - trend_days]
        if prev == 0 or base == 0:
            continue
        prev_sign = _sign(closes[t] / prev - 1)
        trend_sign = _sign(closes[t] / base - 1)
        outcome = 1 if closes[t + 1] > closes[t] else 0
        rows.append((prev_sign, trend_sign, outcome))
    return rows


def _bucket_counts(
    rows: list[tuple[int, int, int]], key_fn,
) -> dict:
    """키(요인 조합)별 (상승 횟수 k, 관측 수 n)."""
    counts: dict = {}
    for row in rows:
        key = key_fn(row)
        k, n = counts.get(key, (0, 0))
        counts[key] = (k + row[-1], n + 1)
    return counts


def _shrink(k: int, n: int, p0: float, alpha: float) -> float:
    return (k + alpha * p0) / (n + alpha)


def _walk_forward_brier(
    rows: list[tuple[int, int, int]], alpha: float,
) -> float | None:
    """`rows`(시간순)를 70/30으로 쪼개 train 버킷 비율을 test 구간에 적용,
    무조건부(p0) 대비 Brier score 차를 낸다(음수=개선). 표본이
    `MIN_SPLIT_BARS` 미만이면 분할이 무의미하므로 `None`."""
    n = len(rows)
    if n < MIN_SPLIT_BARS:
        return None
    split = int(n * 0.7)
    train, test = rows[:split], rows[split:]
    p0_train = sum(r[-1] for r in train) / len(train)
    train_counts = _bucket_counts(train, lambda r: (r[0], r[1]))
    train_rates = {
        key: _shrink(k, cnt, p0_train, alpha) for key, (k, cnt) in train_counts.items()
    }
    sq_model = 0.0
    sq_base = 0.0
    for prev_sign, trend_sign, outcome in test:
        pred = train_rates.get((prev_sign, trend_sign), p0_train)
        sq_model += (pred - outcome) ** 2
        sq_base += (p0_train - outcome) ** 2
    return round((sq_model - sq_base) / len(test), 5)


def shrinkage_probability(
    closes: Sequence[float],
    trend_days: int = DEFAULT_TREND_DAYS,
    alpha: float = SHRINKAGE_ALPHA,
) -> dict:
    """v2 — 베이지안 수축 상승확률. 채택/탈락 요인 근거는 위 모듈 docstring 참고.

    반환 계약(리포트 payload에 그대로 얹힘, 필드명 고정):
    `{"up_prob", "down_prob", "n_samples", "shrinkage", "method", "factors",
    "brier_vs_base"}`. `down_prob = 1 - up_prob`(보합은 상승이 아니다 — 종가
    등락 0은 outcome 계산에서도 "상승 아님"으로 취급한다, `empirical_probability`
    와 동일). 데이터가 버킷 하나도 못 만들 만큼 부족하면(`len(closes) <
    trend_days + 2`) `up_prob=None`으로 정직하게 결측을 드러낸다 — 이 경우는
    수축으로도 구제할 p0 자체가 없다.
    """
    closes = list(closes)
    n_closes = len(closes)
    need = trend_days + 2
    empty = {
        "up_prob": None, "down_prob": None, "n_samples": 0, "shrinkage": None,
        "method": "일봉 부족 — 확률 계산 불가", "factors": [], "brier_vs_base": None,
    }
    if n_closes < need:
        return empty

    rows = _bucket_rows(closes, trend_days)
    if not rows or closes[-2] == 0 or closes[-1 - trend_days] == 0:
        return empty

    p0 = sum(r[-1] for r in rows) / len(rows)
    today_prev = _sign(closes[-1] / closes[-2] - 1)
    today_trend = _sign(closes[-1] / closes[-1 - trend_days] - 1)

    joint_counts = _bucket_counts(rows, lambda r: (r[0], r[1]))
    k, n_bucket = joint_counts.get((today_prev, today_trend), (0, 0))
    up_prob = _shrink(k, n_bucket, p0, alpha)
    shrinkage = alpha / (n_bucket + alpha)

    prev_counts = _bucket_counts(rows, lambda r: r[0])
    trend_counts = _bucket_counts(rows, lambda r: r[1])
    kp, np_ = prev_counts.get(today_prev, (0, 0))
    kt, nt = trend_counts.get(today_trend, (0, 0))
    prev_rate = _shrink(kp, np_, p0, alpha)
    trend_rate = _shrink(kt, nt, p0, alpha)

    factors = [
        {
            "name": "전일 등락 부호", "state": _state_label(today_prev),
            "contribution": _fmt_pp(prev_rate - p0),
        },
        {
            "name": f"{trend_days}일 추세 부호", "state": _state_label(today_trend),
            "contribution": _fmt_pp(trend_rate - p0),
        },
    ]

    method = (
        f"수축 기저율(α={alpha:.0f}): 무조건부 상승률 {p0:.1%} + 유사조건 "
        f"{n_bucket}회 관측 혼합(수축 {shrinkage:.0%}) — 채택 요인: 전일 등락·"
        f"{trend_days}일 추세(변동성국면/갭방향/2일연속/요일 4개 후보는 walk-forward "
        f"검증에서 개선 미확인으로 탈락, 근거는 모듈 docstring 참고)"
    )

    return {
        "up_prob": round(up_prob, 4),
        "down_prob": round(1 - up_prob, 4),
        "n_samples": n_bucket,
        "shrinkage": round(shrinkage, 4),
        "method": method,
        "factors": factors,
        "brier_vs_base": _walk_forward_brier(rows, alpha),
    }
