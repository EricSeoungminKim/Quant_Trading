"""Benjamini-Hochberg 다중검정 보정 — 순수 함수, 표준 라이브러리만 사용.

## 왜 필요한가 (결정 ⑧, 2026-09-07)

`quant/control/allocator.py::decide`(자본 자동 강등)와
`quant/control/experiments.py::record_death_watch`(전략 자동 비활성 킬스위치의
입력)는 매일 여러 전략을 동시에 "이 전략이 지고 있는가"로 검정한다. 전략이
17개면 유의수준 0.05(또는 90% 신뢰상한)짜리 검정을 17번 독립적으로 돌리는
것과 같다 — 전부 진짜 엣지가 0이어도(귀무가설이 전부 참이어도) 우연히 유의한
것처럼 보이는 전략이 평균 17*0.05 ≈ 0.85개, 실제로는 이항분포라 1개 이상 나올
확률이 훨씬 높다. 이걸 보정하지 않으면 **자동 판단**(자본 축소·킬스위치)이
우연을 근거로 자산을 움직인다.

Benjamini-Hochberg(BH) 절차는 위양성 비율이 아니라 "발견된 것 중 거짓일
비율"(FDR, False Discovery Rate)을 q 이하로 통제한다 — 본페로니처럼 검정력을
거의 다 죽이지 않으면서도 다중검정 문제를 잡는, 가장 널리 쓰이는 절충안이다.

**사람이 보는 숫자(스코어보드, 리포트)는 이 보정을 받지 않는다** — 사람은
맥락과 함께 원시 p값을 보고 판단할 수 있다. 이 보정은 **자동으로 자본을
움직이거나 전략을 끄는** 결정에만 적용한다.

## 알고리즘

1. p값을 오름차순 정렬: p_(1) <= p_(2) <= ... <= p_(m)
2. 각 순위 i(1-based)에 대해 문턱 i/m * q 를 계산
3. p_(i) <= i/m * q 를 만족하는 **가장 큰 i**를 찾는다 (= k)
4. 순위 1..k(원시 p값이 가장 작은 k개)를 "발견"(유의)으로 표시, 나머지는 기각 안 함

동점 처리: 정렬만 하면 자동으로 처리된다 — 같은 p값이 여럿이면 그중 하나가
문턱을 통과하는 순간 정렬 순서상 앞선(동점인) 것들도 이미 더 낮은 순위에서
같은 값으로 문턱을 통과했거나, 다 같이 통과하거나 다 같이 실패한다(순위가
오를수록 문턱도 커지므로 동점값들은 항상 함께 판정된다).
"""
from __future__ import annotations


def benjamini_hochberg(pvalues: list[float], q: float = 0.10) -> list[bool]:
    """각 p값이 BH 절차 하에서 FDR<=q 로 "유의"(발견)로 판정되는지.

    입력 순서와 같은 순서의 bool 리스트를 반환한다(정렬은 내부에서만 한다).
    빈 입력이면 빈 리스트. 모든 판단은 결정론적이다(난수 없음).
    """
    m = len(pvalues)
    if m == 0:
        return []

    order = sorted(range(m), key=lambda i: pvalues[i])

    # 가장 큰 순위 k(1-based)로, 정렬된 p_(k) <= (k/m)*q 를 만족하는 것을 찾는다.
    max_rank = 0  # 0 = 아무 순위도 통과 못함
    for rank, idx in enumerate(order, start=1):
        threshold = (rank / m) * q
        if pvalues[idx] <= threshold:
            max_rank = rank

    result = [False] * m
    for rank, idx in enumerate(order, start=1):
        if rank <= max_rank:
            result[idx] = True
    return result
