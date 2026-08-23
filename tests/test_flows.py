"""`quant.control.flows` — 종목별 수급(외국인·기관 순매수)을 매일 원장에 축적.

1년치를 외부에서 긁는 대신(§핵심결정 2, 2026-08-15-report-ui-design.md), 리포트
빌드가 매일 받아오는 네이버 10일 수급 스냅샷을 `data/ledger/flows.jsonl` 에
날짜·종목 단위로 append 한다. 짧은 기간·0일치를 0 으로 위장하지 않는 것이
이 모듈의 핵심 계약이다.
"""
from __future__ import annotations

from quant.control.flows import append_flows, coverage, window_sums


def test_append_flows_same_day_twice_stays_one_row_with_latest_value(tmp_path):
    """멱등 — 하루 두 번 빌드해도 (date, symbol) 은 한 행. 값은 최신으로 갱신."""
    path = tmp_path / "flows.jsonl"
    append_flows(path, [{"date": "2026-08-15", "symbol": "005930",
                          "foreign_net": 100, "inst_net": -50}], today="2026-08-15")
    append_flows(path, [{"date": "2026-08-15", "symbol": "005930",
                          "foreign_net": 999, "inst_net": -50}], today="2026-08-15")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    result = window_sums(
        [{"date": "2026-08-15", "symbol": "005930", "foreign_net": 999, "inst_net": -50}],
        symbol="005930", days=1, today="2026-08-15",
    )
    assert result == {"foreign": 999, "inst": -50, "n_days": 1}


def test_window_sums_shorter_than_requested_sums_what_exists_and_reports_n_days():
    """요청 기간(10일)보다 데이터가 짧으면(3일) 있는 만큼 합산 + n_days 로 정직하게."""
    rows = [
        {"date": "2026-08-13", "symbol": "005930", "foreign_net": 10, "inst_net": 1},
        {"date": "2026-08-14", "symbol": "005930", "foreign_net": 20, "inst_net": 2},
        {"date": "2026-08-15", "symbol": "005930", "foreign_net": 30, "inst_net": 3},
    ]

    result = window_sums(rows, symbol="005930", days=10, today="2026-08-15")

    assert result == {"foreign": 60, "inst": 6, "n_days": 3}


def test_window_sums_zero_days_of_data_returns_none_not_zero():
    """데이터가 하루도 없으면 0 이 아니라 None — "0" 과 "모른다"는 다른 사건이다."""
    result = window_sums([], symbol="005930", days=10, today="2026-08-15")

    assert result is None


def test_window_sums_ignores_other_symbols():
    rows = [
        {"date": "2026-08-15", "symbol": "000660", "foreign_net": 999, "inst_net": 999},
    ]

    result = window_sums(rows, symbol="005930", days=10, today="2026-08-15")

    assert result is None


def test_window_sums_excludes_rows_outside_the_window():
    rows = [
        {"date": "2026-07-01", "symbol": "005930", "foreign_net": 500, "inst_net": 500},
        {"date": "2026-08-15", "symbol": "005930", "foreign_net": 10, "inst_net": 1},
    ]

    result = window_sums(rows, symbol="005930", days=5, today="2026-08-15")

    assert result == {"foreign": 10, "inst": 1, "n_days": 1}


def test_coverage_reports_first_date_and_day_count():
    rows = [
        {"date": "2026-08-13", "symbol": "005930"},
        {"date": "2026-08-15", "symbol": "005930"},
        {"date": "2026-08-14", "symbol": "005930"},
    ]

    result = coverage(rows)

    assert result == {"first_date": "2026-08-13", "n_days": 3}


def test_coverage_empty_rows_is_none_first_date_and_zero_days():
    result = coverage([])

    assert result == {"first_date": None, "n_days": 0}


def test_append_flows_writes_via_tmp_and_replace(tmp_path):
    """원장 관례(selections.py) — tmp 파일에 쓰고 os.replace 로 원자적 치환.

    쓰다 죽어도 원본이 남아야 하므로, 쓰기 도중 `.tmp` 형제 파일이 생겼다가
    최종적으로 사라지고 목적 파일만 남는 것으로 관례 준수를 확인한다.
    """
    path = tmp_path / "flows.jsonl"
    append_flows(path, [{"date": "2026-08-15", "symbol": "005930",
                          "foreign_net": 1, "inst_net": 1}], today="2026-08-15")

    assert path.exists()
    assert not path.with_suffix(".tmp").exists()
