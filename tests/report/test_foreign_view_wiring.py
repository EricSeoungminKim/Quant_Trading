"""`report_cli._build_foreign_view` — 외국인 수급 추종 뷰 조립(서브프로젝트 I).

**사용자 원칙(2026-08-17)**: "한국주식은 기관 매수세는 노이즈 — 메인 리서치와
비리 없는 매수는 오로지 외국인 매수 추세." 라벨 판단 자체는
`quant.analyze.foreign_trend.classify()` 가 하므로, 여기선 배선만 검증한다:
`frgn_flow.jsonl` 원장 → `load_series`/`classify` → 섹터(`sector_map`) 로 묶기
→ 코스피 종합 외국인(features) 붙이기. US 시장은 이 데이터가 없으므로 항상
`None`(섹션 생략) 이어야 한다.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from quant.apps import report_cli
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult

KST = ZoneInfo("Asia/Seoul")
_AT = datetime(2026, 8, 17, 8, 0, tzinfo=KST)


def _ledger(root: Path, rows: list[dict]) -> None:
    path = root / "data" / "ledger" / "frgn_flow.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _kospi_flow_result() -> SourceResult:
    return SourceResult(
        key="kospi_flow", ok=True, error=None, url="https://x",
        fetched_at=_AT, latency_ms=1,
        data={"market": "KOSPI", "unit": "억원", "rows": [
            {"date": "2026-08-17", "외국인": 1234, "기관계": -400, "개인": -800},
        ]},
    )


def _snap(market: str = "KR", results: dict | None = None) -> Snapshot:
    return Snapshot(SCHEMA_VERSION, market, date(2026, 8, 17), _AT, results or {})


def _cont() -> dict:
    return {
        "005930": {"name": "삼성전자"},
        "000660": {"name": "SK하이닉스"},
        "035420": {"name": "네이버"},  # 원장에 시계열 없음 — 빠져야 한다
    }


def test_us_market_returns_none_without_reading_ledger(tmp_path):
    """US 는 frgn_flow 원장 자체가 없다 — 시장 가드가 먼저 걸린다."""
    _ledger(tmp_path, [
        {"date": "2026-08-17", "symbol": "005930", "foreign_net": 100, "inst_net": -50},
        {"date": "2026-08-16", "symbol": "005930", "foreign_net": -30, "inst_net": 10},
    ])
    snap = _snap(market="US")

    view = report_cli._build_foreign_view(tmp_path, snap, _cont(), {}, {"features": {}})

    assert view is None


def test_no_ledger_data_returns_none(tmp_path):
    """후보 종목 전부 원장에 시계열이 없으면(원장 미존재 포함) None — 0/빈 리스트로 위장하지 않는다."""
    snap = _snap(results={"kospi_flow": _kospi_flow_result()})

    view = report_cli._build_foreign_view(tmp_path, snap, _cont(), {}, {"features": {}})

    assert view is None


def test_builds_rows_grouped_by_sector_ordered_by_net_sum_desc(tmp_path):
    _ledger(tmp_path, [
        # 005930: 유입(+100) → 단발 이탈(-50) = 관망(잔여 있음) — 라벨 자체는
        # 여기서 검증하지 않는다(그건 test_foreign_trend.py 의 몫), 그룹핑만 확인.
        {"date": "2026-08-14", "symbol": "005930", "foreign_net": 100, "inst_net": 0},
        {"date": "2026-08-17", "symbol": "005930", "foreign_net": -50, "inst_net": 10},
        # 000660: 순매수만 계속 — 매수 시그널(재유입)
        {"date": "2026-08-14", "symbol": "000660", "foreign_net": 50, "inst_net": 0},
        {"date": "2026-08-17", "symbol": "000660", "foreign_net": 900, "inst_net": 5},
    ])
    sector_map = {"005930": "반도체와반도체장비", "000660": "반도체와반도체장비"}
    payload = {"features": {"foreign_net_100m_krw": 1234}}
    snap = _snap(results={"kospi_flow": _kospi_flow_result()})

    view = report_cli._build_foreign_view(tmp_path, snap, _cont(), sector_map, payload)

    assert view is not None
    assert view["market_foreign_net"] == 1234
    assert view["market_date"] == "2026-08-17"
    assert len(view["sectors"]) == 1
    sector = view["sectors"][0]
    assert sector["name"] == "반도체와반도체장비"
    symbols = {row["symbol"] for row in sector["rows"]}
    assert symbols == {"005930", "000660"}
    hynix = next(r for r in sector["rows"] if r["symbol"] == "000660")
    assert hynix["label"] == "매수 시그널(재유입)"
    assert hynix["days"] == 2
    assert hynix["series"] == [
        {"date": "2026-08-14", "foreign_net": 50},
        {"date": "2026-08-17", "foreign_net": 900},
    ]


def test_symbol_without_sector_map_entry_grouped_as_unclassified(tmp_path):
    _ledger(tmp_path, [
        {"date": "2026-08-14", "symbol": "005930", "foreign_net": 10, "inst_net": 0},
        {"date": "2026-08-17", "symbol": "005930", "foreign_net": 20, "inst_net": 0},
    ])
    snap = _snap(results={"kospi_flow": _kospi_flow_result()})

    view = report_cli._build_foreign_view(tmp_path, snap, _cont(), {}, {"features": {}})

    assert view is not None
    assert view["sectors"][0]["name"] == "미분류"


def test_sectors_ordered_by_foreign_net_sum_descending(tmp_path):
    _ledger(tmp_path, [
        {"date": "2026-08-17", "symbol": "005930", "foreign_net": 10, "inst_net": 0},
        {"date": "2026-08-16", "symbol": "005930", "foreign_net": 10, "inst_net": 0},
        {"date": "2026-08-17", "symbol": "000660", "foreign_net": 9999, "inst_net": 0},
        {"date": "2026-08-16", "symbol": "000660", "foreign_net": 9999, "inst_net": 0},
    ])
    sector_map = {"005930": "업종A", "000660": "업종B"}
    snap = _snap(results={"kospi_flow": _kospi_flow_result()})

    view = report_cli._build_foreign_view(tmp_path, snap, _cont(), sector_map, {"features": {}})

    names = [s["name"] for s in view["sectors"]]
    assert names == ["업종B", "업종A"]


def test_missing_kospi_flow_result_leaves_market_fields_none(tmp_path):
    """kospi_flow 결측이어도(features 에 foreign_net_100m_krw 없음) 종목 단위
    뷰는 그대로 만든다 — 시장 종합 라인만 빠진다."""
    _ledger(tmp_path, [
        {"date": "2026-08-14", "symbol": "005930", "foreign_net": 10, "inst_net": 0},
        {"date": "2026-08-17", "symbol": "005930", "foreign_net": 20, "inst_net": 0},
    ])
    snap = _snap(results={})

    view = report_cli._build_foreign_view(tmp_path, snap, _cont(), {}, {"features": {}})

    assert view is not None
    assert view["market_foreign_net"] is None
    assert view["market_date"] is None
    assert view["sectors"][0]["rows"][0]["symbol"] == "005930"
