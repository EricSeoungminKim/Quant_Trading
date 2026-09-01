"""`quant.report.collect.money_flow.build_money_flow_view` — "돈의 흐름" 섹션
배선(2026-08-31 소유자 지시). `_emit` 전체를 태우지 않고 이 헬퍼 하나만 단위로
검증한다(`test_report_cli_digest_prose.py`와 같은 관례)."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.report.collect.money_flow import build_money_flow_view

_AT = datetime(2026, 8, 31, 8, 0, tzinfo=KST)


class _FakeNarrator:
    def __init__(self, reply):
        self._reply = reply
        self.called_with: str | None = None

    def narrate(self, prompt: str):
        self.called_with = prompt
        return self._reply


def _snap(market: str = "KR", quotes: dict | None = None) -> Snapshot:
    results = {}
    if quotes is not None:
        results["market"] = SourceResult(
            key="market", ok=True, data={"quotes": quotes}, error=None,
            url="x", fetched_at=_AT, latency_ms=1,
        )
    return Snapshot(SCHEMA_VERSION, market, date(2026, 8, 31), _AT, results)


def _write_ledger(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _rich_rows():
    rows = []
    for d in range(1, 26):
        rows.append({"date": f"2026-08-{d:02d}", "series": "us_10y", "value": 4.50 + d * 0.01})
        rows.append({"date": f"2026-08-{d:02d}", "series": "vix", "value": 14.0})
        rows.append({"date": f"2026-08-{d:02d}", "series": "oil_wti", "value": 80.0 + d * 0.5})
    return rows


def test_build_money_flow_view_none_when_ledger_missing(tmp_path):
    snap = _snap(quotes={"^KS11": {"change_pct": -0.5}})
    assert build_money_flow_view(snap, tmp_path) is None


def test_build_money_flow_view_populates_series_flow_cash_sector_tilt(tmp_path, monkeypatch):
    _write_ledger(tmp_path / "data" / "ledger" / "macro_rates.jsonl", _rich_rows())
    monkeypatch.setattr(
        "quant.adapters.narrate.make_narrator", lambda: _FakeNarrator("돈의 흐름: 유가·금리 부담이 이어진다."),
    )
    snap = _snap(market="KR", quotes={"^KS11": {"change_pct": -0.8}})

    view = build_money_flow_view(snap, tmp_path)

    assert view is not None
    assert "us_10y" in view["series"]
    assert view["flow"]["label"]
    assert view["cash"]["label"]
    assert set(view.keys()) == {"series", "flow", "cash", "sector_tilt", "prose", "fallback_text"}
    assert isinstance(view["sector_tilt"], dict)
    assert view["prose"] == "유가·금리 부담이 이어진다."
    assert view["fallback_text"]


def test_build_money_flow_view_us_market_uses_us_sector_tilt_and_nasdaq_quote(tmp_path, monkeypatch):
    _write_ledger(tmp_path / "data" / "ledger" / "macro_rates.jsonl", _rich_rows())
    monkeypatch.setattr("quant.adapters.narrate.make_narrator", lambda: _FakeNarrator(None))
    snap = _snap(market="US", quotes={"^IXIC": {"change_pct": 1.1}})

    view = build_money_flow_view(snap, tmp_path)

    assert view is not None
    # US 시장 리포트는 US 섹터 기울기(GICS ETF 라벨)만 받는다 — KR 업종명이 섞이지 않는다.
    for sector in view["sector_tilt"]:
        assert "은행" != sector and "석유와가스" != sector
    assert view["prose"] is None  # narrator 실패 → None, fallback_text로 완전해야 한다
    assert view["fallback_text"]


def test_build_money_flow_view_missing_equity_quote_defers_flow_judgment(tmp_path):
    _write_ledger(tmp_path / "data" / "ledger" / "macro_rates.jsonl", _rich_rows())
    snap = _snap(market="KR", quotes={})  # 지수 시세 없음

    view = build_money_flow_view(snap, tmp_path)

    assert view is not None
    assert "보류" in view["flow"]["label"]


def test_build_money_flow_view_survives_analyze_failure(tmp_path, monkeypatch):
    """판정 함수가 예외를 던져도(원장 손상 등) 리포트 발행 자체는 막지 않는다."""
    _write_ledger(tmp_path / "data" / "ledger" / "macro_rates.jsonl", _rich_rows())
    monkeypatch.setattr(
        "quant.report.collect.money_flow.analyze_money_flow",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    snap = _snap(market="KR", quotes={"^KS11": {"change_pct": 0.1}})

    assert build_money_flow_view(snap, tmp_path) is None
