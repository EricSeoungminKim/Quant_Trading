"""Rolling-window walk-forward 검증 — 과최적화 방지의 핵심 장치.

각 윈도우: train 구간에서만 파라미터를 탐색하고(quant.research.optimize),
그 결과를 test 구간에 "한 번만" 적용해 평가한다. **성과로 보고할 수 있는 것은
test(out-of-sample) 결과뿐이다** — train(in-sample) 숫자는 별도로, 명확히
라벨링해서만 보여준다. train-test 갭이 클수록 과최적화 신호다.

look-ahead 방지: split_windows가 만드는 test 구간은 자신의 train 구간과 겹치지
않고, embargo_days만큼 간격을 둘 수 있다(구간 경계 근처 자기상관으로 인한 정보
누설 방지). 실제 리플레이는 quant.backtest.run_backtest(end=...)가 담당하며,
그 함수는 항상 `end` 이전 완성봉만 쓴다(ADR-4) — 워밍업(lookback_bars)은 로드된
전체 과거 데이터 중 `end` 이전분에서만 채워지므로 test 구간 데이터가 train/워밍업에
새어 들어갈 수 없다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

from quant.backtest import BacktestResult
from quant.backtest import run_backtest as _default_run_backtest
from quant.research.optimize import DEFAULT_MIN_TRADES, OptimizeResult, optimize
from quant.research.sample_guard import SampleSizeCheck, check_sample_size

logger = logging.getLogger(__name__)


@dataclass
class Window:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_days: int
    test_days: int


@dataclass
class WindowResult:
    window: Window
    best_params: dict
    train_metrics: dict  # in-sample — 참고용, 성과 근거로 쓰지 않는다
    test_metrics: dict  # out-of-sample — 유일하게 성과로 보고 가능한 숫자
    test_equity: pd.Series
    test_trades: pd.DataFrame


@dataclass
class WalkForwardResult:
    window_results: list[WindowResult] = field(default_factory=list)
    oos_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    oos_metrics: dict = field(default_factory=dict)
    sample_check: SampleSizeCheck | None = None
    param_stability: dict = field(default_factory=dict)


def split_windows(
    start, end, train_days: int, test_days: int, step_days: int, embargo_days: int = 0,
) -> list[Window]:
    """[start, end] 구간을 영업일(월~금) 기준으로 롤링 train/test 윈도우로 자른다.

    영업일 카운트는 실제 거래 캘린더(공휴일 등)의 근사치다 — 정확한 봉 경계는
    walk_forward가 run_backtest(end=..., days=...)를 호출할 때 실데이터 기준으로
    다시 계산되므로, 여기서의 근사 오차가 look-ahead로 이어지지는 않는다.
    embargo_days만큼 train 끝과 test 시작 사이에 간격을 둔다."""
    bdays = pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))
    windows: list[Window] = []
    train_start_idx = 0
    while True:
        train_end_idx = train_start_idx + train_days - 1
        test_start_idx = train_end_idx + 1 + embargo_days
        test_end_idx = test_start_idx + test_days - 1
        if test_end_idx >= len(bdays):
            break
        windows.append(Window(
            train_start=bdays[train_start_idx], train_end=bdays[train_end_idx],
            test_start=bdays[test_start_idx], test_end=bdays[test_end_idx],
            train_days=train_days, test_days=test_days,
        ))
        train_start_idx += step_days
    return windows


def _equity_stats(equity: pd.Series) -> dict:
    """OOS 체인 equity curve(정규화 후 곱연결)의 요약 통계.
    quant.backtest.engine._compute_metrics와 유사하지만 trades 없이 equity
    만으로 계산한다(윈도우별 trades는 WindowResult.test_trades에 개별 보관됨)."""
    if equity.empty:
        return {"total_return_pct": 0.0, "mdd_pct": 0.0, "sharpe": 0.0, "n_bars": 0}
    start_v, end_v = float(equity.iloc[0]), float(equity.iloc[-1])
    total_return_pct = (end_v / start_v - 1) * 100 if start_v else 0.0
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    mdd_pct = float(drawdown.min()) * 100 if not drawdown.empty else 0.0
    rets = equity.pct_change().dropna()
    sharpe = 0.0
    if len(rets) > 1 and rets.std() > 0:
        years = max((equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 24 * 3600), 1e-9)
        periods_per_year = len(rets) / years
        sharpe = float((rets.mean() / rets.std()) * (periods_per_year ** 0.5))
    return {
        "total_return_pct": round(total_return_pct, 2),
        "mdd_pct": round(mdd_pct, 2),
        "sharpe": round(sharpe, 2),
        "n_bars": len(equity),
    }


def _concat_oos_equity(window_results: list[WindowResult]) -> pd.Series:
    """각 윈도우의 test equity curve를 정규화(첫 값=1)해 곱연결한다 — 윈도우마다
    독립된 start_cash로 새로 시작하므로 원 스케일 그대로는 이어붙일 수 없다."""
    chained: list[pd.Series] = []
    multiplier = 1.0
    for wr in window_results:
        eq = wr.test_equity
        if eq.empty:
            continue
        normalized = eq / float(eq.iloc[0]) * multiplier
        chained.append(normalized)
        multiplier = float(normalized.iloc[-1])
    return pd.concat(chained) if chained else pd.Series(dtype=float)


def _param_stability(window_results: list[WindowResult], param_space: dict) -> dict:
    """윈도우 간 선택된 파라미터가 얼마나 요동치는지 — 심하게 flip하면 과최적화
    신호다(진짜 엣지라면 인접 윈도우에서 비슷한 값이 나와야 한다)."""
    stability: dict = {}
    for name, spec in param_space.items():
        values = [wr.best_params.get(name) for wr in window_results]
        if not values:
            continue
        if spec["type"] == "categorical":
            flips = sum(1 for i in range(1, len(values)) if values[i] != values[i - 1])
            flip_rate = flips / (len(values) - 1) if len(values) > 1 else 0.0
            stability[name] = {"values": values, "flip_rate": round(flip_rate, 2)}
        else:
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            stdev = variance ** 0.5
            cv = (stdev / abs(mean)) if mean else 0.0
            stability[name] = {"values": values, "mean": mean, "stdev": round(stdev, 4), "cv": round(cv, 2)}
    return stability


def walk_forward(
    strategy_id: str,
    param_space: dict,
    windows: list[Window],
    n_trials: int = 20,
    objective=None,
    seed: int = 42,
    min_trades: int = DEFAULT_MIN_TRADES,
    source: str = "stub",
    interval: str = "15m",
    settings_path: str = "config/settings.yaml",
    storage_dir: str | Path | None = None,
    run_backtest_fn: Callable[..., BacktestResult] | None = None,
) -> WalkForwardResult:
    """윈도우마다 train 구간에서 optimize() → 그 결과를 test 구간에 한 번만 적용.
    반환된 WalkForwardResult.oos_metrics/oos_equity만 "성과"로 인용할 수 있다 —
    window_results[i].train_metrics는 in-sample이므로 참고용일 뿐이다."""
    if not windows:
        raise ValueError("windows가 비어 있음 — start~end 구간이 train_days+test_days(+embargo)를 못 채움")

    backtest_fn = run_backtest_fn or _default_run_backtest
    window_results: list[WindowResult] = []

    for w in windows:
        storage_path = Path(storage_dir) / f"{strategy_id}_{w.train_end.date()}.db" if storage_dir else None

        opt_result: OptimizeResult = optimize(
            strategy_id=strategy_id, param_space=param_space, days=w.train_days, end=w.train_end,
            interval=interval, source=source, settings_path=settings_path, n_trials=n_trials,
            objective=objective, seed=seed, min_trades=min_trades, storage_path=storage_path,
            run_backtest_fn=backtest_fn,
        )
        test_result = backtest_fn(
            strategy_id=strategy_id, days=w.test_days, interval=interval, source=source,
            settings_path=settings_path, end=w.test_end, param_overrides=opt_result.best_params,
        )
        window_results.append(WindowResult(
            window=w, best_params=opt_result.best_params, train_metrics=opt_result.best_metrics,
            test_metrics=test_result.metrics, test_equity=test_result.equity_curve,
            test_trades=test_result.trades,
        ))

    oos_equity = _concat_oos_equity(window_results)
    oos_metrics = _equity_stats(oos_equity)
    total_test_trades = sum(int(wr.test_metrics.get("n_trades", 0)) for wr in window_results)
    total_test_days = sum(wr.window.test_days for wr in window_results)
    sample_check = check_sample_size(
        trading_days=total_test_days, n_bars=len(oos_equity), n_trades=total_test_trades,
    )
    if not sample_check.actionable:
        logger.warning(
            "표본 부족 — 이 walk-forward 결과는 NOT ACTIONABLE: %s", "; ".join(sample_check.reasons),
        )

    return WalkForwardResult(
        window_results=window_results, oos_equity=oos_equity, oos_metrics=oos_metrics,
        sample_check=sample_check, param_stability=_param_stability(window_results, param_space),
    )
