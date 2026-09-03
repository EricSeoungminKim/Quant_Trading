"""배포 게이트(quant.backtest.gate) 단위 테스트 — walk-forward fold + trade
analytics 를 손으로 지어 go/no-go/판단 불가 판정을 검증한다."""
from __future__ import annotations

import pytest

from quant.backtest.gate import GateThresholds, evaluate_gate, render_gate


def _fold(net_bps: float, n_round_trips: int = 40, mdd_pct: float = -5.0) -> dict:
    return {"net_bps": net_bps, "n_round_trips": n_round_trips, "mdd_pct": mdd_pct}


def _analytics(sharpe: float = 0.15, n: int = 150, exp_1x: float = 20.0,
               cost_bp: float = 14.0) -> dict:
    return {
        "trade_sharpe": {"n": n, "sharpe": sharpe, "skew": 0.0, "kurtosis": 3.0,
                          "moments_estimated": False},
        "cost_sensitivity": {
            "1x": exp_1x, "1.5x": exp_1x - 0.5 * cost_bp, "2x": exp_1x - cost_bp,
        },
    }


# ── 판단 불가 ──────────────────────────────────────────────────────────────

def test_no_folds_is_pandan_bulga():
    gate = evaluate_gate([], _analytics(), trials=1, cost_bp=14.0, market="US")
    assert gate["verdict"] == "판단 불가"
    assert gate["criteria"] == {}


def test_too_few_oos_trades_is_pandan_bulga():
    folds = [_fold(net_bps=10.0, n_round_trips=5)]  # 총 5건 < 최소 판단선(30)
    gate = evaluate_gate(folds, _analytics(), trials=1, cost_bp=14.0, market="US")
    assert gate["verdict"] == "판단 불가"


# ── GO ─────────────────────────────────────────────────────────────────────

def test_clearly_positive_strategy_is_go():
    folds = [_fold(net_bps=25.0, n_round_trips=40, mdd_pct=-5.0) for _ in range(4)]
    analytics = _analytics(sharpe=0.15, n=150, exp_1x=20.0, cost_bp=14.0)
    gate = evaluate_gate(folds, analytics, trials=1, cost_bp=14.0, market="US")

    assert gate["verdict"] == "GO"
    for name, c in gate["criteria"].items():
        assert c["pass"] is True, f"{name} 가 GO 시나리오에서 실패: {c}"


def test_go_criteria_table_has_defaults_and_values():
    folds = [_fold(net_bps=25.0, n_round_trips=40) for _ in range(4)]
    gate = evaluate_gate(folds, _analytics(), trials=1, cost_bp=14.0, market="US")
    assert gate["thresholds"] == {
        "min_oos_trades": 100,
        "min_expectancy_ci_lower_bp": -5.0,
        "min_deflated_sharpe": 0.95,
        "min_fold_positive_share": 0.60,
        "max_worst_fold_mdd_pct": 15.0,
        "min_worst_fold_expectancy_bp": -30.0,
        "min_plateau_ratio": 0.50,
    }
    assert gate["criteria"]["oos_n_trades"]["value"] == 160


# ── NO_GO: 2배 비용에서 죽는 전략 ────────────────────────────────────────────

def test_strategy_that_dies_at_2x_cost_is_no_go_with_that_criterion_failing():
    folds = [_fold(net_bps=25.0, n_round_trips=40, mdd_pct=-5.0) for _ in range(4)]
    # 1x 기대값은 양수(10bp)지만 비용을 2배로 올리면 음전환(10 - 14 = -4)
    analytics = _analytics(sharpe=0.15, n=150, exp_1x=10.0, cost_bp=14.0)
    gate = evaluate_gate(folds, analytics, trials=1, cost_bp=14.0, market="US")

    assert gate["verdict"] == "NO_GO"
    assert gate["criteria"]["survives_2x_cost"]["pass"] is False
    assert gate["criteria"]["survives_2x_cost"]["value"] < 0
    # 다른 필수 기준은 여전히 통과 — "그 기준 하나 때문에" NO_GO 임을 확인
    for name, c in gate["criteria"].items():
        if name == "survives_2x_cost":
            continue
        assert c["pass"] is True, f"{name} 가 예상외로 실패: {c}"
    assert "survives_2x_cost" not in gate["rationale"]  # label 로 치환돼 나간다
    assert "2배 비용" in gate["rationale"]


def test_unstable_folds_fail_fold_stability():
    folds = [_fold(net_bps=v, n_round_trips=40, mdd_pct=-5.0)
             for v in (30.0, -10.0, -20.0, 15.0, -5.0)]  # 5개 중 2개만 양수 (40%)
    gate = evaluate_gate(folds, _analytics(), trials=1, cost_bp=14.0, market="US")
    assert gate["verdict"] == "NO_GO"
    assert gate["criteria"]["fold_stability"]["pass"] is False


def test_worst_fold_mdd_too_deep_fails():
    folds = [_fold(net_bps=25.0, n_round_trips=40, mdd_pct=-20.0)]  # 20% > 15% 상한
    folds += [_fold(net_bps=25.0, n_round_trips=40, mdd_pct=-5.0) for _ in range(3)]
    gate = evaluate_gate(folds, _analytics(), trials=1, cost_bp=14.0, market="US")
    assert gate["criteria"]["worst_fold_mdd"]["pass"] is False


# ── 파라미터 안정성(선택) ────────────────────────────────────────────────────

def test_param_plateau_not_evaluated_when_absent():
    folds = [_fold(net_bps=25.0, n_round_trips=40) for _ in range(4)]
    gate = evaluate_gate(folds, _analytics(), trials=1, cost_bp=14.0, market="US")
    assert "param_plateau" not in gate["criteria"]


def test_param_plateau_passes_when_neighbors_hold_up():
    folds = [_fold(net_bps=20.0, n_round_trips=40) for _ in range(4)]  # center = 20bp
    gate = evaluate_gate(
        folds, _analytics(), trials=1, cost_bp=14.0, market="US",
        param_plateau={"lookback-10%": 18.0, "lookback+10%": 16.0},  # median 17 >= 10(50%)
    )
    assert gate["criteria"]["param_plateau"]["pass"] is True
    assert gate["verdict"] == "GO"


def test_param_plateau_fails_when_neighbors_collapse():
    folds = [_fold(net_bps=20.0, n_round_trips=40) for _ in range(4)]  # center = 20bp
    gate = evaluate_gate(
        folds, _analytics(), trials=1, cost_bp=14.0, market="US",
        param_plateau={"lookback-10%": 2.0, "lookback+10%": 1.0},  # median 1.5 < 10(50%)
    )
    assert gate["criteria"]["param_plateau"]["pass"] is False
    assert gate["verdict"] == "NO_GO"


# ── deflated Sharpe: 탐색 횟수를 늘리면 더 깐깐해진다 ────────────────────────

def test_more_trials_makes_deflated_sharpe_harder_to_pass():
    folds = [_fold(net_bps=25.0, n_round_trips=40) for _ in range(4)]
    analytics = _analytics(sharpe=0.10, n=150)
    low_trials = evaluate_gate(folds, analytics, trials=1, cost_bp=14.0, market="US")
    high_trials = evaluate_gate(folds, analytics, trials=500, cost_bp=14.0, market="US")
    assert low_trials["criteria"]["deflated_sharpe"]["value"] >= high_trials["criteria"]["deflated_sharpe"]["value"]


# ── 커스텀 임계값 ────────────────────────────────────────────────────────────

def test_custom_thresholds_are_honored():
    folds = [_fold(net_bps=25.0, n_round_trips=40) for _ in range(4)]  # 160건
    strict = GateThresholds(min_oos_trades=1000)
    gate = evaluate_gate(folds, _analytics(), trials=1, cost_bp=14.0, market="US",
                          thresholds=strict)
    assert gate["criteria"]["oos_n_trades"]["pass"] is False
    assert gate["verdict"] == "NO_GO"


def test_render_gate_smoke():
    folds = [_fold(net_bps=25.0, n_round_trips=40) for _ in range(4)]
    gate = evaluate_gate(folds, _analytics(), trials=1, cost_bp=14.0, market="US")
    text = render_gate(gate)
    assert "GO" in text
    assert "배포 게이트" in text
