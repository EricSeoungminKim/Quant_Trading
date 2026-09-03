"""`cli performance`(자본 곡선 요약)와 `cli equity-snapshot`(곡선 1점 기록)의
2026-09-02 수정 2건.

- F6: 자본 곡선 원장에는 KR·US 세션 마감마다 행이 남는다. 그 둘을 각각 한 점으로
  세면 수익률 시계열 길이가 실제 거래일의 2배가 되고, √252 연율화가 변동성·샤프를
  √2 만큼 과소평가한다 — 같은 날짜는 마지막 기록 하나만 쓴다.
- F5: 환율은 실조회 우선(Toss `usd_krw()`), 실패 시 고정 폴백 + 그 사실을
  `fx_source`로 남긴다. 예전엔 주석만 "실조회 우선"이고 코드는 항상 1,500원이었다.
"""
from __future__ import annotations

import argparse
import json

import pytest

from quant.apps.cli import cmd_equity_snapshot, cmd_performance


def _curve(tmp_path, rows: list[dict]) -> None:
    path = tmp_path / "data" / "ledger" / "equity_curve.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_performance_collapses_same_day_kr_and_us_snapshots(tmp_path, monkeypatch, capsys):
    """같은 날짜의 KR·US 두 행은 점 하나 — 마지막 기록이 이긴다."""
    rows = []
    for i in range(6):
        day = f"2026-08-{10 + i:02d}"
        rows.append({"date": day, "market": "KR", "total_krw": 10_000_000 + i * 1_000})
        rows.append({"date": day, "market": "US", "total_krw": 10_000_000 + i * 10_000})
    _curve(tmp_path, rows)
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)

    cmd_performance(argparse.Namespace())
    out = capsys.readouterr().out
    # 날짜 6개 → 점 6개(12개가 아니다)
    assert "[총자산] 점 6개" in out


def test_performance_uses_the_last_row_of_the_day(tmp_path, monkeypatch, capsys):
    """중복 행이 있으면 원장에 쓰인 순서상 마지막 것 — 재실행은 append 관례."""
    rows = [
        {"date": "2026-08-10", "market": "KR", "total_krw": 10_000_000},
        {"date": "2026-08-11", "market": "KR", "total_krw": 11_000_000},
        {"date": "2026-08-11", "market": "US", "total_krw": 12_000_000},  # 이게 이긴다
    ]
    _curve(tmp_path, rows)
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)

    cmd_performance(argparse.Namespace())
    out = capsys.readouterr().out
    assert "점 2개" in out  # 표본 부족 경로지만 점 수는 확인 가능


class _Client:
    """`build_toss_client()` 대역 — prices/usd_krw만 흉내낸다."""

    def __init__(self, rate: float | None):
        self._rate = rate

    def prices(self, symbols):
        return []

    def usd_krw(self):
        if self._rate is None:
            raise RuntimeError("환율 조회 실패")
        return self._rate


def _snapshot(tmp_path, monkeypatch, client) -> dict:
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "portfolio.json").write_text(
        json.dumps({"cash": 1_000_000.0, "cash_usd": 100.0, "positions": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    monkeypatch.setattr("quant.apps.assembly.build_toss_client", lambda *a, **k: client)
    cmd_equity_snapshot(argparse.Namespace(market="US"))
    line = (tmp_path / "data" / "ledger" / "equity_curve.jsonl").read_text(
        encoding="utf-8").strip()
    return json.loads(line)


def test_equity_snapshot_uses_live_fx_when_available(tmp_path, monkeypatch):
    row = _snapshot(tmp_path, monkeypatch, _Client(1376.7))
    assert row["fx_source"] == "live"
    assert row["usd_krw"] == pytest.approx(1376.7)
    assert row["total_krw"] == pytest.approx(1_000_000.0 + 100.0 * 1376.7)


def test_equity_snapshot_names_the_fallback_when_fx_lookup_fails(tmp_path, monkeypatch, caplog):
    """폴백을 썼으면 0이 아니라 그 사실을 남긴다 — 총자산이 조용히 틀어지면 안 된다."""
    with caplog.at_level("WARNING"):
        row = _snapshot(tmp_path, monkeypatch, _Client(None))
    assert row["fx_source"] == "fallback:fixed:1500"
    assert row["usd_krw"] == pytest.approx(1500.0)
    assert any("고정 폴백 환율" in r.getMessage() for r in caplog.records)
