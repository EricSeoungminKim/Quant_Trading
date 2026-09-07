"""`quant/control/multiple_testing.py::benjamini_hochberg` — 손으로 검산한
고전 예시 + 경계 조건."""
from __future__ import annotations

from quant.control.multiple_testing import benjamini_hochberg


def test_classic_ten_pvalue_example_rejects_first_two():
    """교과서 고전 예시(q=0.05): 정렬된 p값과 순위별 문턱(i/10*0.05)을 손으로
    비교하면 i=1(0.001<=0.005)과 i=2(0.008<=0.01)만 통과하고, i=3부터는
    문턱을 못 넘는다(0.039 > 0.015 등) — 유의한 두 개만 살아남는다."""
    pvalues = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    out = benjamini_hochberg(pvalues, q=0.05)
    assert out == [True, True, False, False, False, False, False, False, False, False]


def test_classic_example_is_order_independent():
    """입력 순서를 섞어도(정렬은 내부에서만) 같은 두 개(0.001, 0.008)만 통과."""
    pvalues = [0.216, 0.001, 0.212, 0.041, 0.008, 0.042, 0.205, 0.06, 0.074, 0.039]
    out = benjamini_hochberg(pvalues, q=0.05)
    flagged = {p for p, ok in zip(pvalues, out) if ok}
    assert flagged == {0.001, 0.008}


def test_empty_input_returns_empty_list():
    assert benjamini_hochberg([], q=0.10) == []


def test_all_ones_never_pass():
    assert benjamini_hochberg([1.0, 1.0, 1.0], q=0.10) == [False, False, False]


def test_ties_are_judged_together():
    """동점 p값은 함께 통과하거나 함께 실패한다(순위가 오를수록 문턱도 커지므로)."""
    out = benjamini_hochberg([0.01, 0.01, 0.01], q=0.10)
    assert out == [True, True, True]

    out2 = benjamini_hochberg([0.5, 0.5, 0.5], q=0.10)
    assert out2 == [False, False, False]


def test_single_candidate_matches_raw_threshold():
    """후보가 하나뿐이면 BH 문턱은 그냥 q 자체 — 보정할 다른 검정이 없다."""
    assert benjamini_hochberg([0.05], q=0.10) == [True]
    assert benjamini_hochberg([0.15], q=0.10) == [False]
    # 경계값(p == q)은 통과(<=).
    assert benjamini_hochberg([0.10], q=0.10) == [True]
