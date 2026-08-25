"""종목 점수 일일 원장(`quant/control/symbol_log.py`) — 2026-08-26.

계약: ① 조사한 전 종목 기록(후보 여부 무관) ② 없는 축은 키 생략 ③ 재실행 멱등
④ 연속 강세 추출(다음날 후보 재료) ⑤ 점수 vs 실제 움직임 대조(적중률).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from quant.control.symbol_log import (
    accuracy_join, append_scores, build_score_rows, hot_streak_symbols, load_scores,
)

D = date(2026, 8, 26)


def _cont():
    return {
        "005930": {"today_articles": 5, "streak_days": 3},
        "000660": {"today_articles": 1},
    }


def _scores():
    return {
        "005930": {"score": 3, "label": "강세", "factors": [
            {"key": "news_hot", "delta": 1}, {"key": "foreign_5d", "delta": 1},
            {"key": "news_streak", "delta": 1}]},
        # 000660 은 점수 없음 — 키 생략 계약 검증용
    }


def test_rows_record_all_researched_symbols_not_just_candidates():
    rows = build_score_rows(D, "KR", _cont(), _scores(),
                            sym_quotes={"005930": {"change_pct": 2.5}},
                            names={"005930": "삼성전자"})
    assert len(rows) == 2, "후보가 아니어도 조사됐으면 기록한다"
    r = next(r for r in rows if r["symbol"] == "005930")
    assert r["score"] == 3 and r["name"] == "삼성전자"
    assert r["factors"] == ["news_hot", "foreign_5d", "news_streak"]
    assert r["change_pct"] == 2.5


def test_missing_axes_omit_keys():
    rows = build_score_rows(D, "KR", _cont(), _scores())
    r = next(r for r in rows if r["symbol"] == "000660")
    assert "score" not in r and "change_pct" not in r, "없는 값을 0으로 위장하지 않는다"
    assert r["today_articles"] == 1


def test_append_is_idempotent_per_day_market_symbol(tmp_path):
    p = tmp_path / "symbol_scores.jsonl"
    rows = build_score_rows(D, "KR", _cont(), _scores())
    assert append_scores(rows, p) == 2
    assert append_scores(rows, p) == 0, "아침 리포트를 다시 돌려도 원장이 불지 않는다"
    assert len(load_scores(p)) == 2


def test_load_scores_days_window(tmp_path):
    p = tmp_path / "s.jsonl"
    append_scores([{"date": "2026-08-20", "market": "KR", "symbol": "A"}], p)
    append_scores([{"date": "2026-08-25", "market": "KR", "symbol": "B"}], p)
    recent = load_scores(p, days=3, today=D)
    assert [r["symbol"] for r in recent] == ["B"]


def _streak_rows():
    out = []
    for d, syms in (("2026-08-24", {"005930": 3, "000660": 1}),
                    ("2026-08-25", {"005930": 2, "000660": 3})):
        for sym, sc in syms.items():
            out.append({"date": d, "market": "KR", "symbol": sym, "score": sc})
    return out


def test_hot_streak_requires_repeated_strength():
    syms = hot_streak_symbols(_streak_rows(), D, min_days=2, min_score=2)
    assert syms == ["005930"], "이틀 연속 score>=2 는 005930 뿐 (000660 은 하루)"


def test_hot_streak_excludes_today_and_other_market():
    rows = _streak_rows() + [
        {"date": "2026-08-26", "market": "KR", "symbol": "TODAY", "score": 5},
        {"date": "2026-08-25", "market": "US", "symbol": "NVDA", "score": 5},
    ]
    syms = hot_streak_symbols(rows, D, min_days=1, min_score=2)
    assert "TODAY" not in syms, "오늘 기록은 내일의 연속성 근거지 오늘의 근거가 아니다"
    assert "NVDA" not in syms


def test_accuracy_join_buckets_and_hit_rate():
    rows = [
        {"date": "2026-08-24", "symbol": "A", "score": 3},
        {"date": "2026-08-24", "symbol": "B", "score": 3},
        {"date": "2026-08-24", "symbol": "C", "score": 1},
        {"date": "2026-08-24", "symbol": "D", "score": 2},  # 다음날 가격 없음 → 제외
    ]
    nxt = {("2026-08-24", "A"): 2.0, ("2026-08-24", "B"): -1.0,
           ("2026-08-24", "C"): -3.0}
    out = accuracy_join(rows, nxt)
    assert out["score>=3"]["n"] == 2
    assert out["score>=3"]["avg_next_pct"] == pytest.approx(0.5)
    assert out["score>=3"]["hit_rate"] == pytest.approx(0.5)
    assert out["score<=1"]["hit_rate"] == 0.0
    assert "score=2" not in out, "가격 없는 표본은 지어내지 않고 뺀다"


def test_accuracy_join_empty_returns_none():
    assert accuracy_join([{"date": "d", "symbol": "A", "score": 3}], {}) is None
