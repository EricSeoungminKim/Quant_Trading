"""`quant.analyze.news_momentum` — 뉴스 발행량 z-score (H-2 Task 4).

`news_zscore` 는 오늘을 뺀 과거 이력으로 평균·표준편차를 구하고(self-influence
방지), `daily_counts` 는 `mentions.jsonl` 을 종목·일자로 집계해 결측일을
0으로 채운다.

파일명이 `test_news_momentum.py` 가 아니라 `test_analyze_news_momentum.py` 인
이유: `tests/test_news_momentum.py` 는 이미 `quant/trade/strategy/news_momentum.py`
(뉴스 모멘텀 개장매수 전략, 거래 평면)의 테스트가 선점하고 있다. 이름이 같아도
서로 다른 평면의 다른 모듈이라 파일을 나눴다 — 기존 테스트를 덮어쓰면 그
전략의 회귀 방지망이 통째로 사라진다.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from quant.analyze.news_momentum import daily_counts, news_zscore


# ── news_zscore ──────────────────────────────────────────────────────────

def test_zscore_of_a_clear_spike_is_positive_and_large():
    # 과거 4일: 1,2,1,2 (변동 있음) → 오늘 10건은 뚜렷한 급증
    counts = [1, 2, 1, 2, 10]

    z = news_zscore(counts)

    assert z is not None
    assert z > 2


def test_zscore_excludes_today_from_the_baseline():
    """오늘 값을 평균·표준편차 계산에 넣으면 급증 자체가 baseline 을 부풀려
    z 가 눌린다 — 오늘을 뺀 계산과 넣은 계산이 달라야 이 설계가 지켜진 것이다."""
    counts = [2, 3, 2, 3, 20]

    z_excluding_today = news_zscore(counts)
    # 오늘까지 포함해 직접 계산하면(잘못된 방식) 분모가 커져 z가 더 작아진다.
    import statistics
    naive_mean = statistics.fmean(counts)
    naive_std = statistics.stdev(counts)
    z_including_today = (counts[-1] - naive_mean) / naive_std

    assert z_excluding_today > z_including_today


def test_fewer_than_five_days_is_none():
    assert news_zscore([1, 1, 1, 5]) is None


def test_exactly_five_days_is_computable():
    # 정확히 5일(과거 4일 + 오늘)이면 표본 부족으로 거절되지 않는다.
    assert news_zscore([0, 1, 0, 1, 5]) is not None


def test_zero_variance_history_is_none_not_zero():
    """과거 며칠이 전부 같은 건수면 표준편차가 0 — 나눗셈 대신 None(0 위장 금지)."""
    assert news_zscore([3, 3, 3, 3, 3]) is None
    assert news_zscore([0, 0, 0, 0, 0]) is None


def test_empty_input_is_none():
    assert news_zscore([]) is None


# ── daily_counts ─────────────────────────────────────────────────────────

def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_daily_counts_aggregates_per_calendar_day(tmp_path):
    path = tmp_path / "mentions.jsonl"
    _write_ledger(path, [
        {"date": "2026-08-14", "symbol": "005930", "title": "a", "link": "l1"},
        {"date": "2026-08-14", "symbol": "005930", "title": "b", "link": "l2"},
        {"date": "2026-08-15", "symbol": "005930", "title": "c", "link": "l3"},
        {"date": "2026-08-15", "symbol": "000660", "title": "d", "link": "l4"},
    ])

    counts = daily_counts(path, "005930", upto=date(2026, 8, 15), days=3)

    # 과거→오늘: 08-13(0), 08-14(2), 08-15(1)
    assert counts == [0, 2, 1]


def test_daily_counts_zero_fills_missing_days(tmp_path):
    path = tmp_path / "mentions.jsonl"
    _write_ledger(path, [
        {"date": "2026-08-10", "symbol": "005930", "title": "a", "link": "l1"},
    ])

    counts = daily_counts(path, "005930", upto=date(2026, 8, 15), days=6)

    assert len(counts) == 6
    assert counts[0] == 1        # 08-10
    assert counts[1:] == [0, 0, 0, 0, 0]


def test_daily_counts_ignores_other_symbols(tmp_path):
    path = tmp_path / "mentions.jsonl"
    _write_ledger(path, [
        {"date": "2026-08-15", "symbol": "000660", "title": "d", "link": "l4"},
    ])

    counts = daily_counts(path, "005930", upto=date(2026, 8, 15), days=3)

    assert counts == [0, 0, 0]


def test_daily_counts_missing_ledger_is_all_zero(tmp_path):
    path = tmp_path / "does_not_exist.jsonl"

    counts = daily_counts(path, "005930", upto=date(2026, 8, 15), days=5)

    assert counts == [0, 0, 0, 0, 0]


def test_daily_counts_skips_broken_lines(tmp_path):
    path = tmp_path / "mentions.jsonl"
    path.write_text(
        '{"date": "2026-08-15", "symbol": "005930", "title": "a", "link": "l1"}\n'
        "not json\n"
        '{"date": "2026-08-15", "symbol": "005930"\n'  # 잘린 줄
        "\n",
        encoding="utf-8",
    )

    counts = daily_counts(path, "005930", upto=date(2026, 8, 15), days=1)

    assert counts == [1]
