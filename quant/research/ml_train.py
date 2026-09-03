"""ML 학습 하네스 v2 — selection⋈forward_return 라벨을 다중 타깃·다중 모델로
학습하고 purged walk-forward로 검증한다. 매 실행이 직전 실행과 비교돼 "돌릴 때마다
유의미한 변화가 보인다"를 산출물로 증명한다 (로컬 맥 원버튼 파이프라인 전용,
2026-08-30 v1 신설 → 2026-08-30 v2 고도화, `local/ml/run.sh` → `make ml`).

## 이 모듈이 하는 일과 안 하는 일

`quant/analyze/ml_scorer.py`는 매일 온라인으로 릿지 회귀를 새로 학습해 그날
후보를 채점하는 **운영 경로**다(judgments 원장에 판단만 남긴다, 실거래 금지 —
아키텍처 테스트가 `quant.trade` 임포트를 막는다). 이 모듈은 그 반대다 —
**연구 평면**에서 전체 과거 라벨 데이터를 한 번에 모아 purged walk-forward로
OOS 성적을 재고, 더 표현력 있는 모델이 릿지보다, 그리고 **현행 규칙 채점기보다**
나은지 가늠한다. **아무것도 자동 반영하지 않는다** — 리포트와 모델 파일와
레지스트리 한 줄만 낸다. 참전 여부는 사람이 리포트를 보고 판단한다.

## v1 → v2: 뭐가 달라졌나

1. **다중 타깃**: D+1 방향(분류) 하나뿐이던 v1에서 D+1 수익률(회귀)·D+5 방향
   (분류)까지 셋으로 늘렸다. "달성 가능 이익"(MFE 성격) 라벨은 **의도적으로
   구현하지 않았다** — `forward_return` 스키마(`quant/adapters/schema/
   005_forward_return_rebuild.sql`)에는 `return_bps`(지평 시점의 단일 수익률)만
   있고 구간 내 최고/최저가(high-water mark) 컬럼이 없다. 없는 라벨을 근사로
   지어내면 "측정"이 아니라 "발명"이 된다 — 이 저장소의 원칙(모듈 CLAUDE.md
   "시장 데이터를 조작하지 않는다")과 정면으로 충돌한다.
2. **베이스라인 정면 대결**: `selections`에 기록된 규칙 채점기 점수
   (`baseline_score100`, `quant/control/judgment.py`가 판단에 쓰는 그 점수)와
   ML의 D+1 수익률 예측을 같은 날·같은 종목 집합에서 순위상관(rank IC, 리더보드와
   같은 지표 — `quant.control.leaderboard.daily_rank_ic` 그대로 재사용)과 상위 N
   평균 수익으로 정면 대결시킨다. **2026-08-30 실측: MySQL `selection` 테이블에
   `baseline_score100` 컬럼이 없다**(`quant/adapters/schema/001_initial.sql`
   확인, `quant/control/warehouse.py`의 `SELECTION_COLS`도 미포함 — JSONL
   선정 원장에는 있지만 DB 적재 코드가 그 필드를 옮기지 않는다). 그래서
   `local/ml/remote_dump.py`가 매 실행 `information_schema`로 컬럼 존재를
   확인해 있으면 자동 포함하고, 없으면 이 사실을 리포트에 그대로 신고한다 —
   "동기화 대상 제안"은 이 모듈 하단 docstring 참고.
3. **확률 보정**: `CalibratedClassifierCV`(표본 크기에 따라 sigmoid/isotonic
   자동 선택)로 분류기 출력을 실제 확률에 가깝게 만들고, Brier 점수로 보정
   품질을 신고한다.
4. **OOS permutation importance**: v1의 피처 중요도는 훈련 데이터 전체로 적합한
   최종 모델의 **인샘플** 중요도라 오도 위험이 있었다(`sklearn.ensemble`의
   impurity 기반 중요도는 훈련 데이터 자체의 분할 이득이지 일반화 성능이 아니다).
   v2는 각 purged fold의 **테스트 세트**에서 permutation importance를 재고
   fold 평균을 낸다.
5. **모델 레지스트리 + 델타**: `local/ml/registry.jsonl`에 실행마다 한 줄
   append. 직전 줄과 비교해 "지난번보다 AUC +0.02, 표본 +N일" 같은 델타 절을
   리포트에 낸다.
6. **하이퍼파라미터 탐색**: v1은 탐색 0회였다(표본이 게이트 미만이라 탐색 자체가
   과최적합 위험). v2는 게이트(30거래일)를 넘긴 시장에 한해 `d1_direction`
   분류 타깃에서만 8콤보 그리드를 outer purged OOS 폴드로 직접 선택한다 —
   **nested CV가 아니다**(같은 OOS 폴드로 콤보를 고르고 그 폴드로 다시
   성적을 보고하므로 약간의 낙관 편향이 있다). 나머지 두 타깃은 여기서 고른
   하이퍼파라미터를 재사용해 탐색 예산을 추가로 쓰지 않는다 — 타깃 수만큼
   다중검정 노출을 배로 늘리지 않기 위해서다. 조합 수·타깃 수·모델 수는
   리포트에 그대로 신고한다(§E, `quant.control.leaderboard.required_t`로
   보정 요구 t도 참고 표시).

## 표본이 왜 작은가 (다시 재지 마라, `local/ml/run.sh`가 실측한다)

`local/ml/run.sh`가 매일 EC2 MySQL에서 `selection ⋈ forward_return`(D+1, D+5)을
받아온다. 2026-08-30 실측: KR/US 둘 다 `ml_scorer.MIN_TRAIN_DAYS`(30) 미만이다.
이 문턱 미만이면 `main()`이 학습을 아예 하지 않고 "표본 수집 중" 한 줄만 내고
exit 0 한다(에러가 아니다) — 그게 지금 매일의 정상 상태다.

## 왜 `data/ledger/selections.jsonl`이 아니라 MySQL인가

`quant/control/selections.py`의 의도는 각 행의 `outcome_*`를 사후 갱신하는
것이지만, 2026-08-30 EC2 실측(`selections.jsonl` 2,133행 전수 확인)으로는
`outcome_filled`가 **단 한 건도 True가 아니다** — 정정(2026-09-03 D4 감사): 이건
JSONL에 값이 없다는 뜻이 아니다. `outcome_dN_bps` 개별 필드는 그 실측 시점에도
채워지고 있었다(`cmd_outcomes`가 `apply_outcome`으로 매일 채운다) — `outcome_filled`
은 D+1·D+5·D+20 **셋 다** 채워져야만 True로 뒤집히는 별도 플래그라, 아직 D+20이
안 된 최근 행은 개별 outcome 이 있어도 항상 False다(`quant/control/outcomes.py`의
`apply_outcome` 참고). 실제 문제는 D1/D2 결함(같은 세션 조회의 가짜 0bp, 기준가에
날짜가 없어 세션 카운트가 틀렸던 것 — 2026-09-03 수리)으로 D+1이 영구히 못 채워진
행이 많았다는 것이지, 필드 자체가 비어 있었던 게 아니다. 그래도 이 모듈이
`local/ml/remote_dump.py`로 MySQL `forward_return`을 직접 받아오는 이유는 남는다:
JSONL은 append-only 로그라 대량 조인·purged 폴드 연산에 MySQL만큼 적합하지 않다.
`local/ml/run.sh`는 `local/ml/remote_dump.py`로 그 MySQL 조인 결과를 직접
받아온다. 이 모듈은 그 결과만 안다 — JSONL의 `outcome_*`는 신경 쓰지 않는다.

## Purged fold의 단위가 "행"이 아니라 "거래일"인 이유

같은 `session_date` 안에 여러 종목(행)이 있다. 행 단위로 순서를 매기면 같은
날의 행들이 fold 경계에서 train/test로 갈라져 `purged_cv`가 막으려는 라벨
누수(그날 관측 여러 개가 같은 D+1/D+5 가격 구간을 공유)를 못 막는다. 그래서
`day_purged_splits`가 거래일 인덱스에서 purge/embargo 대상을 정하고, 그
거래일에 속한 행 전체를 통째로 한쪽(train 또는 test)에 배정한다. 타깃마다
`label_horizon`이 다르므로(D+1=1, D+5=5) purge 폭도 타깃별로 다르게 계산한다.

## 동기화 대상 제안 (베이스라인 head-to-head를 실제로 켜려면)

`baseline_score100`이 MySQL `selection`에 없어 head-to-head가 매일 "데이터
없음"으로 보고되는 게 지금 정상 상태다. 다음 세 가지가 갖춰지면 다음
`make ml`부터 자동으로 켜진다(코드는 이미 이 순서로 짜여 있다):

1. `quant/adapters/schema/`에 `ALTER TABLE selection ADD COLUMN
   baseline_score100 SMALLINT NULL` 마이그레이션 추가.
2. `quant/control/warehouse.py`의 `SELECTION_COLS`/`selection_row`에
   `baseline_score100` 추가 — JSONL 선정 원장에는 이미 있는 필드니 컬럼
   순서만 맞추면 된다.
3. 그 다음 `local/ml/remote_dump.py`는 `information_schema`로 컬럼 존재를
   확인해 자동으로 SELECT에 포함한다(별도 코드 변경 불필요).

이 저장소 CLAUDE.md 라우팅상 (1)(2)는 `quant/adapters/`·`quant/control/`
영역이라 이 태스크(연구 평면 한정) 범위 밖이다 — 그래서 구현이 아니라 제안으로
남긴다.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as _date, datetime, timezone
from pathlib import Path

import numpy as np

from quant.analyze import ml_scorer
from quant.backtest.purged_cv import purged_splits
from quant.control.leaderboard import daily_rank_ic, required_t

log = logging.getLogger(__name__)

LABEL_HORIZON_D1 = 1
LABEL_HORIZON_D5 = 5
DEFAULT_N_FOLDS = 4
DEFAULT_EMBARGO_PCT = 0.01
DEFAULT_RANDOM_STATE = 42
DEFAULT_TOP_N = 5
DEFAULT_REGISTRY_PATH = Path("local/ml/registry.jsonl")

# 타깃 하나가 게이트(ml_scorer.MIN_TRAIN_DAYS)를 넘긴 시장 안에서도, D+5처럼
# 라벨 성숙에 시간이 걸리는 타깃은 표본이 더 작을 수 있다. 이 문턱 미만이면
# 그 타깃만 "표본 부족"으로 건너뛴다(시장 전체를 막지 않는다).
TARGET_MIN_DAYS = 10

# 베이스라인 대결에서 "우위"를 주장하려면 최소 이만큼의 거래일이 필요하다.
# `leaderboard.MIN_DAYS`(20, 운영 승격 문턱)보다 낮춘 탐색용 문턱이다 — 이
# 모듈은 운영 승격을 하지 않고 "참전 제안"만 하므로 더 이른 신호도 보고할
# 가치가 있다. 다만 승격 자체는 여전히 leaderboard의 20일 문턱을 통과해야 한다.
MIN_DAYS_FOR_BASELINE_EDGE = 10

# 하이퍼파라미터 그리드 — d1_direction 분류 타깃에서만 쓴다(모듈 docstring
# §6). 순서 고정(재현성). 2(n_estimators) × 2(max_depth) × 2(learning_rate) = 8.
HYPERPARAM_GRID: list[dict] = [
    {"n_estimators": n, "max_depth": d, "learning_rate": lr}
    for n in (50, 100)
    for d in (2, 3)
    for lr in (0.05, 0.1)
]
DEFAULT_HYPERPARAMS = {"n_estimators": 100, "max_depth": 3, "learning_rate": 0.1}


# ------------------------------------------------------------------ 타깃 정의

@dataclass(frozen=True)
class TargetSpec:
    name: str
    kind: str          # "classification" | "regression"
    label_key: str      # rows[i][label_key] 가 None 이 아닌 행만 이 타깃에 쓴다
    label_horizon: int  # purge 폭(거래일) — D+1=1, D+5=5
    description: str


TARGETS: tuple[TargetSpec, ...] = (
    TargetSpec("d1_direction", "classification", "return_bps", LABEL_HORIZON_D1,
              "D+1 방향(상승/하락) — 부호 분류"),
    TargetSpec("d1_return_bps", "regression", "return_bps", LABEL_HORIZON_D1,
              "D+1 수익률(bps) — 회귀"),
    TargetSpec("d5_direction", "classification", "return_bps_d5", LABEL_HORIZON_D5,
              "D+5 방향(상승/하락) — 부호 분류(라벨 성숙 필요)"),
)


# ------------------------------------------------------------------ 데이터 적재

def load_labeled_rows(path: Path) -> list[dict]:
    """`local/ml/remote_dump.py`가 낸 MySQL 조인 결과(JSON 배열)를 읽는다."""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"라벨 데이터 형식이 리스트가 아니다: {path}")
    return rows


def rows_by_market(rows: list[dict]) -> dict[str, list[dict]]:
    """market 열 기준으로 행을 나눈다."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r.get("market") or ""), []).append(r)
    return out


def train_day_count(rows: list[dict], date_key: str = "session_date") -> int:
    """독립 거래일 수 — `ml_scorer.enough_sample` 게이트에 그대로 넣는 값이다."""
    return len({str(r.get(date_key)) for r in rows})


def sorted_distinct_days(rows: list[dict], date_key: str = "session_date") -> list[str]:
    """오름차순 정렬된 고유 거래일 목록. ISO 날짜 문자열이므로 사전식 정렬이
    곧 시간순 정렬이다(`ml_scorer.training_rows_before`와 같은 전제)."""
    return sorted({str(r.get(date_key)) for r in rows})


def rows_for_target(rows: list[dict], spec: TargetSpec) -> list[dict]:
    """이 타깃의 라벨(`spec.label_key`)이 채워진 행만 남긴다. D+1 두 타깃은
    보통 전체 행이지만, D+5는 최근 며칠(라벨이 아직 안 만들어진 날)이 자연히
    빠진다 — 라벨을 지어내지 않고 있는 그대로 표본을 줄인다."""
    return [r for r in rows if r.get(spec.label_key) is not None]


# ------------------------------------------------------------------ purged 분할(거래일 단위)

def day_purged_splits(
    rows: list[dict], n_folds: int,
    embargo_pct: float = DEFAULT_EMBARGO_PCT,
    label_horizon: int = LABEL_HORIZON_D1,
    date_key: str = "session_date",
) -> list[tuple[list[int], list[int]]]:
    """거래일 단위 purge+embargo를 행 인덱스로 환산한 `(train_idx, test_idx)` 목록.

    같은 거래일의 행은 절대 train/test로 갈라지지 않는다 — `purged_cv.purged_splits`
    가 거래일 인덱스에서 뭘 버릴지 정하고, 여기서 그 거래일에 속한 행 전체를
    한쪽으로 몰아준다. 모듈 docstring 참고."""
    days = sorted_distinct_days(rows, date_key)
    day_idx = {d: i for i, d in enumerate(days)}
    rows_per_day: dict[int, list[int]] = {}
    for i, r in enumerate(rows):
        d = day_idx[str(r.get(date_key))]
        rows_per_day.setdefault(d, []).append(i)

    out: list[tuple[list[int], list[int]]] = []
    for train_days, test_days in purged_splits(len(days), n_folds, embargo_pct, label_horizon):
        train_idx = [i for d in train_days for i in rows_per_day.get(d, [])]
        test_idx = [i for d in test_days for i in rows_per_day.get(d, [])]
        out.append((train_idx, test_idx))
    return out


# ------------------------------------------------------------------ 특성/타깃 행렬

def _feature_matrix(rows: list[dict]) -> np.ndarray:
    """`ml_scorer.FEATURE_NAMES`/`to_matrix`를 그대로 재사용 — 운영 채점기와
    같은 피처 정의로 학습한다."""
    return ml_scorer.to_matrix(rows)


def _target_array(rows: list[dict], spec: TargetSpec) -> np.ndarray:
    raw = np.array([float(r.get(spec.label_key) or 0.0) for r in rows], dtype=float)
    if spec.kind == "classification":
        return (raw > 0).astype(int)
    return raw


# ------------------------------------------------------------------ 확률 보정

def _fit_calibrated_classifier(X_tr: np.ndarray, y_tr: np.ndarray, params: dict,
                               random_state: int) -> tuple[object, str, bool]:
    """GradientBoostingClassifier + CalibratedClassifierCV.

    표본이 작으면(< 300행) isotonic이 과적합되기 쉽다(sklearn 문서 권고) —
    sigmoid(Platt)로 낮춘다. 훈련 fold의 클래스 최소 개수가 CV 분할 수보다
    작으면(StratifiedKFold 요구 조건 미달) 보정 자체를 건너뛰고 원본 분류기로
    폴백한다 — 폴백 사실은 반환값(`calibrated`)으로 호출부에 알린다.
    """
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import GradientBoostingClassifier

    method = "isotonic" if len(y_tr) >= 300 else "sigmoid"
    base = GradientBoostingClassifier(random_state=random_state, **params)
    min_class_count = min(int((y_tr == 0).sum()), int((y_tr == 1).sum()))
    cv = min(3, min_class_count) if min_class_count >= 2 else 0

    if cv >= 2:
        model = CalibratedClassifierCV(base, method=method, cv=cv)
        try:
            model.fit(X_tr, y_tr)
            return model, method, True
        except ValueError:
            pass  # 폴백으로 떨어진다 — 아래 원본 분류기

    base.fit(X_tr, y_tr)
    return base, "none(표본 부족으로 보정 생략)", False


# ------------------------------------------------------------------ 타깃별 OOS 평가

def evaluate_target_oos(
    rows: list[dict], spec: TargetSpec, params: dict,
    n_folds: int = DEFAULT_N_FOLDS,
    embargo_pct: float = DEFAULT_EMBARGO_PCT,
    random_state: int = DEFAULT_RANDOM_STATE,
) -> dict:
    """타깃 하나의 purged walk-forward OOS 평가. sklearn은 여기서만 지연
    임포트한다 — `research` 그룹 선택 의존성이라 표본 게이트 미달일 땐 아예
    안 불린다.

    반환에 `oos_preds`(행 인덱스 → OOS 예측값)를 포함한다 — 분류는
    양성 확률, 회귀는 예측 수익률(bps). 베이스라인 head-to-head와 permutation
    importance가 재사용한다.
    """
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import (
        brier_score_loss, mean_absolute_error, precision_score, r2_score,
        roc_auc_score, root_mean_squared_error,
    )

    X_all = _feature_matrix(rows)
    y_all = _target_array(rows, spec)
    base_rate = float(y_all.mean()) if spec.kind == "classification" and len(y_all) else None

    fold_metrics: list[dict] = []
    oos_preds: dict[int, float] = {}
    perm_importances: list[np.ndarray] = []
    calibration_methods: list[str] = []

    for train_idx, test_idx in day_purged_splits(rows, n_folds, embargo_pct, spec.label_horizon):
        if len(train_idx) < 10 or len(test_idx) == 0:
            fold_metrics.append({"n_train": len(train_idx), "n_test": len(test_idx),
                                 "skipped": "표본 부족"})
            continue
        y_tr, y_te = y_all[train_idx], y_all[test_idx]
        if spec.kind == "classification" and (len(set(y_tr.tolist())) < 2 or len(set(y_te.tolist())) < 2):
            fold_metrics.append({"n_train": len(train_idx), "n_test": len(test_idx),
                                 "skipped": "train/test 한쪽이 단일 클래스"})
            continue

        X_tr, medians = ml_scorer.impute_median(X_all[train_idx])
        X_te = ml_scorer.fill_missing(X_all[test_idx], medians)

        if spec.kind == "classification":
            model, calib_method, calibrated = _fit_calibrated_classifier(X_tr, y_tr, params, random_state)
            calibration_methods.append(calib_method)
            proba = model.predict_proba(X_te)[:, 1]
            pred = (proba >= 0.5).astype(int)
            for i, idx in enumerate(test_idx):
                oos_preds[idx] = float(proba[i])
            fold_metrics.append({
                "auc": float(roc_auc_score(y_te, proba)),
                "precision": float(precision_score(y_te, pred, zero_division=0)),
                "brier": float(brier_score_loss(y_te, proba)),
                "calibrated": calibrated, "calibration_method": calib_method,
                "n_train": len(train_idx), "n_test": len(test_idx), "skipped": None,
            })
            try:
                pi = permutation_importance(model, X_te, y_te, n_repeats=10,
                                            random_state=random_state, scoring="roc_auc")
                perm_importances.append(pi.importances_mean)
            except ValueError:
                pass  # OOS 세트가 너무 작아 permutation importance를 못 낸 fold
        else:
            model = GradientBoostingRegressor(random_state=random_state, **params)
            model.fit(X_tr, y_tr)
            pred = model.predict(X_te)
            for i, idx in enumerate(test_idx):
                oos_preds[idx] = float(pred[i])
            fold_metrics.append({
                "rmse": float(root_mean_squared_error(y_te, pred)),
                "mae": float(mean_absolute_error(y_te, pred)),
                "r2": float(r2_score(y_te, pred)) if len(y_te) > 1 else None,
                "n_train": len(train_idx), "n_test": len(test_idx), "skipped": None,
            })
            try:
                pi = permutation_importance(model, X_te, y_te, n_repeats=10,
                                            random_state=random_state,
                                            scoring="neg_mean_squared_error")
                perm_importances.append(pi.importances_mean)
            except ValueError:
                pass

    valid = [m for m in fold_metrics if not m.get("skipped")]
    importances: dict[str, float] = {}
    if perm_importances:
        mean_importance = np.mean(np.vstack(perm_importances), axis=0)
        importances = dict(zip(ml_scorer.FEATURE_NAMES, mean_importance.tolist()))

    metrics: dict = {
        "kind": spec.kind,
        "label_horizon": spec.label_horizon,
        "n_rows": len(rows),
        "n_days": train_day_count(rows),
        "n_folds": n_folds,
        "folds": fold_metrics,
        "hyperparams": params,
    }
    if spec.kind == "classification":
        metrics["base_rate"] = base_rate
        metrics["mean_oos_auc"] = float(np.mean([m["auc"] for m in valid])) if valid else None
        metrics["mean_oos_precision"] = float(np.mean([m["precision"] for m in valid])) if valid else None
        metrics["mean_oos_brier"] = float(np.mean([m["brier"] for m in valid])) if valid else None
        metrics["calibration_methods"] = sorted(set(calibration_methods))
    else:
        metrics["mean_oos_rmse"] = float(np.mean([m["rmse"] for m in valid])) if valid else None
        metrics["mean_oos_mae"] = float(np.mean([m["mae"] for m in valid])) if valid else None
        r2s = [m["r2"] for m in valid if m["r2"] is not None]
        metrics["mean_oos_r2"] = float(np.mean(r2s)) if r2s else None
        metrics["mean_oos_rank_ic"] = _mean_rank_ic(rows, oos_preds, y_all)

    return {"metrics": metrics, "importances": importances, "oos_preds": oos_preds}


def _mean_rank_ic(rows: list[dict], idx_score: dict[int, float], y_all: np.ndarray) -> float | None:
    """OOS 예측 점수의 일별 순위상관(rank IC) 평균 — `leaderboard.daily_rank_ic`를
    그대로 재사용한다(같은 지표로 재는 게 이 저장소의 "제1 계약")."""
    by_day_scores: dict[str, dict[str, float]] = defaultdict(dict)
    by_day_returns: dict[str, dict[str, float]] = defaultdict(dict)
    for idx, score in idx_score.items():
        r = rows[idx]
        day, sym = str(r.get("session_date")), str(r.get("symbol"))
        by_day_scores[day][sym] = score
        by_day_returns[day][sym] = float(y_all[idx])
    ic_values = [
        daily_rank_ic(by_day_scores[day], by_day_returns[day])
        for day in by_day_scores
    ]
    ic_values = [v for v in ic_values if v is not None]
    return float(np.mean(ic_values)) if ic_values else None


# ------------------------------------------------------------------ 하이퍼파라미터 탐색

def select_hyperparams(
    rows: list[dict], spec: TargetSpec,
    n_folds: int = DEFAULT_N_FOLDS, embargo_pct: float = DEFAULT_EMBARGO_PCT,
    random_state: int = DEFAULT_RANDOM_STATE,
) -> tuple[dict, list[dict]]:
    """`d1_direction` 분류 타깃 하나에서만 부른다(모듈 docstring §6). outer
    purged OOS 폴드로 콤보를 직접 고르는 non-nested 탐색이라 약간의 낙관
    편향이 있다는 걸 호출부가 리포트에 신고해야 한다."""
    trials: list[dict] = []
    best_params = HYPERPARAM_GRID[0]
    best_score = float("-inf")
    for params in HYPERPARAM_GRID:
        result = evaluate_target_oos(rows, spec, params, n_folds, embargo_pct, random_state)
        score = result["metrics"]["mean_oos_auc"]
        trials.append({"params": params, "mean_oos_auc": score})
        if score is not None and score > best_score:
            best_score = score
            best_params = params
    return best_params, trials


# ------------------------------------------------------------------ 베이스라인 정면 대결

def compare_to_baseline(
    rows: list[dict], oos_preds: dict[int, float],
    top_n: int = DEFAULT_TOP_N,
    min_days: int = MIN_DAYS_FOR_BASELINE_EDGE,
) -> dict:
    """`d1_return_bps` 회귀의 OOS 예측(연속값)과 `baseline_score100`(현행
    규칙 채점기)을 같은 날·같은 종목 집합에서 정면 대결시킨다.

    `rows`는 `d1_return_bps`를 평가할 때 쓴 것과 같은 행 목록(즉 D+1 라벨이
    있는 전체 행)이어야 하고, `oos_preds`는 그 타깃의 `evaluate_target_oos`가
    돌려준 `oos_preds`(행 인덱스 → 예측 수익률)여야 한다 — 인덱스 정합이
    깨지면 잘못된 종목끼리 비교하게 된다.

    `baseline_score100`이 한 행도 없으면 `available=False` — 모듈 docstring
    "동기화 대상 제안" 참고. 있어도 거래일이 `min_days` 미만이면 우위를
    주장하지 않는다(표본 부족).
    """
    idxs = [i for i, r in enumerate(rows) if r.get("baseline_score100") is not None and i in oos_preds]
    if not idxs:
        return {
            "available": False,
            "reason": ("baseline_score100 이 라벨 데이터에 없다 — MySQL selection 테이블에 "
                      "이 컬럼이 아직 없다(모듈 docstring '동기화 대상 제안' 참고)"),
        }

    by_day: dict[str, list[tuple[str, float, float, float]]] = defaultdict(list)
    for i in idxs:
        r = rows[i]
        by_day[str(r["session_date"])].append((
            str(r["symbol"]), float(oos_preds[i]), float(r["baseline_score100"]),
            float(r.get("return_bps") or 0.0),
        ))

    ml_ic_by_day: dict[str, float | None] = {}
    base_ic_by_day: dict[str, float | None] = {}
    ml_topn_returns: list[float] = []
    base_topn_returns: list[float] = []
    for day, items in sorted(by_day.items()):
        if len(items) < 3:
            continue
        ml_scores = {sym: ml for sym, ml, _, _ in items}
        base_scores = {sym: bl for sym, _, bl, _ in items}
        returns = {sym: ret for sym, _, _, ret in items}
        ml_ic_by_day[day] = daily_rank_ic(ml_scores, returns)
        base_ic_by_day[day] = daily_rank_ic(base_scores, returns)

        n = min(top_n, len(items))
        ml_top = sorted(items, key=lambda t: -t[1])[:n]
        base_top = sorted(items, key=lambda t: -t[2])[:n]
        ml_topn_returns.append(sum(t[3] for t in ml_top) / n)
        base_topn_returns.append(sum(t[3] for t in base_top) / n)

    ml_ic_vals = [v for v in ml_ic_by_day.values() if v is not None]
    base_ic_vals = [v for v in base_ic_by_day.values() if v is not None]
    n_days = len(ml_topn_returns)

    result = {
        "available": True,
        "n_days": n_days,
        "n_rows": len(idxs),
        "top_n": top_n,
        "mean_ml_rank_ic": float(np.mean(ml_ic_vals)) if ml_ic_vals else None,
        "mean_baseline_rank_ic": float(np.mean(base_ic_vals)) if base_ic_vals else None,
        "mean_ml_topn_return_bps": float(np.mean(ml_topn_returns)) if ml_topn_returns else None,
        "mean_baseline_topn_return_bps": float(np.mean(base_topn_returns)) if base_topn_returns else None,
    }
    result["ml_beats_baseline"] = bool(
        n_days >= min_days
        and result["mean_ml_rank_ic"] is not None and result["mean_baseline_rank_ic"] is not None
        and result["mean_ml_topn_return_bps"] is not None and result["mean_baseline_topn_return_bps"] is not None
        and (result["mean_ml_rank_ic"] - result["mean_baseline_rank_ic"]) > 0
        and (result["mean_ml_topn_return_bps"] - result["mean_baseline_topn_return_bps"]) > 0
    )
    return result


# ------------------------------------------------------------------ 최종 모델(참고용, 배포 안 함)

def fit_final_model(rows: list[dict], spec: TargetSpec, params: dict,
                    random_state: int = DEFAULT_RANDOM_STATE):
    """전체 데이터로 최종 모델을 적합 — 저장용 모델 파일을 낸다. **이 모델은
    배포되지 않는다** — 참고용 산출물일 뿐이다."""
    from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor

    X = _feature_matrix(rows)
    y = _target_array(rows, spec)
    if spec.kind == "classification" and len(set(y.tolist())) < 2:
        return None
    X_imputed, _ = ml_scorer.impute_median(X)
    if spec.kind == "classification":
        model = GradientBoostingClassifier(random_state=random_state, **params)
    else:
        model = GradientBoostingRegressor(random_state=random_state, **params)
    model.fit(X_imputed, y)
    return model


# ------------------------------------------------------------------ 모델 레지스트리 + 델타

def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent, capture_output=True, text=True,
            timeout=5, check=True,
        )
        return out.stdout.strip() or "unknown"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "unknown"


def append_registry(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_last_registry_entry(path: Path) -> dict | None:
    """레지스트리 마지막 줄. 없거나 깨진 줄은 조용히 건너뛴다(원장 전반 원칙과
    동일 — 한 줄 손상이 전체를 막지 않는다)."""
    if not path.exists():
        return None
    last: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            last = rec
    return last


_DELTA_METRIC_KEYS = (
    "mean_oos_auc", "mean_oos_precision", "mean_oos_brier",
    "mean_oos_rmse", "mean_oos_mae", "mean_oos_r2", "mean_oos_rank_ic",
)


def compute_deltas(current: dict, previous: dict | None) -> dict:
    """`{market: {"n_days_delta": int, "targets": {target: {metric: delta}}}}`.
    직전 레지스트리 줄이 없거나 같은 시장/타깃이 없으면 그 항목은 비운다 —
    "처음 도는 실행"과 "델타가 0인 실행"을 구분해야 한다."""
    if previous is None:
        return {}
    out: dict = {}
    prev_markets = previous.get("markets") or {}
    for market, cur_m in (current.get("markets") or {}).items():
        prev_m = prev_markets.get(market)
        if not prev_m:
            continue
        target_deltas: dict = {}
        for tname, cur_t in (cur_m.get("targets") or {}).items():
            prev_t = (prev_m.get("targets") or {}).get(tname)
            if not prev_t:
                continue
            d = {}
            for key in _DELTA_METRIC_KEYS:
                cv, pv = cur_t.get(key), prev_t.get(key)
                if cv is not None and pv is not None:
                    d[key] = cv - pv
            if d:
                target_deltas[tname] = d
        out[market] = {
            "n_days_delta": cur_m.get("n_days", 0) - prev_m.get("n_days", 0),
            "targets": target_deltas,
        }
    return out


# ------------------------------------------------------------------ 리포트 렌더링

_METRIC_LABELS = {
    "mean_oos_auc": "AUC", "mean_oos_precision": "정밀도", "mean_oos_brier": "Brier",
    "mean_oos_rmse": "RMSE(bps)", "mean_oos_mae": "MAE(bps)", "mean_oos_r2": "R²",
    "mean_oos_rank_ic": "일별 rank IC 평균",
}


def _render_target_section(name: str, spec_desc: str, r: dict) -> list[str]:
    m = r["metrics"]
    lines = [f"#### {name} — {spec_desc}", ""]
    lines.append(f"- 표본: 거래일 {m['n_days']}일, 행 {m['n_rows']}개 "
                f"(purge+embargo 폭: label_horizon={m['label_horizon']}거래일)")
    lines.append(f"- 하이퍼파라미터: {m['hyperparams']}")
    if m["kind"] == "classification":
        if m.get("base_rate") is not None:
            lines.append(f"- 기저율(양수 수익 비율): {m['base_rate']:.1%}")
        if m["mean_oos_auc"] is not None:
            lines.append(f"- OOS AUC 평균: {m['mean_oos_auc']:.3f} (0.5=무작위 기준선)")
            lines.append(f"- OOS 정밀도 평균: {m['mean_oos_precision']:.3f}")
            lines.append(f"- OOS Brier 점수 평균: {m['mean_oos_brier']:.4f} "
                        f"(0=완벽 보정, 낮을수록 좋음, 보정: {', '.join(m['calibration_methods']) or '없음'})")
        else:
            lines.append("- OOS 성적: 산출 불가(fold마다 표본 부족 또는 단일 클래스)")
    else:
        if m["mean_oos_rmse"] is not None:
            lines.append(f"- OOS RMSE 평균: {m['mean_oos_rmse']:.1f}bps")
            lines.append(f"- OOS MAE 평균: {m['mean_oos_mae']:.1f}bps")
            r2 = m["mean_oos_r2"]
            lines.append(f"- OOS R² 평균: {r2:.3f}" if r2 is not None else "- OOS R² 평균: 산출 불가")
            ic = m["mean_oos_rank_ic"]
            lines.append(f"- OOS 일별 rank IC 평균: {ic:+.3f}" if ic is not None else "- OOS 일별 rank IC 평균: 산출 불가")
        else:
            lines.append("- OOS 성적: 산출 불가(fold마다 표본 부족)")
    lines.append("")
    lines.append("##### fold별 상세")
    for i, f in enumerate(m["folds"]):
        if f.get("skipped"):
            lines.append(f"  - fold {i}: 건너뜀({f['skipped']}, train={f['n_train']} test={f['n_test']})")
        elif m["kind"] == "classification":
            lines.append(f"  - fold {i}: AUC={f['auc']:.3f} 정밀도={f['precision']:.3f} "
                        f"Brier={f['brier']:.4f} (train={f['n_train']} test={f['n_test']})")
        else:
            r2s = f"{f['r2']:.3f}" if f["r2"] is not None else "N/A"
            lines.append(f"  - fold {i}: RMSE={f['rmse']:.1f}bps MAE={f['mae']:.1f}bps R²={r2s} "
                        f"(train={f['n_train']} test={f['n_test']})")
    lines.append("")
    imp = r.get("importances") or {}
    if imp:
        top10 = sorted(imp.items(), key=lambda kv: -abs(kv[1]))[:10]
        lines.append("##### 피처 중요도 (OOS permutation, fold 평균, 상위 10)")
        for feat, val in top10:
            lines.append(f"  - {feat}: {val:+.4f}")
    else:
        lines.append("##### 피처 중요도: 산출 불가(OOS 세트가 없거나 모두 단일 클래스)")
    lines.append("")
    return lines


def _render_baseline_section(cmp: dict) -> list[str]:
    lines = ["### 베이스라인(규칙 채점기) 대 ML 정면 대결", ""]
    if not cmp.get("available"):
        lines.append(f"- {cmp.get('reason')}")
        lines.append("- 동기화 대상 제안: 모듈 docstring '동기화 대상 제안' 절 참고 "
                    "(selection.baseline_score100 컬럼 마이그레이션 + warehouse.SELECTION_COLS 추가).")
        lines.append("")
        return lines
    lines.append(f"- 비교 표본: 거래일 {cmp['n_days']}일, 행 {cmp['n_rows']}개, 상위 N={cmp['top_n']}")
    ml_ic = cmp["mean_ml_rank_ic"]
    base_ic = cmp["mean_baseline_rank_ic"]
    lines.append(f"- 일별 rank IC 평균: ML {ml_ic:+.3f} vs 베이스라인 {base_ic:+.3f}"
                if ml_ic is not None and base_ic is not None else "- 일별 rank IC: 산출 불가")
    ml_top = cmp["mean_ml_topn_return_bps"]
    base_top = cmp["mean_baseline_topn_return_bps"]
    lines.append(f"- 상위 N 평균 D+1 수익률: ML {ml_top:+.1f}bps vs 베이스라인 {base_top:+.1f}bps"
                if ml_top is not None and base_top is not None else "- 상위 N 평균 수익률: 산출 불가")
    if cmp["ml_beats_baseline"]:
        lines.append("- **판정: ML이 순위상관·상위 N 수익률 둘 다에서 베이스라인을 앞선다.** "
                    "아래 '참전 제안' 절 참고.")
    else:
        lines.append(f"- 판정: 아직 베이스라인 우위 없음(또는 표본 {cmp['n_days']}일 < "
                    f"{MIN_DAYS_FOR_BASELINE_EDGE}일 최소 문턱).")
    lines.append("")
    return lines


def _render_delta_section(market_delta: dict | None) -> list[str]:
    lines = ["### 직전 실행 대비 델타", ""]
    if not market_delta:
        lines.append("- 직전 레지스트리 기록 없음(첫 실행이거나 이 시장이 직전엔 게이트 미달) "
                    "— 델타 없음.")
        lines.append("")
        return lines
    lines.append(f"- 표본: 지난번보다 거래일 {market_delta['n_days_delta']:+d}일")
    for tname, deltas in market_delta.get("targets", {}).items():
        parts = [f"{_METRIC_LABELS.get(k, k)} {v:+.4f}" for k, v in deltas.items()]
        lines.append(f"- {tname}: " + (", ".join(parts) if parts else "비교 가능한 지표 없음"))
    lines.append("")
    return lines


def _render_proposal_section(market: str, cmp: dict) -> list[str]:
    """베이스라인 대비 유의 우위(§7)일 때만 낸다 — 자동 배포는 여전히 없다."""
    lines = ["### 참전 제안", ""]
    lines.append(f"- **무엇을**: `{market}` 시장에서 `d1_return_bps` 회귀 예측(GradientBoosting)을 "
                f"현행 규칙 채점기(`baseline_score100`) 옆에 두 번째 순위 신호로 시범 병행.")
    lines.append(f"- **왜**: 거래일 {cmp['n_days']}일 표본에서 일별 rank IC "
                f"{cmp['mean_ml_rank_ic']:+.3f}(베이스라인 {cmp['mean_baseline_rank_ic']:+.3f}) "
                f"및 상위 {cmp['top_n']} 평균 D+1 수익률 {cmp['mean_ml_topn_return_bps']:+.1f}bps"
                f"(베이스라인 {cmp['mean_baseline_topn_return_bps']:+.1f}bps)로 둘 다 우위.")
    lines.append("- **기대효과·리스크**: 기대효과는 순위 상관 개선분만큼의 후보 선별 정밀도 향상 — "
                f"다만 표본 {cmp['n_days']}일은 여전히 `leaderboard.MIN_DAYS`(20일 운영 승격 문턱) "
                "근처이거나 미만일 수 있어 우연과 실력을 완전히 구분하지 못한다. 다중검정 노출도 "
                "고려해야 한다(아래 §다중검정 참고). 실거래 반영 전 leaderboard 승격 문턱을 별도로 "
                "통과해야 한다 — 이 리포트는 그 전 단계 신호일 뿐이다.")
    lines.append("")
    return lines


def render_report_md(
    results: dict[str, dict], run_date: str, git_sha: str,
    skipped: dict[str, int] | None = None,
    min_train_days: int = ml_scorer.MIN_TRAIN_DAYS,
    deltas: dict | None = None,
    n_targets: int = len(TARGETS), hyperparam_trials: int = len(HYPERPARAM_GRID),
) -> str:
    """`results[market]` = {"metrics": {target: evaluate_target_oos 결과}, "baseline": compare_to_baseline 결과}."""
    lines = [f"# ML 학습 리포트 v2 — {run_date} (git {git_sha})", ""]

    n_markets = len(results)
    total_trials = max(n_targets * max(n_markets, 1), 1) + hyperparam_trials
    lines.append("## 실행 메타 · 다중검정 신고")
    lines.append("")
    lines.append(f"- 타깃 수 {n_targets} × 시장 수 {n_markets} = 모델 적합 {n_targets * max(n_markets, 1)}회"
                f" + 하이퍼파라미터 탐색 {hyperparam_trials}콤보(d1_direction 한정, 시장별) "
                f"= 유효 시행 근사 {total_trials}회.")
    lines.append(f"- 참고(Bonferroni 근사) 요구 t: {required_t(total_trials):.2f} "
                f"— `quant.control.leaderboard.required_t` 재사용, 리더보드 운영 승격과 같은 척도.")
    lines.append("- 하이퍼파라미터 탐색은 outer purged OOS 폴드로 직접 콤보를 고르는 "
                "non-nested 방식이라 약간의 낙관 편향이 있다(모듈 docstring §6).")
    lines.append("")

    for market, r in results.items():
        lines.append(f"## {market}")
        lines.append("")
        lines.append("### 타깃별 OOS 성적")
        lines.append("")
        for spec in TARGETS:
            target_result = r["metrics"].get(spec.name)
            if target_result is None:
                lines.append(f"#### {spec.name} — {spec.description}")
                lines.append("")
                lines.append(f"- 표본 부족으로 건너뜀(성숙한 라벨 {r.get('target_skip_days', {}).get(spec.name, 0)}일 "
                            f"< {TARGET_MIN_DAYS}일)")
                lines.append("")
                continue
            lines += _render_target_section(spec.name, spec.description, target_result)
        lines += _render_baseline_section(r["baseline"])
        lines += _render_delta_section((deltas or {}).get(market))
        if r["baseline"].get("ml_beats_baseline"):
            lines += _render_proposal_section(market, r["baseline"])

    if skipped:
        skip_note = " · ".join(f"{mkt} {d}/{min_train_days}" for mkt, d in skipped.items())
        lines.append(f"참고: 표본 부족으로 건너뛴 시장 — {skip_note}")
        lines.append("")
    lines.append("---")
    lines.append("참전 제안은 사람이 결정: 리포트를 보고 판단하라.")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI

def _run_market(market_rows: list[dict], n_folds: int, embargo_pct: float,
                random_state: int, out_dir: Path, market: str,
                top_n: int) -> tuple[dict, dict, dict]:
    """한 시장의 다중 타깃 학습 전체. `(metrics_by_target, baseline_cmp, target_skip_days)`."""
    d1_spec = next(s for s in TARGETS if s.name == "d1_direction")
    d1_rows = rows_for_target(market_rows, d1_spec)
    best_params, hp_trials = select_hyperparams(d1_rows, d1_spec, n_folds, embargo_pct, random_state)
    log.info("[%s] 하이퍼파라미터 탐색 %d콤보 — 최적: %s", market, len(hp_trials), best_params)

    metrics_by_target: dict[str, dict] = {}
    target_skip_days: dict[str, int] = {}
    d1_return_oos_preds: dict[int, float] = {}
    d1_return_rows: list[dict] = []

    for spec in TARGETS:
        target_rows = rows_for_target(market_rows, spec)
        days = train_day_count(target_rows)
        if days < TARGET_MIN_DAYS:
            target_skip_days[spec.name] = days
            continue
        target_n_folds = max(2, min(n_folds, days // 3))
        result = evaluate_target_oos(target_rows, spec, best_params, target_n_folds, embargo_pct, random_state)
        metrics_by_target[spec.name] = result
        model = fit_final_model(target_rows, spec, best_params, random_state)
        if model is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            import joblib
            joblib.dump(model, out_dir / f"model_{market}_{spec.name}.joblib")
        if spec.name == "d1_return_bps":
            d1_return_oos_preds = result["oos_preds"]
            d1_return_rows = target_rows

    baseline_cmp = compare_to_baseline(d1_return_rows, d1_return_oos_preds, top_n=top_n) if d1_return_rows \
        else {"available": False, "reason": "d1_return_bps 타깃이 표본 부족으로 건너뛰어 비교할 예측이 없다"}

    return metrics_by_target, baseline_cmp, target_skip_days


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ML 학습 하네스 v2 — 다중 타깃 + purged walk-forward + 베이스라인 대결")
    parser.add_argument("--labeled-json", required=True, type=Path,
                        help="local/ml/remote_dump.py 출력(JSON 배열) 경로")
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="리포트/모델을 낼 디렉터리 (local/ml/out/YYYY-MM-DD/)")
    parser.add_argument("--min-train-days", type=int, default=ml_scorer.MIN_TRAIN_DAYS,
                        help=f"게이트 문턱 (기본 ml_scorer.MIN_TRAIN_DAYS={ml_scorer.MIN_TRAIN_DAYS})")
    parser.add_argument("--n-folds", type=int, default=DEFAULT_N_FOLDS)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help="베이스라인 대결 상위 N (기본 5)")
    parser.add_argument("--markets", default="KR,US")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH,
                        help="모델 레지스트리 JSONL 경로 (기본 local/ml/registry.jsonl)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    rows = load_labeled_rows(args.labeled_json)
    by_market = rows_by_market(rows)

    results: dict[str, dict] = {}
    skipped: dict[str, int] = {}
    for market in args.markets.split(","):
        market_rows = by_market.get(market, [])
        d1_spec = next(s for s in TARGETS if s.name == "d1_direction")
        days = train_day_count(rows_for_target(market_rows, d1_spec))
        if not ml_scorer.enough_sample(days, args.min_train_days):
            skipped[market] = days
            continue
        n_folds = max(2, min(args.n_folds, days // 3))
        metrics_by_target, baseline_cmp, target_skip_days = _run_market(
            market_rows, n_folds, DEFAULT_EMBARGO_PCT, DEFAULT_RANDOM_STATE,
            args.out_dir, market, args.top_n,
        )
        results[market] = {
            "metrics": metrics_by_target, "baseline": baseline_cmp,
            "n_days": days, "target_skip_days": target_skip_days,
        }

    if not results:
        gate_msg = " · ".join(
            f"{mkt} {skipped.get(mkt, 0)}/{args.min_train_days}"
            for mkt in args.markets.split(",")
        )
        print(f"표본 수집 중 {gate_msg} — 학습 생략")
        return 0

    run_date = _date.today().isoformat()
    git_sha = _git_sha()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    registry_current = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": git_sha, "run_date": run_date,
        "markets": {
            market: {
                "n_days": r["n_days"],
                "targets": {tname: t["metrics"] for tname, t in r["metrics"].items()},
                "baseline": r["baseline"],
            }
            for market, r in results.items()
        },
        "n_targets": len(TARGETS), "hyperparam_trials": len(HYPERPARAM_GRID),
    }
    previous = load_last_registry_entry(args.registry)
    deltas = compute_deltas(registry_current, previous)

    report = render_report_md(
        {m: {"metrics": r["metrics"], "baseline": r["baseline"],
            "target_skip_days": r["target_skip_days"]} for m, r in results.items()},
        run_date, git_sha, skipped, args.min_train_days, deltas,
    )
    (args.out_dir / "report.md").write_text(report, encoding="utf-8")
    append_registry(args.registry, registry_current)

    summary = {
        market: {
            "n_days": r["n_days"],
            "targets": {
                tname: {k: v for k, v in t["metrics"].items() if k not in ("folds", "hyperparams")}
                for tname, t in r["metrics"].items()
            },
            "baseline": r["baseline"],
        }
        for market, r in results.items()
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"학습 완료 — {', '.join(results.keys())}. 리포트: {args.out_dir / 'report.md'} "
         f"레지스트리: {args.registry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
