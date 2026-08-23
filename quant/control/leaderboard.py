"""리더보드 승격 규칙 — Phase 7.5. 순수 함수만.

## 무엇을 재는가

"이 생산자가 그날 **어느 종목이 더 오를지 순서**를 맞췄나" — 하루의 횡단면 순위
상관(rank IC)이다. 그리고 표본 수는 **거래일 수**로 센다.

## 왜 판단 수가 아니라 거래일 수인가

선정 원장은 하루에 76~123종목을 남긴다(2026-08-14 실측: 2거래일 199행). 그걸 n=199
로 세면 거의 무엇이든 유의해진다 — 같은 날의 종목들은 **같은 시장 움직임을 공유**하기
때문에 독립 관측이 아니다. 시장이 오른 날에는 아무 생산자나 훌륭해 보인다.

순위 상관을 쓰면 그 공통 성분이 상쇄된다. 그리고 날짜끼리는 훨씬 덜 상관되므로
"거래일 수"가 실효 표본에 가깝다. **표본을 부풀리지 않는 것이 이 모듈의 핵심 기능이다.**

부수 효과로 척도 문제도 사라진다 — LLM 이 0~10 을, 스코어러가 0~100 을 내도 순위는
비교 가능하다.

## 문턱의 근거 (감으로 정하지 않았다)

주식 횡단면의 일별 IC 표준편차는 대략 0.10~0.15다. IC 평균 0.05(약한 엣지)를 t=2 로
잡아내려면:

    t = mean / (sd / sqrt(n))  →  2 = 0.05 / (0.12 / sqrt(n))  →  n ≈ 23

그래서 최소 20거래일이다. 이 값은 "얼마나 기다려야 말할 자격이 생기나"이고,
그 전에는 IC 가 아무리 좋아도 `insufficient_sample` 이다.

## 다중검정

프롬프트·모델을 K개 시험하면 그중 최고는 **반드시** 좋아 보인다. `n_trials` 를 받아
요구 t 를 올린다(Bonferroni). **시행 횟수를 세지 않는 리더보드는 우연을 실력으로
승격한다** — 계획서가 Phase 8 을 마지막에 둔 것과 같은 이유다.

## 이 모듈이 하지 않는 것

승격을 **자동 적용하지 않는다.** 판정과 숫자를 돌려줄 뿐이고, 반영은 사람이 한다
(Phase 8.3 "자동 머지 금지"와 같은 원칙). 그리고 IC 는 비용을 모른다 — 순위를 맞춘
생산자가 실제로 돈이 되는지는 체결·수수료를 통과한 뒤에야 안다.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

# 근거는 위 docstring 의 검정력 계산. 20거래일 = "말할 자격"의 하한이다.
MIN_DAYS = 20

# 하루에 순위가 의미를 갖는 최소 종목 수. 2종목의 순위 상관은 항상 ±1 이라 정보가 없다.
MIN_SYMBOLS_PER_DAY = 3


@dataclass(frozen=True)
class Verdict:
    verdict: str          # "insufficient_sample" | "no_edge" | "promote"
    promote: bool
    mean_ic: float | None
    t_stat: float
    required_t: float
    n_days: int
    n_days_dropped: int
    n_trials: int
    reason: str


def _ranks(values: list[float]) -> list[float]:
    """평균 순위(동점은 평균). Spearman 을 Pearson-on-ranks 로 계산하기 위한 것."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        # 한쪽에 변동이 없다 = 순위를 만들지 않았다. 0(중립)과 다르다.
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def daily_rank_ic(scores: dict[str, float | None],
                  returns: dict[str, float | None]) -> float | None:
    """하루의 횡단면 순위 상관. 겹치는 종목이 부족하거나 순위가 없으면 `None`.

    **수익률을 못 구한 종목은 버린다 — 0으로 넣지 않는다.** 0으로 넣으면 조회 실패가
    "본전"으로 순위에 끼어 IC 를 오염시킨다.
    """
    pairs = [
        (float(scores[s]), float(returns[s]))
        for s in scores
        if scores.get(s) is not None and returns.get(s) is not None
    ]
    if len(pairs) < MIN_SYMBOLS_PER_DAY:
        return None
    xs = _ranks([p[0] for p in pairs])
    ys = _ranks([p[1] for p in pairs])
    return _pearson(xs, ys)


def required_t(n_trials: int) -> float:
    """다중검정 보정된 요구 t. K개를 시험했으면 최고값의 문턱이 올라간다.

    Bonferroni 를 정규 근사로 쓴다 — 양측 alpha=0.05 를 K로 나눈 분위수.
    정확한 t분포 분위수가 아니라 **보수적인 근사**이고, 그 방향(더 엄격)이 맞다.
    """
    k = max(int(n_trials), 1)
    # 표준정규 양측 분위수의 근사: z ≈ sqrt(2*ln(2K/alpha)) 계열 대신, K=1 에서 1.96 이
    # 되도록 맞춘 단순 근사를 쓴다. 정밀도보다 "K가 크면 문턱이 오른다"가 중요하다.
    return 1.96 + 0.62 * math.log(k)


def promotion_verdict(ic_by_day: dict[str, float | None], n_trials: int,
                      min_days: int = MIN_DAYS) -> Verdict:
    """일별 IC 로 승격 판정. **자동 적용하지 않는다** — 판정과 숫자만 돌려준다.

    세 갈래다:
      `insufficient_sample` — 거래일이 문턱 미만. IC 가 완벽해도 여기서 멈춘다.
      `no_edge`             — 표본은 됐는데 t 가 요구치 미달(또는 부호가 반대).
      `promote`             — 표본·유의성·부호 셋 다 통과.
    """
    usable = {d: v for d, v in ic_by_day.items() if v is not None}
    dropped = len(ic_by_day) - len(usable)
    n = len(usable)
    req = required_t(n_trials)

    if n < min_days:
        return Verdict(
            verdict="insufficient_sample", promote=False,
            mean_ic=(sum(usable.values()) / n if n else None),
            t_stat=float("nan"), required_t=req, n_days=n, n_days_dropped=dropped,
            n_trials=n_trials,
            reason=(f"거래일 {n}일 — 최소 {min_days}일 필요. IC 가 좋아 보여도 "
                    "이 표본으로는 실력과 우연을 구분할 수 없다"),
        )

    values = list(usable.values())
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)

    if sd <= 0:
        # 매일 IC 가 완전히 똑같다 = **분산을 추정할 수 없다.** t 검정은 표준오차를
        # 요구하므로 여기서 무한 확신을 주장하면 안 된다 — 그러면 다중검정 보정이
        # 통째로 무력화된다(구현 중 실제로 그랬다: t=inf 가 요구 t 를 항상 넘겼다).
        # 실데이터에서는 사실상 오지 않는 분기이고, 오면 데이터가 합성이거나 망가진 것이다.
        return Verdict(
            verdict="insufficient_sample", promote=False, mean_ic=mean,
            t_stat=float("nan"), required_t=req, n_days=n, n_days_dropped=dropped,
            n_trials=n_trials,
            reason=(f"거래일 {n}일의 IC 가 전부 동일({mean:+.4f}) — 분산을 추정할 수 "
                    "없어 유의성을 계산할 수 없다(데이터가 합성이거나 망가졌다)"),
        )

    t = mean / (sd / math.sqrt(n))

    if mean <= 0 or t < req:
        return Verdict(
            verdict="no_edge", promote=False, mean_ic=mean, t_stat=t, required_t=req,
            n_days=n, n_days_dropped=dropped, n_trials=n_trials,
            reason=(f"평균 IC {mean:+.4f}, t {t:.2f} < 요구 {req:.2f} "
                    f"(시행 {n_trials}회 보정) — 승격 근거 없음"),
        )

    return Verdict(
        verdict="promote", promote=True, mean_ic=mean, t_stat=t, required_t=req,
        n_days=n, n_days_dropped=dropped, n_trials=n_trials,
        reason=(f"평균 IC {mean:+.4f}, t {t:.2f} >= 요구 {req:.2f} "
                f"(거래일 {n}일, 시행 {n_trials}회 보정). "
                "비용·체결을 통과하는지는 별도 확인이 필요하다"),
    )


def verdicts_from_ledger(
    sel_rows: list[dict], judgments: list[dict],
    horizon_days: int, n_trials: int,
) -> dict[tuple[str, str], tuple[Verdict, dict[str, float | None]]]:
    """생산자별 승격 판정 + 일별 IC — `cmd_close_report`/`cmd_leaderboard`(둘 다
    `quant/apps/cli.py`)가 각자 들고 있던 동일한 20줄(원장에서 생산자별 일별
    rank IC 를 구해 `promotion_verdict` 를 돌리는 부분)을 추출한 것이다.

    반환은 `(Verdict, ic_by_day)` 튜플이다 — close-report 는 `Verdict.reason` 한
    줄만 쓰고, leaderboard 는 사람이 검산할 `ic_by_day` 도 그대로 출력한다(두
    호출부의 실제 필요가 달라서 둘 다 돌려준다).
    """
    key = f"outcome_d{horizon_days}_bps"
    returns_by_day: dict[str, dict[str, float]] = defaultdict(dict)
    for r in sel_rows:
        if r.get(key) is not None:
            returns_by_day[str(r.get("date"))][str(r.get("symbol"))] = float(r[key])

    scores: dict[tuple, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for j in judgments:
        if j.get("score") is None:
            continue
        who = (str(j.get("producer")), str(j.get("producer_version")))
        scores[who][str(j.get("session_date"))][str(j.get("symbol"))] = float(j["score"])

    out: dict[tuple[str, str], tuple[Verdict, dict[str, float | None]]] = {}
    for who in sorted(scores):
        ic_by_day = {
            day: daily_rank_ic(scores[who][day], returns_by_day.get(day, {}))
            for day in sorted(scores[who])
        }
        v = promotion_verdict(ic_by_day, n_trials=max(len(scores), n_trials))
        out[who] = (v, ic_by_day)
    return out
