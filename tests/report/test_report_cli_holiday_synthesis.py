"""`quant/report/collect/holiday_synthesis.py` 배선 테스트 — 휴장 기간 종합
(소유자 요청 2026-08-29).

`quant.analyze.holiday_synthesis`(순수 집계)는 `tests/test_holiday_synthesis.py`
가 이미 단위로 덮는다. 여기서는 파일 I/O(engine.json/스냅샷 읽기)와 narrator
배선(`_build_digest_prose` 테스트와 같은 관례 — narrator 를 직접 주입해 검증)만 본다.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quant.analyze.opendays import anchor_dir_for
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.collect.snapshot import save_snapshot
from quant.report.collect.holiday_synthesis import (
    _apply_holiday_synthesis, _build_holiday_synthesis_prose,
)
from quant.report.paths import _engine_json_path

KST = ZoneInfo("Asia/Seoul")


class _FakeNarrator:
    def __init__(self, reply):
        self._reply = reply
        self.called_with: str | None = None

    def narrate(self, prompt: str):
        self.called_with = prompt
        return self._reply


def _write_anchor(tmp_path: Path, market: str, dates: list[str]) -> None:
    anchor = anchor_dir_for(market, tmp_path)
    idx = pd.DatetimeIndex(
        [pd.Timestamp(d, tz="UTC") + pd.Timedelta(hours=4) for d in dates]
    )
    n = len(dates)
    df = pd.DataFrame({
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
        "close": [100.5] * n, "volume": [1000.0] * n,
    }, index=idx)
    for (year, month), part in df.groupby([df.index.year, df.index.month]):
        path = anchor / f"{year:04d}" / f"{month:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(path)


def _write_engine(out_root: Path, market: str, d: date, payload: dict) -> None:
    path = _engine_json_path(out_root, market, d)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_snapshot(snap_root: Path, market: str, d: date, events: list[dict]) -> None:
    snap = Snapshot(
        schema_version=SCHEMA_VERSION,
        market=market,
        session_date=d,
        generated_at=datetime(d.year, d.month, d.day, 8, 0, tzinfo=KST),
        results={
            "calendar": SourceResult(
                key="calendar", ok=True, data={"events": events}, error=None,
                url="", fetched_at=datetime(d.year, d.month, d.day, 8, 0, tzinfo=KST),
                latency_ms=1,
            ),
        },
    )
    save_snapshot(snap, snap_root)


def _monday_snap(market="KR") -> Snapshot:
    """2026-08-17(월) 아침 — 2026-08-14(금) 개장, 토·일 휴장."""
    return Snapshot(
        schema_version=SCHEMA_VERSION, market=market, session_date=date(2026, 8, 17),
        generated_at=datetime(2026, 8, 17, 8, 0, tzinfo=KST), results={},
    )


# ── 평일 연속(어제 개장)이면 None ────────────────────────────────────────

def test_apply_holiday_synthesis_none_when_no_gap(tmp_path):
    _write_anchor(tmp_path, "KR", ["2026-08-17", "2026-08-18"])  # 월·화 연속 개장
    snap = Snapshot(
        schema_version=SCHEMA_VERSION, market="KR", session_date=date(2026, 8, 18),
        generated_at=datetime(2026, 8, 18, 8, 0, tzinfo=KST), results={},
    )

    out = _apply_holiday_synthesis(snap, tmp_path, tmp_path / "out", tmp_path / "snapshots")

    assert out is None


# ── 월요일 시나리오: 갭 감지 + 결정론 집계가 payload 로 조립된다 ─────────

def test_apply_holiday_synthesis_monday_aggregates_window(tmp_path):
    _write_anchor(tmp_path, "KR", ["2026-08-14"])  # 금요일 개장
    out_root = tmp_path / "out"
    snap_root = tmp_path / "snapshots"

    _write_engine(out_root, "KR", date(2026, 8, 15), {
        "symbols": [
            {"symbol": "005930", "name": "삼성전자", "sector": "반도체", "is_new": True},
        ],
        "stance": {"score100": 48, "label": "중립"},
    })
    _write_snapshot(snap_root, "KR", date(2026, 8, 15), [
        {"name": "Consumer Price Index", "high_impact": True, "is_today": True},
    ])
    _write_engine(out_root, "KR", date(2026, 8, 16), {
        "symbols": [
            {"symbol": "000660", "name": "SK하이닉스", "sector": "반도체", "is_new": True},
        ],
        "stance": {"score100": 53, "label": "중립"},
    })
    _write_snapshot(snap_root, "KR", date(2026, 8, 16), [])

    out = _apply_holiday_synthesis(
        _monday_snap(), tmp_path, out_root, snap_root,
        narrator=_FakeNarrator("휴장 종합: 반도체 강세가 이어졌다."),
    )

    assert out is not None
    assert out["market"] == "KR"
    assert out["last_open"] == "2026-08-14"
    assert out["gap_days"] == 2
    assert out["window_dates"] == ["2026-08-15", "2026-08-16"]
    assert out["missing_engine_days"] == []
    assert out["missing_calendar_days"] == []
    assert out["theme_freq"] == [{"theme": "반도체", "count": 2}]
    assert [s["symbol"] for s in out["new_symbols"]] == ["005930", "000660"]
    assert out["stance_trend"] == [
        {"date": "2026-08-15", "score100": 48, "label": "중립"},
        {"date": "2026-08-16", "score100": 53, "label": "중립"},
    ]
    assert out["high_impact_events"] == [
        {"date": "2026-08-15", "name": "Consumer Price Index"},
    ]
    assert out["prose"] == "반도체 강세가 이어졌다."


# ── 스냅샷/엔진 결손 시 부분 집계 + 결손 표기 ────────────────────────────

def test_apply_holiday_synthesis_partial_when_one_day_missing(tmp_path):
    _write_anchor(tmp_path, "KR", ["2026-08-14"])
    out_root = tmp_path / "out"
    snap_root = tmp_path / "snapshots"

    # 8/15 는 아무 파일도 없음(결손). 8/16 만 존재.
    _write_engine(out_root, "KR", date(2026, 8, 16), {
        "symbols": [{"symbol": "000660", "name": "SK하이닉스", "sector": "반도체",
                     "is_new": True}],
        "stance": {"score100": 60, "label": "우호"},
    })
    _write_snapshot(snap_root, "KR", date(2026, 8, 16), [])

    out = _apply_holiday_synthesis(
        _monday_snap(), tmp_path, out_root, snap_root, narrator=_FakeNarrator(None),
    )

    assert out is not None
    assert out["missing_engine_days"] == ["2026-08-15"]
    assert out["missing_calendar_days"] == ["2026-08-15"]
    # 결손이 아닌 8/16 데이터로는 그대로 집계된다.
    assert out["theme_freq"] == [{"theme": "반도체", "count": 1}]
    assert out["new_symbols"] == [
        {"symbol": "000660", "name": "SK하이닉스", "first_seen": "2026-08-16"},
    ]


# ── LLM 실패 시 산문 없이 섹션이 성립한다 ────────────────────────────────

def test_apply_holiday_synthesis_survives_narrator_failure(tmp_path):
    _write_anchor(tmp_path, "KR", ["2026-08-14"])
    out_root = tmp_path / "out"
    snap_root = tmp_path / "snapshots"
    _write_engine(out_root, "KR", date(2026, 8, 15), {
        "symbols": [{"symbol": "005930", "name": "삼성전자", "sector": "반도체",
                     "is_new": True}],
    })
    _write_snapshot(snap_root, "KR", date(2026, 8, 15), [])

    out = _apply_holiday_synthesis(
        _monday_snap(), tmp_path, out_root, snap_root, narrator=_FakeNarrator(None),
    )

    assert out is not None
    assert out["prose"] is None
    assert out["theme_freq"] == [{"theme": "반도체", "count": 1}]


def test_apply_holiday_synthesis_skips_narrator_when_window_has_no_data(tmp_path):
    """창 안에 engine.json/calendar 가 전부 없으면(=집계 재료 0) narrator 를
    아예 부르지 않는다 — `_build_digest_prose`의 빈 다이제스트 관례와 동일."""
    _write_anchor(tmp_path, "KR", ["2026-08-14"])
    out_root = tmp_path / "out"
    snap_root = tmp_path / "snapshots"

    narrator = _FakeNarrator("무시됨")
    out = _apply_holiday_synthesis(
        _monday_snap(), tmp_path, out_root, snap_root, narrator=narrator,
    )

    assert out is not None
    assert out["prose"] is None
    assert narrator.called_with is None


# ── _build_holiday_synthesis_prose 단위 (narrator 배선만) ────────────────

def test_build_holiday_synthesis_prose_parses_prefix(monkeypatch):
    narrator = _FakeNarrator("휴장 종합: 테스트 문단입니다.")
    view = {
        "gap_days": 2, "last_open": "2026-08-14", "theme_freq": [{"theme": "반도체", "count": 2}],
        "new_symbols": [], "stance_trend": [], "high_impact_events": [],
    }

    out = _build_holiday_synthesis_prose(view, narrator=narrator)

    assert out == "테스트 문단입니다."
    assert "반도체" in narrator.called_with
