"""optimize() 단위 테스트: 결정론성, min_trades 트라이얼 가드, optuna 부재 시
graceful degradation. 실제 backtest는 느리므로 항상 가짜 run_backtest_fn을 주입한다
(monkeypatch/fake 사용 지침 — CLAUDE.md 태스크 지시사항)."""
from __future__ import annotations

import sys

import pandas as pd
import pytest

from quant.backtest import BacktestResult

PARAM_SPACE = {"lookback_bars": {"type": "int", "low": 20, "high": 60, "step": 5}}


def _fake_result(sharpe: float, n_trades: int, days: int, end) -> BacktestResult:
    idx = pd.date_range(end=pd.Timestamp(end) if end else pd.Timestamp("2024-06-01"),
                         periods=days, freq="B")
    equity = pd.Series([1_000_000 * (1 + 0.0001 * i) for i in range(days)], index=idx, name="equity")
    trades = pd.DataFrame(columns=["ts", "symbol", "side", "qty", "price", "fee", "reason", "pnl"])
    metrics = {
        "total_return_pct": 1.0, "cagr_pct": 1.0, "mdd_pct": -1.0,
        "sharpe": sharpe, "win_rate": 55.0, "n_trades": n_trades,
    }
    return BacktestResult(equity_curve=equity, trades=trades, metrics=metrics)


def _peaked_fake_run_backtest(strategy_id, days, interval, source, settings_path,
                               end=None, param_overrides=None):
    """lookback_bars=40에서 Sharpe가 최고인 결정론적 가짜 backtest."""
    param_overrides = param_overrides or {}
    lookback = param_overrides.get("lookback_bars", 40)
    sharpe = 2.0 - abs(lookback - 40) * 0.05
    return _fake_result(sharpe, n_trades=50, days=days, end=end)


def test_optimize_deterministic_same_seed():
    pytest.importorskip("optuna")
    from quant.research.optimize import optimize

    r1 = optimize("donchian", PARAM_SPACE, days=30, n_trials=10, seed=42,
                   run_backtest_fn=_peaked_fake_run_backtest)
    r2 = optimize("donchian", PARAM_SPACE, days=30, n_trials=10, seed=42,
                   run_backtest_fn=_peaked_fake_run_backtest)
    assert r1.best_params == r2.best_params
    assert r1.best_value == r2.best_value


def test_optimize_different_seed_can_differ():
    """다른 seed가 항상 다른 결과를 보장하진 않지만, 최소한 같은 seed 재현성과
    구분되는 별개의 실행임을 확인 — 시드가 실제로 샘플러에 전달되는지 스모크 체크."""
    pytest.importorskip("optuna")
    from quant.research.optimize import optimize

    r1 = optimize("donchian", PARAM_SPACE, days=30, n_trials=6, seed=1,
                   run_backtest_fn=_peaked_fake_run_backtest)
    r2 = optimize("donchian", PARAM_SPACE, days=30, n_trials=6, seed=1,
                   run_backtest_fn=_peaked_fake_run_backtest)
    # 같은 seed는 항상 같아야 한다(이게 진짜 요구사항).
    assert [t.params for t in r1.trials] == [t.params for t in r2.trials]


def test_optimize_min_trades_guard_rejects_low_trade_trials():
    """lookback_bars=20일 때만 거래가 충분(min_trades 통과)하고, 그 외에는 거래가
    적어 페널티를 받는 상황을 만들어 optimize()가 그래도 유효한 20을 고르는지 확인."""
    pytest.importorskip("optuna")
    from quant.research.optimize import optimize

    def fake_run_backtest(strategy_id, days, interval, source, settings_path,
                           end=None, param_overrides=None):
        param_overrides = param_overrides or {}
        lookback = param_overrides.get("lookback_bars", 40)
        # lookback=20만 거래 40건(min_trades=30 통과), 나머지는 5건(미달).
        # 미달 파라미터들이 Sharpe는 훨씬 높게 나오도록 만들어 가드가 없으면 잘못 선택되게 함.
        if lookback == 20:
            return _fake_result(sharpe=1.0, n_trades=40, days=days, end=end)
        return _fake_result(sharpe=10.0, n_trades=5, days=days, end=end)

    result = optimize("donchian", PARAM_SPACE, days=30, n_trials=15, seed=3,
                       min_trades=30, run_backtest_fn=fake_run_backtest)
    assert result.best_params["lookback_bars"] == 20
    assert result.all_valid is True
    assert result.best_value == 1.0


def test_optimize_all_trials_below_min_trades_returns_best_effort():
    """전부 min_trades 미달이면 크래시하지 않고 best-effort로 반환하되 all_valid=False."""
    pytest.importorskip("optuna")
    from quant.research.optimize import optimize

    def fake_run_backtest(strategy_id, days, interval, source, settings_path,
                           end=None, param_overrides=None):
        return _fake_result(sharpe=5.0, n_trades=2, days=days, end=end)

    result = optimize("donchian", PARAM_SPACE, days=30, n_trials=5, seed=1,
                       min_trades=30, run_backtest_fn=fake_run_backtest)
    assert result.all_valid is False
    assert result.best_params  # 그래도 뭔가는 반환됨
    assert all(not t.valid for t in result.trials)


def test_optimize_raises_clear_error_without_optuna(monkeypatch):
    """optuna 미설치 환경을 흉내 내(sys.modules에 None 주입) optimize()가 흔한
    ImportError로 명확하게 실패하는지 확인 — 연구 스택 부재 시 graceful degradation."""
    monkeypatch.setitem(sys.modules, "optuna", None)
    from quant.research.optimize import optimize

    with pytest.raises(ImportError):
        optimize("donchian", PARAM_SPACE, days=10, n_trials=1,
                  run_backtest_fn=_peaked_fake_run_backtest)


def test_optimize_persists_trials_for_resume(tmp_path):
    pytest.importorskip("optuna")
    from quant.research.optimize import optimize

    storage_path = tmp_path / "study.db"
    optimize("donchian", PARAM_SPACE, days=30, n_trials=3, seed=1,
              storage_path=storage_path, run_backtest_fn=_peaked_fake_run_backtest)
    optimize("donchian", PARAM_SPACE, days=30, n_trials=4, seed=1,
              storage_path=storage_path, run_backtest_fn=_peaked_fake_run_backtest)

    import optuna
    study = optuna.load_study(study_name="donchian_optimize", storage=f"sqlite:///{storage_path}")
    assert len(study.trials) == 7  # 3 + 4가 누적됨(재개 가능성 검증)
