"""`report_cli._emit` → 외국인 수급 원장(`frgn_flow.jsonl`) 배선(서브프로젝트 I).

`_record_flows`(기존 `flows.jsonl`, 5일 합계 뷰용)와 별개로, `stock_detail.
fetch_many` 가 돌려주는 `flow_daily`(최대 20일치)를 `quant.control.frgn_flow.
append_daily` 로 새 원장에 쌓는다(`_record_frgn_flow`). 원장 기록 게이트
(`_should_record_ledger`, §G Task 4 — 휴장일 재기록 차단)를 `_record_flows`
와 그대로 공유해야 한다: 이 패턴은 `tests/report/test_report_build_quotes.py`
의 게이트 테스트를 최소 형태로 재현한다(그 파일은 건드리지 않는다).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quant.analyze.opendays import anchor_dir_for
from quant.apps import report_cli
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.report.collect import core as report_core
from quant.report.collect import midterm as report_midterm

KST = ZoneInfo("Asia/Seoul")


def _write_daily_anchor(root: Path, market: str, dates: list[str]) -> None:
    anchor = anchor_dir_for(market, root)
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in dates])
    n = len(dates)
    df = pd.DataFrame({
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
        "close": [100.5] * n, "volume": [1000.0] * n,
    }, index=idx)
    for (year, month), part in df.groupby([df.index.year, df.index.month]):
        path = anchor / f"{year:04d}" / f"{month:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(path)


def _cont_entry() -> dict:
    return {
        "name": "삼성전자", "days": 1, "articles": 1, "today_articles": 3,
        "streak_days": 1, "is_new": True, "history": [True], "titles": [],
    }


def _kr_snap(session_date: date) -> Snapshot:
    boards = {"거래상위": [{"rank": 1, "symbol": "005930", "price": 70000.0,
                           "change_pct": 1.0, "trading_amount": 1000}]}
    at = datetime(session_date.year, session_date.month, session_date.day, 8, 0, tzinfo=KST)
    return Snapshot(
        schema_version=SCHEMA_VERSION, market="KR", session_date=session_date,
        generated_at=at,
        results={
            "toss_rankings": SourceResult(
                key="toss_rankings", ok=True, data={"boards": boards}, error=None,
                url="https://x", fetched_at=at, latency_ms=1,
            ),
        },
    )


def _wire_pipeline(monkeypatch, flow_daily: list[dict]) -> None:
    cont = {"005930": _cont_entry()}
    monkeypatch.setattr(report_core, "load_us_table", lambda cache_dir: {})
    monkeypatch.setattr(report_core, "load_table", lambda cache_dir: {})
    monkeypatch.setattr(report_core, "collect_mentions", lambda snap, table, market: [])
    monkeypatch.setattr(report_core, "append_ledger", lambda mentions, path: 0)
    monkeypatch.setattr(report_core, "load_ledger", lambda path: [])
    monkeypatch.setattr(
        report_core, "continuity", lambda ledger, today, market=None: cont
    )
    monkeypatch.setattr(report_core, "load_market_map", lambda cache_dir: {"005930": "005930.KS"})
    # `_midterm_entities`(→ `_build_midterm_watch_view`, `_emit`이 KR 리포트에서
    # 부른다)는 `_derive`와 별개로 `load_table`을 자기 모듈에서 다시 임포트한다
    # (Phase D 엔진 분리) — 두 곳 다 막아야 이 테스트가 네트워크를 타지 않는다.
    monkeypatch.setattr(report_midterm, "load_table", lambda cache_dir: {})
    monkeypatch.setattr(
        report_core, "fetch_symbol_quotes",
        lambda syms: {s: {"close": 100.0, "change_pct": 0.0} for s in syms},
    )
    monkeypatch.setattr(
        report_core, "fetch_many",
        lambda codes, limit=20: {
            "005930": {"flow": [], "flow_daily": flow_daily},
        },
    )
    monkeypatch.setattr(report_cli, "_fetch_youtube_briefs", lambda: {})
    if hasattr(report_cli, "_fetch_blog_briefs"):
        monkeypatch.setattr(report_cli, "_fetch_blog_briefs", lambda: {})
    # 텔레그램 인사이트(서브프로젝트 S part 2) — _emit 이 실제 네트워크를
    # 타지 않게 유튜브/블로그와 같은 이유로 막는다.
    monkeypatch.setattr(report_cli, "_fetch_telegram_briefs", lambda root, getter=None: {})


def _frgn_flow_path(root: Path) -> Path:
    return root / "data" / "ledger" / "frgn_flow.jsonl"


def test_emit_records_flow_daily_rows_into_frgn_flow_ledger(monkeypatch, tmp_path):
    _write_daily_anchor(tmp_path, "KR", ["2026-08-17"])
    flow_daily = [
        {"date": "2026-08-17", "foreign_net": 100, "inst_net": -50},
        {"date": "2026-08-14", "foreign_net": -30, "inst_net": 10},
    ]
    _wire_pipeline(monkeypatch, flow_daily)
    out_root = tmp_path / "out"
    snap = _kr_snap(date(2026, 8, 18))  # 정상 화요일, 마지막 개장일 월(08-17)

    report_cli._emit(snap, tmp_path, out_root, tmp_path / "snapshots")

    path = _frgn_flow_path(tmp_path)
    assert path.exists()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    by_date = {r["date"]: r for r in rows}
    assert by_date["2026-08-17"] == {
        "date": "2026-08-17", "symbol": "005930", "foreign_net": 100, "inst_net": -50,
    }
    assert by_date["2026-08-14"] == {
        "date": "2026-08-14", "symbol": "005930", "foreign_net": -30, "inst_net": 10,
    }


def test_emit_skips_frgn_flow_ledger_on_stale_weekend_rerun(monkeypatch, tmp_path):
    """게이트(§G Task 4) 공유 확인 — 금요일(08-14) 마감 데이터의 재기록인 일요일
    (08-16)엔 flows.jsonl 뿐 아니라 frgn_flow.jsonl 도 건너뛴다(같은 게이트,
    `_should_record_ledger`, 를 통과해야 두 원장 다 기록된다)."""
    _write_daily_anchor(tmp_path, "KR", ["2026-08-14"])
    flow_daily = [{"date": "2026-08-14", "foreign_net": 100, "inst_net": -50}]
    _wire_pipeline(monkeypatch, flow_daily)
    out_root = tmp_path / "out"
    snap = _kr_snap(date(2026, 8, 16))  # 일요일

    report_cli._emit(snap, tmp_path, out_root, tmp_path / "snapshots")

    assert not _frgn_flow_path(tmp_path).exists()


def test_emit_records_frgn_flow_on_saturday_first_capture(monkeypatch, tmp_path):
    """토요일은 예외 — 금요일 마감 데이터의 첫 기록이므로 기록한다(`_should_record_ledger`
    docstring과 동일한 케이스)."""
    _write_daily_anchor(tmp_path, "KR", ["2026-08-14"])
    flow_daily = [{"date": "2026-08-14", "foreign_net": 100, "inst_net": -50}]
    _wire_pipeline(monkeypatch, flow_daily)
    out_root = tmp_path / "out"
    snap = _kr_snap(date(2026, 8, 15))  # 토요일

    report_cli._emit(snap, tmp_path, out_root, tmp_path / "snapshots")

    assert _frgn_flow_path(tmp_path).exists()
