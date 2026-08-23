"""`quant.backtest.report_replay` — 과거 리포트 재구성 백테스트(뉴스 축 부재).

네트워크는 절대 타지 않는다 — yfinance/stock_detail 호출은 전부 fake로
대체한다(모듈 지침과 동일한 규율).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from quant.analyze.foreign_flow_v2 import FOREIGN_V2_MAX, foreign_score_v2
from quant.analyze.foreign_trend import LABEL_INFLOW, LABEL_NEUTRAL, LABEL_OUTFLOW_TREND
from quant.backtest.report_replay import (
    ANCHOR_SYMBOL,
    COMPOSITION_RETAIN_HIGH,
    COMPOSITION_RETAIN_LOW,
    FRGN_FLOW_LEDGER,
    MIN_SAMPLE_FOR_JUDGEMENT,
    MOVER_THRESHOLD_PCT,
    REPLAY_WEIGHTS,
    REPLAY_WEIGHTS_B,
    REPLAY_WEIGHTS_C,
    actual_direction,
    aggregate_mover_metrics,
    aggregate_replay,
    amplitude_pct,
    backfill_flow_history,
    bar_mover_record,
    build_bar_cache,
    fetch_daily_bars_ohlcv,
    foreign_label_for,
    format_ab_comparison,
    format_abc_comparison,
    format_report,
    kodex_5d_trend,
    market_foreign_proxy,
    mover_precision_recall,
    picks_outcome,
    prior_flow_rows,
    rank_replay,
    rank_replay_c,
    rank_replay_v2,
    reconstructed_stance,
    run_ab_reconstruction,
    run_reconstruction,
    run_variant_comparison,
    score_replay,
    score_replay_c,
    score_replay_v2,
    select_universe,
    stance_agreement,
    trending_score_proxy,
    upside_reach_pct,
    write_ledger,
)
from quant.backtest.intraday_verify import index_frgn_flow

D = date(2026, 8, 15)


# ------------------------------------------------------------------ fake candle source

class FakeCandleSource:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames
        self.calls: list[str] = []

    def fetch(self, symbol: str, start, end) -> pd.DataFrame:
        self.calls.append(symbol)
        df = self.frames.get(symbol)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        start_ts, end_ts = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
        return df.loc[(df.index >= start_ts) & (df.index <= end_ts)]


def _row(o: float, c: float, v: float = 1000.0) -> dict:
    return {"open": o, "high": max(o, c), "low": min(o, c), "close": c, "volume": v}


def _frame(rows: dict[date, dict]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in rows], name="date")
    return pd.DataFrame(list(rows.values()), index=idx)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _bars_with_history(anchor: date, n_days: int, closes: list[float], vols: list[float]) -> dict[date, dict]:
    """`n_days` 거래일치(anchor 포함, 과거→anchor 순) 봉을 만든다."""
    assert len(closes) == n_days == len(vols)
    out: dict[date, dict] = {}
    for i in range(n_days):
        d = anchor - timedelta(days=(n_days - 1 - i))
        out[d] = {"open": closes[i], "close": closes[i], "volume": vols[i]}
    return out


# ------------------------------------------------------------------ REPLAY_WEIGHTS

def test_replay_weights_sum_to_100():
    assert sum(REPLAY_WEIGHTS.values()) == 100.0


def test_replay_weights_preserve_original_ratio():
    # 원래 30:15:15 = 2:1:1 이 그대로 유지돼야 한다.
    assert REPLAY_WEIGHTS["foreign"] == 2 * REPLAY_WEIGHTS["trending"]
    assert REPLAY_WEIGHTS["trending"] == REPLAY_WEIGHTS["catalyst"]


# ------------------------------------------------------------------ 유니버스 선정

def test_select_universe_fallback_disclosures_only_when_no_sector_file(tmp_path):
    _write_jsonl(tmp_path / "data" / "ledger" / "disclosures.jsonl", [
        {"stock_code": "A"}, {"stock_code": "A"}, {"stock_code": "B"},
    ])
    universe, meta = select_universe(tmp_path, max_symbols=10)
    assert universe == ["A", "B"]
    assert "fallback" in meta["method"]


def test_select_universe_unions_sector_members_when_present(tmp_path):
    _write_jsonl(tmp_path / "data" / "ledger" / "disclosures.jsonl", [{"stock_code": "A"}])
    sector_path = tmp_path / "data" / "ledger" / "sector_members.json"
    sector_path.parent.mkdir(parents=True, exist_ok=True)
    sector_path.write_text(json.dumps({"C": "업종1", "D": "업종2"}), encoding="utf-8")
    universe, meta = select_universe(tmp_path, max_symbols=10)
    assert set(universe) == {"A", "C", "D"}
    assert "sector_members.json" in meta["method"]


def test_select_universe_caps_by_max_symbols(tmp_path):
    rows = [{"stock_code": s} for s in ["A", "A", "A", "B", "B", "C"]]
    _write_jsonl(tmp_path / "data" / "ledger" / "disclosures.jsonl", rows)
    universe, _ = select_universe(tmp_path, max_symbols=2)
    assert universe == ["A", "B"]


# ------------------------------------------------------------------ Step 1: 수급 백필(fake fetcher, 네트워크 없음)

def test_backfill_flow_history_writes_real_dates_and_counts_failures(tmp_path):
    def fetcher(code: str) -> dict:
        if code == "FAIL":
            raise RuntimeError("network down")
        return {"flow_daily": [
            {"date": "2026-08-14", "foreign_net": 100, "inst_net": -10},
            {"date": "2026-08-13", "foreign_net": -50, "inst_net": 5},
        ]}

    result = backfill_flow_history(tmp_path, ["005930", "FAIL"], sleep_seconds=0, fetcher=fetcher)
    assert result["n_ok"] == 1
    assert result["n_failed"] == 1
    assert result["failed_sample"] == ["FAIL"]
    assert result["rows_written"] == 2

    ledger_rows = [json.loads(line) for line in (tmp_path / "data" / "ledger" / FRGN_FLOW_LEDGER).read_text().splitlines()]
    dates = {r["date"] for r in ledger_rows}
    assert dates == {"2026-08-14", "2026-08-13"}
    assert all(r["symbol"] == "005930" for r in ledger_rows)


def test_backfill_flow_history_empty_flow_daily_counts_as_failure(tmp_path):
    def fetcher(code: str) -> dict:
        return {"flow_daily": []}

    result = backfill_flow_history(tmp_path, ["005930"], sleep_seconds=0, fetcher=fetcher)
    assert result["n_ok"] == 0
    assert result["n_failed"] == 1
    assert result["rows_written"] == 0


def test_backfill_flow_history_is_idempotent(tmp_path):
    def fetcher(code: str) -> dict:
        return {"flow_daily": [{"date": "2026-08-14", "foreign_net": 100, "inst_net": -10}]}

    backfill_flow_history(tmp_path, ["005930"], sleep_seconds=0, fetcher=fetcher)
    backfill_flow_history(tmp_path, ["005930"], sleep_seconds=0, fetcher=fetcher)
    lines = (tmp_path / "data" / "ledger" / FRGN_FLOW_LEDGER).read_text().splitlines()
    assert len(lines) == 1  # 같은 (date, symbol) — upsert, 늘지 않는다


# ------------------------------------------------------------------ look-ahead 방지

def test_foreign_label_excludes_same_day_row():
    frgn_idx = index_frgn_flow([
        {"symbol": "005930", "date": (D - timedelta(days=2)).isoformat(), "foreign_net": -100, "inst_net": 0},
        {"symbol": "005930", "date": (D - timedelta(days=1)).isoformat(), "foreign_net": -20, "inst_net": 0},
        {"symbol": "005930", "date": D.isoformat(), "foreign_net": 500, "inst_net": 0},  # D 당일 — 배제돼야 함
    ])
    label = foreign_label_for(frgn_idx, "005930", D)
    assert label == LABEL_OUTFLOW_TREND
    assert label != LABEL_INFLOW


def test_foreign_label_none_when_no_prior_rows():
    frgn_idx = index_frgn_flow([{"symbol": "005930", "date": D.isoformat(), "foreign_net": 100, "inst_net": 0}])
    assert foreign_label_for(frgn_idx, "005930", D) is None


def test_trending_score_proxy_requires_full_baseline():
    bars = _bars_with_history(D - timedelta(days=1), n_days=10, closes=[100.0] * 10, vols=[100.0] * 10)
    score, meta = trending_score_proxy(bars, D)
    assert score is None
    assert "baseline" in meta["reason"] or "거래일" in meta["reason"]


def test_trending_score_proxy_high_rvol_and_positive_return():
    n = 21
    closes = [100.0] * (n - 5) + [104.0, 105.0, 106.0, 108.0, 110.0]  # 마지막 5일 상승
    vols = [100.0] * (n - 1) + [300.0]  # D-1 거래량이 baseline 대비 3배
    bars = _bars_with_history(D - timedelta(days=1), n_days=n, closes=closes, vols=vols)
    score, meta = trending_score_proxy(bars, D)
    assert score == 75.0
    assert meta["ret5_pct"] > 0
    assert meta["rvol"] > 1.5


def test_trending_score_proxy_flat_or_negative_scores_low():
    n = 21
    closes = [100.0] * n  # 등락 없음
    vols = [100.0] * n
    bars = _bars_with_history(D - timedelta(days=1), n_days=n, closes=closes, vols=vols)
    score, _ = trending_score_proxy(bars, D)
    assert score == 45.0


def test_dart_types_cutoff_matches_catalyst_study_precursor(tmp_path):
    """spec: 촉매는 D-1/D via classify_report — catalyst_study.precursor_types 재사용을 검증."""
    from quant.backtest.catalyst_study import index_disclosures_by_symbol, precursor_types
    idx = index_disclosures_by_symbol([
        {"stock_code": "005930", "report_nm": "수주 계약", "rcept_dt": D.strftime("%Y%m%d")},
        {"stock_code": "005930", "report_nm": "유상증자", "rcept_dt": (D + timedelta(days=1)).strftime("%Y%m%d")},
    ])
    types = precursor_types(idx, "005930", D)
    assert "수주" in types
    assert "유상증자" not in types  # 미래 공시 배제


# ------------------------------------------------------------------ 채점(순수)

def test_score_replay_none_when_no_foreign_label():
    assert score_replay(None, 80.0, ["수주"]) is None


def test_score_replay_full_marks():
    result = score_replay(LABEL_INFLOW, 75.0, ["수주"])
    assert result["score100"] == pytest.approx(100.0)
    assert result["foreign_pts"] == pytest.approx(REPLAY_WEIGHTS["foreign"])
    assert result["trending_pts"] == pytest.approx(REPLAY_WEIGHTS["trending"])
    assert result["catalyst_pts"] == pytest.approx(REPLAY_WEIGHTS["catalyst"])


def test_score_replay_none_trending_scores_zero_not_crash():
    result = score_replay(LABEL_NEUTRAL, None, [])
    assert result["score100"] == pytest.approx(0.0)
    assert result["trending_pts"] == 0.0


def test_rank_replay_deterministic_tie_break_by_symbol():
    candidates = {
        "B": {"foreign_label": LABEL_INFLOW, "trending_score100": None, "dart_types": []},
        "A": {"foreign_label": LABEL_INFLOW, "trending_score100": None, "dart_types": []},
        "C": {"foreign_label": None, "trending_score100": 90.0, "dart_types": ["수주"]},  # 제외 대상
    }
    ranked = rank_replay(candidates, top=5)
    assert [sym for sym, _ in ranked] == ["A", "B"]  # 동점 → symbol 오름차순, C는 foreign_label 없어 제외


def test_rank_replay_respects_top_cap():
    candidates = {
        s: {"foreign_label": LABEL_INFLOW, "trending_score100": float(i), "dart_types": []}
        for i, s in enumerate("ABCDE")
    }
    ranked = rank_replay(candidates, top=2)
    assert len(ranked) == 2


# ------------------------------------------------------------------ Step 3: 산수(순수)

def test_picks_outcome_subtracts_fee():
    bars = {D: {"open": 100.0, "close": 102.0, "volume": 500.0}}
    out = picks_outcome(bars, D, fee_bp=20.0)
    assert out["gross_bp"] == pytest.approx(200.0)
    assert out["net_bp"] == pytest.approx(180.0)


def test_picks_outcome_none_when_no_bar():
    assert picks_outcome({}, D, fee_bp=20.0) is None


def test_aggregate_replay_math():
    records = [
        {"net_bp": 100.0, "rvol": 2.0}, {"net_bp": -50.0, "rvol": None}, {"net_bp": 30.0, "rvol": 4.0},
    ]
    agg = aggregate_replay(records)
    assert agg["n"] == 3
    assert agg["hit_rate"] == pytest.approx(2 / 3)
    assert agg["avg_net_bp"] == pytest.approx(80.0 / 3)
    assert agg["sum_net_bp"] == pytest.approx(80.0)
    assert agg["avg_rvol"] == pytest.approx(3.0)


def test_aggregate_replay_empty():
    agg = aggregate_replay([])
    assert agg == {"n": 0, "hit_rate": None, "avg_net_bp": None, "sum_net_bp": None, "avg_rvol": None}


def test_kodex_5d_trend_requires_six_days():
    bars = {D - timedelta(days=1): {"close": 100.0}}
    assert kodex_5d_trend(bars, D) is None


def test_kodex_5d_trend_computes_pct():
    n = 6
    bars = _bars_with_history(D - timedelta(days=1), n_days=n, closes=[100.0, 101, 102, 103, 104, 110.0], vols=[1.0] * n)
    trend = kodex_5d_trend(bars, D)
    assert trend == pytest.approx((110.0 / 100.0 - 1) * 100)


def test_market_foreign_proxy_sums_only_matching_date():
    frgn_idx = index_frgn_flow([
        {"symbol": "A", "date": "2026-08-14", "foreign_net": 100},
        {"symbol": "B", "date": "2026-08-14", "foreign_net": -30},
        {"symbol": "B", "date": "2026-08-13", "foreign_net": 999},  # 다른 날 — 제외
    ])
    proxy = market_foreign_proxy(frgn_idx, ["A", "B"], date(2026, 8, 14))
    assert proxy == pytest.approx(70.0)


def test_market_foreign_proxy_none_when_no_data():
    assert market_foreign_proxy({}, ["A"], date(2026, 8, 14)) is None
    assert market_foreign_proxy({}, ["A"], None) is None


def test_reconstructed_stance_sign_of_signs():
    assert reconstructed_stance(2.0, 1000.0) == "상승"       # +1 +1
    assert reconstructed_stance(-2.0, -1000.0) == "하락"     # -1 -1
    assert reconstructed_stance(2.0, -1000.0) == "중립"      # +1 -1 → 0
    assert reconstructed_stance(None, 1000.0) is None
    assert reconstructed_stance(0.0, 0.0) == "중립"


def test_actual_direction_compares_close_to_close():
    bars = {D: {"close": 110.0}, D - timedelta(days=1): {"close": 100.0}}
    assert actual_direction(bars, D, D - timedelta(days=1)) == "상승"
    assert actual_direction(bars, D, None) is None


def test_stance_agreement_math():
    rows = [
        {"stance": "상승", "actual": "상승"}, {"stance": "하락", "actual": "상승"},
        {"stance": None, "actual": "상승"},  # 제외
    ]
    result = stance_agreement(rows)
    assert result["n"] == 2
    assert result["agree_rate"] == pytest.approx(0.5)


# ------------------------------------------------------------------ Step 3 addendum: movers 포착(순수)

def test_amplitude_pct_computes_high_low_range_over_open():
    bar = {"open": 100.0, "high": 110.0, "low": 95.0, "close": 102.0}
    assert amplitude_pct(bar) == pytest.approx(15.0)


def test_amplitude_pct_none_when_missing_high_low():
    assert amplitude_pct({"open": 100.0, "close": 102.0}) is None
    assert amplitude_pct(None) is None


def test_upside_reach_pct_uses_high_not_close():
    bar = {"open": 100.0, "high": 106.0, "close": 101.0}
    assert upside_reach_pct(bar) == pytest.approx(6.0)  # close 기준(+1%)이 아니라 high 기준(+6%)


def test_upside_reach_pct_none_when_missing_high():
    assert upside_reach_pct({"open": 100.0, "close": 101.0}) is None


def test_bar_mover_record_flags_mover_by_high_threshold():
    mover_bar = {"open": 100.0, "high": 106.0, "low": 99.0, "close": 100.5}
    flat_bar = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.2}
    mover_rec = bar_mover_record(mover_bar)
    flat_rec = bar_mover_record(flat_bar)
    assert mover_rec["is_mover"] is True
    assert flat_rec["is_mover"] is False
    assert mover_rec["upside_reach_pct"] == pytest.approx(6.0)


def test_bar_mover_record_none_when_bar_missing():
    assert bar_mover_record(None) is None
    assert bar_mover_record({"open": 0.0, "high": 10.0}) is None


def test_aggregate_mover_metrics_math():
    records = [
        {"amplitude_pct": 4.0, "upside_reach_pct": 2.0, "is_mover": False},
        {"amplitude_pct": 8.0, "upside_reach_pct": 6.0, "is_mover": True},
        {"amplitude_pct": 6.0, "upside_reach_pct": 3.5, "is_mover": False},
    ]
    agg = aggregate_mover_metrics(records)
    assert agg["n"] == 3
    assert agg["amplitude_median"] == pytest.approx(6.0)
    assert agg["reach3_rate"] == pytest.approx(2 / 3)   # 3.5, 6.0 >= 3%
    assert agg["reach5_rate"] == pytest.approx(1 / 3)   # 6.0 >= 5%
    assert agg["mover_rate"] == pytest.approx(1 / 3)


def test_aggregate_mover_metrics_empty():
    agg = aggregate_mover_metrics([])
    assert agg == {"n": 0, "amplitude_median": None, "reach3_rate": None, "reach5_rate": None, "mover_rate": None}


def test_mover_precision_recall_picks_subset_of_universe():
    universe = [
        {"is_mover": True}, {"is_mover": True}, {"is_mover": False}, {"is_mover": False},
    ]
    picks = [universe[0], universe[2]]  # 1개는 mover, 1개는 아님
    pr = mover_precision_recall(picks, universe)
    assert pr["n_picks"] == 2
    assert pr["n_pick_movers"] == 1
    assert pr["n_universe_movers"] == 2
    assert pr["precision"] == pytest.approx(0.5)   # picks 중 1/2가 mover
    assert pr["recall"] == pytest.approx(0.5)       # 유니버스 movers 2개 중 1개를 잡음


def test_mover_precision_recall_none_when_empty():
    pr = mover_precision_recall([], [])
    assert pr["precision"] is None
    assert pr["recall"] is None


def test_mover_threshold_matches_5_percent():
    assert MOVER_THRESHOLD_PCT == 5.0


# ------------------------------------------------------------------ 봉 캐시(fake candle source)

def test_fetch_daily_bars_ohlcv_includes_volume_and_high_low():
    frame = _frame({D: _row(100, 110, 500)})
    source = FakeCandleSource({"TEST": frame})
    from datetime import datetime
    bars = fetch_daily_bars_ohlcv(source, "TEST", datetime(2026, 1, 1), datetime(2026, 12, 31), sleep_seconds=0)
    assert bars == {D: {"open": 100.0, "close": 110.0, "high": 110.0, "low": 100.0, "volume": 500.0}}


def test_build_bar_cache_separates_success_and_failure():
    from datetime import datetime
    frame = _frame({D: _row(100, 110)})
    source = FakeCandleSource({"OK": frame})
    cache, failed = build_bar_cache(source, ["OK", "MISSING"], datetime(2026, 1, 1), datetime(2026, 12, 31), sleep_seconds=0)
    assert list(cache.keys()) == ["OK"]
    assert failed == ["MISSING"]


# ------------------------------------------------------------------ 전체 오케스트레이션(fake candle source, 네트워크 없음)

def test_run_reconstruction_end_to_end(tmp_path):
    today = date(2026, 8, 17)
    d = date(2026, 8, 14)   # trading day
    prior = date(2026, 8, 13)

    # frgn_flow: D 이전 재유입(INFLOW) 라벨이 나오도록
    _write_jsonl(tmp_path / "data" / "ledger" / "frgn_flow.jsonl", [
        {"date": (d - timedelta(days=2)).isoformat(), "symbol": "005930", "foreign_net": -100, "inst_net": 0},
        {"date": prior.isoformat(), "symbol": "005930", "foreign_net": 150, "inst_net": 0},
    ])
    _write_jsonl(tmp_path / "data" / "ledger" / "disclosures.jsonl", [
        {"stock_code": "005930", "report_nm": "수주 계약 체결", "rcept_dt": d.strftime("%Y%m%d")},
    ])

    # 21+ 거래일 봉(005930, RVOL/5일수익률 계산 가능하도록) + 앵커(069500)
    n = 25
    symbol_bars = _bars_with_history(
        d, n_days=n,
        closes=[100.0] * (n - 6) + [101, 102, 103, 104, 105, 110.0],
        vols=[100.0] * (n - 1) + [400.0],
    )
    anchor_bars_raw = _bars_with_history(d, n_days=n, closes=[1000.0] * (n - 1) + [990.0], vols=[1.0] * n)

    symbol_frame = pd.DataFrame(
        [{"open": v["open"], "high": v["open"], "low": v["open"], "close": v["close"], "volume": v["volume"]} for v in symbol_bars.values()],
        index=pd.DatetimeIndex([pd.Timestamp(dd, tz="UTC") for dd in symbol_bars], name="date"),
    )
    anchor_frame = pd.DataFrame(
        [{"open": v["open"], "high": v["open"], "low": v["open"], "close": v["close"], "volume": v["volume"]} for v in anchor_bars_raw.values()],
        index=pd.DatetimeIndex([pd.Timestamp(dd, tz="UTC") for dd in anchor_bars_raw], name="date"),
    )
    source = FakeCandleSource({"005930": symbol_frame, ANCHOR_SYMBOL: anchor_frame})

    result = run_reconstruction(tmp_path, days=1, top=5, universe=["005930"], candle_source=source, fee_bp=20.0, today=today, sleep_seconds=0)

    assert result["trading_days"] == [d.isoformat()]
    assert len(result["records"]) == 1
    rec = result["records"][0]
    assert rec["symbol"] == "005930"
    assert rec["net_bp"] == pytest.approx(rec["gross_bp"] - 20.0)
    assert result["aggregate"]["n"] == 1
    assert result["n_symbols_with_flow"] == 1
    # movers 지표도 채워져야 한다(고가가 있으므로 amplitude/reach 계산 가능).
    assert result["mover_universe_aggregate"]["n"] == 1
    assert result["mover_picks_aggregate"]["n"] == 1
    assert result["mover_precision_recall"]["n_picks"] == 1

    text = format_report(result)
    assert "Step" not in text  # 출력엔 코드 용어가 아니라 사람이 읽는 표만
    assert "재구성" in text

    path = write_ledger(result, tmp_path, today=today)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["params"]["days"] == 1


def test_run_reconstruction_symbol_without_flow_history_is_excluded(tmp_path):
    today = date(2026, 8, 17)
    d = date(2026, 8, 14)
    # frgn_flow 원장이 아예 없음 — 005930 은 채점 대상에서 빠져야 한다.
    _write_jsonl(tmp_path / "data" / "ledger" / "disclosures.jsonl", [])

    anchor_frame = _frame({d: _row(1000, 1000, 1.0)})
    source = FakeCandleSource({ANCHOR_SYMBOL: anchor_frame})

    result = run_reconstruction(tmp_path, days=1, top=5, universe=["005930"], candle_source=source, today=today, sleep_seconds=0)
    assert result["trading_days"] == [d.isoformat()]
    assert result["records"] == []
    assert result["n_symbols_with_flow"] == 0


def test_run_reconstruction_missing_ledgers_do_not_crash(tmp_path):
    source = FakeCandleSource({})
    result = run_reconstruction(tmp_path, days=1, top=5, universe=["005930"], candle_source=source, today=date(2026, 8, 17), sleep_seconds=0)
    assert result["trading_days"] == []
    assert result["records"] == []


# ------------------------------------------------------------------ 출력 포맷

def test_format_report_flags_insufficient_sample():
    result = {
        "params": {"days": 1, "top": 5, "max_symbols": 10, "fee_bp": 20.0, "today": "2026-08-17"},
        "universe_size": 10, "n_symbols_with_flow": 0,
        "n_symbols_bars_ok": 0, "n_symbols_bars_failed": 0,
        "trading_days": [], "day_rows": [], "records": [],
        "aggregate": aggregate_replay([]),
        "stance_rows": [], "stance_summary": stance_agreement([]),
        "mover_picks_aggregate": aggregate_mover_metrics([]),
        "mover_universe_aggregate": aggregate_mover_metrics([]),
        "mover_precision_recall": mover_precision_recall([], []),
    }
    text = format_report(result)
    assert "판단 불가" in text
    assert str(MIN_SAMPLE_FOR_JUDGEMENT) in text
    assert "Movers 포착" in text
    assert "판정:" in text


# ==================================================================
# 서브프로젝트 O — 외국인 축 v2(실무 규칙) + A/B
# ==================================================================

def _flow(d: date, foreign: float, inst: float = 0.0) -> dict:
    return {"date": d.isoformat(), "foreign_net": foreign, "inst_net": inst}


# ------------------------------------------------------------------ prior_flow_rows(공유 look-ahead 경계)

def test_prior_flow_rows_excludes_same_day_and_limits_window():
    frgn_idx = index_frgn_flow([
        {"symbol": "005930", "date": (D - timedelta(days=3)).isoformat(), "foreign_net": -10, "inst_net": 0},
        {"symbol": "005930", "date": (D - timedelta(days=2)).isoformat(), "foreign_net": 20, "inst_net": 0},
        {"symbol": "005930", "date": D.isoformat(), "foreign_net": 500, "inst_net": 0},  # D 당일 — 배제
    ])
    rows = prior_flow_rows(frgn_idx, "005930", D, days=1)
    assert len(rows) == 1
    assert rows[0]["date"] == (D - timedelta(days=2)).isoformat()  # 최근 1개만


def test_prior_flow_rows_empty_when_no_symbol():
    assert prior_flow_rows({}, "005930", D) == []


# foreign_score_v2 자체의 규칙별 on/off·결정론·no-lookahead 테스트는
# tests/test_foreign_flow_v2.py 로 옮겼다(함수가 quant/analyze/foreign_flow_v2.py
# 로 이동했으므로 — 2026-08-17 라이브 스코어러 적용 후속). 여기서는 그 함수를
# report_replay 의 변형 B/C 채점(score_replay_v2/rank_replay_v2, REPLAY_WEIGHTS_B/C)
# 에 배선하는 부분만 검증한다.

# ------------------------------------------------------------------ REPLAY_WEIGHTS_B

def test_replay_weights_b_sum_to_100():
    assert sum(REPLAY_WEIGHTS_B.values()) == 100.0


def test_replay_weights_b_matches_documented_split():
    assert REPLAY_WEIGHTS_B == {"foreign_v2": 40.0, "trending": 25.0, "catalyst": 20.0, "intensity": 15.0}


# ------------------------------------------------------------------ score_replay_v2 / rank_replay_v2

def test_score_replay_v2_full_marks():
    result = score_replay_v2(FOREIGN_V2_MAX, 75.0, ["수주"], 0.05)
    assert result["score100"] == pytest.approx(100.0)
    assert result["foreign_pts"] == pytest.approx(REPLAY_WEIGHTS_B["foreign_v2"])
    assert result["trending_pts"] == pytest.approx(REPLAY_WEIGHTS_B["trending"])
    assert result["catalyst_pts"] == pytest.approx(REPLAY_WEIGHTS_B["catalyst"])
    assert result["intensity_pts"] == pytest.approx(REPLAY_WEIGHTS_B["intensity"])


def test_score_replay_v2_zero_marks_does_not_crash_on_none_inputs():
    result = score_replay_v2(0, None, [], None)
    assert result["score100"] == pytest.approx(0.0)


def test_score_replay_v2_medium_trending_and_intensity_are_fractional():
    result = score_replay_v2(0, 60.0, [], 0.02)  # trending MED, intensity MED
    assert result["trending_pts"] == pytest.approx(REPLAY_WEIGHTS_B["trending"] * (8 / 15))
    assert result["intensity_pts"] == pytest.approx(REPLAY_WEIGHTS_B["intensity"] * 0.5)


def test_rank_replay_v2_deterministic_tie_break_by_symbol():
    candidates = {
        "B": {"foreign_v2_score": FOREIGN_V2_MAX, "trending_score100": None, "dart_types": [], "intensity_ratio": None},
        "A": {"foreign_v2_score": FOREIGN_V2_MAX, "trending_score100": None, "dart_types": [], "intensity_ratio": None},
    }
    ranked = rank_replay_v2(candidates, top=5)
    assert [sym for sym, _ in ranked] == ["A", "B"]


def test_rank_replay_v2_respects_top_cap():
    candidates = {
        s: {"foreign_v2_score": i, "trending_score100": None, "dart_types": [], "intensity_ratio": None}
        for i, s in enumerate("ABCDE")
    }
    ranked = rank_replay_v2(candidates, top=2)
    assert len(ranked) == 2


def test_rank_replay_v2_orders_by_score_then_foreign_v2_strength():
    candidates = {
        "LOW": {"foreign_v2_score": 5, "trending_score100": None, "dart_types": [], "intensity_ratio": None},
        "HIGH": {"foreign_v2_score": FOREIGN_V2_MAX, "trending_score100": None, "dart_types": [], "intensity_ratio": None},
    }
    ranked = rank_replay_v2(candidates, top=5)
    assert [sym for sym, _ in ranked] == ["HIGH", "LOW"]


# ------------------------------------------------------------------ run_reconstruction(variant="b") / run_ab_reconstruction

def _ab_fixture(tmp_path: Path):
    today = date(2026, 8, 17)
    d = date(2026, 8, 14)
    prior = date(2026, 8, 13)

    _write_jsonl(tmp_path / "data" / "ledger" / "frgn_flow.jsonl", [
        {"date": (d - timedelta(days=2)).isoformat(), "symbol": "005930", "foreign_net": -100, "inst_net": 0},
        {"date": prior.isoformat(), "symbol": "005930", "foreign_net": 150, "inst_net": 30},
    ])
    _write_jsonl(tmp_path / "data" / "ledger" / "disclosures.jsonl", [
        {"stock_code": "005930", "report_nm": "수주 계약 체결", "rcept_dt": d.strftime("%Y%m%d")},
    ])

    n = 25
    symbol_bars = _bars_with_history(
        d, n_days=n,
        closes=[100.0] * (n - 6) + [101, 102, 103, 104, 105, 110.0],
        vols=[100.0] * (n - 1) + [400.0],
    )
    anchor_bars_raw = _bars_with_history(d, n_days=n, closes=[1000.0] * (n - 1) + [990.0], vols=[1.0] * n)

    symbol_frame = pd.DataFrame(
        [{"open": v["open"], "high": v["open"], "low": v["open"], "close": v["close"], "volume": v["volume"]} for v in symbol_bars.values()],
        index=pd.DatetimeIndex([pd.Timestamp(dd, tz="UTC") for dd in symbol_bars], name="date"),
    )
    anchor_frame = pd.DataFrame(
        [{"open": v["open"], "high": v["open"], "low": v["open"], "close": v["close"], "volume": v["volume"]} for v in anchor_bars_raw.values()],
        index=pd.DatetimeIndex([pd.Timestamp(dd, tz="UTC") for dd in anchor_bars_raw], name="date"),
    )
    source = FakeCandleSource({"005930": symbol_frame, ANCHOR_SYMBOL: anchor_frame})
    return today, d, source


def test_run_reconstruction_variant_b_uses_foreign_score_v2(tmp_path):
    today, d, source = _ab_fixture(tmp_path)
    result = run_reconstruction(
        tmp_path, days=1, top=5, universe=["005930"], candle_source=source, fee_bp=20.0, today=today,
        sleep_seconds=0, variant="b",
    )
    assert result["params"]["variant"] == "b"
    assert result["trading_days"] == [d.isoformat()]
    assert len(result["records"]) == 1
    assert result["records"][0]["symbol"] == "005930"


def test_run_reconstruction_variant_b_excludes_symbol_without_flow_history(tmp_path):
    today = date(2026, 8, 17)
    d = date(2026, 8, 14)
    _write_jsonl(tmp_path / "data" / "ledger" / "disclosures.jsonl", [])
    anchor_frame = _frame({d: _row(1000, 1000, 1.0)})
    source = FakeCandleSource({ANCHOR_SYMBOL: anchor_frame})

    result = run_reconstruction(
        tmp_path, days=1, top=5, universe=["005930"], candle_source=source, today=today, sleep_seconds=0, variant="b",
    )
    assert result["records"] == []
    assert result["n_symbols_with_flow"] == 0


def test_run_ab_reconstruction_shares_bar_fetch_and_returns_both_variants(tmp_path):
    today, d, source = _ab_fixture(tmp_path)
    ab = run_ab_reconstruction(
        tmp_path, days=1, top=5, universe=["005930"], candle_source=source, fee_bp=20.0, today=today, sleep_seconds=0,
    )
    assert ab["a"]["params"]["variant"] == "a"
    assert ab["b"]["params"]["variant"] == "b"
    assert ab["a"]["trading_days"] == ab["b"]["trading_days"] == [d.isoformat()]
    # 같은 universe(005930) ∪ 앵커(069500) 를 한 번씩만 조회 — 변형 두 개가 봉 캐시를 공유했다는 증거.
    assert sorted(source.calls) == sorted({"005930", ANCHOR_SYMBOL})

    text = format_ab_comparison(ab["a"], ab["b"])
    assert "A/B 비교" in text
    assert "판정:" in text


# ==================================================================
# 변형 C — 강도보정 축 격리 실험 (오케스트레이터 후속 지시, 2026-08-17)
# ==================================================================

# ------------------------------------------------------------------ REPLAY_WEIGHTS_C

def test_replay_weights_c_sum_to_100():
    assert sum(REPLAY_WEIGHTS_C.values()) == 100.0


def test_replay_weights_c_matches_orchestrator_split():
    assert REPLAY_WEIGHTS_C == {"foreign_v2": 47.0, "trending": 29.0, "catalyst": 24.0}


def test_replay_weights_c_has_no_intensity_axis():
    # B에는 있고 C에는 없어야 하는 게 이 격리 실험의 핵심 차이.
    assert "intensity" in REPLAY_WEIGHTS_B
    assert "intensity" not in REPLAY_WEIGHTS_C


# ------------------------------------------------------------------ score_replay_c / rank_replay_c

def test_score_replay_c_full_marks():
    result = score_replay_c(FOREIGN_V2_MAX, 75.0, ["수주"])
    assert result["score100"] == pytest.approx(100.0)
    assert result["foreign_pts"] == pytest.approx(REPLAY_WEIGHTS_C["foreign_v2"])
    assert result["trending_pts"] == pytest.approx(REPLAY_WEIGHTS_C["trending"])
    assert result["catalyst_pts"] == pytest.approx(REPLAY_WEIGHTS_C["catalyst"])
    assert "intensity_pts" not in result


def test_score_replay_c_zero_marks():
    result = score_replay_c(0, None, [])
    assert result["score100"] == pytest.approx(0.0)


def test_rank_replay_c_deterministic_tie_break_by_symbol():
    candidates = {
        "B": {"foreign_v2_score": FOREIGN_V2_MAX, "trending_score100": None, "dart_types": []},
        "A": {"foreign_v2_score": FOREIGN_V2_MAX, "trending_score100": None, "dart_types": []},
    }
    ranked = rank_replay_c(candidates, top=5)
    assert [sym for sym, _ in ranked] == ["A", "B"]


def test_rank_replay_c_respects_top_cap():
    candidates = {
        s: {"foreign_v2_score": i, "trending_score100": None, "dart_types": []}
        for i, s in enumerate("ABCDE")
    }
    ranked = rank_replay_c(candidates, top=2)
    assert len(ranked) == 2


# ------------------------------------------------------------------ run_reconstruction(variant="c") / run_variant_comparison

def test_run_reconstruction_variant_c_uses_foreign_score_v2_without_intensity_axis(tmp_path):
    today, d, source = _ab_fixture(tmp_path)
    result = run_reconstruction(
        tmp_path, days=1, top=5, universe=["005930"], candle_source=source, fee_bp=20.0, today=today,
        sleep_seconds=0, variant="c",
    )
    assert result["params"]["variant"] == "c"
    assert result["trading_days"] == [d.isoformat()]
    assert len(result["records"]) == 1


def test_run_variant_comparison_shares_bar_fetch_across_three_variants(tmp_path):
    today, d, source = _ab_fixture(tmp_path)
    results = run_variant_comparison(
        tmp_path, days=1, top=5, universe=["005930"], candle_source=source, fee_bp=20.0, today=today,
        sleep_seconds=0, variants=("a", "b", "c"),
    )
    assert set(results.keys()) == {"a", "b", "c"}
    assert results["a"]["params"]["variant"] == "a"
    assert results["b"]["params"]["variant"] == "b"
    assert results["c"]["params"]["variant"] == "c"
    assert results["a"]["trading_days"] == results["b"]["trading_days"] == results["c"]["trading_days"] == [d.isoformat()]
    # 세 변형이 봉 캐시를 공유했다는 증거 — 종목당 fetch 는 한 번뿐이어야 한다
    # (variant 수만큼 배로 늘지 않는다 = 재수집 없음).
    assert sorted(source.calls) == sorted({"005930", ANCHOR_SYMBOL})


def test_run_ab_reconstruction_still_works_after_generalization(tmp_path):
    """`run_ab_reconstruction`이 `run_variant_comparison`의 얇은 래퍼로
    바뀐 뒤에도 기존 반환 shape({"a", "b"}만)을 유지하는지 회귀 확인."""
    today, d, source = _ab_fixture(tmp_path)
    ab = run_ab_reconstruction(
        tmp_path, days=1, top=5, universe=["005930"], candle_source=source, fee_bp=20.0, today=today, sleep_seconds=0,
    )
    assert set(ab.keys()) == {"a", "b"}


# ------------------------------------------------------------------ format_abc_comparison

def _synthetic_result(n: int, hit_rate: float, avg_net_bp: float, precision: float, mover_rate: float = 0.24) -> dict:
    return {
        "aggregate": {"n": n, "hit_rate": hit_rate, "avg_net_bp": avg_net_bp},
        "mover_picks_aggregate": {"n": n, "amplitude_median": 5.0, "mover_rate": mover_rate},
        "mover_precision_recall": {"precision": precision, "recall": 0.05},
        "mover_universe_aggregate": {"n": n * 10, "amplitude_median": 5.5, "mover_rate": 0.25},
    }


def test_format_abc_comparison_c_retains_most_of_b_edge_declares_practitioner_rules_win():
    result_a = _synthetic_result(n=150, hit_rate=0.50, avg_net_bp=7.0, precision=0.24)
    result_b = _synthetic_result(n=150, hit_rate=0.60, avg_net_bp=20.0, precision=0.30)
    result_c = _synthetic_result(n=150, hit_rate=0.58, avg_net_bp=18.0, precision=0.29)  # retain ~0.8, ~0.83
    text = format_abc_comparison(result_a, result_b, result_c)
    assert "A/B/C 비교" in text
    assert "실무 규칙" in text
    assert "C 채택 권고" in text


def test_format_abc_comparison_c_collapses_toward_a_declares_intensity_driver():
    result_a = _synthetic_result(n=150, hit_rate=0.50, avg_net_bp=7.0, precision=0.24)
    result_b = _synthetic_result(n=150, hit_rate=0.60, avg_net_bp=20.0, precision=0.30)
    result_c = _synthetic_result(n=150, hit_rate=0.51, avg_net_bp=8.0, precision=0.245)  # retain ~0.1, ~0.08
    text = format_abc_comparison(result_a, result_b, result_c)
    assert "붕괴" in text
    assert "driver" in text


def test_format_abc_comparison_insufficient_sample():
    result_a = _synthetic_result(n=5, hit_rate=0.50, avg_net_bp=7.0, precision=0.24)
    result_b = _synthetic_result(n=5, hit_rate=0.60, avg_net_bp=20.0, precision=0.30)
    result_c = _synthetic_result(n=5, hit_rate=0.58, avg_net_bp=18.0, precision=0.29)
    text = format_abc_comparison(result_a, result_b, result_c)
    assert "표본 부족" in text
    assert str(MIN_SAMPLE_FOR_JUDGEMENT) in text


def test_format_abc_comparison_b_does_not_beat_a_comparison_moot():
    result_a = _synthetic_result(n=150, hit_rate=0.50, avg_net_bp=7.0, precision=0.24)
    result_b = _synthetic_result(n=150, hit_rate=0.49, avg_net_bp=5.0, precision=0.20)  # B가 A보다 못함
    result_c = _synthetic_result(n=150, hit_rate=0.50, avg_net_bp=7.0, precision=0.24)
    text = format_abc_comparison(result_a, result_b, result_c)
    assert "성립하지 않는다" in text


def test_composition_retain_thresholds_are_between_zero_and_one():
    assert 0.0 < COMPOSITION_RETAIN_LOW < COMPOSITION_RETAIN_HIGH < 1.0
