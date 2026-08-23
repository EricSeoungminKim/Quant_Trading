"""Optuna 기반 파라미터 탐색.

optuna는 함수 내부에서만 지연 임포트한다 — 연구 스택(pyproject.toml
[dependency-groups] research) 없이도 quant.apps.cli paper/backtest이 그대로
동작해야 하기 때문이다(AWS 배포는 거래 엔진만 돌린다).

목적함수는 기본적으로 Sharpe(위험조정수익)를 최대화한다. 단, 거래 건수가
min_trades 미만인 트라이얼은 큰 페널티를 줘 사실상 배제한다 — 3거래짜리
backtest가 우연히 Sharpe가 높게 나오는 건 우위가 아니라 노이즈다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from quant.backtest import BacktestResult
from quant.backtest import run_backtest as _default_run_backtest

# 거래 30건 미만은 이항분포 승률의 표준오차가 너무 커서 "이 파라미터가 낫다"고
# 주장할 근거가 안 된다. sample_guard.MIN_TRADES와 값은 같지만 적용 단위가 다르다
# (여긴 트라이얼 1건, sample_guard는 walk-forward 전체 OOS 합산).
DEFAULT_MIN_TRADES = 30
_MIN_TRADES_PENALTY = -1e9


@dataclass
class Trial:
    number: int
    params: dict
    value: float
    metrics: dict
    valid: bool  # min_trades 가드 통과 여부


@dataclass
class OptimizeResult:
    best_params: dict
    best_value: float
    best_metrics: dict
    trials: list[Trial] = field(default_factory=list)
    all_valid: bool = True  # False면 min_trades를 통과한 트라이얼이 하나도 없었다는 뜻


def _sharpe_objective(result: BacktestResult) -> float:
    return float(result.metrics.get("sharpe", 0.0))


def _sample_params(trial: Any, param_space: dict) -> dict:
    """param_space는 데이터로 선언한다(코드에 하드코딩하지 않음) — YAML로도 그대로
    옮길 수 있는 형태: name -> {"type": "int"|"float", "low", "high", "step"?} 또는
    {"type": "categorical", "choices": [...]}."""
    params = {}
    for name, spec in param_space.items():
        kind = spec["type"]
        if kind == "int":
            params[name] = trial.suggest_int(name, spec["low"], spec["high"], step=spec.get("step", 1))
        elif kind == "float":
            params[name] = trial.suggest_float(name, spec["low"], spec["high"], step=spec.get("step"))
        elif kind == "categorical":
            params[name] = trial.suggest_categorical(name, spec["choices"])
        else:
            raise ValueError(f"알 수 없는 파라미터 타입: {kind!r} (name={name!r})")
    return params


def optimize(
    strategy_id: str,
    param_space: dict,
    days: int,
    end: Any = None,
    interval: str = "15m",
    source: str = "stub",
    settings_path: str = "config/settings.yaml",
    n_trials: int = 20,
    objective: Callable[[BacktestResult], float] | None = None,
    seed: int = 42,
    min_trades: int = DEFAULT_MIN_TRADES,
    storage_path: str | Path | None = None,
    run_backtest_fn: Callable[..., BacktestResult] | None = None,
) -> OptimizeResult:
    """`run_backtest_fn`(기본 quant.backtest.run_backtest)을 목적함수로 삼아
    optuna(TPESampler, seed 고정 — 결정론적)로 param_space를 탐색한다.

    storage_path를 주면 SQLite에 트라이얼을 영속화해 재개 가능한 장기 실행을
    지원한다(같은 경로로 다시 호출하면 기존 스터디에 트라이얼을 이어 쌓는다).
    """
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    backtest_fn = run_backtest_fn or _default_run_backtest
    obj_fn = objective or _sharpe_objective
    trials: list[Trial] = []

    def _objective(trial: "optuna.Trial") -> float:
        params = _sample_params(trial, param_space)
        result = backtest_fn(
            strategy_id=strategy_id, days=days, interval=interval, source=source,
            settings_path=settings_path, end=end, param_overrides=params,
        )
        n_trades = int(result.metrics.get("n_trades", 0))
        valid = n_trades >= min_trades
        value = obj_fn(result) if valid else _MIN_TRADES_PENALTY
        trials.append(Trial(
            number=trial.number, params=params, value=value,
            metrics=dict(result.metrics), valid=valid,
        ))
        return value

    sampler = optuna.samplers.TPESampler(seed=seed)
    storage = f"sqlite:///{storage_path}" if storage_path else None
    study = optuna.create_study(
        direction="maximize", sampler=sampler, storage=storage,
        study_name=f"{strategy_id}_optimize", load_if_exists=bool(storage),
    )
    study.optimize(_objective, n_trials=n_trials)

    valid_trials = [t for t in trials if t.valid]
    pool = valid_trials or trials  # 전부 min_trades 미달이면 그나마 최선을 best-effort로 반환(all_valid=False로 표시)
    best = max(pool, key=lambda t: t.value)

    return OptimizeResult(
        best_params=best.params, best_value=best.value, best_metrics=best.metrics,
        trials=trials, all_valid=bool(valid_trials),
    )
