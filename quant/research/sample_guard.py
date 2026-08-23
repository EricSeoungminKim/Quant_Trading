"""표본 크기 정직성 가드 — 표본이 작을 때 "권장 파라미터"를 확신에 찬 것처럼
보여주지 않기 위한 문턱 체크. quant/research/walkforward.py가 walk-forward
전체(OOS 구간 합산)에 대해 호출한다.

배경: 이 저장소의 실측 15분봉 과거 데이터는 (docs/data-availability.md 기준) 약
38거래일뿐이다. 그 정도 표본으로 뽑은 "최적 파라미터"는 노이즈에 맞춘 것과
구분이 안 된다 — 그래서 문턱을 명시적 상수로 못박고, 못 넘으면 결과를
NOT ACTIONABLE로 표시해 출력한다(숨기지 않고, 다만 신뢰하지 않는다).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 최소 거래일 수. train 60일 + test 20일짜리 윈도우가 최소 1개는 확보돼야
# "학습 구간과 분리된 검증"이라는 walk-forward의 전제가 성립한다 — 현재 실측
# 가용 데이터(~38거래일)로는 이 문턱을 넘지 못한다. 이는 의도된 결과다: 데이터가
# 늘어나기 전까지는 이 파이프라인이 "권장 파라미터"를 자신 있게 내놓지 않아야 한다.
MIN_TRADING_DAYS = 80

# 최소 거래 건수. n=30 미만이면 이항분포 승률의 표준오차가 너무 커서(예: 10건 중
# 7승도 동전던지기와 통계적으로 구분 안 됨) "이 파라미터가 우위가 있다"고 주장할
# 근거가 되지 않는다. optimize.DEFAULT_MIN_TRADES(트라이얼 단위 가드)와 값은
# 같지만 적용 대상이 다르다 — 여긴 walk-forward 전체 OOS 합산 거래 수 기준이다.
MIN_TRADES = 30


@dataclass
class SampleSizeCheck:
    trading_days: int
    n_bars: int
    n_trades: int
    actionable: bool
    reasons: list[str] = field(default_factory=list)


def check_sample_size(
    trading_days: int,
    n_bars: int,
    n_trades: int,
    min_trading_days: int = MIN_TRADING_DAYS,
    min_trades: int = MIN_TRADES,
) -> SampleSizeCheck:
    reasons: list[str] = []
    if trading_days < min_trading_days:
        reasons.append(f"거래일 {trading_days} < 최소 {min_trading_days}")
    if n_trades < min_trades:
        reasons.append(f"거래 건수 {n_trades} < 최소 {min_trades}")
    return SampleSizeCheck(
        trading_days=trading_days,
        n_bars=n_bars,
        n_trades=n_trades,
        actionable=not reasons,
        reasons=reasons,
    )
