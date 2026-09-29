"""Frozen shadow scoring must preserve its training inputs and never backdate evidence."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("sklearn")
import joblib
from sklearn.linear_model import LinearRegression

from quant.analyze.ml_scorer import FEATURE_NAMES
from quant.control.judgment import selection_judgment


def runner():
    path = Path(__file__).resolve().parents[1] / "server/scripts/ml_shadow.py"
    assert path.exists(), "shadow scoring runner is not implemented"
    spec = importlib.util.spec_from_file_location("ml_shadow", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def artifacts(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    X = np.zeros((4, len(FEATURE_NAMES)))
    X[:, 0] = [0, 10, 20, 30]
    model = LinearRegression().fit(X, [0, 20, 40, 60])
    model_path = model_dir / "model_KR_d1_return_bps.joblib"
    joblib.dump(model, model_path)
    manifest = {
        "market": "KR", "target": "d1_return_bps",
        "feature_names": list(FEATURE_NAMES),
        "imputation_medians": [7.0] + [0.0] * (len(FEATURE_NAMES) - 1),
        "train_start": "2026-08-14", "train_end": "2026-09-28",
        "n_train_rows": 100, "evaluation_protocol": "expanding_walk_forward",
        "hyperparams": {},
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
    }
    manifest_path = model_dir / "model_KR_d1_return_bps.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return model_dir, manifest_path, manifest


def write_candidates(root, rows):
    path = root / "data/ledger/selections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def row(symbol, score=None, baseline=50, **kwargs):
    return {"date": "2026-09-29", "market": "KR", "symbol": symbol,
            "ai_score100": score, "baseline_score100": baseline, **kwargs}


def test_comparison_uses_same_candidates_and_preprocessing_changes_version(tmp_path, artifacts):
    model_dir, path, manifest = artifacts
    root = tmp_path / "repo"
    write_candidates(root, [row("A", None, 60), row("B", 100, None), row("C", 3, 20)])
    first = runner().run_shadow(root, model_dir, "KR", "2026-09-29")
    assert {r["symbol"] for r in first["comparison_ml_top5"]} == {"A", "C"}
    manifest["imputation_medians"][0] = 9
    path.write_text(json.dumps(manifest))
    second = runner().run_shadow(root, model_dir, "KR", "2026-09-29")
    assert first["producer_version"] != second["producer_version"]


def test_label_available_date_must_precede_prediction_date(tmp_path, artifacts):
    model_dir, path, manifest = artifacts
    manifest["training_labels_available_through"] = "2026-09-29"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="label"):
        runner().run_shadow(tmp_path / "repo", model_dir, "KR", "2026-09-29")


@pytest.mark.parametrize("train_end", ["2026-09-29", "2026-09-30"])
def test_rejects_model_trained_on_prediction_day_or_future(tmp_path, artifacts, train_end):
    model_dir, path, manifest = artifacts
    manifest["train_end"] = train_end
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="train_end"):
        runner().run_shadow(tmp_path, model_dir, "KR", "2026-09-29")
    assert not (tmp_path / "data/ledger/judgments.jsonl").exists()


def test_rejects_changed_model_before_deserialization(tmp_path, artifacts):
    model_dir, _, _ = artifacts
    (model_dir / "model_KR_d1_return_bps.joblib").write_bytes(b"not a pickle")
    with pytest.raises(ValueError, match="SHA-256"):
        runner().run_shadow(tmp_path, model_dir, "KR", "2026-09-29")


@pytest.mark.parametrize("field,value", [
    ("feature_names", list(reversed(FEATURE_NAMES))),
    ("imputation_medians", [7.0]),
    ("market", "US"),
])
def test_rejects_incompatible_manifest(tmp_path, artifacts, field, value):
    model_dir, path, manifest = artifacts
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        runner().run_shadow(tmp_path, model_dir, "KR", "2026-09-29")


def test_uses_training_imputer_records_same_input_and_is_idempotent(tmp_path, artifacts):
    model_dir, _, manifest = artifacts
    candidates = [row("A", None, 10), row("B", 20, 90)]
    write_candidates(tmp_path, candidates + [row("B", 999), row("X", 900, producer="other"),
                                             row("Y", 800, market="US"), row("Z", 700, date="2026-09-28")])
    report = runner().run_shadow(tmp_path, model_dir, "KR", "2026-09-29")
    predictions = {item["symbol"]: item for item in report["predictions"]}
    assert set(predictions) == {"A", "B"}
    assert predictions["A"]["predicted_return_bps"] == pytest.approx(14)
    assert predictions["B"]["predicted_return_bps"] == pytest.approx(40)
    assert predictions["A"]["features"]["ai_score100"] is None
    assert predictions["A"]["imputed_features"]["ai_score100"] == 7
    ledger = tmp_path / "data/ledger/judgments.jsonl"
    before = ledger.read_bytes()
    judgments = [json.loads(line) for line in before.splitlines()]
    assert len(judgments) == 2
    assert judgments[0]["input_hash"] == selection_judgment(candidates[0], "2").input_hash
    assert judgments[0]["producer"] == "ml_shadow"
    expected_bundle = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    assert judgments[0]["producer_version"] == expected_bundle[:12]
    assert [j["score"] for j in judgments] == [0, 100]
    assert report["ml_top5"][0]["symbol"] == "B"
    assert report["baseline_top5"][0]["symbol"] == "B"
    runner().run_shadow(tmp_path, model_dir, "KR", "2026-09-29")
    assert ledger.read_bytes() == before
    assert (tmp_path / "data/ml_shadow/2026-09-29_KR.md").exists()
    assert (tmp_path / "data/ml_shadow/2026-09-29_KR.json").exists()


def test_missing_baseline_is_disclosed_and_no_candidates_create_no_judgments(tmp_path, artifacts):
    model_dir, _, _ = artifacts
    write_candidates(tmp_path, [row("A", 1, None)])
    report = runner().run_shadow(tmp_path, model_dir, "KR", "2026-09-29")
    assert report["baseline_top5"] == []
    assert report["baseline_missing_count"] == 1
    assert report["overlap_symbols"] == []
    write_candidates(tmp_path, [])
    before = (tmp_path / "data/ledger/judgments.jsonl").read_bytes()
    report = runner().run_shadow(tmp_path, model_dir, "KR", "2026-09-30")
    assert report["predictions"] == []
    assert (tmp_path / "data/ledger/judgments.jsonl").read_bytes() == before
