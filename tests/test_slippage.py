"""`quant.control.slippage` — 실측 슬리피지 리포트 (2026-09-06 live-readiness §4).

paper 체결(trades.jsonl 행)과 스프레드 실측(spread.jsonl 행)을 이어 붙여
market/strategy별 반쪽 스프레드 분포를 낸다. 전부 오프라인 순수 함수 — 네트워크
없이 리스트만 조작한다.
"""
from __future__ import annotations

import pytest

from quant.control.slippage import (
    SlippageRow,
    _percentile,
    load_spread_rows,
    realized_half_spreads,
    slippage_report,
    slippage_report_text,
)


def _fill(symbol, ts, *, market="US", strategy="gap_fade"):
    return {"ts": ts, "symbol": symbol, "market": market, "strategy_id": strategy,
            "side": "BUY", "qty": 1, "price": 70.0, "fee": 0.0, "realized_pnl": None}


def _spread(symbol, ts, spread_bp):
    return {"ts": ts, "symbol": symbol, "spread_bp": spread_bp, "bid": 100.0, "ask": 101.0}


# --------------------------------------------------------------- _percentile

def test_percentile_single_value():
    assert _percentile([5.0], 0.90) == 5.0


def test_percentile_matches_known_linear_interpolation():
    # n=5, p90 rank = (5-1)*0.9 = 3.6 → interpolate between index 3 and 4
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _percentile(values, 0.90) == pytest.approx(4.6)


def test_percentile_empty_is_zero():
    assert _percentile([], 0.5) == 0.0


# --------------------------------------------------------- realized_half_spreads

def test_matches_fill_to_nearest_spread_sample_within_window():
    fills = [_fill("TQQQ", "2026-09-01T10:00:00+00:00")]
    spread_rows = [
        _spread("TQQQ", "2026-09-01T09:57:00+00:00", 8.0),  # 3분 전 — 더 가깝다
        _spread("TQQQ", "2026-09-01T10:04:00+00:00", 20.0),  # 4분 후
    ]
    matched = realized_half_spreads(fills, spread_rows)
    assert len(matched) == 1
    assert matched[0]["half_spread_bp"] == pytest.approx(4.0)  # 8.0 / 2


def test_drops_fills_with_no_sample_within_the_gap_window():
    fills = [_fill("TQQQ", "2026-09-01T10:00:00+00:00")]
    spread_rows = [_spread("TQQQ", "2026-09-01T09:00:00+00:00", 8.0)]  # 1시간 전 — 너무 멀다
    assert realized_half_spreads(fills, spread_rows) == []


def test_drops_fills_with_no_spread_sample_for_that_symbol():
    fills = [_fill("TQQQ", "2026-09-01T10:00:00+00:00")]
    spread_rows = [_spread("SQQQ", "2026-09-01T10:00:00+00:00", 8.0)]
    assert realized_half_spreads(fills, spread_rows) == []


def test_custom_gap_window_is_respected():
    fills = [_fill("TQQQ", "2026-09-01T10:00:00+00:00")]
    spread_rows = [_spread("TQQQ", "2026-09-01T09:56:00+00:00", 8.0)]  # 4분 전
    assert realized_half_spreads(fills, spread_rows, max_gap_seconds=300.0) != []
    assert realized_half_spreads(fills, spread_rows, max_gap_seconds=120.0) == []


# ------------------------------------------------------------- slippage_report

def test_report_groups_by_market_and_strategy_with_median_p90():
    fills = [
        _fill("TQQQ", "2026-09-01T10:00:00+00:00", market="US", strategy="gap_fade"),
        _fill("TQQQ", "2026-09-01T10:10:00+00:00", market="US", strategy="gap_fade"),
        _fill("TQQQ", "2026-09-01T10:20:00+00:00", market="US", strategy="gap_fade"),
    ]
    spread_rows = [
        _spread("TQQQ", "2026-09-01T10:00:00+00:00", 4.0),
        _spread("TQQQ", "2026-09-01T10:10:00+00:00", 8.0),
        _spread("TQQQ", "2026-09-01T10:20:00+00:00", 12.0),
    ]
    rows = slippage_report(fills, spread_rows)
    assert len(rows) == 1
    r = rows[0]
    assert (r.market, r.strategy, r.n) == ("US", "gap_fade", 3)
    # half spreads = [2.0, 4.0, 6.0] → median 4.0
    assert r.median_half_spread_bp == pytest.approx(4.0)
    assert r.implied_round_trip_bp == pytest.approx(8.0)


def test_report_keeps_markets_and_strategies_separate():
    fills = [
        _fill("TQQQ", "2026-09-01T10:00:00+00:00", market="US", strategy="gap_fade"),
        _fill("069500", "2026-09-01T10:00:00+00:00", market="KR", strategy="scalp_1m"),
    ]
    spread_rows = [
        _spread("TQQQ", "2026-09-01T10:00:00+00:00", 4.0),
        _spread("069500", "2026-09-01T10:00:00+00:00", 10.0),
    ]
    rows = slippage_report(fills, spread_rows)
    assert {(r.market, r.strategy) for r in rows} == {("US", "gap_fade"), ("KR", "scalp_1m")}


def test_report_is_empty_when_nothing_matches():
    assert slippage_report([_fill("TQQQ", "2026-09-01T10:00:00+00:00")], []) == []


# --------------------------------------------------------- slippage_report_text

def test_text_reports_no_data_message_when_empty():
    text = slippage_report_text([], assumed_one_way_bp=2.5)
    assert "표본 없음" in text
    assert "편도 2.5bp" in text and "왕복 5bp" in text


def test_text_marks_near_when_within_10_pct_of_assumption():
    # 가정 왕복 5bp, 실측 왕복 5.2bp → 4% 차이 → 근접
    row = SlippageRow(market="US", strategy="gap_fade", n=40,
                      median_half_spread_bp=2.6, p90_half_spread_bp=3.0,
                      implied_round_trip_bp=5.2)
    text = slippage_report_text([row], assumed_one_way_bp=2.5)
    assert "근접" in text
    assert "n=40" in text


def test_text_marks_optimistic_when_realized_cost_is_much_higher():
    """실측이 가정보다 훨씬 비싸면 "가정이 낙관적이었다"는 뜻의 낙관 판정."""
    row = SlippageRow(market="US", strategy="gap_fade", n=40,
                      median_half_spread_bp=6.0, p90_half_spread_bp=9.0,
                      implied_round_trip_bp=12.0)
    text = slippage_report_text([row], assumed_one_way_bp=2.5)
    assert "낙관" in text


def test_text_marks_conservative_when_realized_cost_is_much_lower():
    row = SlippageRow(market="US", strategy="gap_fade", n=40,
                      median_half_spread_bp=1.0, p90_half_spread_bp=1.5,
                      implied_round_trip_bp=2.0)
    text = slippage_report_text([row], assumed_one_way_bp=2.5)
    assert "보수" in text


# --------------------------------------------------------------- load_spread_rows

def test_load_spread_rows_skips_broken_lines(tmp_path):
    p = tmp_path / "spread.jsonl"
    p.write_text(
        '{"ts": "2026-09-01T10:00:00+00:00", "symbol": "TQQQ", "spread_bp": 4.0}\n'
        "깨진 줄{{{\n",
        encoding="utf-8",
    )
    rows = load_spread_rows(p)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "TQQQ"


def test_load_spread_rows_missing_file_returns_empty(tmp_path):
    assert load_spread_rows(tmp_path / "nope.jsonl") == []
