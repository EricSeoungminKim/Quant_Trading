"""가감점 합계 → 0~100 점수 변환 공용 모듈.

`+3`/`-2` 같은 가감점 합계는 기준을 모르면 해석이 안 된다. 100점 만점으로
바꾸면 "몇 점인가"만으로 뜻이 통한다. symbol_score 와 briefing 양쪽이 이
모듈을 공유해 변환 규칙을 한 곳에서만 정의한다.
"""
from __future__ import annotations

NEUTRAL = 50


def to_100(raw: int, span: int) -> int:
    """가감점 합계를 0~100 으로 선형 변환. 50 = 중립.

    span 은 **이론상 가능한 최대 가점 수**(고정값)다. 실제로 발동한 요인 수로
    나누지 않는다 — 그러면 근거가 하나뿐인 종목이 100점을 받는다. 고정 분모를
    쓰면 근거가 부족할수록 자연히 50점 근처에 머물러, 점수가 곧 확신의 크기가
    된다.
    """
    if span <= 0:
        return NEUTRAL
    frac = max(-1.0, min(1.0, raw / span))
    return round(NEUTRAL + frac * NEUTRAL)


def label_100(score100: int, positive: str, negative: str) -> str:
    """0~100 → 5단계 서열 라벨."""
    if score100 >= 75:
        return f"강한 {positive}"
    if score100 >= 60:
        return f"약한 {positive}"
    if score100 <= 25:
        return f"강한 {negative}"
    if score100 <= 40:
        return f"약한 {negative}"
    return "중립"
