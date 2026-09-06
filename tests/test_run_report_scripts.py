"""`run_report.sh`/`run_close_report.sh` 실패 경로 — 빌드 실패 시 알림에 로그
꼬리 3줄을 실어 보낸다(2026-09-07, 리포트 QA 세션). 린트 게이트가 error로
발행을 막아도 "data/report.log 확인"만 보고는 원인을 알 수 없어, `own_brief.sh`
가 하듯 실제 결함을 알림 본문에 바로 담는다.

`$PY`(`.venv/bin/python`)를 fake 스크립트로 바꿔치기한 sandbox 안에서 두
스크립트를 **그대로** 실행한다 — 실제 저장소 `cd`(`server/scripts/run_report.sh`
의 `cd "$(dirname "$0")/../.."`)를 타므로, 진짜 저장소 루트를 절대 침범하지
않도록 스크립트 사본을 임시 디렉터리 트리에 둔다(tests/test_notify_gate.py의
PATH 스텁 관례와 동일한 격리 원칙).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "server" / "scripts"


@pytest.mark.parametrize(
    "name", ["run_report.sh", "run_close_report.sh", "report_accuracy.sh", "report_review_daily.sh"],
)
def test_bash_syntax_ok(name):
    rc = subprocess.run(["bash", "-n", str(SCRIPTS / name)], check=False).returncode
    assert rc == 0, f"{name}: bash -n 실패"


_FAKE_PY = """#!/usr/bin/env bash
# fake python — 인자에 따라 미리 정한 결과만 낸다. 실제 report_cli 를 부르지 않는다.
if [ "$#" -ge 1 ] && [ "$1" = "-" ]; then
  cat >/dev/null   # notify()의 STATUS 인라인 heredoc — 아무 것도 안 낸다
  exit 0
fi
args="$*"
case "$args" in
  *"report_cli when"*)
    exit "${FAKE_WHEN_RC:-0}"
    ;;
  *"report_cli build"*)
    if [ -n "${FAKE_BUILD_STDOUT:-}" ]; then printf '%s\\n' "$FAKE_BUILD_STDOUT"; fi
    if [ -n "${FAKE_BUILD_STDERR:-}" ]; then printf '%s\\n' "$FAKE_BUILD_STDERR" >&2; fi
    exit "${FAKE_BUILD_RC:-0}"
    ;;
  *"report_cli summary"*)
    if [ -n "${FAKE_SUMMARY_STDOUT:-}" ]; then printf '%s\\n' "$FAKE_SUMMARY_STDOUT"; fi
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""


@pytest.fixture
def sandbox(tmp_path: Path):
    """`run_report.sh`/`run_close_report.sh` 를 진짜 저장소 밖 임시 트리에 두고
    돌리는 러너 — `.venv/bin/python`은 fake, `curl`은 PATH 스텁으로 가로챈다."""
    repo = tmp_path / "repo"
    (repo / "server" / "scripts").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "bin").mkdir()

    for name in ("run_report.sh", "run_close_report.sh"):
        src = SCRIPTS / name
        dst = repo / "server" / "scripts" / name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        dst.chmod(0o755)

    fake_py = repo / ".venv" / "bin" / "python"
    fake_py.write_text(_FAKE_PY, encoding="utf-8")
    fake_py.chmod(0o755)

    # 실패 알림 본문은 여러 줄(로그 꼬리 3줄)이라 curl 호출 하나가 `$*` 안에
    # 개행을 여러 개 담는다 — 단순 줄 단위 분리로는 호출 하나를 여럿으로
    # 쪼개 세게 된다. 호출 사이에 전용 구분자를 찍어 나중에 그 구분자로만
    # 나눈다(test_notify_gate.py 의 curl 스텁 관례를 다중행 본문에 맞게 확장).
    curl_log = tmp_path / "curl.log"
    curl = repo / "bin" / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "printf '===CURL_CALL===\\n%s\\n' \"$*\" >> \"$CURL_LOG\"\n"
        "printf '{\"ok\":true}'\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    (repo / ".env.local").write_text(
        "TELEGRAM_BOT_TOKEN=TESTTOKEN\nTELEGRAM_CHAT_ID=12345\n", encoding="utf-8",
    )

    class Sandbox:
        root = repo
        curl_log_path = curl_log

        def run(self, script: str, market: str = "KR", **env_extra) -> subprocess.CompletedProcess:
            env = {
                "PATH": f"{repo / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "HOME": str(tmp_path),
                "LANG": "en_US.UTF-8",
                "TZ": "Asia/Seoul",  # date +%z 가드가 +0900 을 요구한다
                "CURL_LOG": str(curl_log),
                "REPORT_MAX_WAIT": "0",  # when 이 뭔가 내더라도 대기하지 않는다
            }
            env.update({k: str(v) for k, v in env_extra.items()})
            return subprocess.run(
                ["bash", str(repo / "server" / "scripts" / script), market],
                env=env, capture_output=True, text=True, check=False, cwd=str(repo),
            )

        def sends(self) -> list[str]:
            """curl 호출 하나당 문자열 하나(개행 포함 가능) — `===CURL_CALL===`
            구분자로 나눈다."""
            if not curl_log.exists():
                return []
            raw = curl_log.read_text(encoding="utf-8")
            parts = raw.split("===CURL_CALL===\n")
            return [p.rstrip("\n") for p in parts if p.strip()]

    return Sandbox()


@pytest.mark.parametrize("script,market", [("run_report.sh", "KR"), ("run_close_report.sh", "KR")])
def test_build_failure_alert_includes_last_log_lines(sandbox, script, market):
    r = sandbox.run(
        script, market=market,
        FAKE_BUILD_RC="1",
        FAKE_BUILD_STDERR="리포트 린트 오류 1건 — 발행 중단:\n  [error] candidates: 후보 완전 공백\n(첫 건 요약)",
    )
    assert r.returncode == 1, r.stderr
    sends = sandbox.sends()
    assert len(sends) == 1, f"실패 알림이 정확히 1건 나가야 한다: {sends}"
    sent_text = sends[0]
    assert "리포트 생성 실패" in sent_text or "마감 리포트 생성 실패" in sent_text
    assert "최근 로그 3줄" in sent_text
    assert "후보 완전 공백" in sent_text, "빌드 stderr(린트 결함)가 알림 본문에 그대로 보여야 한다"


@pytest.mark.parametrize("script,market", [("run_report.sh", "KR"), ("run_close_report.sh", "KR")])
def test_build_success_alert_has_no_log_tail_section(sandbox, script, market):
    """성공 시에는 실패 경로의 로그 꼬리 섹션이 섞여 들어가면 안 된다."""
    r = sandbox.run(script, market=market, FAKE_BUILD_RC="0")
    assert r.returncode == 0, r.stderr
    sends = sandbox.sends()
    assert len(sends) == 1
    assert "최근 로그 3줄" not in sends[0]


@pytest.mark.parametrize("script,market", [("run_report.sh", "KR"), ("run_close_report.sh", "KR")])
def test_holiday_skip_sends_no_alert(sandbox, script, market):
    """report_cli의 EXIT_SKIPPED(3) — 휴장일은 실패가 아니다, 알림 없이 조용히 종료."""
    r = sandbox.run(script, market=market, FAKE_BUILD_RC="3")
    assert r.returncode == 0, r.stderr
    assert sandbox.sends() == []


def test_report_lint_gate_env_reaches_the_build_subprocess(sandbox):
    """`REPORT_LINT_GATE=warn ./server/scripts/run_report.sh KR` 처럼 호출부
    환경변수로 준 값이 `$PY -m quant.apps.report_cli build` 하위 프로세스까지
    전달돼야 한다(빠른 재발행 절차의 전제) — run_report.sh 는 이 값을 읽거나
    가공하지 않고 그냥 상속만 시키면 된다(자식 프로세스는 부모 env 를 자동
    상속하므로, 스크립트가 `env -i`류로 지우지만 않으면 된다)."""
    marker = Path(sandbox.root) / "data" / "saw_lint_gate_env.txt"
    fake_py = sandbox.root / ".venv" / "bin" / "python"
    fake_py.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$#" -ge 1 ] && [ "$1" = "-" ]; then cat >/dev/null; exit 0; fi\n'
        'case "$*" in\n'
        '  *"report_cli build"*)\n'
        f'    printf \'%s\' "${{REPORT_LINT_GATE:-<unset>}}" > "{marker}"\n'
        "    exit 0\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_py.chmod(0o755)
    r = sandbox.run("run_report.sh", market="KR", REPORT_LINT_GATE="warn")
    assert r.returncode == 0, r.stderr
    assert marker.exists(), "build 하위 프로세스가 아예 호출되지 않았다"
    assert marker.read_text(encoding="utf-8") == "warn"
