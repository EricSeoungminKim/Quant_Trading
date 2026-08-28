"""다중검정 보정 통계 — "샤프가 좋다"를 "우연이 아니다"로 바꾸는 층 (2026-08-28).

## 왜 이 파일이 생겼나

소유자 원칙: "전략만 자유롭게 바꿔끼면서 매일 모의투자 수익성을 확인하고,
수익성이 좋으면 자연스럽게 그 전략을 쓴다." 그 루프에는 조용한 함정이 하나 있다 —
**규칙 변형을 계속 시험하면 그중 최고는 반드시 좋아 보인다.** 엣지가 0이어도
그렇다. 이건 위험이 아니라 산수다.

`fitness.py`는 표본 부족을 `sufficient` 플래그로 막고, `walkforward.py`는 구간별
안정성을 본다. 둘 다 **탐색 횟수를 모른다.** 같은 90일 창에서 규칙 20개를
시험했든 1개만 봤든 결과 표는 똑같이 생겼다. 그 차이를 숫자로 만드는 게 여기다.

## 무엇을 하지 않는가

이 모듈은 **성과를 만들지 않는다. 성과 주장을 깎는다.** 여기 있는 어떤 함수도
전략을 채택하지 않고, 임계값도 정하지 않는다 — 숫자를 내고 판단은 사람이 한다
(`forensics.py`·`ledger.py`와 같은 계약).

## 단위 규약 (틀리면 결과가 통째로 거짓말이 된다)

**모든 함수의 `sharpe`는 관측 1건당(per-observation) 값이고, `n_obs`는 그
관측의 개수다.** 우리 엔진(`engine._compute_metrics`)이 내는 샤프는 **연율화**돼
있으므로 그대로 넣으면 안 된다 — `to_per_observation()`으로 되돌린 뒤 넣는다.
연율 샤프 1.5를 15분봉 n=9,000에 그대로 넣으면 PSR이 1.0으로 나온다(무조건
유의). 이건 이 파일에서 가장 쉬운 자멸 경로다.

## 출처

- Bailey, D. & López de Prado, M. (2012), "The Sharpe Ratio Efficient Frontier",
  Journal of Risk — PSR, MinTRL.
- Bailey, D., Borwein, J., López de Prado, M. & Zhu, Q. (2014),
  "Pseudo-Mathematics and Financial Charlatanism: The Effects of Backtest
  Overfitting on Out-of-Sample Performance", Notices of the AMS — E[max SR].
- Bailey, D. & López de Prado, M. (2014), "The Deflated Sharpe Ratio: Correcting
  for Selection Bias, Backtest Overfitting, and Non-Normality",
  Journal of Portfolio Management — DSR.

scipy를 쓰지 않는다(직접 의존성이 아니다 — `ledger._wilson_ci`가 같은 이유로
stdlib로 구현돼 있다). 정규분포는 `statistics.NormalDist`로 충분하다.
"""
from __future__ import annotations

import math
from statistics import NormalDist

__all__ = [
    "EULER_MASCHERONI",
    "deflated_sharpe",
    "expected_max_sharpe",
    "min_track_record_length",
    "probabilistic_sharpe",
    "to_per_observation",
]

EULER_MASCHERONI = 0.5772156649015329

_N = NormalDist()


def to_per_observation(sharpe_annualized: float, periods_per_year: float) -> float:
    """연율 샤프 → 관측 1건당 샤프. 이 파일의 다른 함수에 넣기 전에 반드시 거친다.

    한 줄짜리 나눗셈을 함수로 둔 이유는 **빠뜨리면 조용히 틀리기 때문**이다.
    빠뜨린 결과는 예외가 아니라 "PSR 1.00"이라는 그럴듯한 숫자다.
    """
    if periods_per_year <= 0:
        raise ValueError(f"periods_per_year는 양수여야 한다: {periods_per_year!r}")
    return sharpe_annualized / math.sqrt(periods_per_year)


def _variance_term(sharpe: float, skew: float, kurtosis: float) -> float:
    """샤프 추정량의 분산 보정항 `1 - γ3·SR + (γ4-1)/4·SR²`.

    `kurtosis`는 **초과첨도가 아니라 첨도**다(정규분포=3). 트레이딩 수익률은
    보통 왼쪽으로 치우치고(γ3<0) 꼬리가 두꺼워(γ4>3) 이 항이 커진다 — 즉
    비정규성은 샤프를 **덜 믿게** 만든다. 정규 가정(0, 3)은 낙관적인 쪽이다.
    """
    term = 1.0 - skew * sharpe + (kurtosis - 1.0) / 4.0 * sharpe * sharpe
    if term <= 0:
        raise ValueError(
            f"샤프 추정 분산항이 0 이하다 (skew={skew}, kurtosis={kurtosis}, "
            f"sharpe={sharpe}) — 이 왜도/첨도 조합에서는 근사가 성립하지 않는다. "
            "입력 모멘트를 확인한다(kurtosis는 초과첨도가 아니라 첨도, 정규=3)."
        )
    return term


def probabilistic_sharpe(
    sharpe: float, benchmark: float, n_obs: int, skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """PSR — "관측된 샤프가 `benchmark`보다 크다"가 참일 확률.

    Bailey & López de Prado (2012):

        PSR(SR*) = Φ( (SR - SR*)·√(n-1) / √(1 - γ3·SR + (γ4-1)/4·SR²) )

    표본이 적거나 꼬리가 두꺼우면 같은 샤프라도 확률이 내려간다 — 그게 이
    지표의 전부다. `benchmark=0`이면 "엣지가 있는가", `benchmark=E[max SR]`이면
    "탐색을 감안해도 엣지가 있는가"(= `deflated_sharpe`)를 묻는 것이다.

    반환은 0~1 확률. `n_obs < 2`면 분모의 √(n-1)이 0이 되므로 거부한다 —
    표본 1건에 확률을 매기지 않는다.
    """
    if n_obs < 2:
        raise ValueError(f"n_obs는 2 이상이어야 한다(√(n-1) 사용): {n_obs!r}")
    z = (sharpe - benchmark) * math.sqrt(n_obs - 1) / math.sqrt(
        _variance_term(sharpe, skew, kurtosis)
    )
    return _N.cdf(z)


def expected_max_sharpe(
    n_trials: int, n_obs: int, trial_variance: float | None = None,
) -> float:
    """**무가치한 전략 N개를 시험하면 순전히 우연으로 이만큼 나온다**는 기준선.

    Bailey, Borwein, López de Prado & Zhu (2014):

        E[max SR] ≈ √V · [ (1-γ)·Φ⁻¹(1 - 1/N) + γ·Φ⁻¹(1 - 1/(N·e)) ]

    γ는 오일러-마스케로니 상수. `V`는 시험한 N개 샤프 **추정치들의 분산**이다.
    실제 N개 샤프를 갖고 있다면 그 표본분산을 `trial_variance`로 넘긴다.
    없으면 귀무가설(참 샤프 0) 하의 근사 `V ≈ 1/n_obs`를 쓴다 — 우리가 보통
    처한 상황이다(변형들을 다 기록해 두지 않았다).

    `n_trials=1`이면 **0을 반환한다.** 위 공식은 N=1에서 Φ⁻¹(0) = -∞로 발산한다.
    한 번만 뽑은 표본의 "최댓값"의 기댓값은 그 분포의 평균이고, 귀무가설에서
    그건 0이다 — 즉 탐색하지 않았으면 깎을 것도 없다. (이 규약 덕분에
    `deflated_sharpe(n_trials=1)`이 `probabilistic_sharpe(benchmark=0)`과 같아진다.)

    반환 단위는 입력과 같은 **관측 1건당** 샤프다.

    우리 현실 수치 예시 — 관측 490일, 시험 100회:
        E[max SR] ≈ 0.114/관측일 → 연율화(√252) 약 1.81.
        즉 **엣지가 0인 전략 100개를 490일 창에서 돌리면 그중 최고는 연율 샤프
        1.8쯤으로 나온다.** 그 숫자를 성과로 보고하면 그건 성과가 아니라 탐색의
        기록이다.
    """
    if n_trials < 1:
        raise ValueError(f"n_trials는 1 이상이어야 한다: {n_trials!r}")
    if n_obs < 2:
        raise ValueError(f"n_obs는 2 이상이어야 한다: {n_obs!r}")
    if n_trials == 1:
        return 0.0
    variance = (1.0 / n_obs) if trial_variance is None else trial_variance
    if variance < 0:
        raise ValueError(f"trial_variance는 음수일 수 없다: {trial_variance!r}")
    gamma = EULER_MASCHERONI
    z1 = _N.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(variance) * ((1.0 - gamma) * z1 + gamma * z2)


def deflated_sharpe(
    sharpe: float, n_trials: int, n_obs: int, skew: float = 0.0,
    kurtosis: float = 3.0, trial_variance: float | None = None,
) -> float:
    """DSR — **탐색 횟수를 반영해 깎은** 샤프의 유의확률.

    Bailey & López de Prado (2014): `PSR`의 벤치마크를 0이 아니라
    `expected_max_sharpe(n_trials, n_obs)`로 두면 된다. 즉

        DSR = PSR( E[max SR | N개 시험] )

    "이 샤프가 0보다 큰가"가 아니라 **"N번 뒤져서 나온 최고치치고도 큰가"**를
    묻는다. 이 저장소가 실제로 하는 일(규칙 변형을 계속 시험)에 맞는 질문은
    후자다.

    `n_trials`는 **정직하게 세야 한다.** 익절/손절 조합 4종을 재생했으면 4,
    그 전에 눈으로 20개를 보고 4개를 고른 것이면 24다. 축소해 세면 이 함수는
    그만큼 관대해진다 — 자기 자신을 속이는 가장 쉬운 방법이다.

    반환은 0~1 확률. 0.95 같은 임계값은 여기서 정하지 않는다(호출부가 문맥과
    함께 판단한다).
    """
    benchmark = expected_max_sharpe(n_trials, n_obs, trial_variance)
    return probabilistic_sharpe(sharpe, benchmark, n_obs, skew, kurtosis)


def min_track_record_length(
    sharpe: float, benchmark: float = 0.0, skew: float = 0.0,
    kurtosis: float = 3.0, confidence: float = 0.95,
) -> float:
    """**이 샤프를 믿으려면 관측이 몇 개 필요한가.**

    Bailey & López de Prado (2012):

        MinTRL = 1 + [1 - γ3·SR + (γ4-1)/4·SR²] · (Φ⁻¹(conf) / (SR - SR*))²

    PSR을 뒤집은 것이다: "PSR이 `confidence`를 넘으려면 n이 얼마여야 하는가".
    반환 단위는 **관측 개수**(입력 샤프와 같은 주기) — 일봉 샤프를 넣었으면
    거래일 수다.

    `sharpe <= benchmark`면 `math.inf`를 반환한다. 표본을 아무리 늘려도 벤치마크
    이하인 샤프는 벤치마크보다 크다고 유의해지지 않는다 — 여기서 0이나 큰 정수를
    돌려주면 "조금만 더 모으면 된다"는 거짓말이 된다.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence는 0과 1 사이여야 한다: {confidence!r}")
    if sharpe <= benchmark:
        return math.inf
    z = _N.inv_cdf(confidence)
    return 1.0 + _variance_term(sharpe, skew, kurtosis) * (z / (sharpe - benchmark)) ** 2
