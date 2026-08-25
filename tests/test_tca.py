"""슬리피지 TCA(`quant/control/tca.py`) — 2026-08-26 소유자 로드맵 #5."""
from __future__ import annotations

from datetime import date

import pytest

from quant.control.tca import join_intents_fills, slippage_bps, tca_summary


def _intent(symbol, side, price, ts, strategy_id="donchian", **extra):
    row = {
        "event": "intent", "ts": ts, "symbol": symbol, "side": side,
        "quantity": 10, "order_amount": None, "strategy_id": strategy_id,
        "reason": "", "price": price,
    }
    row.update(extra)
    return row


def _fill(symbol, side, price, ts, strategy_id="donchian", market="US", **extra):
    row = {
        "ts": ts, "strategy_id": strategy_id, "symbol": symbol,
        "side": side.lower(), "qty": 10, "price": price, "fee": 0.0,
        "realized_pnl": 0.0, "market": market,
    }
    row.update(extra)
    return row


# ── join_intents_fills ──────────────────────────────────────────────────

def test_join_matches_intent_to_fill_within_window():
    intents = [_intent("AAPL", "BUY", 100.0, "2026-08-24T10:00:00+00:00")]
    trades = [_fill("AAPL", "BUY", 100.5, "2026-08-24T10:00:30+00:00")]
    joined = join_intents_fills(intents, trades, window_seconds=120)
    assert len(joined) == 1
    row = joined[0]
    assert row["symbol"] == "AAPL"
    assert row["intent_price"] == 100.0
    assert row["fill_price"] == 100.5
    assert row["market"] == "US"
    assert row["strategy_id"] == "donchian"


def test_join_drops_intents_without_price():
    intents = [{
        "event": "intent", "ts": "2026-08-24T10:00:00+00:00", "symbol": "AAPL",
        "side": "BUY", "quantity": 10, "order_amount": None,
        "strategy_id": "donchian", "reason": "",
    }]
    trades = [_fill("AAPL", "BUY", 100.5, "2026-08-24T10:00:30+00:00")]
    assert join_intents_fills(intents, trades) == []


def test_join_ignores_fills_outside_window():
    intents = [_intent("AAPL", "BUY", 100.0, "2026-08-24T10:00:00+00:00")]
    trades = [_fill("AAPL", "BUY", 100.5, "2026-08-24T10:05:00+00:00")]  # 5분 뒤
    assert join_intents_fills(intents, trades, window_seconds=120) == []


def test_join_ignores_fills_before_intent():
    intents = [_intent("AAPL", "BUY", 100.0, "2026-08-24T10:00:30+00:00")]
    trades = [_fill("AAPL", "BUY", 100.5, "2026-08-24T10:00:00+00:00")]  # 의도보다 이전
    assert join_intents_fills(intents, trades, window_seconds=120) == []


def test_join_is_greedy_one_to_one_no_double_matching():
    """의도 2건, 체결 2건 — 그리디 시각순으로 각각 하나씩만 매칭, 이중 매칭 없음."""
    intents = [
        _intent("AAPL", "BUY", 100.0, "2026-08-24T10:00:00+00:00"),
        _intent("AAPL", "BUY", 101.0, "2026-08-24T10:01:00+00:00"),
    ]
    trades = [
        _fill("AAPL", "BUY", 100.2, "2026-08-24T10:00:10+00:00"),
        _fill("AAPL", "BUY", 101.3, "2026-08-24T10:01:10+00:00"),
    ]
    joined = join_intents_fills(intents, trades, window_seconds=120)
    assert len(joined) == 2
    prices = sorted((r["intent_price"], r["fill_price"]) for r in joined)
    assert prices == [(100.0, 100.2), (101.0, 101.3)]


def test_join_does_not_cross_match_different_symbols_or_sides():
    intents = [_intent("AAPL", "BUY", 100.0, "2026-08-24T10:00:00+00:00")]
    trades = [
        _fill("MSFT", "BUY", 100.5, "2026-08-24T10:00:10+00:00"),  # 다른 종목
        _fill("AAPL", "SELL", 100.5, "2026-08-24T10:00:10+00:00"),  # 다른 방향
    ]
    assert join_intents_fills(intents, trades, window_seconds=120) == []


def test_join_separates_by_strategy_id():
    intents = [_intent("AAPL", "BUY", 100.0, "2026-08-24T10:00:00+00:00", strategy_id="orb_scan")]
    trades = [_fill("AAPL", "BUY", 100.5, "2026-08-24T10:00:10+00:00", strategy_id="donchian")]
    assert join_intents_fills(intents, trades, window_seconds=120) == []


# ── slippage_bps ─────────────────────────────────────────────────────────

def test_slippage_bps_buy_positive_when_fill_worse():
    joined = [{"side": "BUY", "intent_price": 100.0, "fill_price": 100.5}]
    out = slippage_bps(joined)
    assert out[0]["bps"] == pytest.approx(50.0)


def test_slippage_bps_sell_positive_when_fill_worse():
    joined = [{"side": "SELL", "intent_price": 100.0, "fill_price": 99.5}]
    out = slippage_bps(joined)
    assert out[0]["bps"] == pytest.approx(50.0)


def test_slippage_bps_buy_negative_when_fill_better():
    joined = [{"side": "BUY", "intent_price": 100.0, "fill_price": 99.0}]
    out = slippage_bps(joined)
    assert out[0]["bps"] == pytest.approx(-100.0)


# ── tca_summary ──────────────────────────────────────────────────────────

def test_tca_summary_none_when_empty():
    assert tca_summary([]) is None


def test_tca_summary_buckets_by_market_and_strategy():
    rows = [
        {"bps": 10.0, "market": "US", "strategy_id": "donchian", "fill_ts": "2026-08-24T10:00:00+00:00"},
        {"bps": 20.0, "market": "US", "strategy_id": "donchian", "fill_ts": "2026-08-25T10:00:00+00:00"},
        {"bps": -5.0, "market": "KR", "strategy_id": "orb_scan", "fill_ts": "2026-08-24T01:00:00+00:00"},
    ]
    out = tca_summary(rows)
    assert out["overall"]["n"] == 3
    assert out["overall"]["avg_bps"] == pytest.approx((10.0 + 20.0 - 5.0) / 3, abs=0.01)
    assert out["by_market"]["US"]["n"] == 2
    assert out["by_market"]["KR"]["n"] == 1
    assert out["by_strategy"]["donchian"]["n"] == 2
    assert out["by_strategy"]["orb_scan"]["n"] == 1


def test_tca_summary_filters_by_date_range():
    rows = [
        {"bps": 10.0, "market": "US", "strategy_id": "donchian", "fill_ts": "2026-08-17T10:00:00+00:00"},  # 지난주
        {"bps": 20.0, "market": "US", "strategy_id": "donchian", "fill_ts": "2026-08-24T10:00:00+00:00"},  # 이번 주
    ]
    out = tca_summary(rows, start_date=date(2026, 8, 24), end_date=date(2026, 8, 30))
    assert out["overall"]["n"] == 1
    assert out["overall"]["avg_bps"] == pytest.approx(20.0)


def test_tca_summary_none_when_date_range_empties_rows():
    rows = [{"bps": 10.0, "market": "US", "strategy_id": "donchian",
             "fill_ts": "2026-08-17T10:00:00+00:00"}]
    out = tca_summary(rows, start_date=date(2026, 8, 24), end_date=date(2026, 8, 30))
    assert out is None


# ── 엔드투엔드: 오늘 저장소 실스키마(가격 없는 intent)로는 표본이 0이어야 한다 ──

def test_end_to_end_zero_samples_with_current_intent_schema():
    """order_intents.jsonl 의 실제 스키마(2026-08-26, TossBroker._append_intent)에는
    가격 필드가 없다 — 그 사실을 여기서 회귀 검증한다. 업스트림이 price를 남기기
    시작하면 이 테스트는 깨져야 하고, 그때 이 테스트를 갱신하면 된다."""
    real_schema_intent = {
        "event": "intent", "ts": "2026-08-24T10:00:00+00:00", "client_order_id": "abc",
        "symbol": "AAPL", "side": "BUY", "quantity": 10, "order_amount": None,
        "strategy_id": "donchian", "reason": "채널 상단 돌파",
    }
    trades = [_fill("AAPL", "BUY", 100.5, "2026-08-24T10:00:10+00:00")]
    joined = join_intents_fills([real_schema_intent], trades)
    assert joined == []
    assert tca_summary(slippage_bps(joined)) is None
