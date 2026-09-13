"""Telegram Codex migration: read-only invocation, final output and safe failures."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "server" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tg_bridge  # noqa: E402


def _fake_codex(tmp_path, monkeypatch, body):
    executable = tmp_path / "codex"
    executable.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n{body}\n")
    executable.chmod(0o700)
    monkeypatch.setenv("CODEX_BIN", str(executable))


@pytest.mark.parametrize("binary", ["", "   "])
def test_blank_codex_binary_uses_path_default(tmp_path, monkeypatch, binary):
    monkeypatch.setenv("CODEX_BIN", binary)
    assert tg_bridge.build_codex_argv(tmp_path / "reply.txt")[0] == "codex"


def test_bridge_enables_read_tool_host_without_relaxing_permissions(tmp_path):
    args = tg_bridge.build_codex_argv(tmp_path / "reply.txt")
    assert "features.code_mode_host=true" in args
    assert "features.code_mode_host=false" not in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("--ask-for-approval") + 1] == "never"
    assert "--ignore-user-config" in args
    assert 'web_search="disabled"' in args
    for feature in ("apps", "plugins", "multi_agent", "browser_use", "computer_use",
                    "image_generation", "view_image", "code_mode", "hooks", "memories"):
        assert f"features.{feature}=false" in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args


@pytest.mark.parametrize("platform,landlock", [("linux", True), ("darwin", False)])
def test_bridge_uses_landlock_only_on_linux(tmp_path, monkeypatch, platform, landlock):
    monkeypatch.setattr(tg_bridge.sys, "platform", platform)
    args = tg_bridge.build_codex_argv(tmp_path / "reply.txt")
    assert ("features.use_legacy_landlock=true" in args) is landlock
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("--ask-for-approval") + 1] == "never"
    assert "features.code_mode_host=true" in args


def test_codex_reads_stdin_and_returns_final_message_only(tmp_path, monkeypatch):
    _fake_codex(tmp_path, monkeypatch, """
args = sys.argv[1:]
assert args[:3] == ['--ask-for-approval', 'never', 'exec']
assert args[args.index('--sandbox') + 1] == 'read-only'
assert '--ephemeral' in args
assert '--model' not in args and 'resume' not in args
assert '--dangerously-bypass-approvals-and-sandbox' not in args
assert sys.stdin.read() == '소유자 질문'
assert args[-1] == '-'
print('internal events and tool output are not a Telegram reply')
Path(args[args.index('--output-last-message') + 1]).write_text('최종 답변')
""")

    assert tg_bridge.run_codex("소유자 질문", cwd=tmp_path) == (True, "최종 답변")


@pytest.mark.parametrize("body", ["pass", "Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('  ')"])
def test_codex_success_without_final_message_is_failure(tmp_path, monkeypatch, body):
    _fake_codex(tmp_path, monkeypatch, body)
    ok, message = tg_bridge.run_codex("질문", cwd=tmp_path)
    assert not ok
    assert "최종 응답이 비어" in message


def test_codex_failure_redacts_diagnostics_and_does_not_retry(tmp_path, monkeypatch, capsys):
    secret = "test-secret-long-enough-to-redact"
    monkeypatch.setenv("EXAMPLE_API_KEY", secret)
    _fake_codex(tmp_path, monkeypatch, f"""
with Path('calls').open('a') as fh:
    fh.write('called\\n')
print('authentication failure {secret}', file=sys.stderr)
sys.exit(1)
""")
    ok, message = tg_bridge.run_codex("질문", cwd=tmp_path)
    assert not ok and "Codex 종료 코드 1" in message
    assert secret not in message
    assert secret not in capsys.readouterr().out
    assert (tmp_path / "calls").read_text().splitlines() == ["called"]


def test_codex_missing_binary_explains_installation_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_BIN", str(tmp_path / "missing-codex"))
    ok, message = tg_bridge.run_codex("질문", cwd=tmp_path)
    assert not ok
    assert "FileNotFoundError" in message and "설치와 로그인" in message


def test_codex_timeout_kills_child_process_group(monkeypatch, tmp_path):
    class HungProcess:
        pid = 12345
        calls = 0

        def communicate(self, prompt=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("codex", timeout)
            return None, ""

    proc = HungProcess()
    killed = []

    def popen(argv, **kwargs):
        assert kwargs["start_new_session"] is True
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    ok, message = tg_bridge.run_codex("질문", cwd=tmp_path)
    assert not ok and "시간 초과" in message
    assert killed == [(12345, signal.SIGKILL)]
    assert proc.calls == 2


def test_reply_context_is_bounded_and_secret_reading_is_forbidden():
    prompt = tg_bridge.build_codex_prompt("이유는?", "x" * 5000)
    assert "x" * tg_bridge.REPLY_MAX_CHARS in prompt
    assert "x" * (tg_bridge.REPLY_MAX_CHARS + 1) not in prompt
    assert "시크릿 값은 읽거나 출력하지 마라" in prompt
    assert prompt.endswith("[오너 질문]\n이유는?")


def test_log_redacts_telegram_tokens(capsys):
    token = "123456789:abcdefghijklmnopqrstuvwxyz123456789"
    tg_bridge.log(f"failed https://api.telegram.org/bot{token}/getUpdates")
    output = capsys.readouterr().out
    assert token not in output and "<REDACTED>" in output


def test_polling_failure_logs_status_without_url(monkeypatch, tmp_path, capsys):
    token = "123456789:abcdefghijklmnopqrstuvwxyz123456789"

    class FailedTelegram:
        def __init__(self, _token):
            pass

        def get_updates(self, offset):
            request = httpx.Request("GET", f"https://api.telegram.org/bot{token}/getUpdates")
            response = httpx.Response(502, request=request)
            response.raise_for_status()

    monkeypatch.setattr(tg_bridge, "get_config", lambda: (token, 7))
    monkeypatch.setattr(tg_bridge, "TelegramClient", FailedTelegram)
    monkeypatch.setattr(tg_bridge, "get_toss_client", lambda: None)
    monkeypatch.setattr(tg_bridge, "load_offset", lambda: None)
    monkeypatch.setattr(tg_bridge, "TradingControl", lambda **kwargs: None)
    monkeypatch.setattr(tg_bridge, "_shutdown", False)
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(tg_bridge.time, "sleep", lambda seconds: setattr(tg_bridge, "_shutdown", True))
    tg_bridge.main()
    output = capsys.readouterr().out
    assert "HTTPStatusError HTTP 502" in output
    assert token not in output and "api.telegram.org" not in output
