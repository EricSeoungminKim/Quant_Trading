"""`quant.backtest.report_follow` — 리포트 추종 백테스트(단타·1주 적립).

네트워크는 절대 타지 않는다 — yfinance 호출은 전부 fake candle source 로 대체한다
(모듈 지침: intraday_verify.py 와 같은 관례).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from quant.backtest.report_follow import (
    FIRST_REPORT_DATE,
    MIN_SAMPLE_FOR_JUDGEMENT,
    aggregate_day_trades,
    aggregate_weekly,
    build_applied_candidates,
    candidates_for_day,
    fetch_symbol_bars,
    format_report,
    map_to_trading_day,
    run_follow,
    scan_report_days,
    simulate_day_trades,
    simulate_weekly_accumulate,
    write_ledger,
)

D = date(2026, 8, 17)  # 월요일


# ------------------------------------------------------------------ fake candle source

class FakeCandleSource:
    """symbol → DataFrame(UTC tz-aware DatetimeIndex, open/high/low/close/volume)."""

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


def _row(o: float, c: float, v: float = 1000) -> dict:
    return {"open": o, "high": max(o, c), "low": min(o, c), "close": c, "volume": v}


def _frame(rows: dict[date, dict]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in rows], name="date")
    return pd.DataFrame(list(rows.values()), index=idx)


def _payload(auto_watch: str, session_date: str = "2026-08-17") -> dict:
    return {"session_date": session_date, "auto_watch": f"AUTO_WATCH: {auto_watch}"}


def _write_engine_json(root: Path, d: date, payload: dict, market: str = "KR") -> None:
    p = root / "out" / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}" / f"{market}_engine.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ 리포트 스캔

def test_scan_report_days_reads_from_directory_path(tmp_path):
    _write_engine_json(tmp_path, date(2026, 8, 13), _payload("005930:NEWS"))
    _write_engine_json(tmp_path, date(2026, 8, 14), _payload("000660:RANK"))
    _write_engine_json(tmp_path, date(2026, 8, 14), _payload("US-only"), market="US")  # 다른 시장 — KR 스캔엔 안 잡힘

    result = scan_report_days(tmp_path, market="KR")
    assert set(result) == {date(2026, 8, 13), date(2026, 8, 14)}
    assert result[date(2026, 8, 13)]["auto_watch"] == "AUTO_WATCH: 005930:NEWS"


def test_scan_report_days_skips_corrupt_json(tmp_path):
    p = tmp_path / "out" / "2026" / "08" / "13" / "KR_engine.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    assert scan_report_days(tmp_path, market="KR") == {}


def test_scan_report_days_empty_when_no_out_dir(tmp_path):
    assert scan_report_days(tmp_path, market="KR") == {}


def test_candidates_for_day_extracts_kr_symbols_only():
    payload = _payload("005930:NEWS+RANK 000660:RANK AAPL notasymbol123456")
    assert candidates_for_day(payload, "KR") == ["005930", "000660"]


# ------------------------------------------------------------------ 개장일 매핑

def test_map_to_trading_day_same_day_when_open():
    trading_days = [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17)]
    assert map_to_trading_day(date(2026, 8, 14), trading_days) == date(2026, 8, 14)


def test_map_to_trading_day_holiday_shifts_to_next_open_day():
    # 8/15(토)·8/16(일) 는 개장일 목록에 없음 — 다음 개장일 8/17로 넘어가야 한다.
    trading_days = [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17)]
    assert map_to_trading_day(date(2026, 8, 15), trading_days) == date(2026, 8, 17)
    assert map_to_trading_day(date(2026, 8, 16), trading_days) == date(2026, 8, 17)


def test_map_to_trading_day_none_when_no_future_open_day():
    trading_days = [date(2026, 8, 13), date(2026, 8, 14)]
    assert map_to_trading_day(date(2026, 8, 16), trading_days) is None


def test_build_applied_candidates_merges_holiday_carry_into_next_open_day():
    trading_days = [date(2026, 8, 14), date(2026, 8, 17)]
    report_days = {
        date(2026, 8, 14): _payload("005930:NEWS"),
        date(2026, 8, 15): _payload("000660:RANK"),  # 휴장 — 8/17로 캐리
        date(2026, 8, 16): _payload("035420:NEWS"),  # 휴장 — 8/17로 캐리
        date(2026, 8, 17): _payload("068270:NEWS"),
    }
    applied, mapping = build_applied_candidates(report_days, trading_days, "KR")
    assert applied[date(2026, 8, 14)] == {"005930"}
    assert applied[date(2026, 8, 17)] == {"000660", "035420", "068270"}
    assert mapping[date(2026, 8, 15)] == date(2026, 8, 17)


# ------------------------------------------------------------------ 시세 fetch(fake)

def test_fetch_symbol_bars_only_returns_requested_days_with_data():
    frame = _frame({D: _row(100, 105), D + timedelta(days=1): _row(200, 190)})
    source = FakeCandleSource({"005930": frame})
    bars = fetch_symbol_bars(source, "005930", [D, D + timedelta(days=1), D + timedelta(days=5)], sleep_seconds=0)
    assert bars[D] == {"open": 100.0, "close": 105.0}
    assert bars[D + timedelta(days=1)] == {"open": 200.0, "close": 190.0}
    assert D + timedelta(days=5) not in bars


def test_fetch_symbol_bars_empty_when_no_data():
    source = FakeCandleSource({})
    assert fetch_symbol_bars(source, "UNKNOWN", [D], sleep_seconds=0) == {}


# ------------------------------------------------------------------ 단타 시뮬(순수 산수)

def test_simulate_day_trades_bp_math_with_fee():
    applied = {D: {"005930"}}
    bars = {"005930": {D: {"open": 100.0, "close": 103.0}}}
    records = simulate_day_trades(applied, bars, fee_bp=20.0)
    assert len(records) == 1
    rec = records[0]
    assert rec["gross_bp"] == pytest.approx(300.0)   # (103-100)/100 * 10000
    assert rec["net_bp"] == pytest.approx(280.0)      # 300 - 20


def test_simulate_day_trades_skips_missing_bars():
    applied = {D: {"005930", "000660"}}
    bars = {"005930": {D: {"open": 100.0, "close": 103.0}}}  # 000660 봉 없음
    records = simulate_day_trades(applied, bars, fee_bp=20.0)
    assert [r["symbol"] for r in records] == ["005930"]


def test_aggregate_day_trades_math():
    records = [
        {"gross_bp": 100.0, "net_bp": 80.0},
        {"gross_bp": -50.0, "net_bp": -70.0},
    ]
    agg = aggregate_day_trades(records)
    assert agg["n"] == 2
    assert agg["hit_rate"] == pytest.approx(0.5)
    assert agg["avg_gross_bp"] == pytest.approx(25.0)
    assert agg["avg_net_bp"] == pytest.approx(5.0)
    assert agg["sum_net_bp"] == pytest.approx(10.0)


def test_aggregate_day_trades_empty():
    agg = aggregate_day_trades([])
    assert agg == {"n": 0, "hit_rate": None, "avg_gross_bp": None, "avg_net_bp": None, "sum_net_bp": None}


# ------------------------------------------------------------------ 1주 적립 시뮬(순수 산수)

def test_simulate_weekly_accumulate_buy_hold_sell_final_valuation():
    days = [D, D + timedelta(days=1), D + timedelta(days=2), D + timedelta(days=3)]
    d0, d1, d2, d3 = days
    # 005930: d0,d1 후보(2주 적립) → d2 탈락(1주 매도) → d3까지 보유 1주, d3 종가로 평가
    applied = {d0: {"005930"}, d1: {"005930"}, d2: set(), d3: set()}
    bars = {
        "005930": {
            d0: {"open": 100.0, "close": 102.0},
            d1: {"open": 105.0, "close": 106.0},
            d2: {"open": 108.0, "close": 107.0},  # 매도 시가
            d3: {"open": 110.0, "close": 112.0},  # 잔여분 평가용 종가
        }
    }
    results = simulate_weekly_accumulate(applied, days, bars, fee_bp=0.0)
    r = results["005930"]
    assert r["buys"] == 2
    assert r["sells"] == 1
    assert r["invested_krw"] == pytest.approx(205.0)          # 100 + 105
    assert r["remaining_shares"] == 1
    assert r["remaining_value_krw"] == pytest.approx(112.0)   # 1주 * d3 종가
    # 현금흐름: -100 -105(매수) +108(매도) = -97, + 잔여평가 112 = 15
    assert r["realized_krw"] == pytest.approx(-97.0)
    assert r["pnl_krw"] == pytest.approx(15.0)
    assert r["return_pct"] == pytest.approx(15.0 / 205.0 * 100)


def test_simulate_weekly_accumulate_fee_applied_per_trade():
    days = [D, D + timedelta(days=1)]
    d0, d1 = days
    applied = {d0: {"005930"}, d1: set()}
    bars = {"005930": {d0: {"open": 100.0, "close": 100.0}, d1: {"open": 100.0, "close": 100.0}}}
    results = simulate_weekly_accumulate(applied, days, bars, fee_bp=100.0)  # 1% 수수료
    r = results["005930"]
    # 매수: -100 - 1(수수료) = -101. 매도: +100 - 1(수수료) = +99. 합계 -2, 잔여 0.
    assert r["remaining_shares"] == 0
    assert r["realized_krw"] == pytest.approx(-2.0)
    assert r["pnl_krw"] == pytest.approx(-2.0)


def test_simulate_weekly_accumulate_unresolved_when_final_bar_missing():
    days = [D, D + timedelta(days=1)]
    d0, d1 = days
    applied = {d0: {"005930"}, d1: {"005930"}}  # 계속 보유 중, 아직 탈락 안 함
    bars = {"005930": {d0: {"open": 100.0, "close": 101.0}}}  # d1 봉 없음 — 잔여 평가 불가
    results = simulate_weekly_accumulate(applied, days, bars, fee_bp=0.0)
    r = results["005930"]
    assert r["remaining_shares"] == 1
    assert r["remaining_value_krw"] is None
    assert r["pnl_krw"] is None
    assert r["return_pct"] is None


def test_simulate_weekly_accumulate_never_bought_symbol_not_in_output():
    # 후보에 없던 심볼은 symbols 목록 자체에 안 잡힌다.
    applied = {D: {"005930"}}
    bars = {"005930": {D: {"open": 100.0, "close": 100.0}}}
    results = simulate_weekly_accumulate(applied, [D], bars, fee_bp=0.0)
    assert set(results) == {"005930"}


def test_aggregate_weekly_math_and_unresolved_count():
    results = {
        "A": {"invested_krw": 100.0, "pnl_krw": 20.0},
        "B": {"invested_krw": 50.0, "pnl_krw": None},
    }
    agg = aggregate_weekly(results)
    assert agg["n_symbols"] == 2
    assert agg["n_resolved"] == 1
    assert agg["n_unresolved"] == 1
    assert agg["total_invested_krw"] == pytest.approx(150.0)
    assert agg["total_pnl_krw"] == pytest.approx(20.0)
    assert agg["total_return_pct"] == pytest.approx(20.0 / 150.0 * 100)


def test_aggregate_weekly_empty():
    agg = aggregate_weekly({})
    assert agg["n_symbols"] == 0
    assert agg["total_pnl_krw"] is None
    assert agg["total_return_pct"] is None


# ------------------------------------------------------------------ 표본 부족 문구

def test_format_report_insufficient_sample_warning():
    result = {
        "market": "KR", "fee_bp": 20.0,
        "report_days": [D], "trading_days": [D], "mapping": {},
        "day_trade_records": [{"date": D.isoformat(), "symbol": "005930", "gross_bp": 10.0, "net_bp": -10.0}],
        "day_trade_agg": aggregate_day_trades(
            [{"gross_bp": 10.0, "net_bp": -10.0}]
        ),
        "weekly_results": {}, "weekly_agg": aggregate_weekly({}),
    }
    out = format_report(result, fee_bp=20.0)
    assert f"< {MIN_SAMPLE_FOR_JUDGEMENT}" in out
    assert "판단 불가" in out
    assert FIRST_REPORT_DATE.isoformat() in out


def test_format_report_no_reports_at_all():
    result = {
        "market": "KR", "fee_bp": 20.0,
        "report_days": [], "trading_days": [], "mapping": {},
        "day_trade_records": [], "day_trade_agg": aggregate_day_trades([]),
        "weekly_results": {}, "weekly_agg": aggregate_weekly({}),
    }
    out = format_report(result, fee_bp=20.0)
    assert "표본 0" in out


# ------------------------------------------------------------------ 오케스트레이션(파일 I/O + fake candle source, 네트워크 없음)

def test_run_follow_same_day_report_end_to_end(tmp_path):
    # 리포트가 발행된 날(D) 자체가 개장일 — 같은 날 시가진입/청산.
    _write_engine_json(tmp_path, D, _payload("005930:NEWS", session_date=D.isoformat()))

    symbol_frame = _frame({D: _row(100, 105)})
    anchor_frame = _frame({D: _row(1000, 990)})
    source = FakeCandleSource({"005930": symbol_frame, "069500": anchor_frame})

    result = run_follow(tmp_path, fee_bp=20.0, candle_source=source, today=D + timedelta(days=1))

    assert result["trading_days"] == [D]
    assert result["mapping"][D] == D
    assert len(result["day_trade_records"]) == 1
    rec = result["day_trade_records"][0]
    assert rec["symbol"] == "005930"
    assert rec["gross_bp"] == pytest.approx(500.0)
    assert rec["net_bp"] == pytest.approx(480.0)
    assert result["weekly_results"]["005930"]["buys"] == 1


def test_run_follow_holiday_report_shifts_to_next_open_day(tmp_path):
    # 토요일(휴장)에 리포트가 있었다면(캐리 관례상 발행되므로) 다음 개장일(월요일)에 적용돼야 한다.
    saturday = date(2026, 8, 15)
    monday = date(2026, 8, 17)
    _write_engine_json(tmp_path, saturday, _payload("005930:NEWS", session_date=saturday.isoformat()))

    symbol_frame = _frame({monday: _row(200, 210)})
    anchor_frame = _frame({monday: _row(1000, 1010)})  # 토요일엔 앵커 봉이 없음 = 휴장
    source = FakeCandleSource({"005930": symbol_frame, "069500": anchor_frame})

    result = run_follow(tmp_path, fee_bp=0.0, candle_source=source, today=monday + timedelta(days=1))

    assert result["trading_days"] == [monday]
    assert result["mapping"][saturday] == monday
    assert len(result["day_trade_records"]) == 1
    assert result["day_trade_records"][0]["date"] == monday.isoformat()


def test_run_follow_no_reports_returns_empty(tmp_path):
    source = FakeCandleSource({})
    result = run_follow(tmp_path, fee_bp=20.0, candle_source=source, today=D)
    assert result["report_days"] == []
    assert result["day_trade_agg"]["n"] == 0
    assert result["weekly_agg"]["n_symbols"] == 0


def test_run_follow_anchor_missing_does_not_crash(tmp_path):
    _write_engine_json(tmp_path, D, _payload("005930:NEWS", session_date=D.isoformat()))
    source = FakeCandleSource({})  # 앵커 조회도 실패
    result = run_follow(tmp_path, fee_bp=20.0, candle_source=source, today=D)
    assert result["trading_days"] == []
    assert result["day_trade_agg"]["n"] == 0


# ------------------------------------------------------------------ 원장 기록

def test_write_ledger_appends_summary_row(tmp_path):
    result = {
        "market": "KR", "fee_bp": 20.0,
        "report_days": [D], "trading_days": [D], "mapping": {},
        "day_trade_records": [], "day_trade_agg": aggregate_day_trades([]),
        "weekly_results": {}, "weekly_agg": aggregate_weekly({}),
    }
    path = write_ledger(result, tmp_path, today=D)
    assert path == tmp_path / "data" / "ledger" / "report_follow.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["market"] == "KR"
    assert rows[0]["n_report_days"] == 1
    assert rows[0]["sufficient_day_trade"] is False

    # 두 번째 실행 — append (덮어쓰지 않음)
    write_ledger(result, tmp_path, today=D + timedelta(days=1))
    rows2 = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows2) == 2
