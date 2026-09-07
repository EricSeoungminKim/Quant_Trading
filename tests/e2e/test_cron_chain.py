"""서버 크론 체인 — 새벽부터 마감까지 이어지는 `server/scripts/*.sh`가 실제로
안전하게 실행되는지 셸 레벨에서 검증한다 (2026-09-07 크론체인 전수 시뮬레이션).

**이 파일은 기본 실행에서 빠진다** — `QT_E2E=1`일 때만 돈다(느리고, 서브프로세스로
실제 셸 스크립트를 띄운다). 다른 pytest 파일은 파이썬 함수를 직접 부르지만, 이
스크립트들의 진짜 함정(데드라인 자릿수 8진수 해석, cwd 가정, notify.sh 게이트,
텔레그램 curl 하드코딩)은 셸을 실제로 실행해야만 재현된다 — `tests/
test_notify_gate.py`와 같은 이유.

## 무엇을 흉내내는가

- **텔레그램**: `curl`을 PATH로 가로챈다(`tests/test_notify_gate.py`와 동일 패턴).
  실제 네트워크에 닿지 않고, 호출을 그대로 로그에 남긴다 — 몇 번 보냈는지,
  중복 발송인지를 잰다.
- **systemd**: 이 저장소는 EC2(systemd)에서만 돈다. `systemctl`/`journalctl`/
  `sudo`를 가짜로 깔아 macOS/CI에서도 watchdog.sh의 서비스 다운 분기를 지날 수
  있게 한다(항상 "active"로 응답 — 이 테스트의 목적은 서비스 생사 판정이 아니라
  그 다음 단계인 하트비트 신선도·중복발송 로직이다).
- **`.env.local`은 절대 만들지 않는다** — 토큰이 없으면 셋 다 조용히 성공한다는
  계약(`notify.sh` 자체 문서화) 그대로, 실제 텔레그램 자격증명이 실수로 섞여
  들어갈 길을 원천 차단한다. 대신 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
  **환경변수**를 심어 notify.sh 경로(대부분의 스크립트)가 실제로 발송을
  시도하게 만들고, 가짜 curl이 그걸 잡는다.

## 이 파일이 잡는 것 — 왜 이 6개인가

`server/crontab.txt` KR 체인 중 로컬에서 네트워크(야후/DART/OpenRouter) 없이도
의미 있게 돌릴 수 있는 6개: watchdog(데드맨 스위치) · ops_watch(운영 감시) ·
trade_review(매매 리뷰 — 2026-09-07 중복발송 결함 회귀 테스트) · capital_review ·
pnl_attribution · daily_wrap. 나머지(own_brief/promotion_debate/flow_scan/
report_accuracy/deepdive 등)는 실시세(Toss)·LLM·yfinance 네트워크가 있어야
의미 있는 산출물이 나오므로 이 파일의 범위가 아니다 — 수동 시뮬레이션 결과는
`docs/runbooks/cron-chain.md`에 있다.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("QT_E2E") != "1", reason="QT_E2E=1일 때만 돈다(느림, 서브프로세스)"),
]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_exec(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_stub_bin(bindir: Path) -> None:
    """curl/systemctl/journalctl/sudo/stat 가짜 — 클래스 docstring 참고."""
    bindir.mkdir(parents=True, exist_ok=True)
    _write_exec(bindir / "curl", """#!/usr/bin/env bash
LOG="${CURL_LOG:-/dev/null}"
{ printf 'CALL:'; for a in "$@"; do printf ' %q' "$a"; done; printf '\\n'; } >> "$LOG"
printf '{"ok":true,"result":{"message_id":1}}'
exit 0
""")
    _write_exec(bindir / "systemctl", """#!/usr/bin/env bash
case "$1" in
  is-active) exit 0 ;;
  *) exit 0 ;;
esac
""")
    _write_exec(bindir / "journalctl", "#!/usr/bin/env bash\nexit 0\n")
    _write_exec(bindir / "sudo", "#!/usr/bin/env bash\nexec \"$@\"\n")
    # GNU stat 셈 — 이 저장소 스크립트는 EC2(GNU coreutils) 기준 `stat -c %Y`를
    # 쓴다. macOS(BSD stat)는 -c 자체가 없어 그대로 두면 항상 실패→0(epoch)으로
    # 떨어져 "무조건 낡음"이 되므로, 이 한 형태만 BSD 등가로 옮긴다.
    _write_exec(bindir / "stat", """#!/usr/bin/env bash
if [ "$1" = "-c" ] && [ "$2" = "%Y" ]; then
  shift 2
  exec /usr/bin/stat -f '%m' "$@"
fi
exec /usr/bin/stat "$@"
""")


@pytest.fixture
def sandbox(tmp_path: Path):
    """샌드박스 저장소 루트 — server/quant/.venv/config/pyproject/uv.lock 는
    실제 저장소를 심볼릭 링크로 참조(가볍고, 이 테스트가 실제 배선을 검증하게
    한다), data/는 이 테스트 전용 빈 트리. **EC2도, 이 저장소의 실제 data/도
    절대 건드리지 않는다.**
    """
    root = tmp_path / "sandbox"
    root.mkdir()
    for name in ("server", "quant", ".venv", "config", "pyproject.toml", "uv.lock"):
        src = REPO_ROOT / name
        if src.exists():
            (root / name).symlink_to(src)
    for d in ("data/state", "data/ledger", "data/public", "out", "home"):
        (root / d).mkdir(parents=True, exist_ok=True)

    bindir = tmp_path / "bin"
    _make_stub_bin(bindir)
    curl_log = tmp_path / "curl.log"
    curl_log.write_text("", encoding="utf-8")

    env = {
        # **/opt/homebrew/bin 을 /usr/bin 보다 앞에 둔다.** macOS 기본 `/bin/bash`·
        # `/usr/bin/bash`는 GPLv3 회피로 멈춰 있는 3.2.57이고, 이 버전은 `set -u`
        # 아래서 **빈 배열**을 `"${arr[@]}"`로 펼치면 "unbound variable"로 죽는다
        # (4.4+에서 고쳐졌다). `notify.sh`의 `_notify_send`가 정확히 이 패턴
        # (`thread_args=()`)을 쓴다 — 2026-09-07 이 테스트를 만들며 실측: 이
        # PATH 순서가 아니면 오프아워 발송 경로가 macOS에서 매번 조용히
        # 실패(발송 실패 원장에 기록)한다. EC2(Ubuntu)는 bash 5 라 이 함정에
        # 안 걸리지만, 오너가 로컬(Mac)에서도 paper 루프를 돌리므로
        # (`docs/vault/` 로컬 실행 메모) 실제로 재현되는 문제다 —
        # `docs/runbooks/cron-chain.md` "발견한 구멍" 1번 참고.
        "PATH": f"{bindir}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(root / "home"),
        "LANG": "en_US.UTF-8",
        "TZ": "Asia/Seoul",
        # notify.sh 가 진짜 저장소의 .env.local 을 절대 읽지 않게 한다.
        "NOTIFY_ENV_FILE": "/dev/null",
        "NOTIFY_QUEUE": str(root / "data" / "notify_queue.jsonl"),
        "NOTIFY_LANES_FILE": str(root / "data" / "state" / "tg_lanes.json"),
        "NOTIFY_FAILURE_LEDGER": str(root / "data" / "ledger" / "notify_failures.jsonl"),
        "NOTIFY_SENT_LEDGER": str(root / "data" / "ledger" / "notify_sent.jsonl"),
        "NOTIFY_RATE_DIR": str(root / "data" / "state"),
        "NOTIFY_RATE_SLEEP": "0",
        "TELEGRAM_BOT_TOKEN": "SANDBOXTOKEN",
        "TELEGRAM_CHAT_ID": "999999",
        "TELEGRAM_API_BASE": "http://127.0.0.1:1",
        "CURL_LOG": str(curl_log),
        "OPS_NARRATOR": "none",
        "OPENROUTER_API_KEY": "",
        "REPORT_MAX_WAIT": "0",
    }

    class Sandbox:
        path = root
        curl_log_path = curl_log

        def run(self, script: str, *args: str, extra_env: dict | None = None, timeout: int = 60):
            e = dict(env)
            if extra_env:
                e.update(extra_env)
            # **반드시 샌드박스 안의 심볼릭 링크 경로로 부른다** — 모든 스크립트가
            # `cd "$(dirname "$0")/../.."`로 저장소 루트를 찾는다. 진짜 저장소
            # 경로(REPO_ROOT/server/scripts/...)로 부르면 dirname이 진짜
            # 저장소로 풀려 샌드박스가 아니라 실제 data/를 건드린다 — 실측으로
            # 확인된 함정이라 여기 남긴다.
            return subprocess.run(
                [str(root / "server" / "scripts" / script), *args],
                cwd=root,
                env=e,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

        def curl_calls(self) -> list[str]:
            if not self.curl_log_path.exists():
                return []
            return [l for l in self.curl_log_path.read_text(encoding="utf-8").splitlines() if l]

    return Sandbox()


# ── 1. 개별 스크립트가 빈 원장에서도 안전하게 도는가 ────────────────────────

def test_watchdog_quiet_when_engine_healthy(sandbox):
    """서비스 up + 신선한 하트비트 → 아무 알림도 없이 exit 0."""
    hb = sandbox.path / "data" / "state" / "heartbeat.json"
    hb.write_text("{}", encoding="utf-8")
    result = sandbox.run("watchdog.sh")
    assert result.returncode == 0, result.stderr
    assert sandbox.curl_calls() == []


def test_ops_watch_exit_code_is_within_contract(sandbox):
    """cli health 의 판정 계약은 0(정상)/1(alert)/2(unknown) 뿐이다 — 그 밖의
    코드(스크립트 자체 크래시)가 나오면 계약 위반."""
    result = sandbox.run("ops_watch.sh")
    assert result.returncode in (0, 1, 2), (result.returncode, result.stdout, result.stderr)


def test_capital_review_quiet_on_empty_ledger(sandbox):
    result = sandbox.run("capital_review.sh")
    assert result.returncode == 0, result.stderr
    assert sandbox.curl_calls() == []  # 강등 없음 → 침묵


def test_pnl_attribution_quiet_on_empty_ledger(sandbox):
    result = sandbox.run("pnl_attribution.sh", "KR")
    assert result.returncode == 0, result.stderr


def test_daily_wrap_renders_without_crashing(sandbox):
    """토큰이 없으면(.env.local 부재) sendDocument 를 건너뛴다 — 렌더 자체는
    죽지 않아야 한다."""
    result = sandbox.run("daily_wrap.sh", "KR")
    assert result.returncode == 0, result.stderr


# ── 2. 중복 발송 회귀 테스트 (2026-09-07 실측 결함 → server/scripts/trade_review.sh 수정) ──

def _stub_report_cli_trade_review(sandbox, text: str) -> None:
    """`.venv/bin/python -m quant.apps.report_cli trade-review ...` 호출만
    가로채 고정 텍스트를 stdout 으로 낸다 — 실제 원장 스키마를 몰라도 이
    테스트가 검증하려는 것(스크립트의 중복발송 방지 로직)과 무관하게 report_cli
    내부 계산을 재현할 필요가 없다. 그 외 호출(`--help` 등)은 실제 venv 파이썬
    으로 통과시킨다.
    """
    real_python = (REPO_ROOT / ".venv" / "bin" / "python").resolve()
    fake_venv = sandbox.path / "fake_venv" / "bin"
    fake_venv.mkdir(parents=True, exist_ok=True)
    _write_exec(fake_venv / "python", f"""#!/usr/bin/env bash
case "$*" in
  *"report_cli trade-review"*) printf '%s\\n' {text!r}; exit 0 ;;
esac
exec {real_python} "$@"
""")
    # 스크립트가 `.venv/bin/python` 상대경로로 부르므로, cwd 안의 .venv 심볼릭
    # 링크를 이 fake_venv 로 교체한다.
    (sandbox.path / ".venv").unlink()
    (sandbox.path / ".venv").symlink_to(sandbox.path / "fake_venv")


def test_trade_review_does_not_double_send_on_rerun(sandbox):
    """2026-09-07 크론체인 시뮬레이션 실측: trade_review.sh 를 같은 (시장,날짜)로
    두 번 부르면 동일 카드가 텔레그램에 두 번 나갔다 — report_cli 자체가 순수
    재계산이라 "오늘 이미 보냈다"를 모르기 때문. server/scripts/trade_review.sh
    에 마커 파일 가드를 추가해 고쳤다(이 테스트가 그 회귀를 지킨다)."""
    _stub_report_cli_trade_review(sandbox, "매매 리뷰 카드 — 고정 텍스트")

    r1 = sandbox.run("trade_review.sh", "KR")
    assert r1.returncode == 0, r1.stderr
    assert len(sandbox.curl_calls()) == 1, "첫 실행은 발송 1건이어야 한다"

    r2 = sandbox.run("trade_review.sh", "KR")
    assert r2.returncode == 0, r2.stderr
    assert len(sandbox.curl_calls()) == 1, (
        "같은 날 두 번째 실행은 마커 파일 때문에 재발송하면 안 된다 "
        "(고치기 전엔 여기서 2건이 됐다)"
    )

    marker = sandbox.path / "data" / "state" / "trade_review_sent_KR.txt"
    assert marker.exists()


def test_trade_review_resends_on_new_day(sandbox):
    """마커가 날짜 키라 다음 날은 다시 보낸다 — 영구 침묵이 아니다."""
    _stub_report_cli_trade_review(sandbox, "매매 리뷰 카드")
    r1 = sandbox.run("trade_review.sh", "KR")
    assert r1.returncode == 0
    assert len(sandbox.curl_calls()) == 1

    marker = sandbox.path / "data" / "state" / "trade_review_sent_KR.txt"
    marker.write_text("2000-01-01", encoding="utf-8")  # 과거 날짜로 위조

    r2 = sandbox.run("trade_review.sh", "KR")
    assert r2.returncode == 0
    assert len(sandbox.curl_calls()) == 2, "날짜가 바뀌었으면 다시 보내야 한다"
