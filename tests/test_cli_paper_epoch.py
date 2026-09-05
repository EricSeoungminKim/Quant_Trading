"""`quant.apps.cli paper-epoch`(2026-09-06) — capital_policy: fixed_dual 전환에
맞춘 페이퍼 계정 시대 리셋 도구.

`tests/test_cli_seed_real.py`와 같은 격리 패턴을 쓴다: REPO_ROOT만 tmp_path로
격리하고 cwd는 그대로 둔다(pytest는 저장소 루트에서 실행되므로 `load_settings()`
의 상대경로 "config/settings.yaml"이 실제 파일을 그대로 읽는다 — 지금 실제
설정은 `capital_policy: fixed_dual`이라 이 도구의 정상 경로를 그대로 태운다).

고정하는 것:
- 지금 `capital_policy`가 `fixed_dual`이 아니면 거부한다.
- `--dry-run`은 파일을 하나도 안 쓰고 생성될 책(전략별 KRW/USD)만 출력한다.
- 실제 실행은 books.json/portfolio.json을 새로 쓰고 trades.jsonl에 에폭 마커를
  남긴다 — 기존 상태 파일이 있으면 `.pre-epoch-<시각>`으로 백업한다.
- 엔진이 활성으로 보이면(하트비트 최신) `--force` 없이는 거부한다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest


def _args(*, at="2026-09-07T00:00:00+09:00", dry_run=False, force=False):
    return argparse.Namespace(at=at, dry_run=dry_run, force=force)


def _state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_refuses_when_capital_policy_is_not_fixed_dual(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps import config as config_module
    from quant.apps.cli import cmd_paper_epoch

    real_load_settings = config_module.load_settings

    def _fake_load_settings():
        settings = real_load_settings()
        settings.raw = {**settings.raw, "risk": {**settings.raw["risk"], "capital_policy": "declared"}}
        return settings

    monkeypatch.setattr(config_module, "load_settings", _fake_load_settings)

    with pytest.raises(SystemExit, match="fixed_dual"):
        cmd_paper_epoch(_args())

    assert not (tmp_path / "data" / "state" / "strategy_books.json").exists()


def test_dry_run_prints_books_but_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_paper_epoch

    cmd_paper_epoch(_args(dry_run=True))

    state_dir = tmp_path / "data" / "state"
    assert not (state_dir / "strategy_books.json").exists()
    assert not (state_dir / "portfolio.json").exists()
    assert not (state_dir / "trades.jsonl").exists()

    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["capital_policy"] == "fixed_dual"
    assert result["per_strategy_initial_krw"] == pytest.approx(10_000_000.0)
    assert result["per_strategy_initial_usd"] == pytest.approx(10_000.0)
    # scalp_1m은 KR/US 양쪽에 계좌가 있다 — 실제 로스터를 태운 증거.
    assert result["books"]["scalp_1m"] == {"KRW": 10_000_000.0, "USD": 10_000.0}
    assert result["books"]["letf_pair_qqq"] == {"KRW": 0.0, "USD": 10_000.0}
    assert result["krw_wallet_total"] > 0
    assert result["usd_wallet_total"] > 0


def test_real_run_writes_fresh_books_and_epoch_marker(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_paper_epoch

    cmd_paper_epoch(_args())

    state_dir = tmp_path / "data" / "state"
    books = json.loads((state_dir / "strategy_books.json").read_text(encoding="utf-8"))
    assert books["books"]["scalp_1m"]["cash_krw"] == pytest.approx(10_000_000.0)
    assert books["books"]["scalp_1m"]["cash_usd"] == pytest.approx(10_000.0)
    assert books["books"]["scalp_1m"]["positions"] == {}

    portfolio = json.loads((state_dir / "portfolio.json").read_text(encoding="utf-8"))
    assert portfolio["cash"] > 0
    assert portfolio["cash_usd"] > 0
    assert portfolio["positions"] == {}, "포지션은 옮기지 않는다 — 현금 전용 리셋"

    lines = (state_dir / "trades.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    marker = json.loads(lines[0])
    assert marker["ts"] == "2026-09-07T00:00:00+09:00"
    assert "페이퍼 에폭 리셋" in marker["reason"]

    result = json.loads(capsys.readouterr().out)
    assert result["written"] is True
    assert result["archived"] == []


def test_real_run_archives_existing_state_before_overwriting(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_paper_epoch

    state_dir = _state_dir(tmp_path)
    old_books = state_dir / "strategy_books.json"
    old_books.write_text(json.dumps({"version": 1, "initial_krw": 0.0, "books": {
        "stale_strategy": {"cash_krw": 1234.0, "initial_krw": 1234.0, "positions": {}},
    }}), encoding="utf-8")
    old_portfolio = state_dir / "portfolio.json"
    old_portfolio.write_text(json.dumps({"cash": 999.0, "cash_usd": 0.0, "positions": {}}), encoding="utf-8")

    cmd_paper_epoch(_args())

    backups = list(state_dir.glob("strategy_books.json.pre-epoch-*"))
    assert len(backups) == 1
    archived_books = json.loads(backups[0].read_text(encoding="utf-8"))
    assert "stale_strategy" in archived_books["books"], "백업이 이전 장부를 그대로 담아야 한다"

    portfolio_backups = list(state_dir.glob("portfolio.json.pre-epoch-*"))
    assert len(portfolio_backups) == 1

    # 원본 경로는 새 내용으로 교체됐다 — 이전 장부(stale_strategy)는 사라진다.
    new_books = json.loads(old_books.read_text(encoding="utf-8"))
    assert "stale_strategy" not in new_books["books"]
    assert "scalp_1m" in new_books["books"]


def test_refuses_when_engine_looks_active_via_recent_heartbeat(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_paper_epoch

    state_dir = _state_dir(tmp_path)
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": time.time()}), encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="활성"):
        cmd_paper_epoch(_args())

    assert not (state_dir / "trades.jsonl").exists()


def test_force_bypasses_the_active_engine_check(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_paper_epoch

    state_dir = _state_dir(tmp_path)
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": time.time()}), encoding="utf-8",
    )

    cmd_paper_epoch(_args(force=True))

    assert (state_dir / "trades.jsonl").exists()


def test_stale_heartbeat_does_not_count_as_active(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_paper_epoch

    state_dir = _state_dir(tmp_path)
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": 0.0}), encoding="utf-8",  # 1970년 — 명백히 오래됨
    )

    cmd_paper_epoch(_args())  # force 없이도 통과해야 한다

    assert (state_dir / "trades.jsonl").exists()


# ---------------------------------------------------------------------------
# `_engine_looks_active` — systemctl vs 하트비트 우선순위 (2026-09-06
# live-readiness §2). `systemctl is-active`가 조회 가능하면 하트비트보다
# 권위 있다: 정지 직후엔 poll_seconds 이내라 하트비트가 항상 "최근"으로
# 보이므로, 하트비트만 보면 방금 멈춘 엔진을 활성으로 오판한다.
# ---------------------------------------------------------------------------

def _fresh_heartbeat(state_dir):
    import time

    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": time.time()}), encoding="utf-8",
    )


class _FakeCompleted:
    def __init__(self, stdout: str):
        self.stdout = stdout


def test_systemctl_inactive_overrides_a_fresh_heartbeat(tmp_path, monkeypatch):
    """엔진을 막 멈췄다(systemctl stop) — 하트비트는 아직 최근이지만 systemctl은
    inactive다. systemctl 조회가 가능하면 그쪽을 믿는다."""
    from quant.apps.cli import _engine_looks_active

    state_dir = _state_dir(tmp_path)
    _fresh_heartbeat(state_dir)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _FakeCompleted("inactive\n"))

    active, why = _engine_looks_active(tmp_path)
    assert active is False
    assert "inactive" in why


def test_systemctl_active_is_trusted_even_without_a_heartbeat_file(tmp_path, monkeypatch):
    from quant.apps.cli import _engine_looks_active

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _FakeCompleted("active\n"))

    active, why = _engine_looks_active(tmp_path)  # heartbeat.json 자체가 없다
    assert active is True
    assert "active" in why


def test_heartbeat_rule_still_applies_when_systemctl_is_unavailable(tmp_path, monkeypatch):
    """Mac 등 systemctl이 없는 환경 — 기존 하트비트 규칙만 쓴다."""
    from quant.apps.cli import _engine_looks_active

    state_dir = _state_dir(tmp_path)
    _fresh_heartbeat(state_dir)
    monkeypatch.setattr("shutil.which", lambda name: None)

    active, why = _engine_looks_active(tmp_path)
    assert active is True
    assert "systemctl 없음" in why


def test_paper_epoch_proceeds_when_systemctl_says_inactive_despite_fresh_heartbeat(tmp_path, monkeypatch):
    """`cmd_paper_epoch` 통합 — systemctl이 inactive라고 하면 하트비트가
    최근이어도 --force 없이 통과해야 한다."""
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_paper_epoch

    state_dir = _state_dir(tmp_path)
    _fresh_heartbeat(state_dir)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _FakeCompleted("inactive\n"))

    cmd_paper_epoch(_args())

    assert (state_dir / "trades.jsonl").exists()
