"""`quant.backtest.catalyst_study` — 공시 유형·급등 선행신호·뉴스 키워드 연구.

네트워크는 절대 타지 않는다 — yfinance/DART 호출은 전부 fake로 대체한다
(모듈 지침과 동일: "여기가 거짓말하면 그 위에 쌓은 모든 판단이 조용히 틀린다").
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from quant.backtest.catalyst_study import (
    BASE_RATE_KEY,
    DISCLOSURES_LEDGER,
    aggregate_by_type,
    aggregate_outcomes,
    backfill_disclosures,
    build_bar_cache,
    classify_title,
    disclosure_outcome,
    fetch_daily_bars,
    find_surges,
    flag_candidates,
    format_report,
    index_disclosures_by_symbol,
    next_trading_day,
    precursor_types,
    run_part_a,
    run_part_b,
    run_part_c,
    run_study,
    select_symbols_by_frequency,
    symbol_frequency,
    trading_days_from_bar_cache,
    type_for,
    write_ledger,
)

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


# ------------------------------------------------------------------ 유형 분류

def test_type_for_matches_classify_report():
    assert type_for("단일판매ㆍ공급계약체결") == "수주"


def test_type_for_none_becomes_기타():
    assert type_for("기업설명회(IR)개최(안내공시)") == "기타"
    assert type_for(None) == "기타"
    assert type_for("") == "기타"


def test_classify_title_priority_isolates_look_ahead_class():
    # "실적"과 "급등"이 둘 다 있어도 오염 클래스가 먼저 이긴다(순서상 첫 매치).
    assert classify_title("실적 발표 후 급등한 종목 정리") == "급등 사후보도(오염 클래스)"


def test_classify_title_matches_supply_contract():
    assert classify_title("A사, B사와 대규모 공급계약 체결") == "수주/공급계약"


def test_classify_title_no_match_returns_none():
    assert classify_title("오늘의 증시 브리핑") is None


def test_classify_title_empty_returns_none():
    assert classify_title("") is None


# ------------------------------------------------------------------ 빈도/선정

def test_symbol_frequency_counts_both_sources():
    disclosures = [{"stock_code": "A"}, {"stock_code": "A"}, {"stock_code": "B"}]
    mentions = [{"symbol": "A"}, {"symbol": "C"}]
    freq = symbol_frequency(disclosures, mentions)
    assert freq == {"A": 3, "B": 1, "C": 1}


def test_select_symbols_by_frequency_orders_and_caps():
    freq = {"A": 1, "B": 5, "C": 3}
    assert select_symbols_by_frequency(freq, cap=2) == ["B", "C"]


def test_select_symbols_by_frequency_ties_break_by_symbol():
    freq = {"Z": 1, "A": 1}
    assert select_symbols_by_frequency(freq, cap=2) == ["A", "Z"]


# ------------------------------------------------------------------ 봉 구조 산수

def test_next_trading_day_finds_first_after():
    bars = {D: {}, D + timedelta(days=1): {}, D + timedelta(days=3): {}}
    assert next_trading_day(bars, D) == D + timedelta(days=1)


def test_next_trading_day_none_when_no_future_bar():
    bars = {D: {}}
    assert next_trading_day(bars, D) is None


def test_disclosure_outcome_uses_next_day_open_close_look_ahead_safe():
    """D 당일 봉이 아예 없어도(장 중 공시라 D 자체 확정을 못 믿는 상황을 가정)
    익일 시가→종가는 계산 가능해야 한다 — 주 지표는 D 정보에 의존하지 않는다."""
    nd = D + timedelta(days=1)
    bars = {nd: {"open": 100.0, "close": 110.0}}
    out = disclosure_outcome(bars, D)
    assert out is not None
    assert out["open_close_next_pct"] == pytest.approx(10.0)
    assert out["close_to_close_pct"] is None
    assert out["same_day_open_close_pct"] is None


def test_disclosure_outcome_computes_close_to_close_and_same_day_reference():
    nd = D + timedelta(days=1)
    bars = {
        D: {"open": 90.0, "close": 100.0},
        nd: {"open": 100.0, "close": 121.0},
    }
    out = disclosure_outcome(bars, D)
    assert out["open_close_next_pct"] == pytest.approx(21.0)
    assert out["close_to_close_pct"] == pytest.approx(21.0)  # (121-100)/100
    assert out["same_day_open_close_pct"] == pytest.approx((100 - 90) / 90 * 100)


def test_disclosure_outcome_none_when_no_next_day_yet():
    bars = {D: {"open": 100.0, "close": 105.0}}
    assert disclosure_outcome(bars, D) is None


def test_find_surges_detects_threshold_and_ignores_below():
    bars = {
        D: {"close": 100.0},
        D + timedelta(days=1): {"close": 116.0},   # +16% — 급등
        D + timedelta(days=2): {"close": 118.0},   # +1.7% — 아님
    }
    surges = find_surges(bars, threshold_pct=15.0)
    assert len(surges) == 1
    assert surges[0]["date"] == D + timedelta(days=1)
    assert surges[0]["pct"] == pytest.approx(16.0)


def test_find_surges_empty_when_no_prior_close():
    bars = {D: {"close": 100.0}}
    assert find_surges(bars) == []


def test_precursor_types_matches_d_and_d_minus_1_excludes_future():
    idx = index_disclosures_by_symbol([
        {"stock_code": "005930", "report_nm": "실적 발표", "rcept_dt": (D - timedelta(days=1)).strftime("%Y%m%d")},
        {"stock_code": "005930", "report_nm": "수주 계약", "rcept_dt": D.strftime("%Y%m%d")},
        {"stock_code": "005930", "report_nm": "유상증자", "rcept_dt": (D + timedelta(days=1)).strftime("%Y%m%d")},
    ])
    types = precursor_types(idx, "005930", D)
    assert "실적" in types
    assert "수주" in types
    assert "유상증자" not in types


def test_precursor_types_empty_when_no_disclosure():
    assert precursor_types({}, "005930", D) == []


# ------------------------------------------------------------------ 집계 + 후보 플래그

def test_aggregate_outcomes_math():
    rows = [
        {"open_close_next_pct": 10.0},
        {"open_close_next_pct": -2.0},
        {"open_close_next_pct": 6.0},
        {"open_close_next_pct": None},  # 제외
    ]
    agg = aggregate_outcomes(rows)
    assert agg["n"] == 3
    assert agg["avg_pct"] == pytest.approx((10.0 - 2.0 + 6.0) / 3)
    assert agg["hit_rate"] == pytest.approx(2 / 3)
    assert agg["surge_rate"] == pytest.approx(2 / 3)   # 10.0, 6.0 둘 다 ≥5%
    assert agg["limit_rate"] == pytest.approx(0.0)      # 15% 넘는 값 없음


def test_aggregate_outcomes_empty():
    agg = aggregate_outcomes([])
    assert agg == {"n": 0, "avg_pct": None, "hit_rate": None, "surge_rate": None, "limit_rate": None}


def test_aggregate_by_type_includes_base_rate():
    rows = [
        {"type": "수주", "open_close_next_pct": 10.0},
        {"type": "수주", "open_close_next_pct": 8.0},
        {"type": "기타", "open_close_next_pct": 0.0},
    ]
    result = aggregate_by_type(rows)
    assert result["수주"]["n"] == 2
    assert result[BASE_RATE_KEY]["n"] == 3
    assert result[BASE_RATE_KEY]["avg_pct"] == pytest.approx(6.0)


def test_flag_candidates_positive_and_negative():
    type_stats = {
        "수주": {"n": 40, "avg_pct": 3.0},         # base+0.5 넘음 → 긍정
        "유상증자": {"n": 40, "avg_pct": -3.0},     # base-0.5 밑 → 부정
        "기타": {"n": 40, "avg_pct": 0.6},          # 0.1%p 차이뿐 — 마진 미달
        "표본부족유형": {"n": 5, "avg_pct": 20.0},   # n 미달 — 후보 제외
        BASE_RATE_KEY: {"n": 200, "avg_pct": 0.5},
    }
    positive, negative = flag_candidates(type_stats)
    assert positive == ["수주"]
    assert negative == ["유상증자"]


def test_flag_candidates_no_base_returns_empty():
    assert flag_candidates({}) == ([], [])


# ------------------------------------------------------------------ 봉 캐시 빌드(fake candle source)

def test_fetch_daily_bars_returns_none_when_empty():
    source = FakeCandleSource({})
    assert fetch_daily_bars(source, "UNKNOWN", None, None, sleep_seconds=0) is None


def test_fetch_daily_bars_parses_rows():
    frame = _frame({D: _row(100, 110)})
    source = FakeCandleSource({"TEST": frame})
    from datetime import datetime
    bars = fetch_daily_bars(source, "TEST", datetime(2026, 1, 1), datetime(2026, 12, 31), sleep_seconds=0)
    assert bars == {D: {"open": 100.0, "close": 110.0}}


def test_build_bar_cache_separates_success_and_failure():
    from datetime import datetime
    frame = _frame({D: _row(100, 110)})
    source = FakeCandleSource({"OK": frame})
    cache, failed = build_bar_cache(
        source, ["OK", "MISSING"], datetime(2026, 1, 1), datetime(2026, 12, 31), sleep_seconds=0,
    )
    assert list(cache.keys()) == ["OK"]
    assert failed == ["MISSING"]


# ------------------------------------------------------------------ Part A/B/C 오케스트레이션(순수, bar_cache 주입)

def test_run_part_a_end_to_end():
    nd = D + timedelta(days=1)
    disclosures_rows = [
        {"stock_code": "A", "report_nm": "수주 계약 체결", "rcept_dt": D.strftime("%Y%m%d")},
        {"stock_code": "B", "report_nm": "유상증자결정", "rcept_dt": D.strftime("%Y%m%d")},
        {"stock_code": "C", "report_nm": "아무개 공시", "rcept_dt": D.strftime("%Y%m%d")},  # 봉 없음
    ]
    bar_cache = {
        "A": {D: {"open": 100.0, "close": 100.0}, nd: {"open": 100.0, "close": 110.0}},
        "B": {D: {"open": 100.0, "close": 100.0}, nd: {"open": 100.0, "close": 95.0}},
    }
    result = run_part_a(disclosures_rows, bar_cache)
    assert result["n_disclosures"] == 3
    assert result["skipped_no_bars"] == 1
    assert result["type_stats"]["수주"]["n"] == 1
    assert result["type_stats"]["수주"]["avg_pct"] == pytest.approx(10.0)
    assert result["type_stats"]["유상증자"]["avg_pct"] == pytest.approx(-5.0)


def test_run_part_a_no_next_day_yet_is_skipped_not_zero():
    disclosures_rows = [{"stock_code": "A", "report_nm": "실적", "rcept_dt": D.strftime("%Y%m%d")}]
    bar_cache = {"A": {D: {"open": 100.0, "close": 100.0}}}  # 익일 봉 없음
    result = run_part_a(disclosures_rows, bar_cache)
    assert result["skipped_no_outcome"] == 1
    assert result["type_stats"][BASE_RATE_KEY]["n"] == 0


def test_run_part_b_finds_precursor_and_respects_window():
    surge_day = D + timedelta(days=2)
    bar_cache = {
        "A": {
            D + timedelta(days=1): {"open": 100.0, "close": 100.0},
            surge_day: {"open": 100.0, "close": 120.0},  # +20% — 급등, D+1(=surge_day-1)에 공시 있음
        },
        "B": {
            D + timedelta(days=1): {"open": 100.0, "close": 100.0},
            surge_day: {"open": 100.0, "close": 130.0},  # +30% — 급등이지만 공시 없음
        },
    }
    disclosures_rows = [
        {"stock_code": "A", "report_nm": "수주 계약", "rcept_dt": (surge_day - timedelta(days=1)).strftime("%Y%m%d")},
    ]
    result = run_part_b(disclosures_rows, bar_cache, window_start=D, window_end=D + timedelta(days=5))
    assert result["n"] == 2
    assert result["n_with_precursor"] == 1
    assert result["precursor_rate"] == pytest.approx(0.5)
    assert result["type_distribution"] == {"수주": 1}


def test_run_part_b_excludes_surges_outside_window():
    surge_day = D + timedelta(days=10)
    bar_cache = {"A": {D + timedelta(days=9): {"open": 100.0, "close": 100.0}, surge_day: {"open": 100.0, "close": 120.0}}}
    result = run_part_b([], bar_cache, window_start=D, window_end=D + timedelta(days=5))
    assert result["n"] == 0


def test_run_part_c_same_day_open_close_by_keyword_class():
    mentions_rows = [
        {"symbol": "A", "date": D.isoformat(), "title": "A사, 대규모 공급계약 체결"},
        {"symbol": "B", "date": D.isoformat(), "title": "오늘의 시황 브리핑"},  # 매칭 없음 — base만
    ]
    bar_cache = {
        "A": {D: {"open": 100.0, "close": 108.0}},
        "B": {D: {"open": 100.0, "close": 99.0}},
    }
    result = run_part_c(mentions_rows, bar_cache)
    assert result["수주/공급계약"]["n"] == 1
    assert result["수주/공급계약"]["avg_pct"] == pytest.approx(8.0)
    assert result[BASE_RATE_KEY]["n"] == 2


def test_run_part_c_skips_rows_without_bar():
    mentions_rows = [{"symbol": "A", "date": D.isoformat(), "title": "실적 발표"}]
    result = run_part_c(mentions_rows, {})
    assert result[BASE_RATE_KEY]["n"] == 0


def test_run_part_c_carries_non_trading_crawl_date_to_next_trading_day():
    """실측(2026-08-17 실행): mentions.jsonl의 유일한 크롤일이 토요일이라, carryover
    없이는 Part C가 항상 n=0이었다 — report_follow.map_to_trading_day와 같은
    사고로 다음 거래일로 넘겨야 한다."""
    saturday = date(2026, 8, 15)
    monday = date(2026, 8, 17)
    mentions_rows = [{"symbol": "A", "date": saturday.isoformat(), "title": "A사 실적 발표"}]
    bar_cache = {"A": {monday: {"open": 100.0, "close": 105.0}}}
    trading_days = trading_days_from_bar_cache(bar_cache)  # [monday] — 토요일은 없다

    result = run_part_c(mentions_rows, bar_cache, trading_days)
    assert result[BASE_RATE_KEY]["n"] == 1
    assert result[BASE_RATE_KEY]["avg_pct"] == pytest.approx(5.0)


def test_run_part_c_without_trading_days_falls_back_to_raw_date():
    """`trading_days`를 안 넘기면(하위호환) crawl date를 그대로 쓴다 — 매핑 없음."""
    mentions_rows = [{"symbol": "A", "date": D.isoformat(), "title": "A사 실적"}]
    bar_cache = {"A": {D: {"open": 100.0, "close": 110.0}}}
    result = run_part_c(mentions_rows, bar_cache)
    assert result[BASE_RATE_KEY]["n"] == 1


# ------------------------------------------------------------------ DART 백필(fake getter, 네트워크 없음)

def test_backfill_disclosures_fetches_each_missing_day(tmp_path):
    calls: list[tuple[str, str]] = []

    def getter(params):
        calls.append((params["bgn_de"], params["end_de"]))
        return {"status": "013", "message": "no data"}

    today = date(2026, 8, 17)
    result = backfill_disclosures(tmp_path, days=3, today=today, api_key="dummy", getter=getter, sleep_seconds=0)
    # D-3, D-2, D-1 각각 corp_cls Y/K 두 번씩 조회
    assert len(calls) == 6
    assert result["fetched_days"] == ["20260814", "20260815", "20260816"]
    assert result["skipped_days"] == []
    assert result["errors"] == []


def test_backfill_disclosures_skips_dense_days(tmp_path):
    # 미리 밀집 원장을 만들어둔다(DENSE_DAY_THRESHOLD=20 이상).
    path = tmp_path / "data" / "ledger" / DISCLOSURES_LEDGER
    dense_rows = [
        {"rcept_no": f"N{i}", "stock_code": "005930", "corp_name": "x", "report_nm": "y", "rcept_dt": "20260816"}
        for i in range(25)
    ]
    _write_jsonl(path, dense_rows)

    calls: list[str] = []

    def getter(params):
        calls.append(params["bgn_de"])
        return {"status": "013", "message": "no data"}

    result = backfill_disclosures(tmp_path, days=1, today=date(2026, 8, 17), api_key="dummy", getter=getter, sleep_seconds=0)
    assert calls == []  # 8/16 하루뿐인데 이미 밀집 — 재조회 안 함
    assert result["skipped_days"] == ["20260816"]
    assert result["fetched_days"] == []


def test_backfill_disclosures_no_key_records_error_but_no_network(tmp_path):
    def getter(params):
        raise AssertionError("키 없으면 네트워크를 타면 안 된다")

    result = backfill_disclosures(tmp_path, days=1, today=date(2026, 8, 17), api_key="", getter=getter, sleep_seconds=0)
    assert result["added_total"] == 0
    assert result["errors"] == ["20260816: no key"]


# ------------------------------------------------------------------ 전체 오케스트레이션(fake candle source + fake DART getter)

def test_run_study_end_to_end_with_fakes(tmp_path):
    today = date(2026, 8, 17)

    def dart_getter(params):
        return {"status": "013", "message": "no data"}  # 새 공시 없음(백필은 빈 결과)

    # 기존 disclosures 원장 하나 심어둔다 — 백필과 무관하게 Part A/B 가 이걸 쓴다.
    d = date(2026, 8, 14)
    nd = date(2026, 8, 17)
    _write_jsonl(tmp_path / "data" / "ledger" / DISCLOSURES_LEDGER, [
        {"rcept_no": "R1", "stock_code": "005930", "corp_name": "삼성전자",
         "report_nm": "단일판매ㆍ공급계약체결", "rcept_dt": d.strftime("%Y%m%d")},
    ])
    _write_jsonl(tmp_path / "data" / "ledger" / "mentions.jsonl", [
        {"symbol": "005930", "date": d.isoformat(), "title": "삼성전자 공급계약 체결"},
    ])

    symbol_frame = _frame({
        d: _row(100.0, 100.0),
        nd: _row(100.0, 112.0),
    })
    source = FakeCandleSource({"005930": symbol_frame})

    result = run_study(tmp_path, days=5, max_symbols=10, candle_source=source, dart_api_key="dummy", dart_getter=dart_getter, today=today)

    assert result["n_symbols_fetched"] == 1
    assert result["part_a"]["type_stats"]["수주"]["n"] == 1
    assert result["part_a"]["type_stats"]["수주"]["avg_pct"] == pytest.approx(12.0)
    # format_report/write_ledger 가 예외 없이 도는지도 같이 확인
    text = format_report(result)
    assert "Part A" in text
    assert "Part B" in text
    assert "Part C" in text

    path = write_ledger(result, tmp_path, today=today)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["params"]["days"] == 5


def test_run_study_missing_ledgers_do_not_crash(tmp_path):
    def dart_getter(params):
        return {"status": "013", "message": "no data"}

    source = FakeCandleSource({})
    result = run_study(tmp_path, days=1, max_symbols=5, candle_source=source, dart_api_key="dummy", dart_getter=dart_getter, today=date(2026, 8, 17))
    assert result["n_symbols_fetched"] == 0
    assert result["part_a"]["type_stats"][BASE_RATE_KEY]["n"] == 0
    assert result["part_b"]["n"] == 0
    assert result["part_c"][BASE_RATE_KEY]["n"] == 0


# ------------------------------------------------------------------ 출력 포맷

def test_format_report_shows_flags_and_no_crash_on_empty():
    result = {
        "params": {"days": 1, "max_symbols": 5, "today": "2026-08-17"},
        "backfill": {"fetched_days": [], "skipped_days": [], "added_total": 0, "errors": []},
        "n_symbols_fetched": 0,
        "n_symbols_failed": 0,
        "part_a": {
            "rows": [], "n_disclosures": 0, "skipped_no_bars": 0, "skipped_no_outcome": 0,
            "type_stats": {BASE_RATE_KEY: aggregate_outcomes([])},
            "positive_flags": [], "negative_flags": [],
        },
        "part_b": {"records": [], "n": 0, "n_limit_like": 0, "n_with_precursor": 0, "precursor_rate": None,
                    "type_distribution": {}, "window": {"start": "2026-08-16", "end": "2026-08-17"}},
        "part_c": {BASE_RATE_KEY: {"n": 0, "avg_pct": None, "hit_rate": None}},
    }
    text = format_report(result)
    assert "없음" in text
    assert "n/a" in text
