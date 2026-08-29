"""`quant.research.ml_train` — 로컬 맥 ML 파이프라인의 순수 로직(피처 조인,
거래일 단위 purged 분할, 표본 게이트)을 합성 데이터로 검증한다. sklearn이
필요한 부분(`evaluate_oos`/`fit_final_model`/`main` 학습 경로)은
`pytest.importorskip("sklearn")`으로 개별 스킵한다 — `research` 그룹 없이도
나머지(게이트 판정, 거래일 분할)는 항상 검증된다 (2026-08-30, local/ml/
원버튼 파이프라인 신설과 함께 추가).
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
        **feature_overrides) -> dict:
    row = {"session_date": session_date, "symbol": symbol, "market": market,
           "return_bps": return_bps}
    for i, name in enumerate(ml_scorer.FEATURE_NAMES):
        row[name] = feature_overrides.get(name, float(i + 1))
    return row


def _synthetic_rows(n_days: int, market: str = "KR", symbols_per_day: int = 3) -> list[dict]:
    """`n_days`개의 거래일 × `symbols_per_day`개 종목. 라벨은 relative_volume이
    양이면 양수 수익, 음이면 음수 수익으로 결정론적 신호를 심어 AUC가 0.5를
    넘는지 확인할 수 있게 한다."""
    rows = []
    for d in range(n_days):
        date = f"2026-{(d // 28) % 12 + 1:02d}-{d % 28 + 1:02d}"
        for s in range(symbols_per_day):
            rel_vol = 1.0 if (d + s) % 2 == 0 else -1.0
            rows.append(_row(date, f"SYM{s}", market=market,
                             return_bps=50.0 if rel_vol > 0 else -50.0,
                             relative_volume=rel_vol))
    return rows


# ------------------------------------------------------------------ 순수 로직

def test_rows_by_market_splits_on_market_column():
    rows = _synthetic_rows(3, market="KR") + _synthetic_rows(2, market="US")
    by_market = ml_train.rows_by_market(rows)
    assert {r["market"] for r in by_market["KR"]} == {"KR"}
    assert {r["market"] for r in by_market["US"]} == {"US"}
    assert len(by_market["KR"]) == 9
    assert len(by_market["US"]) == 6


def test_train_day_count_counts_distinct_days_not_rows():
    rows = _synthetic_rows(5, symbols_per_day=4)  # 20행, 5거래일
    assert ml_train.train_day_count(rows) == 5


def test_sorted_distinct_days_is_lexicographic_and_deduped():
    rows = [_row("2026-08-03", "A"), _row("2026-08-01", "B"), _row("2026-08-01", "C")]
    assert ml_train.sorted_distinct_days(rows) == ["2026-08-01", "2026-08-03"]


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


def test_render_report_md_always_ends_with_the_human_decision_line():
    """자동 반영 없음 — 리포트 마지막 줄은 항상 이 문구여야 한다(요구사항 원문)."""
    metrics = {
        "n_rows": 10, "n_days": 30, "base_rate": 0.5, "n_folds": 2,
        "folds": [{"auc": 0.6, "precision": 0.5, "n_train": 8, "n_test": 2, "skipped": None}],
        "mean_oos_auc": 0.6, "mean_oos_precision": 0.5, "hyperparam_search_trials": 0,
    }
    report = ml_train.render_report_md({"KR": {"metrics": metrics, "importances": {}}}, "2026-08-30")
    assert report.strip().splitlines()[-1] == "참전 제안은 사람이 결정: 리포트를 보고 판단하라."


def test_render_report_md_notes_skipped_markets():
    metrics = {
        "n_rows": 10, "n_days": 30, "base_rate": 0.5, "n_folds": 2,
        "folds": [], "mean_oos_auc": None, "mean_oos_precision": None,
        "hyperparam_search_trials": 0,
    }
    report = ml_train.render_report_md(
        {"KR": {"metrics": metrics, "importances": {}}}, "2026-08-30",
        skipped={"US": 11}, min_train_days=30,
    )
    assert "US 11/30" in report


# ------------------------------------------------------------------ main() 게이트 경로 (sklearn 불필요)

def test_main_prints_collecting_message_and_writes_nothing_when_under_threshold(tmp_path, capsys):
    rows = _synthetic_rows(5, market="KR") + _synthetic_rows(4, market="US")
    labeled = tmp_path / "labeled.json"
    labeled.write_text(json.dumps(rows), encoding="utf-8")
    out_dir = tmp_path / "out"

    rc = ml_train.main([
        "--labeled-json", str(labeled), "--out-dir", str(out_dir), "--min-train-days", "30",
    ])

    assert rc == 0
    captured = capsys.readouterr()
    assert "표본 수집 중" in captured.out
    assert "KR 5/30" in captured.out
    assert "US 4/30" in captured.out
    assert not (out_dir / "report.md").exists()
    assert not (out_dir / "summary.json").exists()


# ------------------------------------------------------------------ main() 학습 경로 (sklearn 필요)

def test_main_trains_and_writes_report_when_threshold_is_met(tmp_path, capsys):
    pytest.importorskip("sklearn")
    rows = _synthetic_rows(30, market="KR", symbols_per_day=4)
    labeled = tmp_path / "labeled.json"
    labeled.write_text(json.dumps(rows), encoding="utf-8")
    out_dir = tmp_path / "out"

    rc = ml_train.main([
        "--labeled-json", str(labeled), "--out-dir", str(out_dir),
        "--min-train-days", "30", "--markets", "KR",
    ])

    assert rc == 0
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "## KR" in report
    assert report.strip().splitlines()[-1] == "참전 제안은 사람이 결정: 리포트를 보고 판단하라."
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["KR"]["n_days"] == 30
    assert (out_dir / "model_KR.joblib").exists()


def test_evaluate_oos_reports_zero_hyperparameter_search_trials():
    pytest.importorskip("sklearn")
    rows = _synthetic_rows(30, market="KR", symbols_per_day=4)
    metrics = ml_train.evaluate_oos(rows, n_folds=3, label_horizon=1)
    assert metrics["hyperparam_search_trials"] == 0
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
