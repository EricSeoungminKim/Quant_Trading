"""`report_cli._load_disclosures` 의 (stock_code, report_nm) dedup(서브프로젝트 I
Part B).

EC2 실측(2026-08-17, disclosures.jsonl 2,000건)에서 같은 종목·같은 공시제목이
`rcept_no` 만 다르게 27그룹·53건 중복 적재돼 있었다 — 전부 같은 `rcept_dt`
안이라 리포트의 2일 창에 그대로 노출되면 똑같은 줄이 반복된다. 유형 태그
판단(`classify_report`)은 별도 유닛 테스트(`tests/test_dart.py`)에서 검증하므로
여기선 report_cli 의 dedup 배선만 본다.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from quant.apps import report_cli


def _disclosure_ledger(root: Path, rows: list[dict]) -> None:
    path = root / "data" / "ledger" / "disclosures.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_load_disclosures_dedups_same_code_and_report_nm(tmp_path):
    """같은 (stock_code, report_nm) 이 rcept_no 만 다르게 같은 날 중복 적재된
    사례 — 렌더에서 "외 N건"으로 합쳐 똑같은 줄이 반복되지 않게 한다."""
    _disclosure_ledger(tmp_path, [
        {"rcept_no": "R1", "stock_code": "005930", "corp_name": "삼성전자",
         "report_nm": "임원ㆍ주요주주특정증권등소유상황보고서", "rcept_dt": "20260817"},
        {"rcept_no": "R2", "stock_code": "005930", "corp_name": "삼성전자",
         "report_nm": "임원ㆍ주요주주특정증권등소유상황보고서", "rcept_dt": "20260817"},
    ])

    out = report_cli._load_disclosures(tmp_path, date(2026, 8, 17), {"005930"})

    assert out["005930"] == ["임원ㆍ주요주주특정증권등소유상황보고서 외 1건"]


def test_load_disclosures_distinct_report_nm_not_merged(tmp_path):
    _disclosure_ledger(tmp_path, [
        {"rcept_no": "R1", "stock_code": "005930", "corp_name": "삼성전자",
         "report_nm": "공시A", "rcept_dt": "20260817"},
        {"rcept_no": "R2", "stock_code": "005930", "corp_name": "삼성전자",
         "report_nm": "공시B", "rcept_dt": "20260817"},
    ])

    out = report_cli._load_disclosures(tmp_path, date(2026, 8, 17), {"005930"})

    assert out["005930"] == ["공시A", "공시B"]


def test_load_disclosures_no_ledger_returns_empty(tmp_path):
    out = report_cli._load_disclosures(tmp_path, date(2026, 8, 17), {"005930"})
    assert out == {}


def test_load_disclosures_window_starts_at_last_open_day(tmp_path):
    """연휴 케이스(2026-08-17 실측): 고정 2일 창(일·월)은 금요일(08-14) 공시를
    못 덮어 공시 칩이 0 이었다 — 창의 시작을 마지막 개장일로(G 집계 창 원칙)."""
    import pandas as pd

    anchor = tmp_path / "data" / "history" / "069500" / "1d" / "2026"
    anchor.mkdir(parents=True)
    idx = pd.DatetimeIndex([pd.Timestamp("2026-08-14", tz="UTC")])
    pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
        index=idx,
    ).to_parquet(anchor / "08.parquet")

    _disclosure_ledger(tmp_path, [
        {"rcept_no": "1", "stock_code": "005930", "report_nm": "단일판매ㆍ공급계약체결",
         "rcept_dt": "20260814"},
    ])
    got = report_cli._load_disclosures(tmp_path, date(2026, 8, 17), {"005930"})
    assert got == {"005930": ["단일판매ㆍ공급계약체결"]}


def test_load_disclosures_without_anchor_keeps_two_day_window(tmp_path):
    """앵커 부재 → 기존 2일 창 유지(하위호환) — 3일 전 공시는 안 걸린다."""
    _disclosure_ledger(tmp_path, [
        {"rcept_no": "1", "stock_code": "005930", "report_nm": "옛 공시",
         "rcept_dt": "20260814"},
        {"rcept_no": "2", "stock_code": "005930", "report_nm": "어제 공시",
         "rcept_dt": "20260816"},
    ])
    got = report_cli._load_disclosures(tmp_path, date(2026, 8, 17), {"005930"})
    assert got == {"005930": ["어제 공시"]}
