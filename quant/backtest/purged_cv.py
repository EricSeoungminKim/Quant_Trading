"""Purged & embargoed 시계열 교차검증 — 라벨 누수를 막는 분할 (2026-08-28).

## 왜 필요한가 (우리 데이터가 정확히 그 구조다)

`quant/control/outcomes.py`는 선정 원장의 각 행에 **전방 수익률**을 채운다 —
D+1, D+5, D+20(`judgment.HOLD_HORIZONS`). 즉 8월 1일 관측의 라벨은 **8월 21일까지의
가격**으로 결정된다.

그런 데이터를 K-fold로 아무렇게나 자르면 이렇게 된다:

    훈련: 8/1의 관측 (라벨은 8/21까지의 가격을 씀)
    테스트: 8/10의 관측

훈련 표본이 이미 테스트 구간의 가격을 알고 있다. 모델은 그 겹침을 학습하고,
성과는 부풀고, 실전에서 사라진다. **성과가 좋아 보이는 이유가 데이터 분할이면
그건 전략이 아니다.**

두 가지 조치로 막는다 (López de Prado, *Advances in Financial Machine Learning*,
2018, Ch. 7 "Cross-Validation in Finance"):

1. **Purging** — 라벨 구간이 테스트 블록과 겹치는 훈련 관측을 **버린다**.
   테스트 블록 양쪽 `label_horizon`만큼이 대상이다(테스트 이전 관측의 라벨이
   테스트 안으로 뻗는 경우 + 테스트 관측의 라벨이 테스트 뒤로 뻗어 그 구간의
   훈련 관측과 겹치는 경우).
2. **Embargo** — purge 직후 구간을 추가로 더 버린다. 수익률은 계열상관이 있어
   라벨 구간이 끝난 직후 표본도 여전히 테스트와 정보를 공유한다. 겹침이 0이라고
   상관이 0인 건 아니다.

## 기존 walk-forward와의 관계

`quant/backtest/walkforward.py`는 **전략 백테스트**를 여러 시간 창에서 반복
실행한다(fold = 과거 시각 창, 훈련 개념 없음). 이 모듈은 **관측-라벨 표**를
훈련/테스트로 나눈다(예: 선정 원장의 속성 벡터 → D+5 수익률). 목적도 입력도
다르므로 그쪽 fold 계산을 건드리지 않고 별도 모듈로 둔다.

순수 함수만 있다. 인덱스만 다루고 데이터를 모른다 — 그래서 pandas도 필요 없다.
"""
from __future__ import annotations

__all__ = ["embargo_size", "fold_blocks", "purged_splits"]


def embargo_size(n_obs: int, embargo_pct: float) -> int:
    """엠바고 관측 개수 = `floor(n_obs * embargo_pct)`.

    내림이다 — 표본이 작을 때 엠바고가 0이 되는 것을 숨기지 않는다(엠바고 0은
    "적용했다고 착각"보다 낫다. 호출부가 0을 보고 비율을 올릴 수 있다).
    """
    if not 0.0 <= embargo_pct < 1.0:
        raise ValueError(f"embargo_pct는 [0, 1) 범위여야 한다: {embargo_pct!r}")
    return int(n_obs * embargo_pct)


def fold_blocks(n_obs: int, n_folds: int) -> list[tuple[int, int]]:
    """관측을 `n_folds`개의 **연속** 블록으로 나눈 `[(start, end_inclusive), ...]`.

    시계열이므로 셔플하지 않는다. 나머지는 앞쪽 블록부터 하나씩 나눠 갖는다
    (블록 크기 차이는 최대 1).
    """
    if n_folds < 2:
        raise ValueError(f"n_folds는 2 이상이어야 한다: {n_folds!r}")
    if n_obs < n_folds:
        raise ValueError(f"관측({n_obs})이 fold 수({n_folds})보다 적다 — 분할할 수 없다")
    base, extra = divmod(n_obs, n_folds)
    blocks: list[tuple[int, int]] = []
    start = 0
    for i in range(n_folds):
        size = base + (1 if i < extra else 0)
        blocks.append((start, start + size - 1))
        start += size
    return blocks


def purged_splits(
    n_obs: int, n_folds: int, embargo_pct: float = 0.01, label_horizon: int = 0,
) -> list[tuple[list[int], list[int]]]:
    """`[(train_idx, test_idx), ...]` — purge + embargo를 적용한 시계열 K-fold.

    인자:
      n_obs         관측 개수. 인덱스는 **시간 오름차순**이라고 가정한다.
      n_folds       테스트 블록 개수(2 이상).
      embargo_pct   purge 뒤 추가로 버릴 구간의 비율(전체 관측 대비).
      label_horizon 라벨이 뻗는 관측 수. D+5 전방수익률이면 5. **0이면 purge가
                    없다** — 라벨이 관측 시점에 즉시 확정되는 경우에만 맞다.

    버리는 훈련 인덱스(테스트 블록이 `[t0, t1]`일 때):
      - `[t0 - label_horizon, t0 - 1]` — 라벨이 테스트 안으로 뻗는 이전 관측.
      - `[t1 + 1, t1 + label_horizon + embargo]` — 테스트 관측의 라벨이 뻗는
        구간 + 계열상관을 위한 엠바고.

    **훈련이 빈 fold를 감추지 않는다.** purge+embargo가 훈련을 다 먹었다면 그건
    "그 라벨 지평에 비해 표본이 너무 짧다"는 사실이고, 조용히 fold를 지우면
    그 사실이 사라진다. 호출부가 `len(train)`을 확인해야 한다.
    """
    if label_horizon < 0:
        raise ValueError(f"label_horizon은 음수일 수 없다: {label_horizon!r}")
    emb = embargo_size(n_obs, embargo_pct)

    splits: list[tuple[list[int], list[int]]] = []
    for t0, t1 in fold_blocks(n_obs, n_folds):
        test_idx = list(range(t0, t1 + 1))
        left_drop_from = t0 - label_horizon
        right_drop_to = t1 + label_horizon + emb
        train_idx = [
            i for i in range(n_obs)
            if not (t0 <= i <= t1)                      # 테스트 자신
            and not (left_drop_from <= i < t0)          # purge (좌)
            and not (t1 < i <= right_drop_to)           # purge (우) + 엠바고
        ]
        splits.append((train_idx, test_idx))
    return splits
