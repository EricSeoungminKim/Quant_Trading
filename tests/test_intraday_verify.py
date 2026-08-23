"""`quant.backtest.intraday_verify` — 당일 단타 스코어러 재현 검증 하네스.

네트워크는 절대 타지 않는다 — yfinance 호출은 전부 fake candle source 로
대체한다(모듈 지침: "verify-harness pure parts... do NOT hit network in tests").
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from quant.backtest.intraday_verify import (
    MIN_SAMPLE_FOR_JUDGEMENT,
    _dart_types_for,
    _foreign_v2_score_for,
    _max_dup_count,
    aggregate_metrics,
    candidate_symbols,
    format_report,
    index_disclosures,
    index_frgn_flow,
    index_mentions,
    reconstruct_inputs,
    run_verify,
    symbol_outcome,
    write_ledger,
)

D = date(2026, 8, 15)


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


def _row(d: date, o: float, c: float, v: float) -> dict:
    return {"open": o, "high": max(o, c), "low": min(o, c), "close": c, "volume": v}


def _frame(rows: dict[date, dict]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in rows], name="date")
    return pd.DataFrame(list(rows.values()), index=idx)


# ------------------------------------------------------------------ 원장 인덱싱

def test_index_mentions_groups_by_symbol_and_date():
    rows = [
        {"symbol": "005930", "date": "2026-08-15", "title": "a"},
        {"symbol": "005930", "date": "2026-08-14", "title": "b"},
        {"symbol": "000660", "date": "2026-08-15", "title": "c"},
        {"symbol": None, "date": "2026-08-15", "title": "skip-no-symbol"},
        {"symbol": "005930", "date": "bad-date", "title": "skip-bad-date"},
    ]
    idx = index_mentions(rows)
    assert set(idx["005930"][date(2026, 8, 15)][0].keys()) == {"symbol", "date", "title"}
    assert len(idx["005930"][date(2026, 8, 15)]) == 1
    assert len(idx["005930"][date(2026, 8, 14)]) == 1
    assert len(idx["000660"][date(2026, 8, 15)]) == 1


def test_candidate_symbols_sorted_by_count_and_capped():
    idx = index_mentions([
        {"symbol": "AAA", "date": "2026-08-15", "title": "1"},
        {"symbol": "BBB", "date": "2026-08-15", "title": "1"},
        {"symbol": "BBB", "date": "2026-08-15", "title": "2"},
        {"symbol": "CCC", "date": "2026-08-14", "title": "1"},  # 다른 날 — 후보 아님
    ])
    result = candidate_symbols(idx, date(2026, 8, 15), cap=1)
    assert result == ["BBB"]  # 기사 2건인 BBB 가 1건인 AAA 보다 우선, cap=1


def test_max_dup_count_uses_clustering():
    idx = index_mentions([
        {"symbol": "005930", "date": D.isoformat(), "title": "삼성전자 실적 발표", "link": "a"},
        {"symbol": "005930", "date": D.isoformat(), "title": "삼성전자 실적 발표", "link": "b"},
        {"symbol": "005930", "date": D.isoformat(), "title": "전혀 다른 제목입니다", "link": "c"},
    ])
    assert _max_dup_count(idx["005930"], D) == 2


def test_max_dup_count_no_rows_is_zero():
    assert _max_dup_count({}, D) == 0


# ------------------------------------------------------------------ look-ahead 방지

def test_foreign_v2_score_excludes_same_day_row():
    """D 당일 수급 행이 있어도(마감 후 확정) 채점에 절대 쓰지 않는다."""
    frgn_idx = index_frgn_flow([
        {"symbol": "005930", "date": (D - timedelta(days=2)).isoformat(), "foreign_net": -100, "inst_net": 0},
        {"symbol": "005930", "date": (D - timedelta(days=1)).isoformat(), "foreign_net": -20, "inst_net": 0},
        # D 당일 — 있어도 배제돼야 한다. 포함되면 마지막 관찰일 부호가 양수로
        # 뒤집혀 이탈 패널티 대신 쌍끌이/정합 가점이 트리거된다.
        {"symbol": "005930", "date": D.isoformat(), "foreign_net": 500, "inst_net": 0},
    ])
    # D 미만만 보면 이틀 연속 순매도(길이 2) → 이탈 패널티(-10)만 트리거,
    # floor 0 — 다른 가점(쌍끌이·정합)은 전부 부호상 불가능하다.
    score = _foreign_v2_score_for(frgn_idx, "005930", D)
    assert score == 0


def test_foreign_v2_score_none_when_no_prior_rows():
    frgn_idx = index_frgn_flow([
        {"symbol": "005930", "date": D.isoformat(), "foreign_net": 100, "inst_net": 0},
    ])
    assert _foreign_v2_score_for(frgn_idx, "005930", D) is None


def test_dart_types_cutoff_excludes_future_disclosures():
    disclosures_idx = index_disclosures([
        {"stock_code": "005930", "report_nm": "분기보고서 실적 발표", "rcept_dt": "20260814"},
        {"stock_code": "005930", "report_nm": "수주 계약 체결", "rcept_dt": "20260815"},  # == D, 포함
        {"stock_code": "005930", "report_nm": "유상증자 결정", "rcept_dt": "20260816"},  # D 이후, 제외
    ])
    types = _dart_types_for(disclosures_idx, "005930", D)
    assert "실적" in types
    assert "수주" in types
    assert "유상증자" not in types


def test_reconstruct_inputs_always_none_for_unreconstructable_fields():
    """trending_score100/theme_change_pct 는 과거 재구성 불가 — 구조적으로 항상 None."""
    inputs = reconstruct_inputs("005930", D, index_mentions([]), index_frgn_flow([]), index_disclosures([]))
    assert inputs["trending_score100"] is None
    assert inputs["theme_change_pct"] is None
    assert inputs["dart_types"] == []
    assert inputs["today_articles"] == 0
    assert inputs["foreign_v2_score"] is None  # 수급 시계열 없음


def test_reconstruct_inputs_titles_from_same_day_mentions():
    """서브프로젝트 P — D 당일 mentions 행의 원문 제목이 `titles`로 복원돼야
    호재 마커/악재 거부권 판정이 재구성에서도 된다."""
    mentions_idx = index_mentions([
        {"symbol": "005930", "date": D.isoformat(), "title": "삼성전자 수주 공시"},
        {"symbol": "005930", "date": (D - timedelta(days=1)).isoformat(), "title": "어제 기사"},
    ])
    inputs = reconstruct_inputs("005930", D, mentions_idx, index_frgn_flow([]), index_disclosures([]))
    assert inputs["titles"] == ["삼성전자 수주 공시"]


def test_reconstruct_inputs_titles_empty_when_no_mentions():
    inputs = reconstruct_inputs("005930", D, index_mentions([]), index_frgn_flow([]), index_disclosures([]))
    assert inputs["titles"] == []


def test_reconstruct_inputs_foreign_v2_score_is_int_when_flow_exists():
    frgn_idx = index_frgn_flow([
        {"symbol": "005930", "date": (D - timedelta(days=2)).isoformat(), "foreign_net": -100, "inst_net": 0},
        {"symbol": "005930", "date": (D - timedelta(days=1)).isoformat(), "foreign_net": 150, "inst_net": 80},
    ])
    inputs = reconstruct_inputs("005930", D, index_mentions([]), frgn_idx, index_disclosures([]))
    assert isinstance(inputs["foreign_v2_score"], int)
    assert inputs["foreign_v2_score"] > 0  # 쌍끌이(최근일 외국인+기관 동반 순매수) 트리거


# ------------------------------------------------------------------ 결과(fake candle source)

def test_symbol_outcome_computes_bp_and_volume_multiple():
    frame = _frame({
        D - timedelta(days=2): _row(D - timedelta(days=2), 100, 100, 80),
        D - timedelta(days=1): _row(D - timedelta(days=1), 100, 100, 120),
        D: _row(D, 100, 103, 500),
    })
    # MIN_VOL_BASELINE_DAYS=5 이므로 2일치 baseline 은 부족 — 배율은 None 이어야 한다.
    source = FakeCandleSource({"TEST": frame})
    outcome = symbol_outcome(source, "TEST", D, sleep_seconds=0)
    assert outcome is not None
    assert outcome["open_close_bp"] == pytest.approx(300.0)
    assert outcome["volume_multiple"] is None


def test_symbol_outcome_volume_multiple_with_sufficient_baseline():
    rows = {D - timedelta(days=i): _row(D - timedelta(days=i), 100, 100, 100) for i in range(1, 6)}
    rows[D] = _row(D, 100, 110, 500)
    frame = _frame(rows)
    source = FakeCandleSource({"TEST": frame})
    outcome = symbol_outcome(source, "TEST", D, sleep_seconds=0)
    assert outcome["volume_multiple"] == pytest.approx(5.0)


def test_symbol_outcome_missing_data_returns_none():
    source = FakeCandleSource({})
    assert symbol_outcome(source, "UNKNOWN", D, sleep_seconds=0) is None


# ------------------------------------------------------------------ 지표 산수

def test_aggregate_metrics_math():
    records = [
        {"open_close_bp": 100.0, "volume_multiple": 2.0, "excess_bp": 50.0},
        {"open_close_bp": -50.0, "volume_multiple": None, "excess_bp": -80.0},
        {"open_close_bp": 30.0, "volume_multiple": 4.0, "excess_bp": None},
    ]
    m = aggregate_metrics(records)
    assert m["n_symbol_days"] == 3
    assert m["hit_rate"] == pytest.approx(2 / 3)
    assert m["avg_open_close_bp"] == pytest.approx(80.0 / 3)
    assert m["vol_multiple_median"] == pytest.approx(3.0)
    assert m["excess_vs_anchor_bp"] == pytest.approx(-15.0)


def test_aggregate_metrics_empty():
    m = aggregate_metrics([])
    assert m["n_symbol_days"] == 0
    assert m["hit_rate"] is None
    assert m["avg_open_close_bp"] is None
    assert m["vol_multiple_median"] is None
    assert m["excess_vs_anchor_bp"] is None


# ------------------------------------------------------------------ 오케스트레이션(파일 I/O + fake candle source, 네트워크 없음)

def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_run_verify_end_to_end_with_fake_source(tmp_path):
    # mentions: 20일 창(D 포함)에 변동 있는 기사 건수를 채워 news_z 가 None 이 아니게 한다.
    mentions_rows = []
    for i in range(20):
        d_i = D - timedelta(days=19 - i)
        count = i % 3  # 0,1,2 반복 — stdev != 0
        for j in range(count):
            mentions_rows.append({
                "symbol": "005930", "date": d_i.isoformat(),
                "title": f"기사 {d_i} #{j}", "link": f"https://example.com/{d_i}-{j}",
            })
    _write_jsonl(tmp_path / "data" / "ledger" / "mentions.jsonl", mentions_rows)

    # frgn_flow: D 이전 이틀로 재유입(INFLOW) 라벨을 만든다.
    frgn_rows = [
        {"symbol": "005930", "date": (D - timedelta(days=2)).isoformat(), "foreign_net": -100, "inst_net": 0},
        {"symbol": "005930", "date": (D - timedelta(days=1)).isoformat(), "foreign_net": 150, "inst_net": 0},
    ]
    _write_jsonl(tmp_path / "data" / "ledger" / "frgn_flow.jsonl", frgn_rows)

    # 후보 종목(005930)과 앵커(069500) 둘 다 D 시점 봉만 있으면 충분(윈도우가 넓어서 포함됨).
    symbol_frame = _frame({D: _row(D, 100, 105, 1000)})
    anchor_frame = _frame({D: _row(D, 1000, 990, 5000)})
    source = FakeCandleSource({"005930": symbol_frame, "069500": anchor_frame})

    result = run_verify(tmp_path, days=1, top=5, candle_source=source, today=D + timedelta(days=1))

    assert result["trading_days"] == [D]
    assert len(result["records"]) == 1
    rec = result["records"][0]
    assert rec["symbol"] == "005930"
    assert rec["open_close_bp"] == pytest.approx(500.0)
    assert rec["excess_bp"] == pytest.approx(600.0)  # 500 - (-100)
    assert result["aggregate"]["n_symbol_days"] == 1
    assert result["aggregate"]["hit_rate"] == pytest.approx(1.0)


def test_run_verify_no_trading_days_when_anchor_missing(tmp_path):
    _write_jsonl(tmp_path / "data" / "ledger" / "mentions.jsonl", [])
    source = FakeCandleSource({})
    result = run_verify(tmp_path, days=5, top=5, candle_source=source, today=D)
    assert result["trading_days"] == []
    assert result["records"] == []
    assert result["aggregate"]["n_symbol_days"] == 0


def test_run_verify_missing_ledgers_do_not_crash(tmp_path):
    """mentions/frgn_flow/disclosures 원장이 아예 없어도(로컬 검증 초기 상태) 예외 없이 돈다."""
    anchor_frame = _frame({D: _row(D, 1000, 990, 5000)})
    source = FakeCandleSource({"069500": anchor_frame})
    result = run_verify(tmp_path, days=1, top=5, candle_source=source, today=D + timedelta(days=1))
    assert result["trading_days"] == [D]
    assert result["records"] == []  # 뉴스 후보가 없으니 스코어링 대상도 없다


# ------------------------------------------------------------------ 출력 + 원장 기록

def test_format_report_flags_insufficient_sample():
    result = {
        "trading_days": [D],
        "day_rows": [{"date": D.isoformat(), **aggregate_metrics([])}],
        "records": [],
        "aggregate": aggregate_metrics([]),
    }
    text = format_report(result, days=1, top=5)
    assert "판단 불가" in text
    assert str(MIN_SAMPLE_FOR_JUDGEMENT) in text


def test_write_ledger_appends(tmp_path):
    result = {
        "trading_days": [D],
        "day_rows": [],
        "records": [],
        "aggregate": aggregate_metrics([]),
    }
    path1 = write_ledger(result, tmp_path, days=1, top=5, today=D)
    path2 = write_ledger(result, tmp_path, days=1, top=5, today=D)
    assert path1 == path2
    lines = path1.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["params"] == {"days": 1, "top": 5}
    assert row["sufficient"] is False
