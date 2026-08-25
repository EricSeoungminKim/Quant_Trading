"""주간 재검토(`quant/control/weekly_review.py`) — 2026-08-26 손계산 고정."""
from __future__ import annotations

from datetime import date

import pytest

from quant.control.weekly_review import (
    loss_patterns, week_range, weekly_index_flow, weekly_review_text,
    weekly_strategy_stats,
)

MON, SUN = date(2026, 8, 24), date(2026, 8, 30)


def test_week_range_from_saturday():
    """토 06:25 실행 → 막 끝난 주(월~일)가 나와야 한다."""
    assert week_range(date(2026, 8, 29)) == (MON, SUN)
    assert week_range(MON) == (MON, SUN)


def _trip(day, strategy, symbol, bps, hold_min=30):
    from datetime import datetime, time, timedelta, timezone

    entry = datetime.combine(day, time(1, 0), tzinfo=timezone.utc)
    return {"strategy": strategy, "symbol": symbol, "bps": bps,
            "entry_ts": entry.isoformat(),
            "exit_ts": (entry + timedelta(minutes=hold_min)).isoformat()}


def test_strategy_stats_filters_week_and_computes():
    trips = [
        _trip(date(2026, 8, 25), "scalp_1m", "A", +50),
        _trip(date(2026, 8, 26), "scalp_1m", "B", -100),
        _trip(date(2026, 8, 15), "scalp_1m", "C", +999),  # 지난주 — 제외
    ]
    out = weekly_strategy_stats(trips, MON, SUN)
    assert len(out) == 1
    r = out[0]
    assert (r["n"], r["wins"]) == (2, 1)
    assert r["avg_bps"] == pytest.approx(-25.0)
    assert r["total_pnl_bps"] == pytest.approx(-50.0)


def test_loss_patterns_worst_and_hold_buckets():
    trips = [
        _trip(date(2026, 8, 25), "scalp_1m", "A", -200, hold_min=5),
        _trip(date(2026, 8, 25), "close_bet", "B", -50, hold_min=1200),  # 오버나이트
        _trip(date(2026, 8, 26), "scalp_1m", "C", +80, hold_min=30),
    ]
    out = loss_patterns(trips, MON, SUN)
    assert out["n_week"] == 3 and out["n_losses"] == 2
    assert out["worst"][0]["symbol"] == "A" and out["worst"][0]["bps"] == -200
    assert out["hold_buckets"]["<10분"]["avg_bps"] == pytest.approx(-200.0)
    assert out["hold_buckets"]["오버나이트+"]["n"] == 1


def test_index_flow_week_and_extremes():
    out = weekly_index_flow({"KOSPI200": [100, 102, 101, 103, 104]})
    r = out[0]
    assert r["week_pct"] == pytest.approx(4.0)
    assert r["best_day_pct"] == pytest.approx(2.0)   # 100→102
    assert r["worst_day_pct"] == pytest.approx(-0.98)  # 102→101
    assert weekly_index_flow({"엉터리": [100]}) == []


def test_text_renders_every_section_and_honest_gaps():
    txt = weekly_review_text(
        MON, SUN,
        index_flow=[{"label": "QQQ", "week_pct": 1.2, "best_day_pct": 2.0,
                     "worst_day_pct": -1.0}],
        strategy_stats=[{"strategy": "scalp_1m", "n": 2, "wins": 1,
                         "win_rate": 0.5, "avg_bps": -25.0, "total_pnl_bps": -50.0}],
        losses={"n_week": 2, "n_losses": 1,
                "worst": [{"strategy": "scalp_1m", "symbol": "A", "bps": -200.0,
                           "hold_min": 5}],
                "hold_buckets": {"<10분": {"n": 1, "avg_bps": -200.0}}},
        score_accuracy=None,
        equity_delta={"start": 10_000_000, "end": 9_900_000, "pct": -1.0},
    )
    assert "주간 재검토" in txt and "QQQ" in txt and "scalp_1m" in txt
    assert "표본 부족" in txt, "적중률 표본이 없으면 없다고 말한다"
    assert "결론은 내지 않는다" in txt
