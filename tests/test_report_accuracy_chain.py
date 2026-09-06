"""`report_accuracy.sh` → `report_review_daily.sh` 체인 — "할 일이 없는 날"
(원장 없음/빈 원장 + `.env.local` 없음)에도 죽지 않고, 정확도 채점이
실패해도 회고 카드 체인은 여전히 시도해야 한다는 계약(2026-09-07, 리포트
QA 세션).

**발견한 결함과 수정**: `report_accuracy.sh`는 `report accuracy`(청구 원장이
아예 없으면 exit 1을 낸다 — `quant/apps/report_cli.py::cmd_accuracy`, 이
파일 소유 범위 밖)가 실패하면 `exit "$RC"`로 먼저 빠져나가, 바로 아래
"이 파일의 성패와 무관하게 항상 시도한다"고 주석이 약속한
`report_review_daily.sh` 체인 호출에 **도달하지 못했다**. 이 파일이 그
순서를 고쳐 체인이 항상 실행되게 한다(정확도 채점 결과와 무관하게).

`.venv/bin/python`을 fake 로 바꿔치기한 sandbox 안에서 실제 스크립트를
돌린다 — `tests/test_run_report_scripts.py`와 같은 격리 원칙.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "server" / "scripts"

_FAKE_PY = """#!/usr/bin/env bash
LOGFILE="${FAKE_PY_CALL_LOG:-/dev/null}"
printf '%s\\n' "$*" >> "$LOGFILE"
case "$*" in
  *"report_cli accuracy"*)
    if [ -n "${FAKE_ACCURACY_STDERR:-}" ]; then printf '%s\\n' "$FAKE_ACCURACY_STDERR" >&2; fi
    if [ -n "${FAKE_ACCURACY_STDOUT:-}" ]; then printf '%s\\n' "$FAKE_ACCURACY_STDOUT"; fi
    exit "${FAKE_ACCURACY_RC:-0}"
    ;;
  *"report_cli review-daily --weekly"*)
    if [ -n "${FAKE_WEEKLY_STDOUT:-}" ]; then printf '%s\\n' "$FAKE_WEEKLY_STDOUT"; fi
    exit "${FAKE_WEEKLY_RC:-0}"
    ;;
  *"report_cli review-daily"*)
    if [ -n "${FAKE_REVIEW_DAILY_STDERR:-}" ]; then printf '%s\\n' "$FAKE_REVIEW_DAILY_STDERR" >&2; fi
    exit "${FAKE_REVIEW_DAILY_RC:-0}"
    ;;
  *)
    exit 0
    ;;
esac
"""


@pytest.fixture
def sandbox(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "server" / "scripts" / "lib").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "bin").mkdir()

    for name in ("report_accuracy.sh", "report_review_daily.sh"):
        src = SCRIPTS / name
        dst = repo / "server" / "scripts" / name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        dst.chmod(0o755)
    notify_src = SCRIPTS / "lib" / "notify.sh"
    notify_dst = repo / "server" / "scripts" / "lib" / "notify.sh"
    notify_dst.write_text(notify_src.read_text(encoding="utf-8"), encoding="utf-8")

    fake_py = repo / ".venv" / "bin" / "python"
    fake_py.write_text(_FAKE_PY, encoding="utf-8")
    fake_py.chmod(0o755)

    curl_log = tmp_path / "curl.log"
    call_log = tmp_path / "py_calls.log"
    curl = repo / "bin" / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "printf '===CURL_CALL===\\n%s\\n' \"$*\" >> \"$CURL_LOG\"\n"
        "printf '{\"ok\":true}'\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    # 의도적으로 .env.local 을 만들지 않는다 — "missing .env.local(토큰 없음)"
    # 시나리오 그 자체가 이 테스트의 전제다.

    class Sandbox:
        root = repo

        def run(self, script: str, *args: str, **env_extra) -> subprocess.CompletedProcess:
            env = {
                "PATH": f"{repo / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "HOME": str(tmp_path),
                "LANG": "en_US.UTF-8",
                "TZ": "Asia/Seoul",
                "CURL_LOG": str(curl_log),
                "FAKE_PY_CALL_LOG": str(call_log),
                # notify.sh 가 장중이면 큐로 미루므로, 결정론적으로 "장외"를 강제한다
                # (이 테스트는 발송 여부가 아니라 체인 실행 여부가 관심사다).
                "NOTIFY_NOW_HHMM": "0640", "NOTIFY_NOW_DOW": "2",
            }
            env.update({k: str(v) for k, v in env_extra.items()})
            return subprocess.run(
                ["bash", str(repo / "server" / "scripts" / script), *args],
                env=env, capture_output=True, text=True, check=False, cwd=str(repo),
            )

        def sends(self) -> list[str]:
            if not curl_log.exists():
                return []
            parts = curl_log.read_text(encoding="utf-8").split("===CURL_CALL===\n")
            return [p.rstrip("\n") for p in parts if p.strip()]

        def py_calls(self) -> list[str]:
            if not call_log.exists():
                return []
            return [l for l in call_log.read_text(encoding="utf-8").splitlines() if l]

        def review_log(self) -> str:
            p = repo / "data" / "report_review.log"
            return p.read_text(encoding="utf-8") if p.exists() else ""

    return Sandbox()


def test_bash_syntax_ok():
    for name in ("report_accuracy.sh", "report_review_daily.sh"):
        rc = subprocess.run(["bash", "-n", str(SCRIPTS / name)], check=False).returncode
        assert rc == 0, f"{name}: bash -n 실패"


def test_absent_claims_ledger_still_chains_to_review_daily(sandbox):
    """청구 원장이 아예 없는 날(신선 배포/빈 상태) — `cmd_accuracy`가 exit 1을
    내더라도, 회고 카드 체인은 여전히 시도돼야 한다(주석이 약속한 계약)."""
    r = sandbox.run(
        "report_accuracy.sh",
        FAKE_ACCURACY_RC="1",
        FAKE_ACCURACY_STDERR="리포트 청구 원장이 없습니다 — 먼저 리포트를 빌드해야 합니다",
    )
    assert r.returncode == 1, r.stderr  # 정확도 채점 실패는 여전히 exit 코드로 드러난다
    calls = sandbox.py_calls()
    assert any("report_cli accuracy" in c for c in calls)
    assert any("report_cli review-daily" in c for c in calls), (
        "정확도 채점이 실패해도 review-daily 체인은 항상 시도돼야 한다 — 현재 호출 목록: "
        f"{calls}"
    )


def test_empty_claims_in_range_exits_zero_and_still_chains(sandbox):
    """청구 원장은 있지만 그 구간에 채점할 게 없는 날 — `cmd_accuracy`가 이미
    exit 0 + 안내 문구를 낸다(quant/apps/report_cli.py, 이 파일 소유 범위
    밖). report_accuracy.sh 는 이를 그대로 exit 0으로 전달하고 체인도 탄다."""
    r = sandbox.run(
        "report_accuracy.sh",
        FAKE_ACCURACY_RC="0",
        FAKE_ACCURACY_STDOUT="30일~오늘 구간에 채점할 청구가 없습니다",
    )
    assert r.returncode == 0, r.stderr
    calls = sandbox.py_calls()
    assert any("report_cli review-daily" in c for c in calls)


def test_missing_env_local_sends_nothing_regardless_of_ledger_state(sandbox):
    """.env.local 이 없으면(텔레그램 토큰 공백) 원장 상태와 무관하게 텔레그램
    전송 시도 자체가 없어야 한다(notify.sh 의 무토큰 조용한 성공 계약)."""
    for rc in ("0", "1"):
        r = sandbox.run("report_accuracy.sh", FAKE_ACCURACY_RC=rc)
        assert r.returncode == int(rc)
    assert sandbox.sends() == [], "토큰이 없으면 curl 호출 자체가 없어야 한다"


def test_review_daily_failures_are_isolated_from_accuracy_exit_code(sandbox):
    """review-daily 쪽이 전부 실패해도(`|| true`) report_accuracy.sh 자체의
    종료 코드(정확도 채점 결과)에 영향을 주면 안 된다."""
    r = sandbox.run(
        "report_accuracy.sh", FAKE_ACCURACY_RC="0",
        FAKE_REVIEW_DAILY_RC="1", FAKE_WEEKLY_RC="1",
    )
    assert r.returncode == 0


def test_weekly_mode_with_empty_ledger_exits_quietly(sandbox):
    """`report_review_daily.sh weekly` — 그 주 원장 행이 없으면(무출력) 조용히
    (알림 없이) exit 0, 로그 한 줄만 남긴다."""
    r = sandbox.run("report_review_daily.sh", "weekly", FAKE_WEEKLY_STDOUT="")
    assert r.returncode == 0, r.stderr
    assert sandbox.sends() == []
    log_lines = [l for l in sandbox.review_log().splitlines() if l]
    assert len(log_lines) == 1
    assert "집계할 행 없음" in log_lines[0]


def test_weekly_mode_failure_exits_zero_quietly(sandbox):
    """review-daily --weekly 자체가 실패해도(rc!=0) 크론을 오염시키지 않고
    조용히 exit 0 — 로그 한 줄만."""
    r = sandbox.run("report_review_daily.sh", "weekly", FAKE_WEEKLY_RC="1")
    assert r.returncode == 0, r.stderr
    assert sandbox.sends() == []
    log_lines = [l for l in sandbox.review_log().splitlines() if l]
    assert len(log_lines) == 1
    assert "실패" in log_lines[0]
