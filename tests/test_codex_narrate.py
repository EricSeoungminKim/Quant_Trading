"""Codex 전환: 격리된 텍스트 호출, 실패 폴백, 명시적 비활성화와 계측."""
from __future__ import annotations

import sys

import pytest

from quant.adapters import narrate as N


def test_codex_uses_stdin_and_disables_external_tools():
    seen = {}

    def runner(cmd, stdin, timeout):
        seen.update(cmd=cmd, stdin=stdin, timeout=timeout)
        return " 최종 서술 "

    out = N.CodexCliNarrator("/bin/codex", timeout=23, runner=runner).narrate("제공된 사실")
    assert out == "최종 서술"
    assert seen["stdin"] == "제공된 사실"
    assert seen["timeout"] == 23
    cmd = seen["cmd"]
    assert cmd[:2] == ["/bin/codex", "exec"]
    assert "제공된 사실" not in cmd
    assert "--ephemeral" in cmd and "--ignore-user-config" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert 'approval_policy="never"' in cmd
    assert 'web_search="disabled"' in cmd
    assert "project_doc_max_bytes=0" in cmd
    for feature in ("shell_tool", "apps", "plugins", "multi_agent", "browser_use", "hooks"):
        assert f"features.{feature}=false" in cmd


def test_codex_reads_only_final_file_and_removes_temporary_workspace(monkeypatch):
    from pathlib import Path

    seen = {}

    class Process:
        returncode = 0

        def __init__(self, cmd, **kwargs):
            seen.update(cmd=cmd, **kwargs)
            self.output = Path(cmd[cmd.index("--output-last-message") + 1])

        def communicate(self, input, timeout):
            assert input == "사실" and timeout == 12
            self.output.write_text("최종 답", encoding="utf-8")
            return "진행 상황은 최종 답이 아니다", ""

    monkeypatch.setattr("subprocess.Popen", Process)
    out = N.CodexCliNarrator("codex", timeout=12).narrate("사실")
    assert out == "최종 답"
    assert seen["start_new_session"] is True
    assert seen["cmd"][-1] == "-"
    assert not Path(seen["cwd"]).exists()


@pytest.mark.parametrize("reply", [None, "", "   "])
def test_codex_empty_reply_is_failure(reply):
    assert N.CodexCliNarrator("codex", runner=lambda *a: reply).narrate("p") is None


def test_codex_timeout_is_failure_and_falls_back(monkeypatch):
    import subprocess

    def timeout(*args):
        raise subprocess.TimeoutExpired("codex", 1)

    seen = []
    monkeypatch.setattr(N, "_record_llm_call",
                        lambda lane, ok, seconds, transport: seen.append((ok, transport)))
    primary = N.CodexCliNarrator("codex", runner=timeout)
    fallback = N.CodexCliNarrator("unused", runner=lambda *a: "폴백")
    narrator = N.QualityFallbackNarrator(primary, fallback)
    assert narrator.narrate("p") == "폴백"
    assert narrator.last_transport == "openrouter"
    assert seen == [(False, "codex"), (True, "openrouter")]


def test_quality_respects_none_even_when_codex_exists():
    narrator = N.make_quality_narrator({"OPS_NARRATOR": "none", "CODEX_BIN": sys.executable})
    assert isinstance(narrator, N.NullNarrator)


def test_quality_respects_explicit_openrouter():
    narrator = N.make_quality_narrator({"OPS_NARRATOR": "openrouter", "CODEX_BIN": sys.executable,
                                      "OPENROUTER_API_KEY": "test"})
    assert isinstance(narrator, N.OpenRouterNarrator)


def test_codex_stance_enforces_json_contract_and_records_transport(monkeypatch):
    seen = []
    monkeypatch.setattr(N, "_record_llm_call",
                        lambda lane, ok, seconds, transport: seen.append((ok, transport)))
    monkeypatch.setattr(N.CodexCliNarrator, "_subprocess_runner",
                        staticmethod(lambda *a: '{"stance":"중립","why":"근거 부족"}'))
    assert N.stance_via_codex("p", "codex") == {"stance": "중립", "why": "근거 부족"}
    monkeypatch.setattr(N.CodexCliNarrator, "_subprocess_runner",
                        staticmethod(lambda *a: '```json\n{"stance":"중립","why":"근거 부족"}\n```'))
    assert N.stance_via_codex("p", "codex") is None
    assert seen == [(True, "codex"), (False, "codex")]


def test_codex_primary_failure_rate_uses_subscription_threshold():
    from quant.control.health import ALERT, llm_health_findings

    findings = llm_health_findings({"quality": {"by_transport": {
        "codex": {"total": 10, "failed": 4}, "openrouter": {"total": 10, "failed": 10},
    }}})
    assert len(findings) == 1
    assert findings[0].level == ALERT
    assert "quality/codex" in findings[0].detail


def test_telegram_prose_uses_codex_with_short_timeout(monkeypatch):
    from quant.apps.cli import _narrated_text

    monkeypatch.setenv("OPS_NARRATOR", "codex")
    monkeypatch.setenv("CODEX_BIN", sys.executable)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(N, "get_key", lambda name: None)
    seen = []

    def runner(cmd, prompt, timeout):
        seen.append(timeout)
        return "오늘 체결은 4건이었습니다."

    monkeypatch.setattr(N.CodexCliNarrator, "_subprocess_runner", staticmethod(runner))
    text = _narrated_text("session_pnl", {"n_fills": 4}, "확정 집계", no_narrate=False)
    assert text == "<i>오늘 체결은 4건이었습니다.</i>\n\n확정 집계"
    assert seen == [20]


def test_telegram_none_setting_disables_prose(monkeypatch):
    from quant.apps.cli import _narrate_call

    monkeypatch.setenv("OPS_NARRATOR", "none")
    monkeypatch.setenv("CODEX_BIN", sys.executable)
    assert _narrate_call() is None


def test_json_debate_works_with_codex_only_and_still_rejects_invalid_output(monkeypatch):
    from quant.analyze.ai_trader import run_debate

    narrator = N.make_json_narrator(env={"CODEX_BIN": sys.executable}, timeout=18)
    assert isinstance(narrator._primary, N.CodexCliNarrator)
    assert isinstance(narrator._fallback, N.NullNarrator)
    assert narrator._primary._timeout == 18
    monkeypatch.setattr(N.CodexCliNarrator, "_subprocess_runner",
                        staticmethod(lambda *a: '{"picks": [{"symbol": "005930", "score": 80,'
                                     '"verdict": "pass", "thesis": "제공된 사실"}]}'))
    # 생성자에 바인딩된 runner도 재조립해 실제 팩토리→소비자 계약을 검증한다.
    narrator = N.make_json_narrator(env={"CODEX_BIN": sys.executable}, timeout=18)
    result = run_debate([{"symbol": "005930"}], narrator.narrate)
    assert result is not None and len(result["transcript"]) == 3
    assert result["final"][0]["symbol"] == "005930"
    monkeypatch.setattr(N.CodexCliNarrator, "_subprocess_runner", staticmethod(lambda *a: "invalid json"))
    narrator = N.make_json_narrator(env={"CODEX_BIN": sys.executable})
    assert run_debate([{"symbol": "005930"}], narrator.narrate) is None


def test_json_codex_failure_uses_existing_json_mode_fallback(monkeypatch):
    monkeypatch.setattr(N.CodexCliNarrator, "_subprocess_runner", staticmethod(lambda *a: None))
    narrator = N.make_json_narrator(env={"CODEX_BIN": sys.executable, "OPENROUTER_API_KEY": "test"},
                                   max_tokens=8000, timeout=120)
    assert narrator._fallback._json_mode is True
    assert narrator._fallback._max_tokens == 8000
    assert narrator._fallback._timeout == 20
    monkeypatch.setattr(narrator._fallback, "narrate", lambda prompt: '{"picks": []}')
    assert narrator.narrate("p") == '{"picks": []}'
    assert narrator.last_transport == "openrouter"


def test_json_factory_respects_explicit_none_and_openrouter():
    assert isinstance(N.make_json_narrator(env={"OPS_NARRATOR": "none", "CODEX_BIN": sys.executable}),
                      N.NullNarrator)
    narrator = N.make_json_narrator(env={"OPS_NARRATOR": "openrouter", "CODEX_BIN": sys.executable,
                                       "OPENROUTER_API_KEY": "test"}, timeout=12)
    assert isinstance(narrator, N.OpenRouterNarrator)
    assert narrator._json_mode is True and narrator._timeout == 12


def test_param_propose_none_setting_does_not_reenable_json_fallback(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from quant.analyze import param_proposer
    from quant.apps import cli

    monkeypatch.setenv("OPS_NARRATOR", "none")
    monkeypatch.setattr(cli, "load_settings",
                        lambda: SimpleNamespace(strategies={"test": {"enabled": True, "params": {}}}))
    calls = []
    monkeypatch.setattr(N, "make_json_narrator", lambda **kw: calls.append(kw))

    def propose(review, params, active, narrate):
        assert narrate("제안 요청") is None
        return None

    monkeypatch.setattr(param_proposer, "propose", propose)
    cli.cmd_param_propose(SimpleNamespace(root=str(tmp_path), date="2026-09-13"))
    assert calls == []


def test_param_propose_json_fallback_retains_unexported_file_credentials(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from quant.adapters import env as env_mod
    from quant.analyze import param_proposer
    from quant.apps import cli

    monkeypatch.setattr(cli, "load_settings",
                        lambda: SimpleNamespace(strategies={"test": {"enabled": True, "params": {}}}))
    monkeypatch.setattr(N, "make_narrator", lambda: SimpleNamespace(name="codex", narrate=lambda p: None))
    monkeypatch.setattr(env_mod, "get_key", lambda name: "file-test-key")
    calls = []
    monkeypatch.setattr(N, "make_json_narrator", lambda **kw: calls.append(kw) or N.NullNarrator())

    def propose(review, params, active, narrate):
        assert narrate("제안 요청") is None
        return None

    monkeypatch.setattr(param_proposer, "propose", propose)
    cli.cmd_param_propose(SimpleNamespace(root=str(tmp_path), date="2026-09-13"))
    assert calls[0]["env"]["OPS_NARRATOR"] == "openrouter"
    assert calls[0]["env"]["OPENROUTER_API_KEY"] == "file-test-key"
