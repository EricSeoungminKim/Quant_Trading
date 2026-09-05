"""`quant.apps.cli manual-recs` — D4 회귀(2026-09-05).

`cmd_manual_recs`가 형제 커맨드(`cmd_market_pulse`)와 달리 `load_settings()`를
호출하지 않아 `build_toss_client()`가 `.env.local`을 못 읽고 매번
`MissingCredentials`로 실패했다 — 로컬 일봉이 없는 KR 후보가 조용히 전부
드롭됐다(실측 2026-09-04: 12종목). 이 스위트는 `--market KR` 실행이
`load_settings()`를 호출하는지만 고정한다(Toss 클라이언트 자체는 목으로
막아 네트워크 없이 검증).
"""
from __future__ import annotations

import argparse

import pytest


def test_manual_recs_kr_loads_settings_before_toss_fallback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)

    import quant.apps.cli as cli_module

    calls: list[str] = []
    monkeypatch.setattr(cli_module, "load_settings", lambda: calls.append("load_settings"))

    def _fail_build_toss_client():
        calls.append("build_toss_client")
        from quant.apps.assembly import MissingCredentials
        raise MissingCredentials("TOSS_CLIENT_ID 없음")

    monkeypatch.setattr("quant.apps.assembly.build_toss_client", _fail_build_toss_client)

    args = argparse.Namespace(
        market="KR", scorecard=False, date=None, dry_run=True, no_narrate=True,
    )
    cli_module.cmd_manual_recs(args)

    assert "load_settings" in calls, "manual-recs가 자격증명 로딩 없이 Toss 폴백을 시도하면 안 된다"
    if "build_toss_client" in calls:
        # KR 로컬 일봉이 없어 Toss 폴백을 실제로 탔다면, 반드시 load_settings가 먼저다.
        assert calls.index("load_settings") < calls.index("build_toss_client")


def test_manual_recs_scorecard_does_not_require_market(tmp_path, monkeypatch, capsys):
    """--scorecard 경로는 시장 인자가 없어도(Toss와 무관) 그대로 동작해야 한다 —
    load_settings() 추가가 이 경로를 깨면 안 된다."""
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    import quant.apps.cli as cli_module

    args = argparse.Namespace(market=None, scorecard=True, date=None, dry_run=True, no_narrate=True)
    cli_module.cmd_manual_recs(args)  # 예외 없이 통과해야 한다

    out = capsys.readouterr().out
    assert out  # 성적표 텍스트가 뭔가는 찍혔다
