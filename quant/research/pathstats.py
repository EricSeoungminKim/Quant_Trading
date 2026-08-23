"""자산곡선의 **경로**를 보는 통계 — 끝점 하나로 요약된 성과가 숨기는 것들.

기존 `_compute_metrics`(quant/backtest/engine.py)는 끝점 지표만 낸다:
총수익·CAGR·MDD·샤프·승률. 문제는 **총수익이 같아도 그 돈을 버는 과정이 전혀 다를
수 있다**는 것이다. 한 달에 몰아서 번 전략과 10년간 꾸준히 오른 전략은 같은 CAGR을
찍지만, 실제로 그 안에 돈을 넣고 살아야 하는 사람에게는 완전히 다른 물건이다.
여기 있는 함수들은 그 차이를 드러낸다.

- `log_equity_trend` — 끝점 두 개가 아니라 **모든 점**을 쓰는 성장률과 R².
  R²이 낮으면 그 수익은 추세가 아니라 몇 번의 점프에서 나왔다는 뜻이다.
- `rolling_window_returns` — "아무 때나 들어가서 N개월 들고 있었으면?"의 분포.
- `drawdown_analytics` — 최저점 하나(MDD)가 아니라 낙폭 에피소드 전체 + 수중 시간
  + Ulcer Index(깊이와 길이를 함께 벌준다).
- `trade_bootstrap` — 실현 거래손익을 복원추출해 경로를 다시 만든다.
  실제 경로가 분포의 어디에 있었는지 = 운이었는지.
- `consistency` — 월/연 단위 수익 표, 양수 비율, 최장 연속 손실.

모든 함수는 순수하다(네트워크·디스크·전역상태 없음). 부트스트랩은 시드를 받아
완전 결정론적이다.

**표본 크기를 반드시 함께 낸다.** 창 3개에서 나온 중앙값은 숫자가 아니라 잡음이다.
표본이 모자라면 값을 만들어내지 않고 `sufficient=False`로 표시한다 —
렌더러(quant/research/report.py)가 그 행을 "표본 부족"으로 찍는다.

집계 점수(score)는 의도적으로 만들지 않는다. 구성요소를 직접 보라고 만든 모듈이다.

numpy/pandas만 쓴다. scipy는 quantstats(선택 의존성 그룹 research)를 통해서만
들어오므로 이 모듈이 기대면 안 된다 — OLS는 손으로 짜면 세 줄이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

_SECONDS_PER_YEAR = 365.25 * 24 * 3600

# OLS 기울기의 표준오차를 계산하려면 자유도(n-2)가 최소 1은 있어야 한다.
_MIN_TREND_OBS = 3

# 창 1개짜리 "분포"는 분포가 아니다. 최소 2개는 있어야 p5/p95를 찍는 시늉이라도 한다
# (그래도 겹치는 창이라 독립 표본이 아니다 — rolling_window_returns 독스트링 참고).
_MIN_ROLLING_WINDOWS = 2

# 월/연 표는 최소 2개 구간이 있어야 "일관성"을 말할 수 있다.
_MIN_PERIODS = 2

# 부트스트랩 최소 거래 수. sample_guard.MIN_TRADES와 같은 근거(이항 표준오차)지만
# 적용 대상이 다르다 — 여긴 복원추출 원본 시퀀스의 길이다.
_MIN_BOOTSTRAP_TRADES = 30

ROLLING_WINDOW_MONTHS: dict[str, int] = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12}


@dataclass
class TrendStats:
    """log(자산)을 시간에 회귀한 결과. 끝점만 쓰는 CAGR과 달리 모든 점을 쓴다."""

    n_obs: int
    span_years: float
    sufficient: bool
    # 연 로그기울기(자연로그). growth_pct = (exp(slope)-1)*100 = 복리 연성장률
    slope_log_per_year: float = 0.0
    growth_pct_per_year: float = 0.0
    stderr_log_per_year: float = 0.0
    t_stat: float = 0.0
    r_squared: float = 0.0
    # 기울기 95% 구간을 복리 성장률로 환산한 것(정규근사)
    growth_pct_lo: float = 0.0
    growth_pct_hi: float = 0.0
    n_dropped_nonpositive: int = 0


@dataclass
class RollingWindowStat:
    label: str
    window_bars: int
    count: int  # 겹치는 창의 개수 — 독립 표본 수가 아니다
    sufficient: bool
    median_pct: float = 0.0
    mean_pct: float = 0.0
    p5_pct: float = 0.0
    p25_pct: float = 0.0
    p75_pct: float = 0.0
    p95_pct: float = 0.0
    frac_positive: float = 0.0


@dataclass
class DrawdownEpisode:
    """직전 고점(start)에서 최저점(trough)을 찍고 회복(recovery)하기까지의 한 사건.

    underwater_bars: 고점 아래에 있던 봉 수(회복 봉 제외). 미회복이면 마지막 봉까지.
    recovery_bars: 최저점에서 고점을 되찾기까지의 봉 수(미회복이면 None).
    """

    start: pd.Timestamp
    trough: pd.Timestamp
    recovery: pd.Timestamp | None
    depth_pct: float
    underwater_bars: int
    underwater_days: float
    recovery_bars: int | None
    recovered: bool


@dataclass
class DrawdownStats:
    n_bars: int
    max_depth_pct: float
    ulcer_index: float  # sqrt(mean(낙폭%^2)) — 깊고 긴 낙폭을 함께 벌준다
    time_under_water_frac: float
    longest_underwater_bars: int
    longest_underwater_days: float
    n_episodes: int
    episodes: list[DrawdownEpisode] = field(default_factory=list)
    unrecovered: bool = False


@dataclass
class BootstrapStats:
    n_trades: int
    n_paths: int
    seed: int
    sufficient: bool
    start_equity: float = 0.0
    final_return_pct_p5: float = 0.0
    final_return_pct_p25: float = 0.0
    final_return_pct_median: float = 0.0
    final_return_pct_p75: float = 0.0
    final_return_pct_p95: float = 0.0
    mdd_pct_p5: float = 0.0
    mdd_pct_median: float = 0.0
    mdd_pct_p95: float = 0.0
    frac_negative: float = 0.0
    observed_final_return_pct: float = 0.0
    observed_final_pct_rank: float = 0.0
    observed_mdd_pct: float = 0.0
    observed_mdd_pct_rank: float = 0.0


@dataclass
class PeriodReturn:
    label: str
    return_pct: float


@dataclass
class ConsistencyStats:
    n_months: int
    n_years: int
    months_sufficient: bool
    years_sufficient: bool
    monthly: list[PeriodReturn] = field(default_factory=list)
    yearly: list[PeriodReturn] = field(default_factory=list)
    frac_positive_months: float = 0.0
    frac_positive_years: float = 0.0
    best_month: PeriodReturn | None = None
    worst_month: PeriodReturn | None = None
    best_year: PeriodReturn | None = None
    worst_year: PeriodReturn | None = None
    longest_losing_streak_months: int = 0


@dataclass
class PathStats:
    """경로 통계 묶음. 집계 점수는 없다 — 구성요소를 직접 본다."""

    n_bars: int
    start: pd.Timestamp | None
    end: pd.Timestamp | None
    span_years: float
    n_trades: int
    trend: TrendStats
    rolling: list[RollingWindowStat]
    drawdown: DrawdownStats
    bootstrap: BootstrapStats
    consistency: ConsistencyStats


def _validate(equity: pd.Series) -> None:
    if not isinstance(equity, pd.Series):
        raise TypeError(f"equity_curve는 pd.Series여야 한다(받은 것: {type(equity).__name__})")
    if not isinstance(equity.index, pd.DatetimeIndex):
        raise TypeError("equity_curve의 인덱스는 DatetimeIndex여야 한다")
    if not equity.index.is_monotonic_increasing:
        raise ValueError("equity_curve 인덱스가 시간순이 아니다")


def _span_years(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 0.0
    return float((index[-1] - index[0]).total_seconds() / _SECONDS_PER_YEAR)


def log_equity_trend(equity: pd.Series) -> TrendStats:
    """log(자산)을 경과연수에 OLS 회귀한다.

    왜 CAGR이 아니라 이것인가: CAGR은 첫 점과 끝 점 **두 개**만 본다. 중간에 무슨
    일이 있었든 상관하지 않으므로, 한 번의 점프로 만든 수익과 매일 조금씩 쌓은
    수익을 구분하지 못한다. 여기서 나오는 세 숫자가 그 구분을 한다:

    - `growth_pct_per_year` = (exp(기울기)-1)x100. 모든 점을 쓴 복리 성장률.
    - `r_squared` = 자산 움직임 중 추세로 설명되는 비율. 낮으면 "꾸준한 과정"이
      아니라 "몇 번의 운"이었다는 뜻이다.
    - `stderr_log_per_year` / `t_stat` = 기울기가 0과 구분되기는 하는가.

    한계(정직하게): OLS 표준오차는 잔차가 i.i.d.라고 가정한다. 자산곡선의 로그
    잔차는 자기상관·이분산이 있으므로 여기 표준오차는 **낙관적인(작은) 하한**이다.
    t가 2를 겨우 넘는 정도라면 "유의하다"고 읽지 말 것.
    """
    _validate(equity)
    clean = equity.dropna()
    positive = clean[clean > 0]
    n_dropped = int(len(clean) - len(positive))
    n = len(positive)
    if n < _MIN_TREND_OBS:
        return TrendStats(
            n_obs=n, span_years=_span_years(positive.index), sufficient=False,
            n_dropped_nonpositive=n_dropped,
        )

    t = (positive.index - positive.index[0]).total_seconds().to_numpy() / _SECONDS_PER_YEAR
    y = np.log(positive.to_numpy(dtype=float))
    t_bar, y_bar = t.mean(), y.mean()
    s_tt = float(((t - t_bar) ** 2).sum())
    if s_tt <= 0:
        return TrendStats(n_obs=n, span_years=0.0, sufficient=False, n_dropped_nonpositive=n_dropped)

    slope = float(((t - t_bar) * (y - y_bar)).sum() / s_tt)
    intercept = float(y_bar - slope * t_bar)
    resid = y - (intercept + slope * t)
    ssr = float((resid ** 2).sum())
    sst = float(((y - y_bar) ** 2).sum())
    r_squared = 1.0 - ssr / sst if sst > 0 else 0.0
    stderr = float(np.sqrt(ssr / (n - 2) / s_tt)) if n > 2 else float("inf")
    t_stat = slope / stderr if stderr > 0 and np.isfinite(stderr) else 0.0

    return TrendStats(
        n_obs=n,
        span_years=_span_years(positive.index),
        sufficient=True,
        slope_log_per_year=slope,
        growth_pct_per_year=float(np.expm1(slope) * 100),
        stderr_log_per_year=stderr,
        t_stat=float(t_stat),
        r_squared=float(r_squared),
        growth_pct_lo=float(np.expm1(slope - 1.96 * stderr) * 100) if np.isfinite(stderr) else 0.0,
        growth_pct_hi=float(np.expm1(slope + 1.96 * stderr) * 100) if np.isfinite(stderr) else 0.0,
        n_dropped_nonpositive=n_dropped,
    )


def rolling_window_returns(
    equity: pd.Series,
    windows: dict[str, int] | None = None,
) -> list[RollingWindowStat]:
    """겹치는 모든 N개월 창의 수익률 분포 — "아무 때나 들어가서 N개월 들고 있었으면?"

    창 길이는 인덱스에서 역산한 봉/연 밀도로 개월 -> 봉 수로 환산한다(거래일 캘린더
    가정 없이 실제 데이터 밀도를 쓴다).

    **겹치는 창은 서로 독립이 아니다(자기상관).** 따라서 여기 p5/p95는 기술통계지
    추론통계가 아니다 — 신뢰구간처럼 읽지 말 것. count는 "겹치는 창의 개수"이며
    독립 표본 수가 아니라는 뜻에서 항상 함께 출력한다.

    창이 데이터보다 길면 값을 만들지 않고 `sufficient=False`로 반환한다.
    """
    _validate(equity)
    windows = windows or ROLLING_WINDOW_MONTHS
    clean = equity.dropna()
    n = len(clean)
    span = _span_years(clean.index)
    out: list[RollingWindowStat] = []

    for label, months in windows.items():
        if n < 2 or span <= 0:
            out.append(RollingWindowStat(label=label, window_bars=0, count=0, sufficient=False))
            continue
        bars_per_year = (n - 1) / span
        window_bars = max(1, int(round(bars_per_year * months / 12)))
        count = n - window_bars
        if count < _MIN_ROLLING_WINDOWS:
            out.append(RollingWindowStat(
                label=label, window_bars=window_bars, count=max(count, 0), sufficient=False,
            ))
            continue
        values = clean.to_numpy(dtype=float)
        rets = values[window_bars:] / values[:-window_bars] - 1.0
        rets = rets[np.isfinite(rets)]
        if len(rets) < _MIN_ROLLING_WINDOWS:
            out.append(RollingWindowStat(
                label=label, window_bars=window_bars, count=len(rets), sufficient=False,
            ))
            continue
        p5, p25, p50, p75, p95 = np.percentile(rets, [5, 25, 50, 75, 95])
        out.append(RollingWindowStat(
            label=label, window_bars=window_bars, count=len(rets), sufficient=True,
            median_pct=float(p50 * 100), mean_pct=float(rets.mean() * 100),
            p5_pct=float(p5 * 100), p25_pct=float(p25 * 100),
            p75_pct=float(p75 * 100), p95_pct=float(p95 * 100),
            frac_positive=float((rets > 0).mean()),
        ))
    return out


def drawdown_analytics(equity: pd.Series) -> DrawdownStats:
    """낙폭을 최저점 하나로 줄이지 않는다.

    MDD는 "가장 나빴던 순간"만 본다. 실제로 견뎌야 하는 것은 **얼마나 깊었나**와
    **얼마나 오래 갔나**의 곱이다. 그래서 함께 낸다:

    - 낙폭 에피소드 표(고점 -> 최저점 -> 회복), 미회복 여부 포함
    - `time_under_water_frac` — 직전 고점 아래에 있던 시간의 비율
    - `longest_underwater_bars` — 가장 길었던 수중 구간
    - `ulcer_index` = sqrt(mean(낙폭%^2)). 깊이만 보는 MDD와 달리 **길이도** 벌준다.
    """
    _validate(equity)
    clean = equity.dropna()
    n = len(clean)
    if n == 0:
        return DrawdownStats(
            n_bars=0, max_depth_pct=0.0, ulcer_index=0.0, time_under_water_frac=0.0,
            longest_underwater_bars=0, longest_underwater_days=0.0, n_episodes=0,
        )

    values = clean.to_numpy(dtype=float)
    index = clean.index
    running_max = np.maximum.accumulate(values)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_pct = np.where(running_max > 0, (values / running_max - 1.0) * 100, 0.0)
    ulcer = float(np.sqrt(np.mean(dd_pct ** 2)))
    time_uw = float((values < running_max).mean())

    episodes: list[DrawdownEpisode] = []
    peak_val, peak_pos = values[0], 0
    trough_val, trough_pos = values[0], 0
    in_dd = False

    def _close(recovery_pos: int | None) -> None:
        end_pos = recovery_pos if recovery_pos is not None else n - 1
        underwater_bars = (end_pos - peak_pos - 1) if recovery_pos is not None else (end_pos - peak_pos)
        episodes.append(DrawdownEpisode(
            start=index[peak_pos], trough=index[trough_pos],
            recovery=index[recovery_pos] if recovery_pos is not None else None,
            depth_pct=float((trough_val / peak_val - 1.0) * 100) if peak_val else 0.0,
            underwater_bars=int(underwater_bars),
            underwater_days=float((index[end_pos] - index[peak_pos]).total_seconds() / 86400),
            recovery_bars=int(recovery_pos - trough_pos) if recovery_pos is not None else None,
            recovered=recovery_pos is not None,
        ))

    for pos in range(1, n):
        v = values[pos]
        if v >= peak_val:
            if in_dd:
                _close(pos)
                in_dd = False
            peak_val, peak_pos = v, pos
        elif not in_dd:
            in_dd, trough_val, trough_pos = True, v, pos
        elif v < trough_val:
            trough_val, trough_pos = v, pos
    if in_dd:
        _close(None)

    longest_bars = max((e.underwater_bars for e in episodes), default=0)
    longest_days = max((e.underwater_days for e in episodes), default=0.0)
    return DrawdownStats(
        n_bars=n,
        max_depth_pct=float(dd_pct.min()) if n else 0.0,
        ulcer_index=ulcer,
        time_under_water_frac=time_uw,
        longest_underwater_bars=int(longest_bars),
        longest_underwater_days=float(longest_days),
        n_episodes=len(episodes),
        episodes=episodes,
        unrecovered=any(not e.recovered for e in episodes),
    )


def trade_pnl_sequence(trades: pd.DataFrame) -> np.ndarray:
    """체결 로그에서 라운드트립 실현손익 시퀀스를 뽑는다(매도 행의 `pnl`, KRW).

    매도 행만 쓰는 것은 `_compute_metrics`의 승률 정의와 같은 규약이다. 주의:
    현재 스키마에서 매수 행의 수수료는 매수 행에 실려 있으므로 이 시퀀스는 **진입
    수수료를 포함하지 않는다** — 부트스트랩 수익률이 자산곡선 변화와 정확히 같지
    않을 수 있고, 그만큼 낙관적이다.
    """
    if trades is None or len(trades) == 0 or "pnl" not in trades.columns:
        return np.array([], dtype=float)
    rows = trades[trades["side"] == "sell"] if "side" in trades.columns else trades
    return rows["pnl"].to_numpy(dtype=float)


def trade_bootstrap(
    trades: pd.DataFrame,
    start_equity: float,
    n_paths: int = 2000,
    seed: int = 42,
) -> BootstrapStats:
    """실현 거래손익 시퀀스를 복원추출해 자산경로를 2000번 다시 만든다.

    묻는 질문: **실제로 겪은 그 경로가 운이었나?** 같은 거래손익 주머니에서 순서만
    바꿔 뽑았을 때 나올 수 있는 결과의 폭을 보여준다. 실제 MDD가 부트스트랩 분포의
    얕은 쪽 끝(예: 상위 10%)에 있다면, 그 전략은 겪어본 것보다 더 아프게 만들 수 있다.

    **핵심 가정(한계): 거래손익이 i.i.d.라는 것.** 실제로는 그렇지 않다 — 추세장에서
    이익 거래가 연달아 나오고 횡보장에서 손실이 뭉치는 계열 의존성/레짐 군집이 있다.
    복원추출은 그 구조를 부순다. 따라서 여기 분포는 "순서 의존성이 없었다면"의
    반사실이지, 미래 수익률의 예측 분포가 아니다.

    또 하나: 최종 수익률의 관측 백분위는 구조적으로 50 근처에 나온다(복원추출은
    평균을 보존하므로 합의 기대값이 관측 합과 같다). 정보가 있는 쪽은 **MDD 백분위**
    — 그건 순서에 의존하기 때문이다.

    시드 고정 시 완전 결정론적이다.
    """
    pnl = trade_pnl_sequence(trades)
    n = len(pnl)
    if n < _MIN_BOOTSTRAP_TRADES or start_equity <= 0:
        return BootstrapStats(
            n_trades=n, n_paths=0, seed=seed, sufficient=False, start_equity=float(start_equity),
        )

    rng = np.random.default_rng(seed)
    sampled = rng.choice(pnl, size=(n_paths, n), replace=True)
    paths = start_equity + np.cumsum(sampled, axis=1)
    # 시작점을 앞에 붙여야 첫 거래 이전 자산이 고점 후보로 잡힌다.
    paths = np.hstack([np.full((n_paths, 1), float(start_equity)), paths])
    final_return = paths[:, -1] / start_equity - 1.0

    running_max = np.maximum.accumulate(paths, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(running_max > 0, paths / running_max - 1.0, -1.0)
    mdd = dd.min(axis=1)

    observed_path = np.concatenate([[float(start_equity)], start_equity + np.cumsum(pnl)])
    observed_final = observed_path[-1] / start_equity - 1.0
    observed_run_max = np.maximum.accumulate(observed_path)
    with np.errstate(divide="ignore", invalid="ignore"):
        observed_mdd = float(np.where(
            observed_run_max > 0, observed_path / observed_run_max - 1.0, -1.0,
        ).min())

    f5, f25, f50, f75, f95 = np.percentile(final_return, [5, 25, 50, 75, 95])
    m5, m50, m95 = np.percentile(mdd, [5, 50, 95])
    return BootstrapStats(
        n_trades=n, n_paths=n_paths, seed=seed, sufficient=True, start_equity=float(start_equity),
        final_return_pct_p5=float(f5 * 100), final_return_pct_p25=float(f25 * 100),
        final_return_pct_median=float(f50 * 100), final_return_pct_p75=float(f75 * 100),
        final_return_pct_p95=float(f95 * 100),
        mdd_pct_p5=float(m5 * 100), mdd_pct_median=float(m50 * 100), mdd_pct_p95=float(m95 * 100),
        frac_negative=float((final_return < 0).mean()),
        observed_final_return_pct=float(observed_final * 100),
        observed_final_pct_rank=float((final_return < observed_final).mean() * 100),
        observed_mdd_pct=float(observed_mdd * 100),
        observed_mdd_pct_rank=float((mdd < observed_mdd).mean() * 100),
    )


def _period_returns(equity: pd.Series, freq: str, fmt: str) -> list[PeriodReturn]:
    marks = equity.resample(freq).last().dropna()
    if marks.empty:
        return []
    prev = marks.shift(1)
    prev.iloc[0] = float(equity.iloc[0])
    out: list[PeriodReturn] = []
    for ts, end_v in marks.items():
        base = float(prev.loc[ts])
        if base <= 0:
            continue
        out.append(PeriodReturn(label=ts.strftime(fmt), return_pct=float((end_v / base - 1) * 100)))
    return out


def consistency(equity: pd.Series) -> ConsistencyStats:
    """월별/연도별 수익 표 — 수익이 고르게 났는지, 한 구간에 몰렸는지.

    첫 구간의 기준값은 자산곡선의 첫 값이다(그렇게 하지 않으면 첫 달 수익이 통째로
    사라진다). 구간 수가 2개 미만이면 "일관성"이라는 말 자체가 성립하지 않으므로
    `*_sufficient=False`로 표시한다.
    """
    _validate(equity)
    clean = equity.dropna()
    if clean.empty:
        return ConsistencyStats(n_months=0, n_years=0, months_sufficient=False, years_sufficient=False)

    monthly = _period_returns(clean, "ME", "%Y-%m")
    yearly = _period_returns(clean, "YE", "%Y")

    streak = best_streak = 0
    for m in monthly:
        streak = streak + 1 if m.return_pct < 0 else 0
        best_streak = max(best_streak, streak)

    months_ok = len(monthly) >= _MIN_PERIODS
    years_ok = len(yearly) >= _MIN_PERIODS
    return ConsistencyStats(
        n_months=len(monthly),
        n_years=len(yearly),
        months_sufficient=months_ok,
        years_sufficient=years_ok,
        monthly=monthly,
        yearly=yearly,
        frac_positive_months=float(np.mean([m.return_pct > 0 for m in monthly])) if monthly else 0.0,
        frac_positive_years=float(np.mean([y.return_pct > 0 for y in yearly])) if yearly else 0.0,
        best_month=max(monthly, key=lambda m: m.return_pct) if monthly else None,
        worst_month=min(monthly, key=lambda m: m.return_pct) if monthly else None,
        best_year=max(yearly, key=lambda y: y.return_pct) if yearly else None,
        worst_year=min(yearly, key=lambda y: y.return_pct) if yearly else None,
        longest_losing_streak_months=best_streak,
    )


def compute_path_stats(
    equity: pd.Series,
    trades: pd.DataFrame | None = None,
    n_paths: int = 2000,
    seed: int = 42,
) -> PathStats:
    """다섯 가지 경로 통계를 한 번에 계산한다. 렌더링은 report.render_path_stats."""
    _validate(equity)
    clean = equity.dropna()
    trades = trades if trades is not None else pd.DataFrame()
    start_equity = float(clean.iloc[0]) if not clean.empty else 0.0
    return PathStats(
        n_bars=len(clean),
        start=clean.index[0] if not clean.empty else None,
        end=clean.index[-1] if not clean.empty else None,
        span_years=_span_years(clean.index),
        n_trades=len(trade_pnl_sequence(trades)),
        trend=log_equity_trend(clean),
        rolling=rolling_window_returns(clean),
        drawdown=drawdown_analytics(clean),
        bootstrap=trade_bootstrap(trades, start_equity=start_equity, n_paths=n_paths, seed=seed),
        consistency=consistency(clean),
    )
