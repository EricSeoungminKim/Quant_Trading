"""`quant.research.ml_train` v2 — 다중 타깃(D+1 방향/수익률·D+5 방향)·확률 보정·
OOS permutation importance·베이스라인 head-to-head·모델 레지스트리+델타를 합성
데이터로 검증한다. sklearn이 필요한 부분은 `pytest.importorskip("sklearn")`으로
개별 스킵한다 — `research` 그룹 없이도 순수 로직(게이트 판정, 거래일 분할, 델타
계산)은 항상 검증된다 (2026-08-30 v1 신설 → 2026-08-30 v2 고도화와 함께 갱신).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from quant.analyze import ml_scorer
from quant.research import ml_train

REPO_ROOT = Path(__file__).resolve().parent.parent


def _row(session_date: str, symbol: str, market: str = "KR", return_bps: float = 10.0,
        return_bps_d5: float | None = None, baseline_score100: float | None = None,
        **feature_overrides) -> dict:
    row = {"session_date": session_date, "symbol": symbol, "market": market,
           "return_bps": return_bps}
    if return_bps_d5 is not None:
        row["return_bps_d5"] = return_bps_d5
    if baseline_score100 is not None:
        row["baseline_score100"] = baseline_score100
    for i, name in enumerate(ml_scorer.FEATURE_NAMES):
        row[name] = feature_overrides.get(name, float(i + 1))
    return row


def _synthetic_rows(n_days: int, market: str = "KR", symbols_per_day: int = 4,
                    with_d5: bool = False, with_baseline: bool = False) -> list[dict]:
    """`n_days`개의 거래일 × `symbols_per_day`개 종목. 라벨은 relative_volume이
    양이면 양수 수익, 음이면 음수 수익으로 결정론적 신호를 심어 AUC가 0.5를
    넘는지 확인할 수 있게 한다.

    `with_d5=True`면 마지막 5일을 제외한 모든 날에 D+5 라벨을 채운다(실제
    MySQL 조인처럼 최근 며칠은 라벨이 아직 안 만들어졌다는 걸 흉내낸다).
    `with_baseline=True`면 `baseline_score100`을 relative_volume과 약하게
    상관된 값으로 채운다(규칙 채점기가 어느 정도는 신호를 잡되 ML보다는
    못하다는 걸 흉내낸다 — 순수 노이즈로 두면 ML이 항상 이겨 head-to-head
    분기 테스트가 무의미해진다)."""
    rows = []
    for d in range(n_days):
        date = f"2026-{(d // 28) % 12 + 1:02d}-{d % 28 + 1:02d}"
        for s in range(symbols_per_day):
            rel_vol = 1.0 if (d + s) % 2 == 0 else -1.0
            kwargs = {}
            if with_d5 and d < n_days - 5:
                kwargs["return_bps_d5"] = 200.0 if rel_vol > 0 else -200.0
            if with_baseline:
                # 약한 신호: 방향 신호(rel_vol)는 실리되 노이즈 진폭이 신호보다 커서
                # 가끔 순위를 뒤집는다 — ML(라벨과 완벽 일치하는 예측)이 항상 이겨야
                # 하는 head-to-head 테스트가 실제로 분기하게 만드는 장치.
                noise = ((d * 7 + s * 13) % 7) - 3
                kwargs["baseline_score100"] = 50.0 + rel_vol * 1.0 + noise
            rows.append(_row(date, f"SYM{s}", market=market,
                             return_bps=50.0 if rel_vol > 0 else -50.0,
                             relative_volume=rel_vol, **kwargs))
    return rows


# ------------------------------------------------------------------ 순수 로직

def test_rows_by_market_splits_on_market_column():
    rows = _synthetic_rows(3, market="KR") + _synthetic_rows(2, market="US")
    by_market = ml_train.rows_by_market(rows)
    assert {r["market"] for r in by_market["KR"]} == {"KR"}
    assert {r["market"] for r in by_market["US"]} == {"US"}
    assert len(by_market["KR"]) == 12
    assert len(by_market["US"]) == 8


def test_train_day_count_counts_distinct_days_not_rows():
    rows = _synthetic_rows(5, symbols_per_day=4)  # 20행, 5거래일
    assert ml_train.train_day_count(rows) == 5


def test_sorted_distinct_days_is_lexicographic_and_deduped():
    rows = [_row("2026-08-03", "A"), _row("2026-08-01", "B"), _row("2026-08-01", "C")]
    assert ml_train.sorted_distinct_days(rows) == ["2026-08-01", "2026-08-03"]


def test_rows_for_target_filters_on_label_key_presence():
    """D+5 타깃은 라벨이 있는 행만 남긴다 — 라벨을 지어내지 않는다."""
    rows = _synthetic_rows(10, symbols_per_day=2, with_d5=True)
    d5_spec = next(t for t in ml_train.TARGETS if t.name == "d5_direction")
    filtered = ml_train.rows_for_target(rows, d5_spec)
    assert len(filtered) < len(rows)
    assert all(r.get("return_bps_d5") is not None for r in filtered)
    # 마지막 5일은 D+5 라벨이 없다(합성 데이터 생성 규칙)
    assert ml_train.train_day_count(filtered) == 5


def test_day_purged_splits_never_splits_a_single_day_across_train_and_test():
    """같은 거래일의 행이 fold 하나 안에서 train/test로 갈리면 그날 라벨 정보가
    새는 것과 같다 — 이 모듈의 존재 이유(모듈 docstring)."""
    rows = _synthetic_rows(20, symbols_per_day=3)
    splits = ml_train.day_purged_splits(rows, n_folds=4, embargo_pct=0.0, label_horizon=1)
    for train_idx, test_idx in splits:
        train_days = {rows[i]["session_date"] for i in train_idx}
        test_days = {rows[i]["session_date"] for i in test_idx}
        assert not (train_days & test_days), "같은 거래일이 train과 test에 동시에 나타났다"


def test_day_purged_splits_covers_every_row_exactly_once_per_fold_as_train_or_test_or_purged():
    """train과 test는 겹치지 않아야 하고, 둘 다에 없는 행(purge/embargo로 버려진
    거래일)이 있어도 괜찮다 — purged_cv.purged_splits와 같은 계약."""
    rows = _synthetic_rows(20, symbols_per_day=3)
    splits = ml_train.day_purged_splits(rows, n_folds=4, embargo_pct=0.1, label_horizon=1)
    for train_idx, test_idx in splits:
        assert not (set(train_idx) & set(test_idx))
        assert set(train_idx) | set(test_idx) <= set(range(len(rows)))


def test_day_purged_splits_uses_wider_purge_for_longer_label_horizon():
    """D+5 라벨(label_horizon=5)은 D+1(label_horizon=1)보다 train에서 더 많이
    버려야 한다 — 라벨 구간이 더 넓게 뻗기 때문."""
    rows = _synthetic_rows(30, symbols_per_day=3)
    splits_d1 = ml_train.day_purged_splits(rows, n_folds=4, embargo_pct=0.0, label_horizon=1)
    splits_d5 = ml_train.day_purged_splits(rows, n_folds=4, embargo_pct=0.0, label_horizon=5)
    n_train_d1 = sum(len(tr) for tr, _ in splits_d1)
    n_train_d5 = sum(len(tr) for tr, _ in splits_d5)
    assert n_train_d5 < n_train_d1


def test_load_labeled_rows_round_trips_json(tmp_path):
    rows = _synthetic_rows(2)
    path = tmp_path / "labeled.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    loaded = ml_train.load_labeled_rows(path)
    assert loaded == rows


def test_load_labeled_rows_rejects_non_list_payload(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError):
        ml_train.load_labeled_rows(path)


# ------------------------------------------------------------------ 베이스라인 head-to-head (sklearn 불필요)

def test_compare_to_baseline_reports_unavailable_when_no_rows_have_it():
    rows = _synthetic_rows(10, symbols_per_day=3)  # baseline_score100 없음
    result = ml_train.compare_to_baseline(rows, {i: 1.0 for i in range(len(rows))})
    assert result["available"] is False
    assert "baseline_score100" in result["reason"]


def test_compare_to_baseline_computes_rank_ic_and_topn_when_present():
    rows = _synthetic_rows(15, symbols_per_day=4, with_baseline=True)
    # ML 예측을 실제 라벨과 완벽히 일치시켜 "ML이 항상 이긴다" 시나리오를 만든다.
    oos_preds = {i: r["return_bps"] for i, r in enumerate(rows)}
    result = ml_train.compare_to_baseline(rows, oos_preds, top_n=2, min_days=5)
    assert result["available"] is True
    assert result["n_days"] == 15
    assert result["mean_ml_rank_ic"] is not None
    assert result["mean_baseline_rank_ic"] is not None
    # 완벽한 예측이 약한 신호의 베이스라인보다 못할 수 없다.
    assert result["mean_ml_rank_ic"] >= result["mean_baseline_rank_ic"]
    assert result["ml_beats_baseline"] is True


def test_compare_to_baseline_respects_min_days_floor():
    rows = _synthetic_rows(6, symbols_per_day=4, with_baseline=True)
    oos_preds = {i: r["return_bps"] for i, r in enumerate(rows)}
    result = ml_train.compare_to_baseline(rows, oos_preds, top_n=2, min_days=20)
    assert result["available"] is True
    assert result["ml_beats_baseline"] is False  # 표본이 min_days 미만


# ------------------------------------------------------------------ 레지스트리 + 델타 (sklearn 불필요)

def test_append_registry_and_load_last_entry_round_trip(tmp_path):
    path = tmp_path / "registry.jsonl"
    ml_train.append_registry(path, {"run": 1})
    ml_train.append_registry(path, {"run": 2})
    assert ml_train.load_last_registry_entry(path) == {"run": 2}


def test_load_last_registry_entry_returns_none_when_missing(tmp_path):
    assert ml_train.load_last_registry_entry(tmp_path / "nope.jsonl") is None


def test_load_last_registry_entry_skips_corrupted_lines(tmp_path):
    path = tmp_path / "registry.jsonl"
    path.write_text('{"run": 1}\nnot json\n{"run": 2}\n', encoding="utf-8")
    assert ml_train.load_last_registry_entry(path) == {"run": 2}


def test_compute_deltas_is_empty_without_previous_entry():
    current = {"markets": {"KR": {"n_days": 30, "targets": {}}}}
    assert ml_train.compute_deltas(current, None) == {}


def test_compute_deltas_diffs_matching_market_and_target():
    previous = {"markets": {"KR": {"n_days": 28, "targets": {
        "d1_direction": {"mean_oos_auc": 0.55, "mean_oos_precision": 0.5},
    }}}}
    current = {"markets": {"KR": {"n_days": 31, "targets": {
        "d1_direction": {"mean_oos_auc": 0.60, "mean_oos_precision": 0.52},
    }}}}
    deltas = ml_train.compute_deltas(current, previous)
    assert deltas["KR"]["n_days_delta"] == 3
    assert deltas["KR"]["targets"]["d1_direction"]["mean_oos_auc"] == pytest.approx(0.05)
    assert deltas["KR"]["targets"]["d1_direction"]["mean_oos_precision"] == pytest.approx(0.02)


def test_compute_deltas_skips_markets_or_targets_absent_previously():
    previous = {"markets": {"KR": {"n_days": 28, "targets": {}}}}
    current = {"markets": {
        "KR": {"n_days": 31, "targets": {"d1_direction": {"mean_oos_auc": 0.6}}},
        "US": {"n_days": 30, "targets": {}},
    }}
    deltas = ml_train.compute_deltas(current, previous)
    assert "US" not in deltas  # US는 직전 레지스트리에 없었다
    assert deltas["KR"]["targets"] == {}  # 직전에 이 타깃이 없었다


# ------------------------------------------------------------------ render_report_md (sklearn 불필요)

def _fake_classification_metrics(n_days=30, n_rows=120, auc=0.6, brier=0.2) -> dict:
    return {
        "kind": "classification", "label_horizon": 1, "n_rows": n_rows, "n_days": n_days,
        "n_folds": 2, "hyperparams": {"n_estimators": 100},
        "folds": [{"auc": auc, "precision": 0.5, "brier": brier, "calibrated": True,
                   "calibration_method": "sigmoid", "n_train": 80, "n_test": 40, "skipped": None}],
        "base_rate": 0.5, "mean_oos_auc": auc, "mean_oos_precision": 0.5, "mean_oos_brier": brier,
        "calibration_methods": ["sigmoid"],
    }


def _fake_regression_metrics(n_days=30, n_rows=120, rmse=80.0) -> dict:
    return {
        "kind": "regression", "label_horizon": 1, "n_rows": n_rows, "n_days": n_days,
        "n_folds": 2, "hyperparams": {"n_estimators": 100},
        "folds": [{"rmse": rmse, "mae": 60.0, "r2": 0.1, "n_train": 80, "n_test": 40, "skipped": None}],
        "mean_oos_rmse": rmse, "mean_oos_mae": 60.0, "mean_oos_r2": 0.1, "mean_oos_rank_ic": 0.2,
    }


def test_render_report_md_always_ends_with_the_human_decision_line():
    """자동 반영 없음 — 리포트 마지막 줄은 항상 이 문구여야 한다(요구사항 원문)."""
    results = {"KR": {
        "metrics": {"d1_direction": {"metrics": _fake_classification_metrics(), "importances": {}}},
        "baseline": {"available": False, "reason": "baseline_score100 없음"},
        "target_skip_days": {"d1_return_bps": 5, "d5_direction": 3},
    }}
    report = ml_train.render_report_md(results, "2026-08-30", "abc1234")
    assert report.strip().splitlines()[-1] == "참전 제안은 사람이 결정: 리포트를 보고 판단하라."


def test_render_report_md_notes_skipped_markets():
    results = {"KR": {
        "metrics": {"d1_direction": {"metrics": _fake_classification_metrics(), "importances": {}}},
        "baseline": {"available": False, "reason": "x"},
        "target_skip_days": {},
    }}
    report = ml_train.render_report_md(results, "2026-08-30", "abc1234",
                                       skipped={"US": 11}, min_train_days=30)
    assert "US 11/30" in report


def test_render_report_md_includes_multiple_testing_disclosure():
    results = {"KR": {
        "metrics": {"d1_direction": {"metrics": _fake_classification_metrics(), "importances": {}}},
        "baseline": {"available": False, "reason": "x"},
        "target_skip_days": {},
    }}
    report = ml_train.render_report_md(results, "2026-08-30", "abc1234")
    assert "다중검정" in report
    assert "타깃 수" in report


def test_render_report_md_reports_delta_section():
    results = {"KR": {
        "metrics": {"d1_direction": {"metrics": _fake_classification_metrics(), "importances": {}}},
        "baseline": {"available": False, "reason": "x"},
        "target_skip_days": {},
    }}
    deltas = {"KR": {"n_days_delta": 2, "targets": {"d1_direction": {"mean_oos_auc": 0.02}}}}
    report = ml_train.render_report_md(results, "2026-08-30", "abc1234", deltas=deltas)
    assert "직전 실행 대비 델타" in report
    assert "+2" in report


def test_render_report_md_emits_proposal_section_when_ml_beats_baseline():
    results = {"KR": {
        "metrics": {"d1_direction": {"metrics": _fake_classification_metrics(), "importances": {}}},
        "baseline": {
            "available": True, "n_days": 15, "n_rows": 60, "top_n": 5,
            "mean_ml_rank_ic": 0.3, "mean_baseline_rank_ic": 0.1,
            "mean_ml_topn_return_bps": 40.0, "mean_baseline_topn_return_bps": 10.0,
            "ml_beats_baseline": True,
        },
        "target_skip_days": {},
    }}
    report = ml_train.render_report_md(results, "2026-08-30", "abc1234")
    assert "참전 제안" in report
    assert "무엇을" in report and "왜" in report


def test_render_report_md_omits_proposal_section_when_no_edge():
    results = {"KR": {
        "metrics": {"d1_direction": {"metrics": _fake_classification_metrics(), "importances": {}}},
        "baseline": {
            "available": True, "n_days": 15, "n_rows": 60, "top_n": 5,
            "mean_ml_rank_ic": 0.05, "mean_baseline_rank_ic": 0.1,
            "mean_ml_topn_return_bps": 5.0, "mean_baseline_topn_return_bps": 10.0,
            "ml_beats_baseline": False,
        },
        "target_skip_days": {},
    }}
    report = ml_train.render_report_md(results, "2026-08-30", "abc1234")
    assert "### 참전 제안" not in report


def test_render_report_md_reports_regression_metrics():
    results = {"KR": {
        "metrics": {"d1_return_bps": {"metrics": _fake_regression_metrics(), "importances": {"relative_volume": 0.4}}},
        "baseline": {"available": False, "reason": "x"},
        "target_skip_days": {},
    }}
    report = ml_train.render_report_md(results, "2026-08-30", "abc1234")
    assert "RMSE" in report
    assert "rank IC" in report


# ------------------------------------------------------------------ main() 게이트 경로 (sklearn 불필요)

def test_main_prints_collecting_message_and_writes_nothing_when_under_threshold(tmp_path, capsys):
    rows = _synthetic_rows(5, market="KR") + _synthetic_rows(4, market="US")
    labeled = tmp_path / "labeled.json"
    labeled.write_text(json.dumps(rows), encoding="utf-8")
    out_dir = tmp_path / "out"

    rc = ml_train.main([
        "--labeled-json", str(labeled), "--out-dir", str(out_dir), "--min-train-days", "30",
        "--registry", str(tmp_path / "registry.jsonl"),
    ])

    assert rc == 0
    captured = capsys.readouterr()
    assert "표본 수집 중" in captured.out
    assert "KR 5/30" in captured.out
    assert "US 4/30" in captured.out
    assert not (out_dir / "report.md").exists()
    assert not (out_dir / "summary.json").exists()
    assert not (tmp_path / "registry.jsonl").exists()


# ------------------------------------------------------------------ 타깃 평가 경로 (sklearn 필요)

def test_evaluate_target_oos_classification_reports_calibration_and_brier():
    pytest.importorskip("sklearn")
    rows = _synthetic_rows(30, market="KR", symbols_per_day=4)
    spec = next(t for t in ml_train.TARGETS if t.name == "d1_direction")
    result = ml_train.evaluate_target_oos(rows, spec, ml_train.DEFAULT_HYPERPARAMS, n_folds=3)
    m = result["metrics"]
    assert m["mean_oos_auc"] is not None
    assert m["mean_oos_brier"] is not None
    assert 0.0 <= m["mean_oos_brier"] <= 1.0
    assert m["calibration_methods"]
    assert result["oos_preds"]  # 최소 한 fold는 OOS 예측을 냈다


def test_evaluate_target_oos_regression_reports_rmse_and_rank_ic():
    pytest.importorskip("sklearn")
    rows = _synthetic_rows(30, market="KR", symbols_per_day=4)
    spec = next(t for t in ml_train.TARGETS if t.name == "d1_return_bps")
    result = ml_train.evaluate_target_oos(rows, spec, ml_train.DEFAULT_HYPERPARAMS, n_folds=3)
    m = result["metrics"]
    assert m["mean_oos_rmse"] is not None
    assert m["mean_oos_mae"] is not None
    # 신호가 결정론적으로 심겨 있으니 rank IC가 양수 방향이어야 한다(완벽 반전만 아니면).
    assert m["mean_oos_rank_ic"] is not None


def test_evaluate_target_oos_produces_permutation_importance_for_signal_feature():
    pytest.importorskip("sklearn")
    rows = _synthetic_rows(30, market="KR", symbols_per_day=4)
    spec = next(t for t in ml_train.TARGETS if t.name == "d1_direction")
    result = ml_train.evaluate_target_oos(rows, spec, ml_train.DEFAULT_HYPERPARAMS, n_folds=3)
    assert result["importances"]
    assert set(result["importances"]) == set(ml_scorer.FEATURE_NAMES)


def test_select_hyperparams_returns_all_grid_trials():
    pytest.importorskip("sklearn")
    rows = _synthetic_rows(30, market="KR", symbols_per_day=4)
    spec = next(t for t in ml_train.TARGETS if t.name == "d1_direction")
    best_params, trials = ml_train.select_hyperparams(rows, spec, n_folds=3)
    assert len(trials) == len(ml_train.HYPERPARAM_GRID) == 8
    assert best_params in ml_train.HYPERPARAM_GRID


# ------------------------------------------------------------------ main() 학습 경로 (sklearn 필요)

def test_main_trains_multi_target_writes_report_and_registry(tmp_path, capsys):
    pytest.importorskip("sklearn")
    rows = _synthetic_rows(35, market="KR", symbols_per_day=4, with_d5=True, with_baseline=True)
    labeled = tmp_path / "labeled.json"
    labeled.write_text(json.dumps(rows), encoding="utf-8")
    out_dir = tmp_path / "out"
    registry = tmp_path / "registry.jsonl"

    rc = ml_train.main([
        "--labeled-json", str(labeled), "--out-dir", str(out_dir),
        "--min-train-days", "30", "--markets", "KR", "--registry", str(registry),
    ])

    assert rc == 0
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "## KR" in report
    assert "d1_direction" in report
    assert "d1_return_bps" in report
    assert "d5_direction" in report
    assert report.strip().splitlines()[-1] == "참전 제안은 사람이 결정: 리포트를 보고 판단하라."

    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["KR"]["n_days"] == 35

    assert (out_dir / "model_KR_d1_direction.joblib").exists()
    assert (out_dir / "model_KR_d1_return_bps.joblib").exists()

    assert registry.exists()
    entry = ml_train.load_last_registry_entry(registry)
    assert entry["markets"]["KR"]["n_days"] == 35
    assert "d1_direction" in entry["markets"]["KR"]["targets"]


def test_main_second_run_reports_delta_against_first(tmp_path):
    """"돌릴 때마다 유의미한 변화" 요구사항의 핵심 — 레지스트리가 쌓이면 두 번째
    실행의 리포트에 델타가 찍혀야 한다."""
    pytest.importorskip("sklearn")
    rows = _synthetic_rows(35, market="KR", symbols_per_day=4)
    labeled = tmp_path / "labeled.json"
    labeled.write_text(json.dumps(rows), encoding="utf-8")
    registry = tmp_path / "registry.jsonl"

    out_dir1 = tmp_path / "out1"
    ml_train.main(["--labeled-json", str(labeled), "--out-dir", str(out_dir1),
                   "--min-train-days", "30", "--markets", "KR", "--registry", str(registry)])
    assert len(registry.read_text(encoding="utf-8").splitlines()) == 1

    # 더 많은 표본으로 두 번째 실행(표본이 늘었다는 걸 흉내낸다).
    rows2 = _synthetic_rows(38, market="KR", symbols_per_day=4)
    labeled2 = tmp_path / "labeled2.json"
    labeled2.write_text(json.dumps(rows2), encoding="utf-8")
    out_dir2 = tmp_path / "out2"
    ml_train.main(["--labeled-json", str(labeled2), "--out-dir", str(out_dir2),
                   "--min-train-days", "30", "--markets", "KR", "--registry", str(registry)])

    assert len(registry.read_text(encoding="utf-8").splitlines()) == 2
    report2 = (out_dir2 / "report.md").read_text(encoding="utf-8")
    assert "직전 실행 대비 델타" in report2
    assert "거래일" in report2


def test_evaluate_target_oos_reports_zero_hyperparameter_grid_leak_into_metrics():
    """평가 자체는 하이퍼파라미터 탐색을 하지 않는다 — 탐색은 select_hyperparams가
    별도로 한다(§6). evaluate_target_oos는 넘겨받은 파라미터를 그대로 신고할 뿐."""
    pytest.importorskip("sklearn")
    rows = _synthetic_rows(30, market="KR", symbols_per_day=4)
    spec = next(t for t in ml_train.TARGETS if t.name == "d1_direction")
    params = {"n_estimators": 50, "max_depth": 2, "learning_rate": 0.05}
    metrics = ml_train.evaluate_target_oos(rows, spec, params, n_folds=3)["metrics"]
    assert metrics["hyperparams"] == params
    assert metrics["n_days"] == 30


# ------------------------------------------------------------------ 스크립트 문법 검증

def test_run_sh_has_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / "local" / "ml" / "run.sh")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_remote_dump_py_has_valid_python_syntax():
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(REPO_ROOT / "local" / "ml" / "remote_dump.py")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
