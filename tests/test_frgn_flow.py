"""`quant.control.frgn_flow` — 외국인 수급 원장(서브프로젝트 I).

`quant.control.flows`(기존 5일 합계 뷰용 원장)와 같은 멱등 upsert + tmp/replace
원자적 쓰기 관례를 따른다(`tests/test_flows.py` 와 대응). 이 원장은
`quant.analyze.foreign_trend.classify()` 가 소비할 날짜 오름차순 시계열
(`load_series`)을 낸다는 점만 다르다.
"""
from __future__ import annotations

from quant.control.frgn_flow import append_daily, load_series


def test_append_daily_same_day_twice_stays_one_row_with_latest_value(tmp_path):
    """멱등 — 하루 두 번 리포트를 빌드해도 (date, symbol) 은 한 행. 값은 최신으로 갱신."""
    path = tmp_path / "frgn_flow.jsonl"
    append_daily(
        [{"date": "2026-08-15", "symbol": "005930", "foreign_net": 100, "inst_net": -50}],
        path,
    )
    append_daily(
        [{"date": "2026-08-15", "symbol": "005930", "foreign_net": 999, "inst_net": -50}],
        path,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    series = load_series(path, "005930")
    assert series == [
        {"date": "2026-08-15", "symbol": "005930", "foreign_net": 999, "inst_net": -50}
    ]


def test_append_daily_writes_via_tmp_and_replace(tmp_path):
    """원장 관례(flows.py/selections.py) — tmp 파일에 쓰고 os.replace 로 원자적
    치환. 쓰다 죽어도 원본이 남아야 하므로, `.tmp` 형제 파일이 남지 않는 것으로
    확인한다."""
    path = tmp_path / "frgn_flow.jsonl"
    append_daily(
        [{"date": "2026-08-15", "symbol": "005930", "foreign_net": 1, "inst_net": 1}],
        path,
    )

    assert path.exists()
    assert not path.with_suffix(".tmp").exists()


def test_append_daily_skips_rows_missing_date_or_symbol(tmp_path):
    path = tmp_path / "frgn_flow.jsonl"
    added = append_daily(
        [
            {"symbol": "005930", "foreign_net": 1, "inst_net": 1},  # date 없음
            {"date": "2026-08-15", "foreign_net": 1, "inst_net": 1},  # symbol 없음
            {"date": "2026-08-15", "symbol": "005930", "foreign_net": 1, "inst_net": 1},
        ],
        path,
    )

    assert added == 1
    assert len(load_series(path, "005930")) == 1


def test_load_series_returns_date_ascending(tmp_path):
    path = tmp_path / "frgn_flow.jsonl"
    append_daily(
        [
            {"date": "2026-08-15", "symbol": "005930", "foreign_net": 30, "inst_net": 3},
            {"date": "2026-08-13", "symbol": "005930", "foreign_net": 10, "inst_net": 1},
            {"date": "2026-08-14", "symbol": "005930", "foreign_net": 20, "inst_net": 2},
        ],
        path,
    )

    series = load_series(path, "005930")

    assert [r["date"] for r in series] == ["2026-08-13", "2026-08-14", "2026-08-15"]


def test_load_series_ignores_other_symbols(tmp_path):
    path = tmp_path / "frgn_flow.jsonl"
    append_daily(
        [{"date": "2026-08-15", "symbol": "000660", "foreign_net": 999, "inst_net": 999}],
        path,
    )

    assert load_series(path, "005930") == []


def test_load_series_caps_at_days_keeping_most_recent(tmp_path):
    path = tmp_path / "frgn_flow.jsonl"
    rows = [
        {"date": f"2026-08-{i:02d}", "symbol": "005930", "foreign_net": i, "inst_net": i}
        for i in range(1, 6)
    ]
    append_daily(rows, path)

    series = load_series(path, "005930", days=3)

    assert [r["date"] for r in series] == ["2026-08-03", "2026-08-04", "2026-08-05"]


def test_load_series_shorter_than_requested_returns_what_exists(tmp_path):
    path = tmp_path / "frgn_flow.jsonl"
    append_daily(
        [{"date": "2026-08-15", "symbol": "005930", "foreign_net": 10, "inst_net": 1}],
        path,
    )

    series = load_series(path, "005930", days=20)

    assert len(series) == 1


def test_load_series_missing_ledger_returns_empty_list(tmp_path):
    path = tmp_path / "frgn_flow.jsonl"

    assert load_series(path, "005930") == []
