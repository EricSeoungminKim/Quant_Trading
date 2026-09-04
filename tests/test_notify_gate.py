"""역할별 텔레그램 게이트 — `server/scripts/lib/notify.sh` (2026-08-28).

소유자 지시: "**매매 관련된 것만 장중에** 보내고, 나머지는 장 끝나고 HTML 파일
하나로." 그 규칙이 실제로 지켜지는지는 셸을 직접 돌려야만 알 수 있다 —
파이썬으로 옮겨 쓰면 이 게이트가 가진 진짜 함정(0으로 채운 시각의 8진수 해석,
`local` 스코프, JSON 이스케이프)이 전부 재현되지 않는다. `curl` 은 PATH 로
가로채 **실제 텔레그램 호출을 절대 하지 않는다**.

여기서 검증하는 계약 네 가지:
  1. 장중이면 notify_auto 는 발송하지 않는다 (큐에만 쌓인다).
  2. notify_defer 는 언제나 큐에만 쌓인다 — 텔레그램은 절대 아니다.
  3. notify_now 는 언제나 발송한다 — 장중이든 아니든.
  4. 토큰이 없으면 셋 다 조용히 성공한다 (로컬·테스트가 이것 때문에 죽지 않는다).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "server" / "scripts" / "lib" / "notify.sh"
SCRIPTS = LIB.parent.parent

# 평일 KR 장중 / 평일 장외(밤 8시).
IN_HOURS = {"NOTIFY_NOW_HHMM": "1000", "NOTIFY_NOW_DOW": "1"}
OFF_HOURS = {"NOTIFY_NOW_HHMM": "2000", "NOTIFY_NOW_DOW": "1"}


@pytest.fixture
def gate(tmp_path: Path):
    """셸 게이트를 돌리는 러너 — curl 은 PATH 스텁으로 가로챈다."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$CURL_LOG"\nprintf \'{"ok":true}\'\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)

    queue = tmp_path / "queue.jsonl"
    curl_log = tmp_path / "curl.log"

    class Gate:
        queue_path = queue
        curl_log_path = curl_log

        def run(self, snippet: str, *, token: bool = True, **env_extra):
            env = {
                "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "HOME": str(tmp_path),
                "LANG": "en_US.UTF-8",
                "CURL_LOG": str(curl_log),
                "NOTIFY_QUEUE": str(queue),
                # 실제 저장소의 .env.local 을 절대 읽지 않게 한다 — 안 그러면
                # 개발자 머신에서 진짜 토큰을 집어 든다.
                "NOTIFY_ENV_FILE": "/dev/null",
                # curl 은 스텁이라 도달하지 않지만, 혹시라도 새면 죽는 주소로.
                "TELEGRAM_API_BASE": "http://127.0.0.1:1",
            }
            if token:
                env["TELEGRAM_BOT_TOKEN"] = "TESTTOKEN"
                env["TELEGRAM_CHAT_ID"] = "12345"
            env.update({k: str(v) for k, v in env_extra.items()})
            return subprocess.run(
                ["bash", "-c", f'. "{LIB}"\n{snippet}'],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        def queued(self) -> list[dict]:
            if not queue.exists():
                return []
            return [json.loads(l) for l in queue.read_text(encoding="utf-8").splitlines() if l]

        def sends(self) -> list[str]:
            if not curl_log.exists():
                return []
            return [l for l in curl_log.read_text(encoding="utf-8").splitlines() if l]

    return Gate()


# ── ① notify_auto 는 장중에 발송하지 않는다 ────────────────────────────────

def test_auto_in_market_hours_queues_and_does_not_send(gate):
    r = gate.run('notify_auto "own_brief" "🤖 자동 편입: 005930"', **IN_HOURS)
    assert r.returncode == 0, r.stderr
    assert gate.sends() == [], "장중에는 텔레그램을 치면 안 된다"
    assert [q["source"] for q in gate.queued()] == ["own_brief"]
    assert gate.queued()[0]["level"] == "auto"


# ── ② notify_auto 는 장외에 발송한다 ──────────────────────────────────────

def test_auto_off_hours_sends_and_does_not_queue(gate):
    r = gate.run('notify_auto "own_brief" "🤖 자동 편입: 005930"', **OFF_HOURS)
    assert r.returncode == 0, r.stderr
    assert len(gate.sends()) == 1
    assert "005930" in gate.sends()[0]
    assert gate.queued() == [], "장외면 미룰 이유가 없다"


# ── ③ notify_defer 는 언제나 큐만 ─────────────────────────────────────────

@pytest.mark.parametrize("clock", [IN_HOURS, OFF_HOURS], ids=["장중", "장외"])
def test_defer_never_sends(gate, clock):
    r = gate.run('notify_defer "backfill_1m" "1분봉 백필 완료"', **clock)
    assert r.returncode == 0, r.stderr
    assert gate.sends() == [], "defer 는 시각과 무관하게 텔레그램으로 나가지 않는다"
    assert gate.queued()[0]["level"] == "defer"


# ── ④ notify_now 는 언제나 발송 ───────────────────────────────────────────

@pytest.mark.parametrize("clock", [IN_HOURS, OFF_HOURS], ids=["장중", "장외"])
def test_now_always_sends(gate, clock):
    r = gate.run('notify_now "🚨 워치독: 엔진 다운"', **clock)
    assert r.returncode == 0, r.stderr
    assert len(gate.sends()) == 1
    assert gate.queued() == [], "긴급 알림은 큐로 미루지 않는다"


def test_now_reports_send_failure(gate, tmp_path):
    """ops_watch.sh 의 `if notify_now ...; then mark; fi` 계약 — 실패를 삼키지 않는다.

    발송 실패를 0 으로 보고하면 ops_watch 가 "이미 알렸다"로 상태를 기록해
    그 이상에 대해 영원히 재시도하지 않는다(과거 실제 결함).
    """
    bad = tmp_path / "bin" / "curl"
    bad.write_text('#!/usr/bin/env bash\nprintf \'{"ok":false}\'\n', encoding="utf-8")
    bad.chmod(0o755)
    r = gate.run('notify_now "🚨 이상" && echo SENT || echo FAILED', **OFF_HOURS)
    assert "FAILED" in r.stdout


# ── ⑤ 토큰이 없으면 셋 다 조용히 성공 ─────────────────────────────────────

@pytest.mark.parametrize(
    "call",
    [
        'notify_now "긴급"',
        'notify_auto "own_brief" "편입"',
        'notify_defer "backfill_1m" "요약"',
    ],
)
@pytest.mark.parametrize("clock", [IN_HOURS, OFF_HOURS], ids=["장중", "장외"])
def test_no_token_is_silent_success(gate, call, clock):
    r = gate.run(call, token=False, **clock)
    assert r.returncode == 0, r.stderr
    assert gate.sends() == [], "토큰이 없으면 발송 시도 자체를 하지 않는다"


# ── ⑥ 큐 JSON 형식 ────────────────────────────────────────────────────────

def test_queue_line_shape(gate):
    gate.run('notify_defer "ops_judge" "판단 워치독"', **IN_HOURS)
    row = gate.queued()[0]
    assert sorted(row) == ["level", "source", "text", "ts"]
    assert row["source"] == "ops_judge"
    assert row["text"] == "판단 워치독"
    assert row["level"] == "defer"
    # ts 는 KST 로컬 + 오프셋 (ISO 8601) — 마감 리포트가 그대로 찍는다.
    assert len(row["ts"]) == 24 and row["ts"][10] == "T"


def test_queue_survives_quotes_newlines_and_tabs(gate):
    """실제 메시지에는 따옴표·역슬래시·개행이 들어온다 — JSON 이 깨지면 리포트가 통째로 못 읽는다."""
    text = 'a "quoted" \\back\n둘째 줄\t탭'
    gate.run(f"notify_defer \"close_report\" '{text}'", **IN_HOURS)
    assert gate.queued()[0]["text"] == text


def test_queue_appends_rather_than_overwrites(gate):
    gate.run('notify_defer "a" "첫째"; notify_defer "b" "둘째"', **IN_HOURS)
    assert [q["source"] for q in gate.queued()] == ["a", "b"]


def test_queue_write_failure_does_not_kill_the_script(gate, tmp_path):
    """큐가 못 써져도 크론이 죽으면 안 된다 — 알림 부가기능이 본 작업을 막지 않는다."""
    blocker = tmp_path / "블로커"      # 디렉토리 자리에 일반 파일 — mkdir 도 append 도 실패한다
    blocker.write_text("", encoding="utf-8")
    r = gate.run(
        'notify_defer "x" "본문"; echo ALIVE',
        NOTIFY_QUEUE=str(blocker / "queue.jsonl"),
        **IN_HOURS,
    )
    assert r.returncode == 0
    assert "ALIVE" in r.stdout


def test_queue_write_failure_is_reported_via_return_code(gate, tmp_path):
    """2026-09-04 수정: 예전엔 큐 쓰기가 실패해도 notify_defer/notify_auto가
    항상 0을 반환해, session_pnl.sh/manual_recs.sh 같은 호출부가 "큐 적재
    성공/실패"를 로그로 구분할 방법이 없었다. 이제 실제 쓰기 결과를 그대로
    반환한다 — 스크립트를 죽이지는 않는다(위 테스트)."""
    blocker = tmp_path / "블로커"
    blocker.write_text("", encoding="utf-8")
    r = gate.run(
        'notify_defer "x" "본문" && echo QUEUED || echo FAILED',
        NOTIFY_QUEUE=str(blocker / "queue.jsonl"),
        **IN_HOURS,
    )
    assert "FAILED" in r.stdout, r.stderr


def test_queue_write_success_is_reported_via_return_code(gate):
    r = gate.run('notify_defer "x" "본문" && echo QUEUED || echo FAILED', **IN_HOURS)
    assert "QUEUED" in r.stdout, r.stderr


# ── notify_auto_would_defer — 로그용 순수 조회(2026-09-04) ─────────────────

def test_would_defer_true_in_market_hours(gate):
    r = gate.run("notify_auto_would_defer && echo DEFER || echo SEND", **IN_HOURS)
    assert "DEFER" in r.stdout, r.stderr


def test_would_defer_false_off_hours(gate):
    r = gate.run("notify_auto_would_defer && echo DEFER || echo SEND", **OFF_HOURS)
    assert "SEND" in r.stdout, r.stderr


def test_would_defer_does_not_itself_send_or_queue(gate):
    """순수 조회다 — 호출만으로 큐에 쌓이거나 텔레그램이 나가면 안 된다."""
    gate.run("notify_auto_would_defer", **IN_HOURS)
    assert gate.sends() == []
    assert gate.queued() == []


# ── ⑦ 장중 판정 — 주말·US 세션 경계 ───────────────────────────────────────

@pytest.mark.parametrize(
    "hhmm,dow,expected,why",
    [
        ("0859", "1", False, "KR 개장 1분 전"),
        ("0900", "1", True, "KR 개장 정각"),
        ("1530", "5", True, "KR 마감 정각(금)"),
        ("1531", "5", False, "KR 마감 직후"),
        ("2229", "1", False, "US 개장 1분 전"),
        ("2230", "1", True, "US 개장 창 시작(월밤)"),
        ("2359", "5", True, "금요일 밤 US 세션"),
        ("0000", "6", True, "토 새벽 = 금요일 밤 세션의 연장"),
        ("0600", "6", True, "US 창 끝 정각"),
        ("0601", "6", False, "US 창 종료 직후"),
        ("0300", "2", True, "화 새벽 = 월요일 밤 세션"),
        ("1000", "6", False, "토요일 낮 — KR 장이 아니다"),
        ("1000", "7", False, "일요일 낮"),
        ("2300", "6", False, "토요일 밤 — 미국도 쉰다"),
        ("2300", "7", False, "일요일 밤 — US 월요일 세션은 월요일 밤에 열린다"),
        ("0300", "7", False, "일요일 새벽 — 토요일 밤 세션은 없다"),
        ("0300", "1", False, "월요일 새벽 — 일요일 밤 세션은 없다"),
    ],
)
def test_in_market_hours_boundaries(gate, hhmm, dow, expected, why):
    r = gate.run(
        "_in_market_hours && echo IN || echo OUT",
        NOTIFY_NOW_HHMM=hhmm,
        NOTIFY_NOW_DOW=dow,
    )
    assert r.stdout.strip() == ("IN" if expected else "OUT"), f"{hhmm}/{dow}: {why}"


def test_zero_padded_hours_do_not_explode_as_octal(gate):
    """`[ 0900 -le 1000 ]` 은 bash 산술이 8진수로 읽어 에러다 — own_brief 데드라인 결함과 같은 함정."""
    r = gate.run("_in_market_hours; echo rc=$?", NOTIFY_NOW_HHMM="0800", NOTIFY_NOW_DOW="1")
    assert r.stderr.strip() == ""
    assert "rc=1" in r.stdout


# ── 멱등 source ───────────────────────────────────────────────────────────

def test_sourcing_twice_is_a_no_op(gate):
    r = gate.run(f'. "{LIB}"\n. "{LIB}"\nnotify_defer "x" "본문"; echo OK', **IN_HOURS)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
    assert len(gate.queued()) == 1


# ── parse_mode=HTML + 평문 폴백 (2026-09-04, L1 서식) ──────────────────────
# 크론 리포트가 quant.core.tgfmt 스타일 <b>/<code> 태그를 담은 텍스트를 넘기기
# 시작한다 — 텔레그램이 태그를 거부해도(첫 호출 ok:false) 그 한 통은 평문으로
# 다시 나가야 한다(알림 유실 금지, engine notifier와 동일 계약).

def test_send_tries_html_parse_mode_first(gate):
    r = gate.run('notify_now "<b>제목</b>"', **OFF_HOURS)
    assert r.returncode == 0, r.stderr
    assert len(gate.sends()) == 1
    assert "parse_mode=HTML" in gate.sends()[0]


def test_html_rejected_falls_back_to_plain_text(gate, tmp_path):
    """첫 curl 호출(HTML)은 ok:false, 두 번째(평문)는 ok:true — 발송이 성공해야 한다."""
    curl = tmp_path / "bin" / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$CURL_LOG"\n'
        'if printf \'%s\' "$*" | grep -q "parse_mode=HTML"; then printf \'{"ok":false}\'; '
        'else printf \'{"ok":true}\'; fi\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)
    r = gate.run('notify_now "<b>깨진 태그" && echo SENT || echo FAILED', **OFF_HOURS)
    assert "SENT" in r.stdout, r.stderr
    assert len(gate.sends()) == 2
    assert "parse_mode=HTML" in gate.sends()[0]
    assert "parse_mode=HTML" not in gate.sends()[1]


# ── 스크립트별 배선 — 분류표가 코드와 어긋나지 않게 ────────────────────────

CLASSIFICATION = {
    # 지금 조치하지 않으면 손해인 것만 즉시.
    "notify_now": ["watchdog", "ops_watch", "kiwoom_ws_check", "backup"],
    # 알아야 하지만 급하지 않다 — 장중이면 미뤄진다.
    "notify_auto": [
        "own_brief", "daily_brief", "flow_scan", "us_watch_discover",
        "ai_trader", "ml_scorer", "capital_review", "governor",
    ],
    # 요약·정보성 — 마감 HTML 로만.
    "notify_defer": [
        "backfill_1m", "backfill_kr_daily", "backfill_kr_stock_daily",
        "backfill_us_daily", "macro_collect", "experiments_daily",
        "delivery_check", "close_report", "daily_feedback", "ops_judge",
        "scoreboard_weekly", "session_pnl", "param_propose", "weekly_review",
    ],
}


@pytest.mark.parametrize(
    "name,fn",
    [(n, fn) for fn, names in CLASSIFICATION.items() for n in names],
)
def test_script_uses_only_its_assigned_gate(name, fn):
    """각 스크립트는 자기 등급의 문 하나만 쓴다 — 섞이면 분류표가 거짓말이 된다."""
    text = (SCRIPTS / f"{name}.sh").read_text(encoding="utf-8")
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert 'lib/notify.sh"' in body, f"{name}: 게이트를 source 하지 않는다"
    used = {g for g in CLASSIFICATION if f"{g} " in body}
    assert used == {fn}, f"{name}: {used} 를 쓴다 — 분류는 {fn}"


@pytest.mark.parametrize(
    "name", sorted({n for names in CLASSIFICATION.values() for n in names})
)
def test_script_does_not_call_telegram_directly(name):
    """`tg()` 복제가 다시 자라면 "무엇이 장중에 나가나"를 한곳에서 답할 수 없게 된다."""
    text = (SCRIPTS / f"{name}.sh").read_text(encoding="utf-8")
    assert "api.telegram.org" not in text
