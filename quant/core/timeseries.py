"""자본 곡선(equity curve) 성과 분석 — 순수 stdlib (2026-08-24).

gs-quant(goldmansachs/gs-quant) 대조 분석에서 확인한 우리 최대 공백을 메운다:
거래 **단위** 지표(승률·payoff·bps — control/ledger.py)는 있는데, 자본 **곡선**
지표(실현 변동성·샤프·MDD·CAGR — gs_quant.timeseries.econometrics 상당)를 계산할
층이 없었다. 라이브 페이퍼 기간의 "전략별 1,000만원이 어떻게 자랐나"는 곡선의
질문이지 거래의 질문이 아니다.

## 관례 (gs-quant 와 같게, 다른 곳은 명시)

- 수익률: 단순 수익률(simple returns) 기본.
- 연율화: 일간 기준 √252 (gs-quant 기본과 동일).
- 변동성: 표본 표준편차(N-1) — gs-quant `assume_zero_mean=False` 기본과 동일.
- 샤프: **무위험 이자율 0 가정** — gs-quant 는 통화별 무위험 곡선을 API 로
  조회하지만 우리는 그 소스가 없다. 없는 데이터를 지어내는 대신 rf=0 을 쓰고
  이름에 드러낸다(`sharpe_ratio_rf0`). KR 기준금리 ~3% 환경에서 이는 샤프를
  **과대평가**하는 방향이므로, 이 값으로 전략 간 비교는 되지만 절대 수준 판단은
  하지 말 것.
- MDD: peak 대비 비율(-0.2 = -20%), gs-quant `max_drawdown` 과 동일 정의.

## 정직성

표본이 작으면 작다고 말하는 건 **호출부의 의무**다(여기는 수학만 한다) —
`n_points`를 항상 함께 반환해 호출부가 문턱을 걸 수 있게 한다.

순수 stdlib — pandas 도 없다. `quant/core/` 규칙(외부 의존 0) 그대로.
"""
from __future__ import annotations

import math

TRADING_DAYS_PER_YEAR = 252


def simple_returns(values: list[float]) -> list[float]:
    """가격/자본 시퀀스 → 단순 수익률. 0 이하 값이 끼면 그 지점의 수익률은
    계산 불능이므로 건너뛰지 않고 **예외를 던진다** — 자본이 0/음수라는 건
    데이터가 깨졌다는 뜻이고, 조용히 건너뛰면 곡선이 왜곡된 채 그럴듯한
    숫자가 나온다."""
    if any(v <= 0 for v in values):
        raise ValueError("자본 곡선에 0 이하 값 — 데이터 손상 의심, 계산 거부")
    return [values[i] / values[i - 1] - 1.0 for i in range(1, len(values))]


def annualized_volatility(values: list[float], periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float | None:
    """연율화 실현 변동성(소수, 0.2 = 20%). 수익률 2개 미만이면 None."""
    rets = simple_returns(values)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def sharpe_ratio_rf0(values: list[float], periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float | None:
    """연율화 샤프, **무위험 이자율 0 가정**(모듈 docstring — 과대평가 방향).
    변동성이 0이거나 표본 부족이면 None(0으로 나눠 무한대를 지어내지 않는다)."""
    rets = simple_returns(values)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return (mean / sd) * math.sqrt(periods_per_year)


def max_drawdown(values: list[float]) -> float | None:
    """최대 낙폭(음수 소수, -0.2 = -20%). 값 2개 미만이면 None."""
    if len(values) < 2:
        return None
    peak = values[0]
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1.0)
    return worst


def cagr(values: list[float], n_periods: int, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float | None:
    """연복리 수익률. `n_periods` = 곡선이 덮는 기간 수(관측점 수가 아니라 간격 수).
    1년 미만 표본을 연율화하면 수치가 폭발하므로 그대로 낸다 — 해석은 호출부 몫."""
    if n_periods <= 0 or len(values) < 2 or values[0] <= 0:
        return None
    total = values[-1] / values[0]
    if total <= 0:
        return None
    years = n_periods / periods_per_year
    if years <= 0:
        return None
    return total ** (1.0 / years) - 1.0


def performance_summary(values: list[float], periods_per_year: int = TRADING_DAYS_PER_YEAR) -> dict:
    """자본 곡선 하나의 성과 요약. `n_points`를 항상 포함한다 — 표본 문턱은
    호출부가 건다(여기는 판정하지 않는다)."""
    out: dict = {"n_points": len(values)}
    if len(values) < 2:
        return out
    out["total_return"] = values[-1] / values[0] - 1.0 if values[0] > 0 else None
    out["cagr"] = cagr(values, len(values) - 1, periods_per_year)
    out["volatility"] = annualized_volatility(values, periods_per_year)
    out["sharpe_rf0"] = sharpe_ratio_rf0(values, periods_per_year)
    out["max_drawdown"] = max_drawdown(values)
    return out
