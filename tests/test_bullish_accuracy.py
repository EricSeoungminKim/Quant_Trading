"""`quant.backtest.bullish_accuracy` — 호재 마커 사전 정확도 실측 (서브프로젝트 P Part 2).

네트워크는 절대 타지 않는다 — yfinance/DART 호출은 전부 fake로 대체한다
(`test_catalyst_study.py`와 동일 규율).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from quant.backtest.bullish_accuracy import (
    format_report,
    run_accuracy_study,
    run_part_a,
    run_part_b,
    type_for_bullish,
    write_ledger,
)
from quant.backtest.catalyst_study import BASE_RATE_KEY, DISCLOSURES_LEDGER, MENTIONS_LEDGER

D = date(2026, 8, 15)


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


# ------------------------------------------------------------------ type_for_bullish

def test_type_for_bullish_matches_dictionary():
    assert type_for_bullish("A사, 대규모 수주 공시") == "수주/공급계약"


def test_type_for_bullish_no_match_is_honest_label():
    assert type_for_bullish("오늘의 증시 브리핑") == "미매칭"
    assert type_for_bullish(None) == "미매칭"
    assert type_for_bullish("") == "미매칭"


# ------------------------------------------------------------------ Part A(공시, 순수, bar_cache 주입)

def test_run_part_a_tags_by_bullish_marker():
    nd = D + timedelta(days=1)
    disclosures_rows = [
        {"stock_code": "A", "report_nm": "수주 계약 체결", "rcept_dt": D.strftime("%Y%m%d")},
        {"stock_code": "B", "report_nm": "자사주 매입 결정", "rcept_dt": D.strftime("%Y%m%d")},
        {"stock_code": "C", "report_nm": "아무개 공시", "rcept_dt": D.strftime("%Y%m%d")},  # 봉 없음
    ]
    bar_cache = {
        "A": {D: {"open": 100.0, "close": 100.0}, nd: {"open": 100.0, "close": 110.0}},
        "B": {D: {"open": 100.0, "close": 100.0}, nd: {"open": 100.0, "close": 105.0}},
    }
    result = run_part_a(disclosures_rows, bar_cache)
    assert result["n_disclosures"] == 3
    assert result["skipped_no_bars"] == 1
    assert result["type_stats"]["수주/공급계약"]["n"] == 1
    assert result["type_stats"]["수주/공급계약"]["avg_pct"] == pytest.approx(10.0)
    assert result["type_stats"]["자사주"]["avg_pct"] == pytest.approx(5.0)


def test_run_part_a_no_match_is_labeled_no_match():
    nd = D + timedelta(days=1)
    disclosures_rows = [{"stock_code": "A", "report_nm": "기업설명회 개최", "rcept_dt": D.strftime("%Y%m%d")}]
    bar_cache = {"A": {D: {"open": 100.0, "close": 100.0}, nd: {"open": 100.0, "close": 101.0}}}
    result = run_part_a(disclosures_rows, bar_cache)
    assert result["type_stats"]["미매칭"]["n"] == 1


# ------------------------------------------------------------------ Part B(뉴스, 순수, bar_cache 주입)

def test_run_part_b_tags_by_bullish_marker():
    mentions_rows = [
        {"symbol": "A", "date": D.isoformat(), "title": "A사, 대규모 공급계약 체결"},
        {"symbol": "B", "date": D.isoformat(), "title": "오늘의 시황 브리핑"},
    ]
    bar_cache = {
        "A": {D: {"open": 100.0, "close": 108.0}},
        "B": {D: {"open": 100.0, "close": 99.0}},
    }
    result = run_part_b(mentions_rows, bar_cache)
    assert result["type_stats"]["수주/공급계약"]["n"] == 1
    assert result["type_stats"]["수주/공급계약"]["avg_pct"] == pytest.approx(8.0)
    assert result["type_stats"][BASE_RATE_KEY]["n"] == 2


def test_run_part_b_skips_rows_without_bar():
    mentions_rows = [{"symbol": "A", "date": D.isoformat(), "title": "A사 수주 공시"}]
    result = run_part_b(mentions_rows, {})
    assert result["type_stats"][BASE_RATE_KEY]["n"] == 0


def test_run_part_b_carries_non_trading_crawl_date():
    saturday = date(2026, 8, 15)
    monday = date(2026, 8, 17)
    mentions_rows = [{"symbol": "A", "date": saturday.isoformat(), "title": "A사 자사주 매입 결정"}]
    bar_cache = {"A": {monday: {"open": 100.0, "close": 105.0}}}
    from quant.backtest.catalyst_study import trading_days_from_bar_cache

    trading_days = trading_days_from_bar_cache(bar_cache)
    result = run_part_b(mentions_rows, bar_cache, trading_days)
    assert result["type_stats"]["자사주"]["n"] == 1


# ------------------------------------------------------------------ 전체 오케스트레이션(fake candle source + fake DART getter)

def test_run_accuracy_study_end_to_end_with_fakes(tmp_path):
    today = date(2026, 8, 17)

    def dart_getter(params):
        return {"status": "013", "message": "no data"}

    d = date(2026, 8, 14)
    nd = date(2026, 8, 17)
    _write_jsonl(tmp_path / "data" / "ledger" / DISCLOSURES_LEDGER, [
        {"rcept_no": "R1", "stock_code": "005930", "corp_name": "삼성전자",
         "report_nm": "단일판매ㆍ공급계약체결", "rcept_dt": d.strftime("%Y%m%d")},
    ])
    _write_jsonl(tmp_path / "data" / "ledger" / MENTIONS_LEDGER, [
        {"symbol": "005930", "date": d.isoformat(), "title": "삼성전자 공급계약 체결"},
    ])

    symbol_frame = _frame({d: _row(100.0, 100.0), nd: _row(100.0, 112.0)})
    source = FakeCandleSource({"005930": symbol_frame})

    result = run_accuracy_study(
        tmp_path, days=5, max_symbols=10, candle_source=source,
        dart_api_key="dummy", dart_getter=dart_getter, today=today,
    )

    assert result["n_symbols_fetched"] == 1
    assert result["part_a"]["type_stats"]["수주/공급계약"]["n"] == 1
    assert result["part_a"]["type_stats"]["수주/공급계약"]["avg_pct"] == pytest.approx(12.0)

    text = format_report(result)
    assert "Part A" in text
    assert "Part B" in text

    path = write_ledger(result, tmp_path, today=today)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["params"]["days"] == 5
    assert path.name == "bullish_accuracy.jsonl"


def test_run_accuracy_study_missing_ledgers_do_not_crash(tmp_path):
    def dart_getter(params):
        return {"status": "013", "message": "no data"}

    source = FakeCandleSource({})
    result = run_accuracy_study(
        tmp_path, days=1, max_symbols=5, candle_source=source,
        dart_api_key="dummy", dart_getter=dart_getter, today=date(2026, 8, 17),
    )
    assert result["n_symbols_fetched"] == 0
    assert result["part_a"]["type_stats"][BASE_RATE_KEY]["n"] == 0
    assert result["part_b"]["type_stats"][BASE_RATE_KEY]["n"] == 0
    text = format_report(result)
    assert "n/a" in text
