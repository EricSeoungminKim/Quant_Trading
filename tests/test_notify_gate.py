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
    failure_ledger = tmp_path / "notify_failures.jsonl"
    rate_dir = tmp_path / "rate"

    class Gate:
        queue_path = queue
        curl_log_path = curl_log
        failure_ledger_path = failure_ledger

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
                # 발송 실패 원장 + 레이트 리밋 상태를 실제 저장소 밖으로 격리한다
                # (2026-09-06) — 안 그러면 테스트가 실제 data/ledger·data/state 를
                # 건드리고, 레이트 리밋 상태가 테스트 실행 사이에 누적된다.
                "NOTIFY_FAILURE_LEDGER": str(failure_ledger),
                "NOTIFY_SENT_LEDGER": str(tmp_path / "notify_sent.jsonl"),  # 실제 저장소 원장 오염 방지(2026-09-07)
                "NOTIFY_RATE_DIR": str(rate_dir),
                "NOTIFY_RATE_SLEEP": "0",  # 테스트에서는 실제로 쉬지 않는다
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

        def failures(self) -> list[dict]:
            if not failure_ledger.exists():
                return []
            return [json.loads(l) for l in failure_ledger.read_text(encoding="utf-8").splitlines() if l]

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


# ── 발송 실패 원장 (2026-09-06 라이브 준비 4단계) ──────────────────────────
# HTML+평문 재시도까지 다 실패하면 data/ledger/notify_failures.jsonl 에 한 줄
# 남는다 — quant.control.health.notify_failure_findings 가 하루 총합을 센다.
# quant/adapters/notify/telegram.py 의 엔진 노티파이어도 같은 파일·스키마를 쓴다.

def test_send_failure_is_recorded_in_failure_ledger(gate, tmp_path):
    bad = tmp_path / "bin" / "curl"
    bad.write_text('#!/usr/bin/env bash\nprintf \'{"ok":false}\'\n', encoding="utf-8")
    bad.chmod(0o755)
    r = gate.run('notify_now "🚨 이상"', NOTIFY_LANE="ops", **OFF_HOURS)
    assert r.returncode != 0
    rows = gate.failures()
    assert len(rows) == 1
    assert rows[0]["lane"] == "ops"
    assert "이상" in rows[0]["text"]


def test_successful_send_does_not_touch_failure_ledger(gate):
    r = gate.run('notify_now "정상"', **OFF_HOURS)
    assert r.returncode == 0
    assert gate.failures() == []


def test_queued_notification_does_not_touch_failure_ledger(gate):
    """큐에만 쌓이는 것(notify_defer/장중 notify_auto)은 발송 시도 자체가 없다
    — 실패 원장과 무관해야 한다."""
    r = gate.run('notify_defer "x" "본문"', **IN_HOURS)
    assert r.returncode == 0
    assert gate.failures() == []


# ── 레인별 레이트 리밋 (2026-09-06) ─────────────────────────────────────────
# 텔레그램 실측 한도(~20건/분/챗)에 안전마진을 두고, 최근 60초 안에 같은 레인
# 으로 15건을 넘겨 보내면 짧게 쉰다.

def _stub_sleep(tmp_path: Path) -> Path:
    log = tmp_path / "sleep.log"
    stub = tmp_path / "bin" / "sleep"
    stub.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$1" >> "$SLEEP_LOG"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return log


def test_rate_limit_sleeps_after_15_sends_in_same_lane(gate, tmp_path):
    sleep_log = _stub_sleep(tmp_path)
    cmds = "; ".join(f'notify_now "체결 {i}"' for i in range(16))
    r = gate.run(cmds, SLEEP_LOG=str(sleep_log), NOTIFY_LANE="trades",
                NOTIFY_RATE_SLEEP="7", **OFF_HOURS)
    assert r.returncode == 0, r.stderr
    assert len(gate.sends()) == 16, "리밋에 걸려도 결국은 다 보낸다 — 막지 않고 늦출 뿐"
    assert sleep_log.exists(), "15건을 넘기면 쉬어야 한다"
    assert sleep_log.read_text(encoding="utf-8").strip() == "7"


def test_rate_limit_does_not_sleep_under_15_sends(gate, tmp_path):
    sleep_log = _stub_sleep(tmp_path)
    cmds = "; ".join(f'notify_now "체결 {i}"' for i in range(10))
    r = gate.run(cmds, SLEEP_LOG=str(sleep_log), NOTIFY_LANE="trades", **OFF_HOURS)
    assert r.returncode == 0, r.stderr
    assert not sleep_log.exists(), "한도 안이면 쉬지 않는다"


def test_rate_limit_is_scoped_per_lane(gate, tmp_path):
    """레인 A 가 한도를 채워도 레인 B 는 영향받지 않는다(레인별 상태 파일 분리)."""
    sleep_log = _stub_sleep(tmp_path)
    cmds_a = "; ".join(f'notify_now "매매 {i}"' for i in range(16))
    r = gate.run(cmds_a, SLEEP_LOG=str(sleep_log), NOTIFY_LANE="trades", **OFF_HOURS)
    assert r.returncode == 0, r.stderr
    assert sleep_log.exists()
    n_after_trades = len(sleep_log.read_text(encoding="utf-8").splitlines())

    r = gate.run('notify_now "운영 알림"', SLEEP_LOG=str(sleep_log), NOTIFY_LANE="ops", **OFF_HOURS)
    assert r.returncode == 0, r.stderr
    n_after_ops = len(sleep_log.read_text(encoding="utf-8").splitlines())
    assert n_after_ops == n_after_trades, "다른 레인은 쉬지 않는다"


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
    assert sorted(row) == ["lane", "level", "source", "text", "ts"]
    assert row["source"] == "ops_judge"
    assert row["text"] == "판단 워치독"
    assert row["level"] == "defer"
    assert row["lane"] == ""  # NOTIFY_LANE 미지정 — 기존 호출부와 동일
    # ts 는 KST 로컬 + 오프셋 (ISO 8601) — 마감 리포트가 그대로 찍는다.
    assert len(row["ts"]) == 24 and row["ts"][10] == "T"


def test_queue_line_carries_notify_lane(gate):
    """`NOTIFY_LANE`이 설정돼 있으면 그 값이 큐 줄에 그대로 남는다(2026-09-05) —
    flush하는 쪽(daily_wrap.sh 등)이 나중에 레인별로 다시 보낼 수 있어야 한다."""
    gate.run('notify_defer "ops_judge" "판단 워치독"', NOTIFY_LANE="ops", **IN_HOURS)
    assert gate.queued()[0]["lane"] == "ops"


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


# ── 레인 라우팅 (포럼 토픽, 2026-09-05) ────────────────────────────────────
# quant/core/tglanes.py 의 판정을 셸에서 복제한다 — data/state/tg_lanes.json
# 매핑에 따라 message_thread_id 를 붙이거나, 레거시 chat_id 로 폴백하며 헤더를
# 붙인다.

def _write_lanes_file(tmp_path: Path, mapping: dict) -> Path:
    import json as _json
    path = tmp_path / "tg_lanes.json"
    path.write_text(_json.dumps(mapping), encoding="utf-8")
    return path


def test_notify_now_without_lane_is_unchanged(gate):
    """NOTIFY_LANE 을 안 주면 기존과 완전히 동일 — thread_id 도 헤더도 없다."""
    r = gate.run('notify_now "체결 알림"', **OFF_HOURS)
    assert r.returncode == 0, r.stderr
    sent = gate.sends()[0]
    assert "message_thread_id" not in sent
    assert "text=체결 알림" in sent  # 헤더 없이 원문 그대로


def test_notify_now_with_bound_lane_appends_thread_id(gate, tmp_path):
    lanes = _write_lanes_file(tmp_path, {"chat_id": 111, "threads": {"trades": 42}})
    r = gate.run(
        'notify_now "체결 알림"',
        NOTIFY_LANE="trades", NOTIFY_LANES_FILE=str(lanes), **OFF_HOURS,
    )
    assert r.returncode == 0, r.stderr
    sent = gate.sends()[0]
    assert "chat_id=111" in sent
    assert "message_thread_id=42" in sent


def test_notify_now_with_unbound_lane_and_no_mapping_falls_back_silently(gate, tmp_path):
    """매핑 파일 자체가 없으면(마이그레이션 이전) 레거시 chat_id 그대로, thread_id 없음."""
    r = gate.run(
        'notify_now "체결 알림"',
        NOTIFY_LANE="trades", NOTIFY_LANES_FILE=str(tmp_path / "missing.json"), **OFF_HOURS,
    )
    assert r.returncode == 0, r.stderr
    sent = gate.sends()[0]
    assert "chat_id=12345" in sent  # gate 픽스처의 레거시 TELEGRAM_CHAT_ID
    assert "message_thread_id" not in sent


def test_notify_now_unbound_lane_after_other_binding_gets_header(gate, tmp_path):
    """"ops"는 안 묶였지만 "trades"는 묶여 있다 — 레거시로 떨어지는 ops 메시지는
    헤더로 자기 레인을 밝힌다(섞인 방)."""
    lanes = _write_lanes_file(tmp_path, {"chat_id": 111, "threads": {"trades": 42}})
    r = gate.run(
        'notify_now "운영 경보"',
        NOTIFY_LANE="ops", NOTIFY_LANES_FILE=str(lanes), **OFF_HOURS,
    )
    assert r.returncode == 0, r.stderr
    sent = gate.sends()[0]
    assert "chat_id=12345" in sent
    assert "message_thread_id" not in sent
    assert "text=🚨 운영" in sent  # "🚨 운영" 헤더가 앞에 붙는다


def test_lane_header_matches_python_tglanes_registry():
    """셸의 헤더 문구가 quant.core.tglanes.LANES 와 갈리면 레거시 채팅에서
    보이는 헤더가 파이썬 어댑터 쪽과 달라진다 — 대조해서 잡는다."""
    import subprocess as _sp

    from quant.core import tglanes

    for lane in tglanes.LANES:
        r = _sp.run(
            ["bash", "-c", f'. "{LIB}"\n_notify_lane_header "{lane}"'],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout == tglanes.header(lane), f"{lane}: 셸/파이썬 헤더 불일치"


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


# ── `_notify_record_failure`는 set -u 아래서 스크립트를 죽이지 못한다
# (2026-09-07, 리포트 QA 세션) ──────────────────────────────────────────────
#
# 발견: `${1:0:200}`처럼 위치 인자를 서브셸(command substitution) 안에서
# 직접 잘라 쓰면, 인자 없이 불렸을 때 `set -u`가 그 서브셸만 조용히 죽이고
# (필드는 빈 문자열로 샌다) "unbound variable" 에러 줄이 stderr 에 찍힌다 —
# 호출 스크립트 자체는 죽지 않지만(command substitution 실패는 `errexit`
# 없이는 전파되지 않는다) 알림 실패 원장 기록과 무관한 소음이 남는다.
# `text="${1:-}"`로 먼저 받아두게 고쳤다 — 아래는 그 수정을 잠근다.

def test_notify_record_failure_with_zero_args_does_not_raise_or_kill_script(gate):
    r = gate.run('_notify_record_failure; echo ALIVE rc=$?', **OFF_HOURS)
    assert r.returncode == 0, r.stderr
    assert "ALIVE rc=0" in r.stdout
    assert "unbound variable" not in r.stderr, r.stderr


def test_notify_record_failure_with_zero_args_writes_empty_text_field(gate):
    gate.run('_notify_record_failure', **OFF_HOURS)
    rows = gate.failures()
    assert len(rows) == 1
    assert rows[0]["text"] == ""


def test_notify_record_failure_with_normal_arg_is_unaffected(gate):
    """정상 호출 경로(`_notify_send`가 항상 하는 방식)는 이 수정으로 바뀌지
    않는다 — 회귀 방지."""
    gate.run('_notify_record_failure "정상 텍스트"', **OFF_HOURS)
    rows = gate.failures()
    assert rows[0]["text"] == "정상 텍스트"


# ── NOTIFY_LANE 전파 — notify_auto/notify_now/notify_defer 세 함수 모두
# (2026-09-07, 리포트 QA 세션 §5) ────────────────────────────────────────────
#
# 세 함수 모두 `NOTIFY_LANE`을 자체적으로 파라미터로 받지 않고, `notify.sh`가
# 소스된 같은 셸 프로세스에서 호출부가 미리 설정해둔 전역 변수를 그대로
# 읽는다(`_notify_send`/`_notify_enqueue` 내부의 `${NOTIFY_LANE:-}`). 이미
# `notify_now`(레인 라우팅 절)와 `notify_defer`(`test_queue_line_carries_
# notify_lane`)는 개별적으로 커버돼 있었지만, `notify_auto`는 레인 전파가
# 한 번도 직접 검증된 적이 없었다 — 장중/장외 두 분기가 서로 다른 내부
# 함수(`_notify_enqueue`/`_notify_send`)로 갈라지므로 둘 다 잠근다.

def test_notify_auto_off_hours_propagates_lane_to_bound_thread(gate, tmp_path):
    import json as _json
    lanes = tmp_path / "tg_lanes.json"
    lanes.write_text(_json.dumps({"chat_id": 111, "threads": {"trades": 42}}), encoding="utf-8")
    r = gate.run(
        'notify_auto "own_brief" "체결 요약"',
        NOTIFY_LANE="trades", NOTIFY_LANES_FILE=str(lanes), **OFF_HOURS,
    )
    assert r.returncode == 0, r.stderr
    sent = gate.sends()[0]
    assert "chat_id=111" in sent
    assert "message_thread_id=42" in sent


def test_notify_auto_in_hours_queue_carries_lane(gate):
    r = gate.run('notify_auto "own_brief" "편입"', NOTIFY_LANE="briefs", **IN_HOURS)
    assert r.returncode == 0, r.stderr
    assert gate.sends() == []
    rows = gate.queued()
    assert len(rows) == 1
    assert rows[0]["lane"] == "briefs"
    assert rows[0]["level"] == "auto"


# ── 성공 발송 기록(2026-09-07) — 셸 경로도 "무엇을 보냈는지" 남긴다 ──────────────────
def test_successful_send_is_recorded_in_sent_ledger(gate, tmp_path):
    import json

    sent = tmp_path / "notify_sent.jsonl"
    r = gate.run('NOTIFY_LANE=briefs notify_now "테스트 발송 <b>굵게</b>"', NOTIFY_SENT_LEDGER=str(sent), **OFF_HOURS)
    assert r.returncode == 0
    rows = [json.loads(l) for l in sent.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["lane"] == "briefs" and "테스트 발송" in rows[0]["text"] and rows[0]["ts"]
    assert gate.failures() == []


def test_failed_send_is_not_recorded_in_sent_ledger(gate, tmp_path):
    bad = tmp_path / "bin" / "curl"
    bad.write_text('#!/usr/bin/env bash\nprintf \'{"ok":false}\'\n', encoding="utf-8")
    bad.chmod(0o755)
    sent = tmp_path / "notify_sent.jsonl"
    r = gate.run('notify_now "실패"', NOTIFY_SENT_LEDGER=str(sent), **OFF_HOURS)
    assert r.returncode != 0
    assert not sent.exists() or sent.read_text(encoding="utf-8").strip() == ""


# ── notify_document — 문서(sendDocument) 전송 게이트 (2026-09-07 live-readiness) ──
#
# daily_wrap.sh 가 예전에 이 게이트를 우회해 api.telegram.org 의 sendDocument
# 를 직접 쳤다 — 레인 라우팅·레이트 리밋·발송(실패) 원장이 전부 빠져 있었다.
# `notify_document`는 그 세 문(notify_now/_auto/_defer)과 별도의 네 번째
# 공개 API로, `_notify_send`가 쓰는 헬퍼(토큰/챗 해석·레인 타겟팅·발송 원장)를
# 그대로 재사용한다.

def test_document_send_is_recorded_in_sent_ledger(gate, tmp_path):
    doc = tmp_path / "report.html"
    doc.write_text("<html>요약</html>", encoding="utf-8")
    sent = tmp_path / "notify_sent.jsonl"
    r = gate.run(
        f'notify_document "briefs" "{doc}" "마감 요약" && echo SENT || echo FAILED',
        NOTIFY_SENT_LEDGER=str(sent), **OFF_HOURS,
    )
    assert "SENT" in r.stdout, r.stderr
    call = gate.sends()[0]
    assert "sendDocument" in call
    assert f"document=@{doc};type=text/html" in call
    assert "caption=마감 요약" in call
    assert "parse_mode=HTML" in call
    rows = [json.loads(l) for l in sent.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["lane"] == "briefs"
    assert rows[0]["text"] == "마감 요약"
    assert gate.failures() == []


def test_document_send_failure_is_recorded_in_failure_ledger(gate, tmp_path):
    bad = tmp_path / "bin" / "curl"
    bad.write_text('#!/usr/bin/env bash\nprintf \'{"ok":false}\'\n', encoding="utf-8")
    bad.chmod(0o755)
    doc = tmp_path / "report.html"
    doc.write_text("<html>요약</html>", encoding="utf-8")
    r = gate.run(
        f'notify_document "" "{doc}" "실패 케이스" && echo SENT || echo FAILED',
        **OFF_HOURS,
    )
    assert "FAILED" in r.stdout, r.stderr
    rows = gate.failures()
    assert len(rows) == 1
    assert rows[0]["text"] == "실패 케이스"


def test_document_send_no_token_is_silent_skip(gate, tmp_path):
    doc = tmp_path / "report.html"
    doc.write_text("<html>요약</html>", encoding="utf-8")
    r = gate.run(
        f'notify_document "" "{doc}" "요약" && echo SENT || echo FAILED',
        token=False, **OFF_HOURS,
    )
    assert "SENT" in r.stdout, r.stderr  # 조용한 no-op도 "성공"(0)이다
    assert gate.sends() == [], "토큰이 없으면 발송 시도 자체를 하지 않는다"
    assert gate.failures() == []


def test_document_send_missing_file_fails_without_calling_telegram(gate, tmp_path):
    r = gate.run(
        f'notify_document "" "{tmp_path}/no-such-file.html" "요약" && echo SENT || echo FAILED',
        **OFF_HOURS,
    )
    assert "FAILED" in r.stdout, r.stderr
    assert gate.sends() == [], "파일이 없으면 텔레그램을 치지 않는다"


def test_document_caption_html_special_chars_are_escaped(gate, tmp_path):
    """캡션에 `&`/`<`/`>` 가 섞이면(매매 리뷰 URL 등) parse_mode=HTML 시도에서
    이스케이프된 채 나가야 한다 — 안 그러면 텔레그램이 깨진 HTML로 보고 거부한다."""
    doc = tmp_path / "report.html"
    doc.write_text("<html>요약</html>", encoding="utf-8")
    r = gate.run(
        f'notify_document "" "{doc}" "매매 리뷰 & <b>강조</b>" && echo SENT || echo FAILED',
        **OFF_HOURS,
    )
    assert "SENT" in r.stdout, r.stderr
    call = gate.sends()[0]
    assert "caption=매매 리뷰 &amp; &lt;b&gt;강조&lt;/b&gt;" in call
