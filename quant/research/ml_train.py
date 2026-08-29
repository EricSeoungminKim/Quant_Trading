"""ML 학습 하네스 v1 — selection⋈forward_return 라벨을 GradientBoosting으로
학습하고 purged walk-forward로 검증한다 (로컬 맥 원버튼 파이프라인 전용,
2026-08-30 신설, `local/ml/run.sh` → `make ml`).

## 이 모듈이 하는 일과 안 하는 일

`quant/analyze/ml_scorer.py`는 매일 온라인으로 릿지 회귀를 새로 학습해 그날
후보를 채점하는 **운영 경로**다(judgments 원장에 판단만 남긴다, 실거래 금지 —
아키텍처 테스트가 `quant.trade` 임포트를 막는다). 이 모듈은 그 반대다 —
**연구 평면**에서 전체 과거 라벨 데이터를 한 번에 모아 purged walk-forward로
OOS 성적을 재고, 더 표현력 있는 모델(GradientBoosting)이 릿지보다 나은지
가늠한다. **아무것도 자동 반영하지 않는다** — 리포트와 모델 파일만 낸다.
참전 여부는 사람이 리포트를 보고 판단한다.

## 표본이 왜 작은가 (다시 재지 마라, `local/ml/run.sh`가 실측한다)

`local/ml/run.sh`가 매일 EC2 MySQL에서 `selection ⋈ forward_return`(D+1)을
받아온다. 2026-08-30 실측: KR 10거래일 / US 11거래일 — `ml_scorer.MIN_TRAIN_DAYS`
(30) 미만이다. 이 문턱 미만이면 `main()`이 학습을 아예 하지 않고 "표본 수집
중" 한 줄만 내고 exit 0 한다(에러가 아니다) — 그게 지금 매일의 정상 상태다.

## 왜 `data/ledger/selections.jsonl`이 아니라 MySQL인가

`quant/control/selections.py`의 의도는 각 행의 `outcome_*`를 사후 갱신하는
것이지만, 2026-08-30 EC2 실측(`selections.jsonl` 2,133행 전수 확인)으로는
`outcome_filled`가 **단 한 건도 True가 아니다** — 전방 수익률은 실제로는
MySQL `forward_return` 테이블에만 있고 JSONL에는 반영되지 않는다. 그래서
`local/ml/run.sh`는 `local/ml/remote_dump.py`로 그 MySQL 조인 결과를 직접
받아온다. 이 모듈은 그 결과만 안다 — JSONL의 `outcome_*`는 신경 쓰지 않는다.

## 왜 GradientBoosting인가 (릿지를 대체하자는 게 아니다)

`ml_scorer.py`의 릿지는 소표본(거래일 10일)에서 과최적합을 억제하려 일부러
단순하게 잡은 온라인 모델이다. 이 하네스는 표본이 30일+ 쌓였을 때 비선형
상호작용을 포착할 여지가 있는지 **탐색**하는 용도라 GradientBoosting을 쓴다
— 두 모델 중 뭘 쓸지는 사람이 리더보드 실현 성과로 정한다.

## Purged fold의 단위가 "행"이 아니라 "거래일"인 이유

같은 `session_date` 안에 여러 종목(행)이 있다. 행 단위로 순서를 매기면 같은
날의 행들이 fold 경계에서 train/test로 갈라져 `purged_cv`가 막으려는 라벨
누수(그날 관측 여러 개가 같은 D+1 가격 구간을 공유)를 못 막는다. 그래서
`day_purged_splits`가 거래일 인덱스에서 purge/embargo 대상을 정하고, 그
거래일에 속한 행 전체를 통째로 한쪽(train 또는 test)에 배정한다.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date as _date
from pathlib import Path

import numpy as np

from quant.analyze import ml_scorer
from quant.backtest.purged_cv import purged_splits

log = logging.getLogger(__name__)

# D+1 전방수익률 — purged_cv의 label_horizon 단위(거래일)와 그대로 대응한다.
LABEL_HORIZON_DAYS = 1
DEFAULT_N_FOLDS = 4
DEFAULT_EMBARGO_PCT = 0.01
DEFAULT_RANDOM_STATE = 42


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


# ------------------------------------------------------------------ purged 분할(거래일 단위)

def day_purged_splits(
    rows: list[dict], n_folds: int,
    embargo_pct: float = DEFAULT_EMBARGO_PCT,
    label_horizon: int = LABEL_HORIZON_DAYS,
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

def _feature_target_matrices(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`ml_scorer.FEATURE_NAMES`/`to_matrix`를 그대로 재사용 — 운영 채점기와
    같은 피처 정의로 학습한다. 타깃은 D+1 수익률(bps)과 그 부호(이진)."""
    X = ml_scorer.to_matrix(rows)
    y_bps = np.array([float(r.get("return_bps") or 0.0) for r in rows], dtype=float)
    y_bin = (y_bps > 0).astype(int)
    return X, y_bps, y_bin


# ------------------------------------------------------------------ OOS 평가

def evaluate_oos(
    rows: list[dict], n_folds: int = DEFAULT_N_FOLDS,
    embargo_pct: float = DEFAULT_EMBARGO_PCT,
    label_horizon: int = LABEL_HORIZON_DAYS,
    random_state: int = DEFAULT_RANDOM_STATE,
) -> dict:
    """Purged walk-forward OOS 평가. sklearn은 여기서만 지연 임포트한다 —
    `research` 그룹 선택 의존성이라 표본 게이트 미달일 땐 아예 안 불린다."""
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import precision_score, roc_auc_score

    X_all, _, y_bin_all = _feature_target_matrices(rows)
    base_rate = float(y_bin_all.mean()) if len(y_bin_all) else float("nan")

    fold_metrics: list[dict] = []
    for train_idx, test_idx in day_purged_splits(rows, n_folds, embargo_pct, label_horizon):
        if len(train_idx) < 10 or len(test_idx) == 0:
            fold_metrics.append({
                "auc": None, "precision": None,
                "n_train": len(train_idx), "n_test": len(test_idx),
                "skipped": "표본 부족",
            })
            continue
        y_tr = y_bin_all[train_idx]
        y_te = y_bin_all[test_idx]
        if len(set(y_tr.tolist())) < 2 or len(set(y_te.tolist())) < 2:
            fold_metrics.append({
                "auc": None, "precision": None,
                "n_train": len(train_idx), "n_test": len(test_idx),
                "skipped": "train/test 한쪽이 단일 클래스",
            })
            continue
        X_tr, medians = ml_scorer.impute_median(X_all[train_idx])
        X_te = ml_scorer.fill_missing(X_all[test_idx], medians)

        model = GradientBoostingClassifier(random_state=random_state)
        model.fit(X_tr, y_tr)
        proba = model.predict_proba(X_te)[:, 1]
        pred = model.predict(X_te)
        fold_metrics.append({
            "auc": float(roc_auc_score(y_te, proba)),
            "precision": float(precision_score(y_te, pred, zero_division=0)),
            "n_train": len(train_idx), "n_test": len(test_idx),
            "skipped": None,
        })

    valid = [m for m in fold_metrics if m["auc"] is not None]
    mean_auc = float(np.mean([m["auc"] for m in valid])) if valid else None
    mean_prec = float(np.mean([m["precision"] for m in valid])) if valid else None
    return {
        "n_rows": len(rows),
        "n_days": train_day_count(rows),
        "base_rate": base_rate,
        "n_folds": n_folds,
        "folds": fold_metrics,
        "mean_oos_auc": mean_auc,
        "mean_oos_precision": mean_prec,
        # v1: 기본 하이퍼파라미터만 쓴다 — 탐색 0회(표본이 작아 탐색 자체가
        # 과최적합 위험이다). 탐색을 도입하면 이 숫자를 갱신해서 신고할 것.
        "hyperparam_search_trials": 0,
    }


def fit_final_model(rows: list[dict], random_state: int = DEFAULT_RANDOM_STATE):
    """전체 데이터로 최종 모델을 적합 — 리포트의 피처 중요도 + 저장용 모델
    파일을 낸다. **이 모델은 배포되지 않는다** — 참고용 산출물일 뿐이다."""
    from sklearn.ensemble import GradientBoostingClassifier

    X, _, y_bin = _feature_target_matrices(rows)
    if len(set(y_bin.tolist())) < 2:
        return None, {}
    X_imputed, _ = ml_scorer.impute_median(X)
    model = GradientBoostingClassifier(random_state=random_state)
    model.fit(X_imputed, y_bin)
    importances = dict(zip(ml_scorer.FEATURE_NAMES, model.feature_importances_.tolist()))
    return model, importances


# ------------------------------------------------------------------ 리포트 렌더링

def render_report_md(results: dict[str, dict], run_date: str, skipped: dict[str, int] | None = None,
                     min_train_days: int = ml_scorer.MIN_TRAIN_DAYS) -> str:
    """`results`: market → {"metrics": evaluate_oos 결과, "importances": dict}."""
    lines = [f"# ML 학습 리포트 — {run_date}", ""]
    for market, r in results.items():
        m = r["metrics"]
        lines.append(f"## {market}")
        lines.append("")
        if m["base_rate"] == m["base_rate"]:  # NaN 체크
            lines.append(f"- 표본: 거래일 {m['n_days']}일, 행 {m['n_rows']}개, "
                         f"기저율(D+1 양수 수익 비율) {m['base_rate']:.1%}")
        else:
            lines.append("- 표본: 부족")
        lines.append(f"- Purged walk-forward fold 수: {m['n_folds']} "
                     f"(purge+embargo 단위: 거래일)")
        if m["mean_oos_auc"] is not None:
            lines.append(f"- OOS AUC 평균: {m['mean_oos_auc']:.3f} (0.5=무작위 기준선)")
            lines.append(f"- OOS 정밀도 평균: {m['mean_oos_precision']:.3f} "
                         f"(기저율 {m['base_rate']:.1%} 대비)")
        else:
            lines.append("- OOS AUC/정밀도: 산출 불가(fold마다 표본 부족 또는 단일 클래스)")
        lines.append(f"- 하이퍼파라미터 탐색 수: {m['hyperparam_search_trials']}"
                     f"(기본값만 사용 — 표본이 작아 탐색 자체를 생략했다)")
        lines.append("")
        lines.append("### fold별 상세")
        for i, f in enumerate(m["folds"]):
            if f["skipped"]:
                lines.append(f"  - fold {i}: 건너뜀({f['skipped']}, "
                             f"train={f['n_train']} test={f['n_test']})")
            else:
                lines.append(f"  - fold {i}: AUC={f['auc']:.3f} 정밀도={f['precision']:.3f} "
                             f"(train={f['n_train']} test={f['n_test']})")
        lines.append("")
        imp = r.get("importances") or {}
        if imp:
            lines.append("### 피처 중요도 (전체 데이터로 적합한 최종 모델, 참고용 — 배포 안 함)")
            for name, val in sorted(imp.items(), key=lambda kv: -kv[1]):
                lines.append(f"  - {name}: {val:.3f}")
        else:
            lines.append("### 피처 중요도: 산출 불가(단일 클래스)")
        lines.append("")
    if skipped:
        skip_note = " · ".join(f"{mkt} {d}/{min_train_days}" for mkt, d in skipped.items())
        lines.append(f"참고: 표본 부족으로 건너뛴 시장 — {skip_note}")
        lines.append("")
    lines.append("---")
    lines.append("참전 제안은 사람이 결정: 리포트를 보고 판단하라.")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ML 학습 하네스 v1 — purged walk-forward + GradientBoosting")
    parser.add_argument("--labeled-json", required=True, type=Path,
                        help="local/ml/remote_dump.py 출력(JSON 배열) 경로")
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="리포트/모델을 낼 디렉터리 (local/ml/out/YYYY-MM-DD/)")
    parser.add_argument("--min-train-days", type=int, default=ml_scorer.MIN_TRAIN_DAYS,
                        help=f"게이트 문턱 (기본 ml_scorer.MIN_TRAIN_DAYS={ml_scorer.MIN_TRAIN_DAYS})")
    parser.add_argument("--n-folds", type=int, default=DEFAULT_N_FOLDS)
    parser.add_argument("--markets", default="KR,US")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    rows = load_labeled_rows(args.labeled_json)
    by_market = rows_by_market(rows)

    results: dict[str, dict] = {}
    skipped: dict[str, int] = {}
    for market in args.markets.split(","):
        market_rows = by_market.get(market, [])
        days = train_day_count(market_rows)
        # 게이트를 로컬에서도 재는 판정 함수 — ml_scorer.py 와 같은 함수를 그대로
        # 재사용한다(임계값을 로컬에서 다시 정의하지 않는다).
        if not ml_scorer.enough_sample(days, args.min_train_days):
            skipped[market] = days
            continue
        n_folds = max(2, min(args.n_folds, days // 3))
        metrics = evaluate_oos(market_rows, n_folds=n_folds, label_horizon=LABEL_HORIZON_DAYS)
        model, importances = fit_final_model(market_rows)
        results[market] = {"metrics": metrics, "importances": importances}
        if model is not None:
            import joblib
            args.out_dir.mkdir(parents=True, exist_ok=True)
            joblib.dump(model, args.out_dir / f"model_{market}.joblib")

    if not results:
        gate_msg = " · ".join(
            f"{mkt} {skipped.get(mkt, train_day_count(by_market.get(mkt, [])))}/{args.min_train_days}"
            for mkt in args.markets.split(",")
        )
        print(f"표본 수집 중 {gate_msg} — 학습 생략")
        return 0

    run_date = _date.today().isoformat()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    report = render_report_md(results, run_date, skipped, args.min_train_days)
    (args.out_dir / "report.md").write_text(report, encoding="utf-8")

    summary = {
        market: {
            "mean_oos_auc": r["metrics"]["mean_oos_auc"],
            "mean_oos_precision": r["metrics"]["mean_oos_precision"],
            "base_rate": r["metrics"]["base_rate"],
            "n_days": r["metrics"]["n_days"],
            "n_rows": r["metrics"]["n_rows"],
        }
        for market, r in results.items()
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"학습 완료 — {', '.join(results.keys())}. 리포트: {args.out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
