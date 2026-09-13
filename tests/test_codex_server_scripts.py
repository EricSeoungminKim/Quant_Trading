"""서버 Codex 전환: 실제 셸 → 격리한 CLI 스텁 → 기존 인박스 검증기."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "server" / "scripts"


@pytest.fixture
def isolated_server(tmp_path):
    scripts = tmp_path / "server" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("llm_trader.sh", "llm_trader.py", "codex_prompt.py"):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    python_bin = tmp_path / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True)
    python_bin.symlink_to(sys.executable)
    fake_cli = tmp_path / "fake-codex"
    fake_cli.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
import sys
args = sys.argv[1:]
Path(os.environ['TEST_CODEX_CALL']).write_text(json.dumps({'args': args, 'cwd': str(Path.cwd()), 'prompt': sys.stdin.read()}))
print('CLI events must never reach the trade inbox')
if os.environ.get('TEST_CODEX_FAIL'):
    print('do-not-leak-cli-diagnostic', file=sys.stderr)
    sys.exit(1)
Path(args[args.index('--output-last-message') + 1]).write_text(os.environ['TEST_CODEX_RESPONSE'])
""")
    fake_cli.chmod(0o700)
    env = {
        **os.environ,
        "CODEX_BIN": str(fake_cli),
        "CODEX_MODEL": "test-model",
        "TEST_CODEX_CALL": str(tmp_path / "codex-call.json"),
        "TEST_CODEX_RESPONSE": "[]",
        "TZ": "Asia/Seoul",
        "LLM_TRADER_NOW_HHMM": "1000",
        "LLM_TRADER_NOW_DOW": "1",
        "DRY_RUN": "0",
    }
    return tmp_path, env


def _run_trader(root, env):
    return subprocess.run(["bash", str(root / "server/scripts/llm_trader.sh")],
                          cwd=root, env=env, capture_output=True, text=True, timeout=15)


def test_trader_codex_has_only_web_search_and_preserves_order_contract(isolated_server):
    root, env = isolated_server
    env["TEST_CODEX_RESPONSE"] = json.dumps([
        {"action": "buy", "symbol": "005930", "weight": .15, "horizon": "단타", "reason": "검증용"},
        {"action": "buy", "symbol": "000660", "weight": .99, "horizon": "단타", "reason": "상한 밖"},
    ])
    result = _run_trader(root, env)
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in (root / "data/state/llm_trader_inbox.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["symbol"] == "005930" and rows[0]["weight"] == .15
    call = json.loads((root / "codex-call.json").read_text())
    args = call["args"]
    assert 'web_search="live"' in args
    assert 'approval_policy="never"' in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in args and "--ephemeral" in args
    for feature in ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "hooks", "code_mode"):
        assert f"features.{feature}=false" in args
    assert args[args.index("--model") + 1] == "test-model"
    assert call["cwd"] != str(root)
    assert "참고 시세 데이터" in call["prompt"] and "1,000만원 모의계좌" in call["prompt"]


@pytest.mark.parametrize("response,fail", [("not JSON", False), ("[]", False), ("[]", True)])
def test_trader_failure_or_no_trade_does_not_write_inbox(isolated_server, response, fail):
    root, env = isolated_server
    env["TEST_CODEX_RESPONSE"] = response
    if fail:
        env["TEST_CODEX_FAIL"] = "1"
    result = _run_trader(root, env)
    assert result.returncode == 0, result.stderr
    assert not (root / "data/state/llm_trader_inbox.jsonl").exists()
    assert "do-not-leak-cli-diagnostic" not in (root / "data/llm_trader.log").read_text()


def test_batch_prompt_without_web_flag_has_no_tools(isolated_server):
    root, env = isolated_server
    env["TEST_CODEX_RESPONSE"] = "요약"
    result = subprocess.run([sys.executable, str(root / "server/scripts/codex_prompt.py")],
                            input="주어진 사실만 요약", cwd=root, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    assert result.stdout == "요약\n"
    call = json.loads((root / "codex-call.json").read_text())
    assert 'web_search="disabled"' in call["args"]
    assert "features.shell_tool=false" in call["args"]
    assert call["prompt"] == "주어진 사실만 요약"


def test_trader_outside_market_hours_does_not_call_codex(isolated_server):
    root, env = isolated_server
    env["LLM_TRADER_NOW_HHMM"] = "1700"
    assert _run_trader(root, env).returncode == 0
    assert not (root / "codex-call.json").exists()
