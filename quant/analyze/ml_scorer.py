"""학습형 선정자 `ml_scorer` — 과거 선정→전방수익률로 릿지 회귀를 학습해 오늘
후보를 채점한다 (2026-08-28).

## 이 직원의 자리

`watch_scorer`(결정론적 규칙)·`ai_trader`(LLM 3역할 토론)에 이은 **세 번째
선수**다. 같은 잣대(judgments 원장 → outcomes → 리더보드)로 채점받고, **판단만
기록한다** — 주문·워치리스트에 절대 닿지 않는다(ai_trader와 동일 계약, 아키텍처
테스트가 `quant.trade` 임포트를 막는다).

## 표본이 왜 이렇게 작은가 (2026-08-28 실측 — 다시 재지 마라)

MySQL `selection` 1,393행 / `forward_return` D+1 883행이지만, **독립 거래일은
단 10일**, 종목 566개다. 속성 13개 중 D+1 수익률과 스피어 상관이 있는 것은 넷뿐:
relative_volume −0.464, change_pct −0.129, foreign_buy_streak +0.112,
trending_score100 +0.099. 나머지는 |0.05| 이하 — 사실상 노이즈다.

**10일로 13개 특성을 학습하면 100% 과최적합이다.** `MIN_TRAIN_DAYS` 게이트가
이걸 코드로 강제한다 — 지금은 이 게이트만 동작하는 게 정상 상태다.

## 워크포워드

학습 데이터는 예측 대상 `session_date` 보다 **엄격히 과거**여야 한다. SQL
WHERE 절(호출부, `quant/apps/cli.py`)이 1차 방어선이고, `training_rows_before`가
코드 구조로 다시 강제하는 2차 방어선이다 — 실거래 손실로 이어지는 종류의
버그라 이중으로 막는다.

## 이 모듈이 증명하는 것

**아직 아무것도.** 표본 게이트가 열릴 만큼 거래일이 쌓이기 전까지 이 모델의
성과를 주장할 근거는 없다. 리더보드(`quant/control/leaderboard.py`)가 실현
수익으로 채점해 이겨야만 승격 후보가 된다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from quant.control.judgment import selection_attributes
from quant.core.models import Judgment, input_hash

PRODUCER = "ml_scorer"
PRODUCER_VERSION = "1"

# 학습에 요구하는 최소 독립 거래일. 2026-08-28 실측 근거는 모듈 docstring 참고 —
# 지금 10일인 표본으로는 이 문턱 아래에서 계속 결근(=판단 없음)하는 게 정상이다.
MIN_TRAIN_DAYS = 30

# 릿지 정규화 강도. 소표본(거래일 10일 대) 대비 특성 13개는 과최적합이 거의
# 확실하므로 강하게 수축시킨다 — sklearn 기본값(1.0)보다 훨씬 크게 잡는다.
DEFAULT_RIDGE_LAMBDA = 10.0

# 선정 원장(`selection` 테이블/`selections.jsonl`)에서 학습에 쓰는 속성 13개.
# 2026-08-28 실측 스피어 상관: relative_volume/change_pct/foreign_buy_streak/
# trending_score100 만 유의미하고 나머지는 노이즈에 가깝다 — 그래도 전부 넣고
# 릿지가 수축시키게 둔다(수동으로 특성을 더 줄이면 그 선택 자체가 소표본에
# 과최적합된 사람 판단이 된다).
FEATURE_NAMES: tuple[str, ...] = (
    "ai_score100", "trending_score100", "news_articles_today", "news_streak_days",
    "in_ranking", "ranking_bullish", "best_board_rank", "n_boards",
    "relative_volume", "foreign_buy_streak", "inst_buy_streak", "upside_pct",
    "change_pct",
)

# percentile 점수(0~100)가 이 값 이상이면 pass — "오늘 후보 중 상대적으로
# 예측 수익률 상위 절반"이라는 뜻이다. LLM 픽(ai_trader의 MAX_PICKS)과 달리
# 결정론적 순위라 별도의 인원 상한을 둘 이유가 없다.
PASS_PERCENTILE = 50.0


# ------------------------------------------------------------------ 표본 게이트

def enough_sample(train_days: int, min_days: int = MIN_TRAIN_DAYS) -> bool:
    """학습에 쓸 독립 거래일이 문턱 이상인가. 미만이면 호출부가 학습·예측을
    아예 하지 않는다 — "표본 부족"은 실패가 아니라 정직한 결근이다."""
    return train_days >= min_days


# ------------------------------------------------------------------ 워크포워드

def training_rows_before(rows: list[dict], predict_date: str,
                         date_key: str = "session_date") -> list[dict]:
    """예측일보다 **엄격히 이전**인 행만 남긴다.

    호출부의 SQL `WHERE session_date < %s` 가 1차 방어선이고, 이 함수가 코드
    구조로 다시 강제하는 2차 방어선이다 — 조인·쿼리 실수가 있어도 미래 데이터가
    조용히 학습에 섞이지 않도록 이중으로 막는다. 문자열 비교로 충분한 이유:
    ISO 날짜(YYYY-MM-DD)는 사전식 비교가 곧 시간순 비교다.
    """
    return [r for r in rows if str(r.get(date_key)) < str(predict_date)]


# ------------------------------------------------------------------ 특성 벡터

def feature_vector(attrs: dict, feature_names: tuple[str, ...] = FEATURE_NAMES) -> list[float]:
    """속성 dict → 고정 순서 실수 벡터. 없거나 변환 불가한 값은 NaN(결측)이다 —
    0 으로 위장하지 않는다(이 저장소 원장 전반의 원칙과 동일)."""
    out: list[float] = []
    for name in feature_names:
        v = attrs.get(name)
        try:
            out.append(float(v) if v is not None else float("nan"))
        except (TypeError, ValueError):
            out.append(float("nan"))
    return out


def to_matrix(attr_rows: list[dict], feature_names: tuple[str, ...] = FEATURE_NAMES) -> np.ndarray:
    """속성 dict 목록 → (n, len(feature_names)) 행렬. 학습 행(DB 열 이름)과
    후보 행(`selection_attributes` 출력)이 같은 키 이름을 쓰므로 둘 다 이
    함수 하나로 벡터화된다."""
    if not attr_rows:
        return np.empty((0, len(feature_names)))
    return np.array([feature_vector(a, feature_names) for a in attr_rows], dtype=float)


def fill_missing(X: np.ndarray, medians: np.ndarray) -> np.ndarray:
    """열별 결측(NaN)을 주어진 중앙값으로 대체.

    학습 중앙값을 예측 행렬에도 그대로 적용해야 한다 — 예측 시점에 후보들만의
    중앙값을 새로 계산하면 학습·예측이 서로 다른 기준으로 결측을 메우게 된다."""
    X = np.asarray(X, dtype=float)
    out = X.copy()
    nan_mask = np.isnan(out)
    if nan_mask.any():
        _, col_idx = np.where(nan_mask)
        out[nan_mask] = medians[col_idx]
    return out


def impute_median(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """학습 행렬(X, 결측=NaN) → (대체된 X, 열별 중앙값). 학습 시엔 X 자신의
    중앙값을 쓴다 — 예측 시엔 이 중앙값을 `fill_missing` 으로 재사용한다."""
    medians = np.nanmedian(X, axis=0)
    # 열 전체가 결측이면 nanmedian 도 NaN 이다 — 0.0 으로 떨어뜨려 폭발을 막는다.
    medians = np.where(np.isnan(medians), 0.0, medians)
    return fill_missing(X, medians), medians


# ------------------------------------------------------------------ 릿지 회귀

def fit_ridge(X: np.ndarray, y: np.ndarray, lam: float = DEFAULT_RIDGE_LAMBDA) -> dict:
    """표준화 + 릿지 회귀(closed-form). numpy만 사용(sklearn 미설치, 새 의존성
    없음). `lam` 근거는 모듈 docstring — 소표본 과최적합을 억제하려 강하게 잡는다.

    반환 model dict: {"mean", "std", "weights", "intercept"} — `predict_scores`
    가 그대로 받아 쓴다."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std_safe = np.where(std == 0, 1.0, std)  # 분산 0인 열(상수)로 나누지 않는다
    Xs = (X - mean) / std_safe

    y_mean = float(y.mean())
    yc = y - y_mean

    n_features = Xs.shape[1]
    # w = (Xs^T Xs + λI)^-1 Xs^T yc — intercept 는 별도로 y 평균을 쓴다(표준화된
    # 특성의 릿지 관례: 절편은 정규화하지 않는다).
    a = Xs.T @ Xs + lam * np.eye(n_features)
    weights = np.linalg.solve(a, Xs.T @ yc)

    return {"mean": mean, "std": std_safe, "weights": weights, "intercept": y_mean}


def predict_scores(model: dict, X: np.ndarray) -> np.ndarray:
    """학습된 모델로 예측 D+1 수익률(bps)을 낸다. 학습 때 쓴 mean/std 로만
    표준화한다 — 예측 데이터 자신의 통계로 다시 표준화하면 학습·예측 기준이
    어긋난다."""
    X = np.asarray(X, dtype=float)
    Xs = (X - model["mean"]) / model["std"]
    return model["intercept"] + Xs @ model["weights"]


def to_percentile_scores(preds: np.ndarray) -> np.ndarray:
    """예측값 배열 → 0~100 퍼센타일 점수(순위 변환, 동률은 평균 순위).

    생산자별 척도가 다르므로 원본 bps 를 그대로 점수로 쓰지 않는다 —
    `Judgment.score` 는 상대 순위로 환산해야 다른 생산자와 비교 가능하다
    (`core.models.Judgment` docstring과 같은 원칙)."""
    x = np.asarray(preds, dtype=float)
    n = x.size
    if n == 0:
        return np.array([])
    if n == 1:
        return np.array([50.0])  # 후보가 하나뿐이면 순위가 없다 — 중앙값으로 둔다

    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(n, dtype=float)
    ranks[order] = np.arange(n, dtype=float)

    # 동률 구간은 평균 순위로 묶는다(정렬된 순서 위에서 값이 같은 연속 구간을 찾는다).
    sorted_x = x[order]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_x[j + 1] == sorted_x[i]:
            j += 1
        if j > i:
            avg_rank = (i + j) / 2.0
            ranks[order[i:j + 1]] = avg_rank
        i = j + 1

    return ranks / (n - 1) * 100.0


# ------------------------------------------------------------------ 판단 귀속

def to_judgments(scores: dict[str, float], rows: list[dict],
                 version: str = PRODUCER_VERSION,
                 pass_percentile: float = PASS_PERCENTILE) -> list[Judgment]:
    """오늘 후보 채점 결과 → Judgment 목록. `quant/apps/cli.py` 의
    `cmd_ai_trader`(`quant/analyze/ai_trader.py`)와 **같은 방식** —
    `selection_attributes`/`input_hash` 를 그대로 재사용해 같은 서류를 본
    watch_scorer·ai_trader 판단과 input_hash 가 일치하게 만든다(리더보드
    "같은 입력끼리 비교" 전제, 이 모듈의 제1 계약).

    **전 행을 남긴다** — 픽 상한이 없어도 채점 못 한 종목(학습표본 자체가
    없어 결근한 날)은 있을 수 있으므로 `scores` 에 없는 심볼도 reject/None 으로
    기록한다(selection_judgment/ai_trader.to_judgments 와 같은 원칙)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out: list[Judgment] = []
    for row in rows:
        attrs = selection_attributes(row)
        sym = str(row.get("symbol") or "")
        score = scores.get(sym)
        if score is None:
            verdict = "reject"
            rationale = "학습 표본에 없는 종목 — 채점 못 함"
        else:
            verdict = "pass" if score >= pass_percentile else "reject"
            rationale = f"릿지 회귀 예측 D+1 순위 {score:.0f}/100(오늘 후보 내 상대순위)"
        out.append(Judgment(
            producer=PRODUCER,
            producer_version=str(version),
            input_hash=input_hash(attrs),
            market=str(row.get("market") or ""),
            symbol=sym,
            session_date=str(row.get("date") or ""),
            score=score,
            verdict=verdict,
            rationale=rationale[:255],
            ts=now,
        ))
    return out


# ------------------------------------------------------------------ 텔레그램 카드

def daily_note(scores: dict[str, float], market: str, names: dict[str, str],
               top_n: int = 5) -> str:
    """오늘 채점 결과 상위 `top_n` 개를 담은 짧은 카드. `rows` 가 비어 있지 않은
    호출부에서만 부르므로(호출부가 이미 "후보 없음"을 걸러낸다) 항상 문자열을
    돌려준다 — ai_trader.daily_note 와 달리 픽 없음으로 조용해질 이유가 없다
    (LLM 결근과 달리 릿지 예측은 후보가 있으면 항상 순위를 낸다)."""
    flag = "🇰🇷" if market == "KR" else "🇺🇸"
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    lines = [f"🤖 ML 스코어러 오늘의 순위 {flag}"]
    for sym, sc in ranked:
        name = names.get(sym) or sym
        lines.append(f"  {name} ({sym}) {sc:.0f}점")
    lines.append("※ 릿지 회귀 예측 순위일 뿐 주문하지 않는다 — 성적은 매일 16:20 "
                 "장마감 리포트의 리더보드가 매긴다(같은 서류를 본 watch_scorer·"
                 "ai_trader 와 경쟁).")
    return "\n".join(lines)
