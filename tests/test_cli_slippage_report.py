"""`quant.apps.cli slippage-report` (2026-09-06 live-readiness §4) — 배선만
본다(원장 읽기 → --since 필터 → quant.control.slippage 호출 → 출력). 계산
자체(중앙값/p90/판정)는 tests/test_slippage.py가 이미 다룬다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _args(**overrides):
    base = dict(since=None, root=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_no_ledgers_reports_no_data(tmp_path, monkeypatch, capsys):
    from quant.apps.cli import cmd_slippage_report

    cmd_slippage_report(_args(root=str(tmp_path)))
    out = capsys.readouterr().out
    assert "표본 없음" in out


def test_joins_fills_with_spread_samples_and_prints_a_row(tmp_path, capsys):
    from quant.apps.cli import cmd_slippage_report

    _write_jsonl(tmp_path / "data" / "state" / "trades.jsonl", [
        {"ts": "2026-09-01T10:00:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "BUY", "qty": 1, "price": 70.0, "fee": 0.0, "realized_pnl": None, "market": "US"},
    ])
    _write_jsonl(tmp_path / "data" / "ledger" / "spread.jsonl", [
        {"ts": "2026-09-01T10:01:00+00:00", "symbol": "TQQQ", "spread_bp": 4.0},
    ])

    cmd_slippage_report(_args(root=str(tmp_path)))
    out = capsys.readouterr().out
    assert "[US/gap_fade]" in out
    assert "n=1" in out
    assert "중앙값 2.0bp" in out  # spread_bp 4.0 / 2


def test_since_filters_out_older_fills(tmp_path, capsys):
    from quant.apps.cli import cmd_slippage_report

    _write_jsonl(tmp_path / "data" / "state" / "trades.jsonl", [
        {"ts": "2026-08-01T10:00:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "BUY", "qty": 1, "price": 70.0, "fee": 0.0, "realized_pnl": None, "market": "US"},
        {"ts": "2026-09-01T10:00:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "BUY", "qty": 1, "price": 70.0, "fee": 0.0, "realized_pnl": None, "market": "US"},
    ])
    _write_jsonl(tmp_path / "data" / "ledger" / "spread.jsonl", [
        {"ts": "2026-08-01T10:01:00+00:00", "symbol": "TQQQ", "spread_bp": 100.0},
        {"ts": "2026-09-01T10:01:00+00:00", "symbol": "TQQQ", "spread_bp": 4.0},
    ])

    cmd_slippage_report(_args(root=str(tmp_path), since="2026-08-15T00:00:00+00:00"))
    out = capsys.readouterr().out
    assert "n=1" in out
    assert "중앙값 2.0bp" in out
    assert "50.0bp" not in out
