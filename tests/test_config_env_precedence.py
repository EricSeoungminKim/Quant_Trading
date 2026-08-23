"""환경변수 우선순위: 프로세스 환경 > .env.local > .env

실제로 터졌던 결함: `load_settings()`가 `load_dotenv(".env.local", override=True)`만
호출해 **파일이 실제 프로세스 환경변수까지 덮어썼다.** 그 결과
`MODE=live python -m quant.apps.cli ...`이 조용히 무시되고 `.env.local`의
`MODE=paper`가 이겼다(실측: 승인 게이트가 "MODE=paper"로 판단해 비활성).

`MODE`는 두 안전장치의 스위치다:
- `TossBroker.place_order`는 `MODE != "live"`면 실주문을 거부한다
- 승인 게이트는 `approval.required_modes`에 현재 MODE가 있을 때만 켜진다

운영자가 안전을 위해 명시적으로 건 환경변수가 파일에 덮여 사라지는 방향이라
위험하다. 이 파일은 그 우선순위를 고정한다.
"""
from __future__ import annotations

from quant.apps.config import load_settings

_YAML = "engine:\n  poll_seconds: 10\nstrategies: {}\n"


def _workspace(tmp_path, env_local: str) -> str:
    (tmp_path / ".env").write_text("MODE=paper\nFROM_BASE_ENV=base\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text(env_local, encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text(_YAML, encoding="utf-8")
    return str(settings)


def test_process_env_beats_env_local(tmp_path, monkeypatch):
    """명시적으로 준 환경변수가 최종 승자여야 한다."""
    settings_path = _workspace(tmp_path, "MODE=paper\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODE", "live")

    load_settings(settings_path)

    import os
    assert os.environ["MODE"] == "live", (
        ".env.local이 프로세스 환경변수를 덮어썼다 — 운영자가 명시한 MODE가 사라진다"
    )


def test_env_local_still_beats_env(tmp_path, monkeypatch):
    """파일끼리의 기존 우선순위는 그대로 유지돼야 한다."""
    settings_path = _workspace(tmp_path, "MODE=live\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODE", raising=False)

    load_settings(settings_path)

    import os
    assert os.environ["MODE"] == "live", ".env.local이 .env를 이기지 못했다"


def test_env_local_fills_values_absent_from_process_env(tmp_path, monkeypatch):
    """프로세스 환경에 없는 키는 정상적으로 파일에서 채워져야 한다."""
    settings_path = _workspace(tmp_path, "ONLY_IN_LOCAL=abc\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ONLY_IN_LOCAL", raising=False)

    load_settings(settings_path)

    import os
    assert os.environ["ONLY_IN_LOCAL"] == "abc"
    assert os.environ["FROM_BASE_ENV"] == "base"
