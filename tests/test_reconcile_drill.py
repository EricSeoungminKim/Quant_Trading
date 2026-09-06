"""`quant.apps.cli reconcile-drill` (2026-09-06 라이브 준비 D1).

감사 발견: `Reconciler`(quant/trade/reconcile.py)는 지금까지 실전에서 단 한
번도 불일치를 만나 halt해본 적이 없다 — paper는 대사 대상이 아니었고(항등 대사
도입 전), 실계좌에서도 아직 실제 불일치가 난 적이 없다. 이 드릴은 paper
포트폴리오의 읽기 전용 스냅샷에 불일치를 주입해 실제 `Reconciler`가 감지·halt·
알림하는지 사람이 직접 확인하는 도구다.

이 파일이 지키는 것:
- 4가지 주입 종류(qty/missing/extra/cash) 전부가 감지·halt된다.
- `--inject none`은 '일치'를 내고 halt하지 않는다.
- 실 파일(data/state/portfolio.json, data/state/control.json)은 절대 건드리지
  않는다 — 읽기든 쓰기든.
- `--dry-run`은 흔적을 전혀 남기지 않고, 기본 모드는 drill/ 아래에만 감사
  흔적을 남긴다.
- 반복 실행이 이전 halt 상태를 다음 실행으로 새지 않는다.
- 절대 주문을 내지 않는다(대사가 place_order를 부르면 그 자리에서 죽는다).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from quant.apps.cli import cmd_reconcile_drill

_ALL_KINDS = ["none", "qty", "missing", "extra", "cash"]
_MISMATCH_KINDS = ["qty", "missing", "extra", "cash"]


def _args(inject: str, root: Path, *, dry_run: bool = True, send: bool = False) -> argparse.Namespace:
    return argparse.Namespace(inject=inject, root=str(root), dry_run=dry_run, send=send)


def _write_portfolio(root: Path, *, cash: float, positions: dict[str, dict]) -> None:
    state_dir = root / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "portfolio.json").write_text(
        json.dumps({"cash": cash, "cash_usd": 0.0, "positions": positions}, ensure_ascii=False),
        encoding="utf-8",
    )


def _seed_default_portfolio(root: Path) -> None:
    _write_portfolio(root, cash=1_000_000.0, positions={
        "TQQQ": {"symbol": "TQQQ", "qty": 10.0, "avg_cost": 50.0},
    })


# ------------------------------------------------------------------- 감지/판정

def test_inject_none_reports_match_and_exits_zero(tmp_path, capsys):
    _seed_default_portfolio(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_reconcile_drill(_args("none", tmp_path))

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "일치" in out
    assert "정상(halt 아님)" in out


@pytest.mark.parametrize("kind", _MISMATCH_KINDS)
def test_inject_kind_is_detected_and_halts(tmp_path, capsys, kind):
    _seed_default_portfolio(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_reconcile_drill(_args(kind, tmp_path))

    assert exc.value.code == 0, "기대대로 감지/halt에 성공하면 exit 0이어야 한다"
    out = capsys.readouterr().out
    assert "불일치" in out
    assert "halted" in out


def test_uses_real_portfolio_snapshot_when_present(tmp_path, capsys):
    _write_portfolio(tmp_path, cash=1_000_000.0, positions={
        "005935": {"symbol": "005935", "qty": 3.0, "avg_cost": 900.0},
    })

    with pytest.raises(SystemExit):
        cmd_reconcile_drill(_args("qty", tmp_path))

    out = capsys.readouterr().out
    assert "005935" in out
    assert "fixture로 대체" not in out


def test_falls_back_to_fixture_when_no_real_portfolio(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cmd_reconcile_drill(_args("qty", tmp_path))

    out = capsys.readouterr().out
    assert "fixture로 대체" in out


# --------------------------------------------------------------- 실 파일 보호

def test_never_touches_the_live_portfolio_or_control_files(tmp_path):
    _seed_default_portfolio(tmp_path)
    portfolio_path = tmp_path / "data" / "state" / "portfolio.json"
    before = portfolio_path.read_bytes()
    control_path = tmp_path / "data" / "state" / "control.json"
    assert not control_path.exists()

    for kind in _ALL_KINDS:
        with pytest.raises(SystemExit):
            cmd_reconcile_drill(_args(kind, tmp_path, dry_run=False))

    assert portfolio_path.read_bytes() == before
    assert not control_path.exists()  # 실 control.json은 만들어지지도 않는다


def test_dry_run_leaves_no_trace_under_data_state_drill(tmp_path):
    _seed_default_portfolio(tmp_path)

    with pytest.raises(SystemExit):
        cmd_reconcile_drill(_args("qty", tmp_path, dry_run=True))

    assert not (tmp_path / "data" / "state" / "drill").exists()


def test_default_mode_leaves_audit_trail_under_drill_dir(tmp_path):
    _seed_default_portfolio(tmp_path)

    with pytest.raises(SystemExit):
        cmd_reconcile_drill(_args("qty", tmp_path, dry_run=False))

    drill_dir = tmp_path / "data" / "state" / "drill"
    assert (drill_dir / "portfolio_snapshot.json").exists()
    assert (drill_dir / "control.json").exists()
    assert json.loads((drill_dir / "control.json").read_text())["halted"] is True


def test_repeated_runs_do_not_leak_stale_halt_state(tmp_path, capsys):
    _seed_default_portfolio(tmp_path)

    with pytest.raises(SystemExit) as first:
        cmd_reconcile_drill(_args("qty", tmp_path, dry_run=False))
    assert first.value.code == 0
    capsys.readouterr()

    with pytest.raises(SystemExit) as second:
        cmd_reconcile_drill(_args("none", tmp_path, dry_run=False))

    assert second.value.code == 0, "이전 실행의 halt가 새면 여기서 감지가 실패로 보인다"
    out = capsys.readouterr().out
    assert "일치" in out
    assert "정상(halt 아님)" in out


# --------------------------------------------------------- 절대 주문을 내지 않는다

def test_never_places_orders_for_any_injection_kind(tmp_path):
    """`_DrillBroker.place_order`는 호출되면 AssertionError를 던진다. 다섯 종류
    전부 SystemExit로만 끝나야 한다 — AssertionError가 새 나오면 대사가 실제로
    주문을 내려 했다는 뜻이라 이 테스트가 실패한다."""
    _seed_default_portfolio(tmp_path)

    for kind in _ALL_KINDS:
        with pytest.raises(SystemExit):
            cmd_reconcile_drill(_args(kind, tmp_path))


# ------------------------------------------------------------------------ 알림

def test_send_without_notifier_falls_back_to_capture(tmp_path, capsys, monkeypatch):
    import quant.apps.assembly as assembly_module

    monkeypatch.setattr(assembly_module, "build_notifier", lambda cfg: None)
    _seed_default_portfolio(tmp_path)

    with pytest.raises(SystemExit):
        cmd_reconcile_drill(_args("qty", tmp_path, send=True))

    err = capsys.readouterr().err
    assert "캡처로 대체" in err


def test_capture_mode_prints_the_ops_alert_text_without_sending(tmp_path, capsys):
    _seed_default_portfolio(tmp_path)

    with pytest.raises(SystemExit):
        cmd_reconcile_drill(_args("missing", tmp_path))

    out = capsys.readouterr().out
    assert "캡처됨 — 실제로 보내지 않았다" in out
    assert "ops 레인" in out
    assert "신규 진입을 중단했다" in out
