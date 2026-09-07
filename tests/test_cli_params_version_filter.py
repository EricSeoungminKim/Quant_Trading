"""`run scoreboard --current-params-only` + `run ledger-versions` (2026-09-07,
진화가능성 평가 투자 #2~#3) — apps 계층 배선만 검증한다. 순수 계산
(`annotate_params_version`/`strategy_version_table`/`backfill_params_sidecar`)은
`tests/test_ledger.py`가 이미 두텁게 커버한다."""
from __future__ import annotations

import argparse
import json

import pytest


def _write_ledger(tmp_path, rows: list[dict]) -> None:
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "trades.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _fill_row(ts, strategy, symbol, side, qty, price, *, pnl=None, fp=None, market="US"):
    return {
        "ts": ts, "strategy_id": strategy, "symbol": symbol, "side": side,
        "qty": qty, "price": price, "fee": 0.0, "realized_pnl": pnl,
        "params_fingerprint": fp, "market": market,
    }


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    """`ledger_state_path`/`REPO_ROOT`를 tmp_path로 돌린다 — 실제 저장소의
    trades.jsonl을 절대 건드리지 않는다."""
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    yield tmp_path


def test_scoreboard_always_shows_current_vs_total_trip_counts(monkeypatch, capsys, tmp_path):
    """필터를 켜지 않아도 "현재 판본 트립 n / 전체 N" 줄은 항상 보인다."""
    from quant.control.experiments import params_fingerprint

    cfg = {"class": "gap_fade", "enabled": True, "params": {"dip_bps": 30}}
    current_fp = params_fingerprint(cfg)
    monkeypatch.setattr(
        "quant.apps.cli.load_settings",
        lambda *a, **k: argparse.Namespace(raw={"strategies": {"gap_fade": cfg}}),
    )
    _write_ledger(tmp_path, [
        _fill_row("2026-09-01T00:00:00+00:00", "gap_fade", "TQQQ", "BUY", 1, 50.0, fp=current_fp),
        _fill_row("2026-09-01T00:30:00+00:00", "gap_fade", "TQQQ", "SELL", 1, 55.0, pnl=50.0, fp=current_fp),
        _fill_row("2026-08-01T00:00:00+00:00", "gap_fade", "TQQQ", "BUY", 1, 50.0, fp="old-fp"),
        _fill_row("2026-08-01T00:30:00+00:00", "gap_fade", "TQQQ", "SELL", 1, 51.0, pnl=10.0, fp="old-fp"),
    ])

    from quant.apps.cli import cmd_scoreboard

    cmd_scoreboard(argparse.Namespace(days=None))
    out = capsys.readouterr().out
    assert "[gap_fade] 현재 판본 트립 1 / 전체 2" in out


def test_current_params_only_flag_drops_old_version_trips_from_the_scoreboard(monkeypatch, capsys, tmp_path):
    from quant.control.experiments import params_fingerprint

    cfg = {"class": "gap_fade", "enabled": True, "params": {"dip_bps": 30}}
    current_fp = params_fingerprint(cfg)
    monkeypatch.setattr(
        "quant.apps.cli.load_settings",
        lambda *a, **k: argparse.Namespace(raw={"strategies": {"gap_fade": cfg}}),
    )
    _write_ledger(tmp_path, [
        _fill_row("2026-09-01T00:00:00+00:00", "gap_fade", "TQQQ", "BUY", 1, 50.0, fp=current_fp),
        _fill_row("2026-09-01T00:30:00+00:00", "gap_fade", "TQQQ", "SELL", 1, 60.0, pnl=1000.0, fp=current_fp),
        # 옛 판본 — 큰 손실로 부호를 쉽게 구분할 수 있게 만든다.
        _fill_row("2026-08-01T00:00:00+00:00", "gap_fade", "TQQQ", "BUY", 1, 50.0, fp="old-fp"),
        _fill_row("2026-08-01T00:30:00+00:00", "gap_fade", "TQQQ", "SELL", 1, 10.0, pnl=-4000.0, fp="old-fp"),
    ])

    from quant.apps.cli import cmd_scoreboard

    cmd_scoreboard(argparse.Namespace(days=None, current_params_only=True))
    out = capsys.readouterr().out
    assert "현재 판본만" in out
    assert "[gap_fade] 1건" in out, "옛 판본 트립이 필터로 빠지고 현재 판본 1건만 남아야 한다"


def test_governor_config_default_enables_the_filter_without_the_flag(monkeypatch, capsys, tmp_path):
    """`governor.judge_current_params_only: true`면 `--current-params-only` 없이도
    필터가 걸린다."""
    cfg = {"class": "gap_fade", "enabled": True, "params": {"dip_bps": 30}}
    monkeypatch.setattr(
        "quant.apps.cli.load_settings",
        lambda *a, **k: argparse.Namespace(raw={
            "strategies": {"gap_fade": cfg},
            "governor": {"judge_current_params_only": True},
        }),
    )
    _write_ledger(tmp_path, [
        _fill_row("2026-08-01T00:00:00+00:00", "gap_fade", "TQQQ", "BUY", 1, 50.0, fp="old-fp"),
        _fill_row("2026-08-01T00:30:00+00:00", "gap_fade", "TQQQ", "SELL", 1, 10.0, pnl=-4000.0, fp="old-fp"),
    ])

    from quant.apps.cli import cmd_scoreboard

    cmd_scoreboard(argparse.Namespace(days=None))
    out = capsys.readouterr().out
    assert "현재 판본만" in out
    assert "[gap_fade] 현재 판본 트립 0 / 전체 1" in out
    assert "종결된 트레이드가 아직 없음" in out, (
        "옛 판본만 있는데 config 기본값으로 필터가 켜져 그 트립은 스코어보드에서 빠져야 한다"
    )


def test_ledger_versions_dry_run_prints_a_table_without_writing_anything(monkeypatch, capsys, tmp_path):
    _write_ledger(tmp_path, [
        _fill_row("2026-09-01T00:00:00+00:00", "gap_fade", "TQQQ", "BUY", 1, 50.0),
        _fill_row("2026-09-01T00:30:00+00:00", "gap_fade", "TQQQ", "SELL", 1, 55.0, pnl=50.0),
    ])
    changes_dir = tmp_path / "data" / "ledger"
    changes_dir.mkdir(parents=True, exist_ok=True)
    (changes_dir / "param_changes.jsonl").write_text(
        json.dumps({"strategy": "gap_fade", "fingerprint": "fp-1",
                    "recorded_at": "2026-08-01T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    from quant.apps.cli import cmd_ledger_versions

    cmd_ledger_versions(argparse.Namespace(
        ledger=str(tmp_path / "data" / "state" / "trades.jsonl"),
        changes=str(changes_dir / "param_changes.jsonl"),
        dry_run=True, write_sidecar=False, sidecar=None,
    ))
    out = capsys.readouterr().out
    assert "gap_fade" in out
    assert not (changes_dir / "trades_params_sidecar.jsonl").exists(), (
        "--write-sidecar를 안 줬는데 사이드카 파일이 생겼다"
    )


def test_ledger_versions_write_sidecar_creates_the_approximation_file(monkeypatch, capsys, tmp_path):
    trades_path = tmp_path / "data" / "state" / "trades.jsonl"
    _write_ledger(tmp_path, [
        _fill_row("2026-09-01T00:00:00+00:00", "gap_fade", "TQQQ", "BUY", 1, 50.0),  # fp 없음
        _fill_row("2026-09-01T00:30:00+00:00", "gap_fade", "TQQQ", "SELL", 1, 55.0, pnl=50.0),
    ])
    changes_path = tmp_path / "data" / "ledger" / "param_changes.jsonl"
    changes_path.parent.mkdir(parents=True, exist_ok=True)
    changes_path.write_text(
        json.dumps({"strategy": "gap_fade", "fingerprint": "fp-1",
                    "recorded_at": "2026-08-01T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    sidecar_path = tmp_path / "data" / "ledger" / "trades_params_sidecar.jsonl"
    trades_before = trades_path.read_text(encoding="utf-8")

    from quant.apps.cli import cmd_ledger_versions

    cmd_ledger_versions(argparse.Namespace(
        ledger=str(trades_path), changes=str(changes_path),
        dry_run=False, write_sidecar=True, sidecar=str(sidecar_path),
    ))

    assert sidecar_path.exists()
    rows = [json.loads(line) for line in sidecar_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows and rows[0]["params_fingerprint"] == "fp-1" and rows[0]["approx"] is True
    assert trades_path.read_text(encoding="utf-8") == trades_before, "trades.jsonl은 절대 다시 쓰지 않는다"
