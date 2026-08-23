"""walk-forward 검증 레이어 단위 테스트: 윈도우 분할 수학, embargo, OOS 곱연결,
결정론성, 표본 부족 가드, quantstats 없이도 평문 리포트가 동작하는지.

optuna가 필요한 테스트(walk_forward가 내부적으로 optimize()를 호출)는
pytest.importorskip("optuna")로 개별 스킵한다 — 연구 스택 없이도 나머지(split_windows
수학, sample_guard, quantstats 미설치 시 render_html의 graceful degradation)는
그대로 돌아야 한다."""
from __future__ import annotations

import sys

import pandas as pd
import pytest

from quant.backtest import BacktestResult
from quant.research import report
from quant.research.sample_guard import check_sample_size
from quant.research.walkforward import (
    WalkForwardResult,
    split_windows,
    walk_forward,
)


def _fake_run_backtest(strategy_id, days, interval, source, settings_path,
                        end=None, param_overrides=None, n_trades=50):
    """lookback_bars=40을 정점으로 하는 결정론적 가짜 backtest — 진짜 optuna
    탐색이 의미 있는 답을 찾는지 확인하는 데 쓴다. 실제 backtest는 느리므로
    쓰지 않는다(테스트는 빠르고 결정론적이어야 함)."""
    param_overrides = param_overrides or {}
    lookback = param_overrides.get("lookback_bars", 40)
    sharpe = 2.0 - abs(lookback - 40) * 0.05
    idx = pd.date_range(end=pd.Timestamp(end) if end else pd.Timestamp("2024-06-01"),
                         periods=days, freq="B")
    equity = pd.Series([1_000_000 * (1 + 0.0001 * i) for i in range(days)], index=idx, name="equity")
    trades = pd.DataFrame(columns=["ts", "symbol", "side", "qty", "price", "fee", "reason", "pnl"])
    metrics = {
        "total_return_pct": 1.0, "cagr_pct": 1.0, "mdd_pct": -1.0,
        "sharpe": sharpe, "win_rate": 55.0, "n_trades": n_trades,
    }
    return BacktestResult(equity_curve=equity, trades=trades, metrics=metrics)


PARAM_SPACE = {"lookback_bars": {"type": "int", "low": 20, "high": 60, "step": 5}}


# --- 윈도우 분할 수학 (optuna 불필요) ---------------------------------------

def test_split_windows_no_overlap_between_train_and_own_test():
    windows = split_windows(start="2024-01-01", end="2024-06-01",
                             train_days=20, test_days=10, step_days=10)
    assert windows, "생성된 윈도우가 없음"
    for w in windows:
        assert w.train_end < w.test_start
        assert w.test_start <= w.test_end
        assert w.train_days == 20
        assert w.test_days == 10


def test_split_windows_steps_advance_correctly():
    windows = split_windows(start="2024-01-01", end="2024-06-01",
                             train_days=20, test_days=10, step_days=10)
    assert len(windows) >= 2
    for prev, cur in zip(windows, windows[1:]):
        # step_days=10 영업일만큼 train_start가 앞으로 이동해야 함
        expected_gap = pd.bdate_range(prev.train_start, cur.train_start)
        assert len(expected_gap) == 11  # inclusive range => 10 business-day step


def test_split_windows_embargo_applied():
    no_embargo = split_windows(start="2024-01-01", end="2024-06-01",
                                train_days=20, test_days=10, step_days=10, embargo_days=0)
    with_embargo = split_windows(start="2024-01-01", end="2024-06-01",
                                  train_days=20, test_days=10, step_days=10, embargo_days=3)
    w0, w1 = no_embargo[0], with_embargo[0]
    assert w0.train_end == w1.train_end
    gap_days = len(pd.bdate_range(w0.train_end, w1.test_start)) - 1
    no_embargo_gap = len(pd.bdate_range(w0.train_end, w0.test_start)) - 1
    assert gap_days == no_embargo_gap + 3


def test_split_windows_empty_when_range_too_short():
    windows = split_windows(start="2024-01-01", end="2024-01-10",
                             train_days=60, test_days=20, step_days=20)
    assert windows == []


# --- sample_guard (optuna 불필요) -------------------------------------------

def test_check_sample_size_flags_small_sample():
    check = check_sample_size(trading_days=38, n_bars=38 * 26, n_trades=15)
    assert check.actionable is False
    assert any("거래일" in r for r in check.reasons)
    assert any("거래 건수" in r for r in check.reasons)


def test_check_sample_size_actionable_when_thresholds_met():
    check = check_sample_size(trading_days=200, n_bars=200 * 26, n_trades=100)
    assert check.actionable is True
    assert check.reasons == []


# --- report.render_html graceful degradation (optuna/quantstats 불필요) -----

def test_render_html_graceful_without_quantstats(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "quantstats", None)  # import 강제 실패 유도
    result = WalkForwardResult(oos_equity=pd.Series([1.0, 1.01, 1.02],
                                index=pd.date_range("2024-01-01", periods=3)))
    ok = report.render_html(result, str(tmp_path / "out.html"))
    assert ok is False
    assert not (tmp_path / "out.html").exists()


# --- walk_forward (optuna 필요) ----------------------------------------------

def test_walk_forward_oos_concatenation_covers_expected_length():
    pytest.importorskip("optuna")
    windows = split_windows(start="2024-01-01", end="2024-06-01",
                             train_days=20, test_days=10, step_days=10, embargo_days=1)
    result = walk_forward("donchian", PARAM_SPACE, windows, n_trials=5,
                           run_backtest_fn=_fake_run_backtest)
    assert len(result.oos_equity) == sum(w.test_days for w in windows)


def test_walk_forward_deterministic_same_seed_same_params():
    pytest.importorskip("optuna")
    windows = split_windows(start="2024-01-01", end="2024-04-01",
                             train_days=20, test_days=10, step_days=10)
    r1 = walk_forward("donchian", PARAM_SPACE, windows, n_trials=8, seed=7,
                       run_backtest_fn=_fake_run_backtest)
    r2 = walk_forward("donchian", PARAM_SPACE, windows, n_trials=8, seed=7,
                       run_backtest_fn=_fake_run_backtest)
    assert [wr.best_params for wr in r1.window_results] == [wr.best_params for wr in r2.window_results]


def test_walk_forward_small_sample_marks_not_actionable():
    pytest.importorskip("optuna")
    windows = split_windows(start="2024-01-01", end="2024-03-01",
                             train_days=10, test_days=5, step_days=5)
    assert windows  # 짧은 구간이지만 최소 1개는 나와야 테스트가 의미 있음

    def low_trade_fake(*args, **kwargs):
        return _fake_run_backtest(*args, **kwargs, n_trades=3)

    result = walk_forward("donchian", PARAM_SPACE, windows, n_trials=3,
                           min_trades=1,  # 트라이얼 가드는 통과시키고, 전체 표본 가드만 테스트
                           run_backtest_fn=low_trade_fake)
    assert result.sample_check.actionable is False
    assert any("거래" in r for r in result.sample_check.reasons)


def test_walk_forward_raises_on_empty_windows():
    pytest.importorskip("optuna")
    with pytest.raises(ValueError):
        walk_forward("donchian", PARAM_SPACE, [], run_backtest_fn=_fake_run_backtest)
