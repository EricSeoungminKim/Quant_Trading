#!/usr/bin/env python3
"""stdin → Codex 최종 답변. 서버 배치용: 셸/파일 도구 없이, 선택적으로 웹검색만."""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def run(prompt: str, *, timeout: int, web_search: bool) -> str | None:
    with tempfile.TemporaryDirectory(prefix="quant-codex-") as workdir:
        output = Path(workdir) / "final.txt"
        cmd = [
            os.environ.get("CODEX_BIN") or shutil.which("codex") or str(Path.home() / ".local/bin/codex"),
            "exec", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
            "--sandbox", "read-only", "--color", "never",
            "-c", 'approval_policy="never"',
            "-c", f'web_search="{"live" if web_search else "disabled"}"',
            "-c", "project_doc_max_bytes=0",
        ]
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent",
                        "browser_use", "computer_use", "image_generation", "view_image",
                        "code_mode", "hooks", "memories"):
            cmd += ["-c", f"features.{feature}=false"]
        # 웹검색 실행에도 호스트가 필요하다. 도구 없는 서술 모드는 계속 차단한다.
        cmd += ["-c", f"features.code_mode_host={str(web_search).lower()}"]
        if os.environ.get("CODEX_MODEL"):
            cmd += ["--model", os.environ["CODEX_MODEL"]]
        cmd += ["--cd", workdir, "--output-last-message", str(output), "-"]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True, cwd=workdir, start_new_session=True,
        )
        try:
            proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            print(f"Codex timeout ({timeout}s)", file=sys.stderr)
            return None
        if proc.returncode != 0:
            # CLI stderr에는 프롬프트/인증 정보가 섞일 수 있어 종료 코드만 기록한다.
            print(f"Codex exit={proc.returncode}; check codex login status", file=sys.stderr)
            return None
        answer = output.read_text(encoding="utf-8").strip() if output.exists() else ""
        if not answer:
            print("Codex final response missing", file=sys.stderr)
        return answer or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--web-search", action="store_true")
    args = parser.parse_args()
    try:
        answer = run(sys.stdin.read(), timeout=args.timeout, web_search=args.web_search)
    except OSError as exc:
        print(f"Codex execution failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    if answer is None:
        return 1
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
