"""Purged & embargoed 교차검증 — 실제로 **버리는지**를 인덱스 단위로 확인한다.

이 모듈의 실패 모드는 "안 돈다"가 아니라 **"돌지만 아무것도 안 버린다"**다.
그러면 성과가 부풀고, 부푼 성과는 버그처럼 보이지 않는다. 그래서 여기서는
"분할이 나온다"가 아니라 **어떤 인덱스가 사라졌는지**를 못 박는다.
"""
from __future__ import annotations

import pytest

from quant.backtest.purged_cv import embargo_size, fold_blocks, purged_splits


# ── 블록 분할 ──────────────────────────────────────────────────────────────

def test_fold_blocks_partition_everything_in_order():
    blocks = fold_blocks(10, 3)
    assert blocks == [(0, 3), (4, 6), (7, 9)]  # 나머지는 앞 블록이 하나씩 가져간다
    covered = [i for start, end in blocks for i in range(start, end + 1)]
    assert covered == list(range(10))  # 빠짐도 겹침도 없고, 시간순이다


def test_fold_blocks_reject_degenerate_inputs():
    with pytest.raises(ValueError):
        fold_blocks(10, 1)   # fold 1개는 교차검증이 아니다
    with pytest.raises(ValueError):
        fold_blocks(3, 5)    # 관측보다 fold가 많다


# ── 엠바고 크기 ────────────────────────────────────────────────────────────

def test_embargo_size_floors_and_rejects_bad_pct():
    assert embargo_size(490, 0.01) == 4      # 내림 — 4.9가 아니라 4
    assert embargo_size(50, 0.01) == 0       # 표본이 작으면 0이 되는 걸 숨기지 않는다
    with pytest.raises(ValueError):
        embargo_size(100, 1.0)


# ── purge + embargo ────────────────────────────────────────────────────────

def test_no_embargo_no_horizon_is_plain_kfold():
    """지평 0 · 엠바고 0이면 훈련은 테스트의 여집합 그대로여야 한다(기준선)."""
    splits = purged_splits(10, 5, embargo_pct=0.0, label_horizon=0)
    for train, test in splits:
        assert sorted(train + test) == list(range(10))
        assert not set(train) & set(test)


def test_purge_removes_labels_that_reach_into_the_test_block():
    """D+2 라벨이면 테스트 시작 직전 2개는 테스트 구간 가격을 이미 알고 있다."""
    train, test = purged_splits(10, 5, embargo_pct=0.0, label_horizon=2)[2]
    assert test == [4, 5]
    assert 2 not in train and 3 not in train  # 라벨이 4 이상으로 뻗는다
    assert 1 in train                          # 라벨이 3까지 — 겹치지 않는다


def test_embargo_removes_the_indices_right_after_the_test_block():
    """엠바고는 겹침이 아니라 **계열상관** 때문에 버린다 — 지평 0에서도 버려야 한다."""
    train, test = purged_splits(10, 5, embargo_pct=0.2, label_horizon=0)[2]
    assert test == [4, 5]
    assert embargo_size(10, 0.2) == 2
    assert 6 not in train and 7 not in train
    assert 8 in train


def test_known_split_with_both_purge_and_embargo():
    """알려진 값 한 벌 — purge(좌 1) + purge(우 1) + 엠바고(1)."""
    splits = purged_splits(10, 5, embargo_pct=0.1, label_horizon=1)
    train, test = splits[2]
    assert test == [4, 5]
    assert train == [0, 1, 2, 8, 9]  # 3(좌 purge), 6(우 purge), 7(엠바고) 제거


def test_train_and_test_never_overlap_across_all_folds():
    for train, test in purged_splits(100, 5, embargo_pct=0.05, label_horizon=5):
        assert not set(train) & set(test)


def test_empty_train_fold_is_surfaced_not_hidden():
    """purge+embargo가 훈련을 다 먹으면 그건 "표본이 짧다"는 사실이다.

    fold를 조용히 지우면 그 사실이 사라지고, 남은 fold만 보고 "검증했다"고
    말하게 된다. 빈 훈련 fold는 그대로 나와야 한다."""
    splits = purged_splits(6, 3, embargo_pct=0.0, label_horizon=10)
    assert len(splits) == 3
    assert any(len(train) == 0 for train, _ in splits)


def test_reject_negative_label_horizon():
    with pytest.raises(ValueError):
        purged_splits(10, 2, label_horizon=-1)
