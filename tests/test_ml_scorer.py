"""학습형 선정자 `ml_scorer`(`quant/analyze/ml_scorer.py`) — 2026-08-28.

고정하는 계약:
① **표본 게이트** — 독립 거래일이 `min_train_days` 미만이면 학습도 예측도
   하지 않는다. 2026-08-28 실측(MySQL selection 1,393행/forward_return D+1
   883행, 거래일 10일뿐)이 이 게이트가 왜 필요한지의 근거다.
② **워크포워드** — 예측일 이후 데이터는 학습에 절대 섞이지 않는다.
③ **릿지** — numpy 만으로 표준화+릿지가 알려진 선형관계를 복원한다.
④ **결측 중앙값 대체** — 학습 중앙값을 예측 데이터에도 그대로 적용한다.
⑤ **퍼센타일 점수** — 0~100 경계, 동률은 평균 순위.
"""
from __future__ import annotations

import numpy as np
import pytest

from quant.analyze.ml_scorer import (
    FEATURE_NAMES,
    MIN_TRAIN_DAYS,
    enough_sample,
    feature_vector,
    fill_missing,
    fit_ridge,
    impute_median,
    predict_scores,
    to_judgments,
    to_matrix,
    to_percentile_scores,
    training_rows_before,
)
from quant.control.judgment import selection_judgment


# ---------------------------------------------------------------- ① 표본 게이트

def test_enough_sample_gate_at_default_threshold():
    assert MIN_TRAIN_DAYS == 30, "실측 근거(거래일 10일/특성 13개)가 이 값을 정당화한다"
    assert enough_sample(29) is False
    assert enough_sample(30) is True
    assert enough_sample(10) is False, "2026-08-28 실측 표본(거래일 10일)은 게이트를 통과 못 한다"


def test_enough_sample_respects_custom_threshold():
    assert enough_sample(5, min_days=5) is True
    assert enough_sample(4, min_days=5) is False


# ---------------------------------------------------------------- ② 워크포워드

def test_training_rows_before_excludes_predict_date_and_future():
    rows = [
        {"session_date": "2026-08-20", "return_bps": 10.0},
        {"session_date": "2026-08-27", "return_bps": 20.0},
        {"session_date": "2026-08-28", "return_bps": 999.0},  # 예측 대상 당일
        {"session_date": "2026-08-29", "return_bps": 999.0},  # 미래
    ]
    out = training_rows_before(rows, "2026-08-28")
    dates = {r["session_date"] for r in out}
    assert dates == {"2026-08-20", "2026-08-27"}
    assert all(r["return_bps"] != 999.0 for r in out), "예측일 이후 값이 섞이면 안 된다"


def test_training_rows_before_empty_when_all_future():
    rows = [{"session_date": "2026-09-01", "return_bps": 1.0}]
    assert training_rows_before(rows, "2026-08-28") == []


# ---------------------------------------------------------------- ③ 릿지 회귀

def test_fit_ridge_recovers_known_linear_relationship():
    """노이즈 없는 선형관계 + 낮은 λ 면 부호·상대크기가 복원돼야 한다."""
    rng = np.random.default_rng(42)
    n, p = 300, len(FEATURE_NAMES)
    X = rng.normal(size=(n, p))
    true_w = np.zeros(p)
    true_w[0] = 3.0
    true_w[1] = -2.0
    # 표준화된 X 기준으로 y 를 만든다(fit_ridge 내부 표준화와 스케일을 맞추기 위해
    # 이미 평균0/표준편차1인 X 를 그대로 쓴다).
    y = X @ true_w

    model = fit_ridge(X, y, lam=0.01)
    w = model["weights"]
    assert w[0] > 2.0, "가장 강한 양의 계수가 복원돼야 한다"
    assert w[1] < -1.0, "가장 강한 음의 계수가 복원돼야 한다"
    for i in range(2, p):
        assert abs(w[i]) < 0.5, "관계 없는 특성의 계수는 0 근처여야 한다"


def test_fit_ridge_shrinks_more_with_higher_lambda():
    rng = np.random.default_rng(7)
    n, p = 50, len(FEATURE_NAMES)
    X = rng.normal(size=(n, p))
    y = X[:, 0] * 5.0 + rng.normal(scale=0.1, size=n)

    w_low = fit_ridge(X, y, lam=0.1)["weights"]
    w_high = fit_ridge(X, y, lam=100.0)["weights"]
    assert np.linalg.norm(w_high) < np.linalg.norm(w_low), "λ 가 클수록 계수가 더 수축돼야 한다"


def test_predict_scores_uses_training_mean_std_not_new_data():
    X_train = np.array([[1.0], [2.0], [3.0], [4.0]])
    y_train = np.array([10.0, 20.0, 30.0, 40.0])
    model = fit_ridge(X_train, y_train, lam=0.001)
    # 학습 범위 밖 신규 데이터라도 학습 통계로만 표준화해야 한다(재계산 금지).
    preds = predict_scores(model, np.array([[5.0]]))
    assert preds[0] > 40.0, "학습 관계를 외삽하면 40 을 넘는 값이 나와야 한다"


# ---------------------------------------------------------------- ④ 결측 중앙값 대체

def test_impute_median_fills_nan_with_column_median():
    X = np.array([
        [1.0, np.nan],
        [3.0, 5.0],
        [np.nan, 7.0],
    ])
    out, medians = impute_median(X)
    assert not np.isnan(out).any()
    assert medians[0] == pytest.approx(2.0)  # median(1,3)
    assert medians[1] == pytest.approx(6.0)  # median(5,7)
    assert out[0, 1] == pytest.approx(6.0)
    assert out[2, 0] == pytest.approx(2.0)


def test_impute_median_all_nan_column_falls_back_to_zero():
    X = np.array([[np.nan], [np.nan]])
    out, medians = impute_median(X)
    assert medians[0] == 0.0
    assert (out == 0.0).all()


def test_fill_missing_applies_given_medians_to_new_data():
    """예측 시점엔 학습 중앙값을 그대로 적용해야 한다(자기 자신 재계산 금지)."""
    medians = np.array([100.0, 200.0])
    X_new = np.array([[np.nan, 1.0], [2.0, np.nan]])
    out = fill_missing(X_new, medians)
    assert out[0, 0] == 100.0
    assert out[1, 1] == 200.0
    assert out[0, 1] == 1.0 and out[1, 0] == 2.0


def test_feature_vector_missing_key_is_nan_not_zero():
    v = feature_vector({"ai_score100": 50})
    assert np.isnan(v[FEATURE_NAMES.index("change_pct")]), "없는 속성은 0 이 아니라 NaN(결측)이다"
    assert v[FEATURE_NAMES.index("ai_score100")] == 50.0


def test_to_matrix_shape_matches_feature_names():
    rows = [{"ai_score100": 1}, {"ai_score100": 2, "change_pct": 3.5}]
    m = to_matrix(rows)
    assert m.shape == (2, len(FEATURE_NAMES))


# ---------------------------------------------------------------- ⑤ 퍼센타일 점수

def test_percentile_scores_bounded_zero_to_hundred():
    preds = np.array([10.0, -5.0, 3.0, 100.0, 0.0])
    pct = to_percentile_scores(preds)
    assert pct.min() == pytest.approx(0.0)
    assert pct.max() == pytest.approx(100.0)
    assert (pct >= 0).all() and (pct <= 100).all()
    # 가장 큰 예측값이 가장 높은 점수를 받아야 한다
    assert pct[3] == pytest.approx(100.0)
    assert pct[1] == pytest.approx(0.0)


def test_percentile_scores_average_rank_for_ties():
    preds = np.array([1.0, 1.0, 2.0])
    pct = to_percentile_scores(preds)
    # 동률(0,1번)은 평균 순위(0.5) → 0.5/2*100 = 25
    assert pct[0] == pytest.approx(25.0)
    assert pct[1] == pytest.approx(25.0)
    assert pct[2] == pytest.approx(100.0)


def test_percentile_scores_single_value_is_median():
    assert to_percentile_scores(np.array([42.0]))[0] == 50.0


def test_percentile_scores_empty_input():
    assert to_percentile_scores(np.array([])).size == 0


# ---------------------------------------------------------------- 판단 귀속(핵심 계약)

def test_ml_scorer_input_hash_matches_incumbent_exactly():
    """같은 서류 → 같은 input_hash — watch_scorer 와 비교 가능해야 한다."""
    row = {
        "schema": 1, "date": "2026-08-28", "market": "KR", "symbol": "005930",
        "name": "삼성전자", "baseline_score100": 72, "ai_score100": 68,
        "trending_score100": 55, "change_pct": 2.1, "is_candidate": True,
    }
    [j] = to_judgments({"005930": 77.0}, [row])
    incumbent = selection_judgment(row, producer_version="2")
    assert j.input_hash == incumbent.input_hash
    assert j.producer == "ml_scorer"
    assert j.score == 77.0 and j.verdict == "pass"


def test_ml_scorer_judgments_cover_every_row_even_unscored():
    rows = [
        {"date": "2026-08-28", "market": "KR", "symbol": "005930"},
        {"date": "2026-08-28", "market": "KR", "symbol": "000660"},
    ]
    js = to_judgments({"005930": 90.0}, rows)
    assert len(js) == 2
    rej = next(j for j in js if j.symbol == "000660")
    assert rej.verdict == "reject" and rej.score is None


def test_ml_scorer_verdict_threshold_at_median_percentile():
    rows = [
        {"date": "2026-08-28", "market": "KR", "symbol": "A"},
        {"date": "2026-08-28", "market": "KR", "symbol": "B"},
    ]
    js = to_judgments({"A": 50.0, "B": 49.9}, rows)
    a = next(j for j in js if j.symbol == "A")
    b = next(j for j in js if j.symbol == "B")
    assert a.verdict == "pass"
    assert b.verdict == "reject"
