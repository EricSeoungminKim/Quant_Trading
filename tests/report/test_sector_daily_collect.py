"""quant.report.collect.sector 의 주도 섹터 I/O(§B, 2026-09-03 소유자 철학
지시) — fundamentals_naver.jsonl/frgn_flow.jsonl/sector_members.json을 읽어
sector_daily.jsonl에 적재하고 5일 추이를 얹는 부분. tmp_path로 실제 파일을
쓰고 읽는다(순수 계산은 tests/test_sector_daily.py가 이미 커버)."""
import json

from quant.report.collect.sector import (
    _append_sector_daily, _build_sector_daily_view, _load_foreign_net_for_date,
    _load_sector_daily_history, _load_turnover_today,
)


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------- _load_turnover_today

def test_load_turnover_today_picks_latest_date_and_converts_to_krw(tmp_path):
    path = tmp_path / "data" / "ledger" / "fundamentals_naver.jsonl"
    _write_jsonl(path, [
        {"date": "2026-09-01", "code": "005930", "value_traded": 100},
        {"date": "2026-09-02", "code": "005930", "value_traded": 300},
        {"date": "2026-09-02", "code": "000660", "value_traded": 200},
    ])
    date_str, turnover = _load_turnover_today(tmp_path)
    assert date_str == "2026-09-02"
    assert turnover == {"005930": 300_000_000, "000660": 200_000_000}, "백만원 → 원 환산"


def test_load_turnover_today_missing_file_returns_none_and_empty(tmp_path):
    assert _load_turnover_today(tmp_path) == (None, {})


# --------------------------------------------------------------- _load_foreign_net_for_date

def test_load_foreign_net_for_date_filters_by_date(tmp_path):
    path = tmp_path / "data" / "ledger" / "frgn_flow.jsonl"
    _write_jsonl(path, [
        {"date": "2026-09-01", "symbol": "005930", "foreign_net": 1000},
        {"date": "2026-09-02", "symbol": "005930", "foreign_net": -500},
        {"date": "2026-09-02", "symbol": "000660", "foreign_net": 200},
    ])
    out = _load_foreign_net_for_date(tmp_path, "2026-09-02")
    assert out == {"005930": -500, "000660": 200}


def test_load_foreign_net_for_date_missing_file_returns_empty(tmp_path):
    assert _load_foreign_net_for_date(tmp_path, "2026-09-02") == {}


# --------------------------------------------------------------- _append_sector_daily

def test_append_sector_daily_upserts_by_date_market_sector(tmp_path):
    path = tmp_path / "sector_daily.jsonl"
    row_v1 = {"date": "2026-09-02", "market": "KR", "sector": "반도체와반도체장비",
              "turnover_krw": 100, "foreign_net": None, "n_members": 2, "top_members": []}
    _append_sector_daily(path, [row_v1])
    row_v2 = {**row_v1, "turnover_krw": 999}
    _append_sector_daily(path, [row_v2])

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "같은 (date, market, sector) 재적재는 덮어쓴다 — 행이 늘지 않는다"
    assert json.loads(lines[0])["turnover_krw"] == 999


def test_append_sector_daily_keeps_rows_for_other_dates(tmp_path):
    path = tmp_path / "sector_daily.jsonl"
    _append_sector_daily(path, [
        {"date": "2026-09-01", "market": "KR", "sector": "자동차",
         "turnover_krw": 1, "foreign_net": None, "n_members": 1, "top_members": []},
    ])
    _append_sector_daily(path, [
        {"date": "2026-09-02", "market": "KR", "sector": "자동차",
         "turnover_krw": 2, "foreign_net": None, "n_members": 1, "top_members": []},
    ])
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {r["date"] for r in rows} == {"2026-09-01", "2026-09-02"}


# --------------------------------------------------------------- _load_sector_daily_history

def test_load_sector_daily_history_excludes_today_and_other_markets(tmp_path):
    path = tmp_path / "sector_daily.jsonl"
    _write_jsonl(path, [
        {"date": "2026-09-01", "market": "KR", "sector": "자동차",
         "turnover_krw": 1, "foreign_net": None, "n_members": 1, "top_members": []},
        {"date": "2026-09-02", "market": "KR", "sector": "자동차",
         "turnover_krw": 2, "foreign_net": None, "n_members": 1, "top_members": []},  # 오늘 — 제외
        {"date": "2026-09-01", "market": "US", "sector": "Tech",
         "turnover_krw": 1, "foreign_net": None, "n_members": 1, "top_members": []},  # 다른 시장 — 제외
    ])
    history = _load_sector_daily_history(path, "KR", before_date="2026-09-02", days=5)
    assert len(history) == 1
    assert history[0]["date"] == "2026-09-01"


def test_load_sector_daily_history_caps_to_days(tmp_path):
    path = tmp_path / "sector_daily.jsonl"
    rows = [
        {"date": f"2026-08-{d:02d}", "market": "KR", "sector": "자동차",
         "turnover_krw": d, "foreign_net": None, "n_members": 1, "top_members": []}
        for d in range(20, 30)
    ]
    _write_jsonl(path, rows)
    history = _load_sector_daily_history(path, "KR", before_date="2026-09-01", days=5)
    dates = sorted({r["date"] for r in history})
    assert dates == ["2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-29"]


def test_load_sector_daily_history_missing_file_returns_empty(tmp_path):
    assert _load_sector_daily_history(tmp_path / "missing.jsonl", "KR", "2026-09-02") == []


# --------------------------------------------------------------- _build_sector_daily_view

def _seed_membership(root):
    path = root / "data" / "ledger" / "sector_members.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "반도체와반도체장비": [
            {"code": "005930", "name": "삼성전자", "change_pct": 1.0},
        ],
    }, ensure_ascii=False), encoding="utf-8")


def test_build_sector_daily_view_returns_none_when_membership_missing(tmp_path):
    assert _build_sector_daily_view(tmp_path, "KR") is None


def test_build_sector_daily_view_returns_none_when_turnover_missing(tmp_path):
    _seed_membership(tmp_path)
    assert _build_sector_daily_view(tmp_path, "KR") is None


def test_build_sector_daily_view_builds_and_persists_ledger(tmp_path):
    _seed_membership(tmp_path)
    _write_jsonl(tmp_path / "data" / "ledger" / "fundamentals_naver.jsonl", [
        {"date": "2026-09-02", "code": "005930", "value_traded": 500},
    ])
    _write_jsonl(tmp_path / "data" / "ledger" / "frgn_flow.jsonl", [
        {"date": "2026-09-02", "symbol": "005930", "foreign_net": 1000},
    ])

    view = _build_sector_daily_view(tmp_path, "KR")
    assert view is not None
    assert view["date"] == "2026-09-02"
    assert view["sectors"][0]["sector"] == "반도체와반도체장비"
    assert view["sectors"][0]["turnover_krw"] == 500_000_000
    assert view["sectors"][0]["foreign_net"] == 1000
    assert view["sectors"][0]["rank"] == 1

    # 원장에 실제로 적재됐는지(재실행해도 행이 늘지 않는지) 확인.
    ledger_path = tmp_path / "data" / "ledger" / "sector_daily.jsonl"
    assert ledger_path.exists()
    lines_before = ledger_path.read_text(encoding="utf-8").splitlines()
    _build_sector_daily_view(tmp_path, "KR")
    lines_after = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines_before) == len(lines_after) == 1
