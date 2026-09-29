from copy import deepcopy

from local.ml.prepare_labels import restore_baselines
from quant.analyze.ml_scorer import FEATURE_NAMES


def records():
    attrs = {name: 1.0 for name in FEATURE_NAMES}
    label = {**attrs, "market": "KR", "symbol": "A", "session_date": "2026-09-01", "return_bps": 12}
    source = {"market": "KR", "symbol": "A", "date": "2026-09-01", "attributes": {**attrs, "baseline_score100": 70}}
    return label, source


def test_only_matching_historical_inputs_restore_baseline_without_mutation():
    label, source = records()
    original = deepcopy(label)
    restored = restore_baselines([label], [source])
    assert label == original
    assert restored[0]["baseline_score100"] == 70
    assert restored[0]["baseline_source"] == "selection_exact_features"
    assert restored[0]["return_bps"] == 12


def test_different_features_or_conflicting_scores_remain_missing():
    label, source = records()
    changed = deepcopy(source)
    changed["attributes"][FEATURE_NAMES[0]] = 99
    assert "baseline_score100" not in restore_baselines([label], [changed])[0]
    conflict = deepcopy(source)
    conflict["attributes"]["baseline_score100"] = 30
    assert "baseline_score100" not in restore_baselines([label], [source, conflict])[0]


def test_market_date_and_existing_database_score_are_preserved():
    label, source = records()
    wrong = {**source, "market": "US"}
    assert "baseline_score100" not in restore_baselines([label], [wrong])[0]
    label["baseline_score100"] = 0
    assert restore_baselines([label], [source])[0]["baseline_score100"] == 0
