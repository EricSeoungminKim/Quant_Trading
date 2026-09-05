"""`quant.apps.cli seed-carry` — D3(b), 2026-09-05.

`cmd_seed_real`이 유지 종목(005930)의 이월 수량에 원장 행을 남기지 않아
(2026-09-01 실계좌 이식) 원장 재구성이 영구히 "-6 vs 포트폴리오 0"으로
어긋난 EC2를 손으로 고치기 위한 일회성 수리 도구. 이 스위트가 고정하는 것:

- 캐리 행 하나가 정확한 스키마(side=buy, strategy_id=seed, 캐리 마커)로
  trades.jsonl에 추가된다.
- 같은 (symbol, ts) 조합이 이미 있으면 다시 쓰지 않는다(멱등).
- --dry-run은 행을 미리 보여주되 파일을 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest


def _args(tmp_path, *, symbol="005930", qty=6.0, price=263416.666666,
          at="2026-09-01T23:01:00+09:00", dry_run=False):
    return argparse.Namespace(symbol=symbol, qty=qty, price=price, at=at, dry_run=dry_run)


def _ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "state" / "trades.jsonl"


def test_appends_a_single_carry_row(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_seed_carry

    cmd_seed_carry(_args(tmp_path))

    ledger_path = _ledger_path(tmp_path)
    assert ledger_path.exists()
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "005930"
    assert row["side"] == "buy"
    assert row["qty"] == 6.0
    assert row["price"] == pytest.approx(263416.666666)
    assert row["strategy_id"] == "seed"
    assert "실계좌 이식 이월" in row["reason"]
    assert row["market"] == "KR"

    printed = json.loads(capsys.readouterr().out)
    assert printed == row


def test_dry_run_prints_but_does_not_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_seed_carry

    cmd_seed_carry(_args(tmp_path, dry_run=True))

    assert not _ledger_path(tmp_path).exists()
    printed = json.loads(capsys.readouterr().out)
    assert printed["symbol"] == "005930"
    assert printed["qty"] == 6.0


def test_refuses_to_duplicate_an_existing_carry_row(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_seed_carry

    cmd_seed_carry(_args(tmp_path))
    capsys.readouterr()  # 1차 출력 비움

    with pytest.raises(SystemExit):
        cmd_seed_carry(_args(tmp_path))

    rows = [json.loads(line) for line in _ledger_path(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1, "멱등이어야 한다 — 두 번째 호출이 행을 하나 더 쌓으면 안 된다"


def test_different_symbol_or_boundary_is_not_treated_as_duplicate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_seed_carry

    cmd_seed_carry(_args(tmp_path, symbol="005930", at="2026-09-01T23:01:00+09:00"))
    cmd_seed_carry(_args(tmp_path, symbol="000660", at="2026-09-01T23:01:00+09:00"))
    cmd_seed_carry(_args(tmp_path, symbol="005930", at="2026-09-02T00:00:00+09:00"))

    rows = [json.loads(line) for line in _ledger_path(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3
