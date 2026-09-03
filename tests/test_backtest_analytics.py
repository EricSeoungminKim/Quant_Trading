"""거래 다차원 분석(quant.backtest.analytics) 단위 테스트.

체결 로그를 손으로 지어(진짜 백테스트를 돌리지 않고) 각 축이 맞는지 검증한다 —
①표본부족 판단불가 ②명백히 양(+)인 전략 ③시간대(hour-of-day) tz 변환(KR/US)
④몬테카를로 최대낙폭이 변동성에 단조증가 ⑤승률 CI가 ledger._wilson_ci와 일치.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from quant.backtest.analytics import (
    MIN_ROUND_TRIPS,
    _monte_carlo_max_dd,
    analyze_trades,
)
from quant.control.ledger import _wilson_ci

_COLS = ["ts", "symbol", "side", "qty", "price", "fee",
         "fee_krw", "realized_pnl_krw", "notional_krw", "pnl", "reason"]


def _pair(symbol, entry_ts, exit_ts, notional=1_000_000.0, net_bp=0.0, reason="손절: x"):
    """매수+매도 체결 1쌍 = 라운드트립 1건. 수수료는 0으로 둬(analytics는 fitness의
    비용 가드를 쓰지 않으므로 시험에 영향 없음), realized_pnl_krw만으로 net_bp를
    정확히 만든다."""
    net_pnl = net_bp / 1e4 * notional
    buy = {"ts": entry_ts, "symbol": symbol, "side": "buy", "qty": 10.0, "price": 100.0,
           "fee": 0.0, "fee_krw": 0.0, "realized_pnl_krw": 0.0, "notional_krw": notional,
           "pnl": 0.0, "reason": ""}
    sell = {"ts": exit_ts, "symbol": symbol, "side": "sell", "qty": 10.0, "price": 100.0,
            "fee": 0.0, "fee_krw": 0.0, "realized_pnl_krw": net_pnl, "notional_krw": notional,
            "pnl": net_pnl, "reason": reason}
    return [buy, sell]


def _trades(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_COLS)


def _series_of_trips(n: int, net_bp: float, reason="손절: x", start="2026-01-05 22:35",
                      tz="UTC") -> pd.DataFrame:
    rows: list[dict] = []
    base = pd.Timestamp(start, tz=tz)
    for i in range(n):
        entry = base + pd.Timedelta(days=i)
        exit_ = entry + pd.Timedelta(minutes=5)
        rows += _pair(f"SYM{i % 3}", entry, exit_, net_bp=net_bp, reason=reason)
    return _trades(rows)


# ── ① 표본 부족 → 판단 불가 ───────────────────────────────────────────────

def test_below_min_round_trips_is_not_judgeable():
    trades = _series_of_trips(10, net_bp=5.0)
    result = analyze_trades(trades, market="US", cost_bp=14.0)
    assert result["n"] == 10
    assert result["judgeable"] is False
    assert "판단 불가" in result["note"]
    assert result["win_rate"] is None
    assert result["by_hour_of_day"] == {}


def test_exactly_at_threshold_is_judgeable():
    trades = _series_of_trips(MIN_ROUND_TRIPS, net_bp=5.0)
    result = analyze_trades(trades, market="US", cost_bp=14.0)
    assert result["n"] == MIN_ROUND_TRIPS
    assert result["judgeable"] is True


# ── ② 명백히 양(+)인 전략 ──────────────────────────────────────────────────

def test_clearly_positive_strategy_reports_positive_expectancy_and_full_win_streak():
    n = 50
    trades = _series_of_trips(n, net_bp=20.0, reason="전량 익절(+20bp)")
    result = analyze_trades(trades, market="US", cost_bp=14.0)

    assert result["judgeable"] is True
    assert result["win_rate"] == pytest.approx(1.0)
    assert result["expectancy_bp"] == pytest.approx(20.0)
    assert result["profit_factor"] == float("inf")  # 손실이 하나도 없다
    assert result["streaks"]["max_consecutive_wins"] == n
    assert result["streaks"]["max_consecutive_losses"] == 0
    # 청산사유 파싱: "익절"이 reason 문자열에서 뽑힌다
    assert "익절" in result["by_exit_reason"]
    assert result["by_exit_reason"]["익절"]["n"] == n
    # 비용 민감도: net_bp 가 이미 1x 비용을 반영했다고 가정 → 2x는 cost_bp만큼 더 뺀다
    cs = result["cost_sensitivity"]
    assert cs["1x"] == pytest.approx(20.0)
    assert cs["2x"] == pytest.approx(20.0 - 14.0)


def test_strategy_that_dies_at_2x_cost_shows_negative_expectancy_at_2x():
    n = 40
    trades = _series_of_trips(n, net_bp=10.0)
    result = analyze_trades(trades, market="US", cost_bp=14.0)
    assert result["cost_sensitivity"]["2x"] < 0


# ── ③ hour-of-day 버킷 — 시장 현지시간대 변환 ─────────────────────────────

def test_hour_of_day_bucket_uses_kr_local_time():
    # 14:35 UTC = 23:35 KST (KR은 DST 없음, 항상 UTC+9)
    trades = _series_of_trips(MIN_ROUND_TRIPS, net_bp=5.0, start="2026-01-05 14:35", tz="UTC")
    result = analyze_trades(trades, market="KR", cost_bp=14.0)
    assert "23" in result["by_hour_of_day"]
    assert result["by_hour_of_day"]["23"]["n"] == MIN_ROUND_TRIPS


def test_hour_of_day_bucket_uses_us_local_time_with_dst_offset():
    # 같은 14:35 UTC = 09:35 EST(1월, 서머타임 아님, UTC-5) — KR과 다른 버킷에 잡혀야 한다
    trades = _series_of_trips(MIN_ROUND_TRIPS, net_bp=5.0, start="2026-01-05 14:35", tz="UTC")
    result = analyze_trades(trades, market="US", cost_bp=14.0)
    assert "9" in result["by_hour_of_day"]
    assert result["by_hour_of_day"]["9"]["n"] == MIN_ROUND_TRIPS


# ── ④ 몬테카를로 최대낙폭 — 변동성에 단조증가 ─────────────────────────────

def test_monte_carlo_max_dd_grows_monotonically_with_volatility():
    n = 60
    low_vol = [((-1) ** i) * 1.0 for i in range(n)]     # 평균 0, 진폭 1bp
    high_vol = [((-1) ** i) * 10.0 for i in range(n)]   # 평균 0, 진폭 10bp (10배)

    low = _monte_carlo_max_dd(low_vol, seed=42, n_iters=500)
    high = _monte_carlo_max_dd(high_vol, seed=42, n_iters=500)

    assert low["max_dd_bp_mean"] > 0
    assert high["max_dd_bp_mean"] > low["max_dd_bp_mean"]
    assert high["max_dd_bp_median"] > low["max_dd_bp_median"]
    assert high["max_dd_bp_p95"] > low["max_dd_bp_p95"]
    # 값 크기가 정확히 10배 스케일 — 같은 시드로 같은 셔플 순서를 쓰므로 정확히 성립
    assert high["max_dd_bp_mean"] == pytest.approx(low["max_dd_bp_mean"] * 10, rel=1e-9)


def test_monte_carlo_max_dd_is_deterministic_given_seed():
    n = 30
    vals = [((-1) ** i) * 3.0 for i in range(n)]
    a = _monte_carlo_max_dd(vals, seed=7, n_iters=200)
    b = _monte_carlo_max_dd(vals, seed=7, n_iters=200)
    assert a == b


def test_monte_carlo_max_dd_none_when_too_few_trades():
    result = _monte_carlo_max_dd([1.0], seed=42)
    assert result["max_dd_bp_mean"] is None


# ── ⑤ 승률 95% CI — control.ledger._wilson_ci 와 일치 ────────────────────

def test_win_rate_ci_matches_ledger_wilson_ci():
    n = 30
    wins = 21
    rows: list[dict] = []
    base = pd.Timestamp("2026-01-05 22:35", tz="UTC")
    for i in range(n):
        entry = base + pd.Timedelta(days=i)
        exit_ = entry + pd.Timedelta(minutes=5)
        net_bp = 5.0 if i < wins else -3.0
        rows += _pair(f"SYM{i % 3}", entry, exit_, net_bp=net_bp)
    trades = _trades(rows)

    result = analyze_trades(trades, market="US", cost_bp=14.0)
    expected_lower, expected_upper = _wilson_ci(wins, n)
    lower, upper = result["win_rate_ci"]
    assert lower == pytest.approx(round(expected_lower, 4))
    assert upper == pytest.approx(round(expected_upper, 4))
    assert result["win_rate"] == pytest.approx(wins / n)


# ── holding-minutes 버킷 ──────────────────────────────────────────────────

def test_holding_minutes_bucket_classifies_correctly():
    rows: list[dict] = []
    base = pd.Timestamp("2026-01-05 09:35", tz="America/New_York")
    minutes_list = [2, 10, 30, 120, 300] * 6  # 30건, 각 버킷 6건
    for i, m in enumerate(minutes_list):
        entry = base + pd.Timedelta(days=i)
        exit_ = entry + pd.Timedelta(minutes=m)
        rows += _pair("TQQQ", entry, exit_, net_bp=1.0)
    trades = _trades(rows)
    result = analyze_trades(trades, market="US", cost_bp=14.0)
    buckets = result["by_holding_bucket"]
    assert buckets["<5분"]["n"] == 6
    assert buckets["5-15분"]["n"] == 6
    assert buckets["15-60분"]["n"] == 6
    assert buckets["60-240분"]["n"] == 6
    assert buckets[">240분"]["n"] == 6


# ── MFE/MAE 미보유 명시 ────────────────────────────────────────────────────

def test_mfe_mae_reports_unavailable_with_note():
    trades = _series_of_trips(MIN_ROUND_TRIPS, net_bp=5.0)
    result = analyze_trades(trades, market="US", cost_bp=14.0)
    assert result["mfe_mae"]["available"] is False
    assert "mfe" in result["mfe_mae"]["note"] or "MFE" in result["mfe_mae"]["note"]


def test_no_trades_is_not_judgeable():
    result = analyze_trades(_trades([]), market="US", cost_bp=14.0)
    assert result["n"] == 0
    assert result["judgeable"] is False
