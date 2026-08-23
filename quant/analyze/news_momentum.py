"""뉴스 발행량 z-score — 오늘 언급 건수가 최근 흐름 대비 얼마나 튀었는지 (H-2 Task 4).

## 왜 오늘을 baseline 에서 뺐나

평균·표준편차에 오늘 값을 같이 넣으면, 오늘 급증한 바로 그 값이 자기 자신의
baseline 을 함께 부풀린다 — 표준편차가 커져 z 가 인위적으로 눌린다(self-influence).
그래서 오늘은 "채점 대상"으로만 쓰고, 평균·표준편차는 **오늘을 뺀 과거 이력만으로**
계산한다(표준 이상탐지 관행 — 관측치를 그 관측치의 baseline 계산에 넣지 않는다).

표준편차는 표본표준편차(`statistics.stdev`, n-1)를 쓴다 — 과거 며칠은 그 종목의
언급 건수 전체 모집단이 아니라 표본이고, 표본표준편차가 소표본에서 분산을
덜 과소평가한다.

## 왜 σ=0 이 None 인가

과거 며칠이 전부 같은 건수(대부분 0)면 σ=0 이라 나눗셈이 정의되지 않는다. 0 으로
위장하면 "변동 없음"과 "계산 불가"가 같은 값으로 뭉개진다 — selections/judgment
모듈이 이미 지키는 원칙과 같다(0 은 유효한 값, None 은 "못 구했다").
"""
from __future__ import annotations

import json
import statistics
from datetime import date, timedelta
from pathlib import Path

# 오늘 포함 최소 5일(=과거 4일 이상)이 있어야 baseline 이 의미가 있다. 이보다
# 적으면 표준편차 자체가 노이즈라 z 를 계산하지 않는다(None — 0 위장 금지).
MIN_HISTORY_DAYS = 5


def news_zscore(counts: list[int]) -> float | None:
    """과거→오늘 순 일별 언급 건수(오늘 포함) → 오늘의 z-score.

    표본 부족(< `MIN_HISTORY_DAYS`) 또는 과거 이력의 표준편차가 0 이면 None.
    """
    if len(counts) < MIN_HISTORY_DAYS:
        return None
    today = counts[-1]
    history = counts[:-1]
    stdev = statistics.stdev(history)
    if stdev == 0:
        return None
    mean = statistics.fmean(history)
    return (today - mean) / stdev


def daily_counts(path: Path, symbol: str, upto: date, days: int = 20) -> list[int]:
    """`mentions.jsonl` 을 종목별 일별 언급 건수로 집계한다 — 결측일은 0으로 채운다.

    반환은 과거→오늘 순(오늘 포함, 길이 `days`)이다. 원장 자체가 없거나 줄이
    깨져 있어도 예외 없이 0 으로 채운 리스트를 돌려준다 — 다른 원장 리더와
    같은 관례(일부 손상이 전체 계산을 죽이면 안 된다).
    """
    start = upto - timedelta(days=days - 1)
    counts = {start + timedelta(days=i): 0 for i in range(days)}

    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("symbol") != symbol:
                continue
            try:
                d = date.fromisoformat(str(row.get("date")))
            except ValueError:
                continue
            if d in counts:
                counts[d] += 1

    return [counts[start + timedelta(days=i)] for i in range(days)]
