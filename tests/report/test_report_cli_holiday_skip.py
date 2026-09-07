"""`report_cli` 개장일 판정 — 휴장일엔 빌드를 생략한다(2026-09-06).

유래: market-report@.timer는 주말·공휴일 포함 매일 발행되도록 설계돼 있는데
(holiday_synthesis.py가 결손 날짜를 건너뛰고 부분 집계하는 전제), 실측으로
2026-09-06(일요일)에 후보 2건·중기 0건짜리 빈약한 KR_engine.json이 빌드돼
`cli health`의 report_quality가 "중기 관심 종목 수가 0"으로 거짓 경보를 냈다.
그래서 이제 `--market`/`--date`가 그 시장의 휴장일이면 빌드 자체를 생략하고
셸(run_report.sh)이 NOTIFY_LANE=briefs 한 줄을 보낸다(발송 주체는 2026-09-07 에
파이썬에서 셸로 옮겼다 — 아래 테스트 주석 참고).

`_report_session_calendar()`(Toss 자격증명을 읽어 실제 캘린더를 만드는 I/O
지점)는 전부 monkeypatch로 가짜 캘린더로 갈아치운다 — 로컬 `.env.local`에
실제 TOSS_CLIENT_ID가 있어도 이 테스트 스위트가 실네트워크를 타면 안 된다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from quant.apps import report_cli
from quant.core.session import Session, StaticSessionCalendar

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")


class _FakeCalendar:
    """`closed`에 있는 날짜만 휴장으로 답하는 최소 페이크 — Toss/정적 캘린더와
    같은 `session(market, now) -> Session|None` 계약."""

    def __init__(self, closed: set[date]):
        self._closed = closed

    def session(self, market: str, now: datetime) -> Session | None:
        tz = KST if market == "KR" else NY
        local_date = now.astimezone(tz).date()
        if local_date in self._closed:
            return None
        return Session(open=now, close=now + timedelta(hours=6))


# ── _is_trading_day — 순수 판정 ────────────────────────────────────────────

def test_is_trading_day_true_on_a_normal_weekday():
    cal = _FakeCalendar(closed=set())
    assert report_cli._is_trading_day("KR", date(2026, 9, 4), cal) is True


def test_is_trading_day_false_on_weekend_via_static_calendar():
    """정적 캘린더(주말만 판별)로도 일요일은 정확히 잡는다 — 실측 사고 재현."""
    cal = StaticSessionCalendar()
    assert report_cli._is_trading_day("KR", date(2026, 9, 6), cal) is False


def test_is_trading_day_false_on_a_weekday_holiday():
    """정적 캘린더는 못 잡는 것 — 평일 공휴일(US 노동절, 2026-09-07 월요일)은
    달력 기반(Toss/가짜) 캘린더가 있어야 잡힌다."""
    labor_day = date(2026, 9, 7)
    assert labor_day.weekday() == 0  # 월요일임을 픽스처 스스로 확인
    cal = _FakeCalendar(closed={labor_day})

    assert report_cli._is_trading_day("US", labor_day, cal) is False
    # 정적 캘린더는 평일이라 이 공휴일을 놓친다 — 그래서 실캘린더가 필요하다.
    assert StaticSessionCalendar().session(
        "US", datetime.combine(labor_day, datetime.min.time(), tzinfo=NY)
    ) is not None


# ── _next_trading_day ──────────────────────────────────────────────────────

def test_next_trading_day_skips_a_weekend():
    cal = StaticSessionCalendar()
    # 2026-09-06(일) 다음 개장일은 2026-09-07(월).
    assert report_cli._next_trading_day("KR", date(2026, 9, 6), cal) == date(2026, 9, 7)


def test_next_trading_day_skips_a_holiday_cluster():
    closed = {date(2026, 9, 7), date(2026, 9, 8)}
    cal = _FakeCalendar(closed=closed)

    assert report_cli._next_trading_day("US", date(2026, 9, 6), cal) == date(2026, 9, 9)


def test_next_trading_day_gives_up_after_cap():
    cal = _FakeCalendar(closed={date(2026, 9, 6) + timedelta(days=i) for i in range(1, 11)})

    assert report_cli._next_trading_day("KR", date(2026, 9, 6), cal, cap=10) is None


# ── _skip_if_holiday — 판정 + 알림 + 운영 상태 기록 ────────────────────────

def test_skip_if_holiday_records_skip_and_never_sends_from_python(monkeypatch):
    """스킵은 기록하되 **파이썬에서 직접 텔레그램을 보내지 않는다**(2026-09-07 변경).

    유래: systemd 유닛(market-report@.service)은 TZ 만 주고 텔레그램 자격증명을 주지
    않아 파이썬 쪽 발송이 예외를 삼킨 채 사라졌다 — 09-06·09-07 이틀 연속 사장님께
    휴장 안내가 가지 않았다. 이제 문구만 만들고 발송은 셸(run_report.sh)이 한다.
    """
    monkeypatch.setattr(report_cli, "_report_session_calendar", lambda: StaticSessionCalendar())
    recorded = {}
    monkeypatch.setattr(
        report_cli, "record_run",
        lambda kv, job, ok, detail="": recorded.update(job=job, ok=ok, detail=detail),
    )
    sent = []

    class _ExplodingNotifier:
        def send(self, text, lane=None):
            sent.append(text)

    monkeypatch.setattr(
        "quant.adapters.notify.telegram.TelegramNotifier.from_env",
        classmethod(lambda cls: _ExplodingNotifier()),
    )

    skipped = report_cli._skip_if_holiday("KR", date(2026, 9, 6), "open")

    assert skipped is True
    assert recorded == {"job": "report:KR", "ok": True, "detail": "휴장일 — 스킵"}
    assert sent == [], "파이썬 경로 발송은 자격증명 없는 환경에서 조용히 사라진다 — 셸이 보낸다"


def test_holiday_notice_text_names_the_next_open_day():
    text = report_cli.holiday_notice_text("KR", date(2026, 9, 6), StaticSessionCalendar())
    assert "KR 휴장일" in text
    assert "2026-09-07" in text  # 다음 개장일


def test_skip_if_holiday_records_close_report_job_name(monkeypatch):
    monkeypatch.setattr(report_cli, "_report_session_calendar", lambda: StaticSessionCalendar())
    recorded = {}
    monkeypatch.setattr(
        report_cli, "record_run",
        lambda kv, job, ok, detail="": recorded.update(job=job),
    )
    assert report_cli._skip_if_holiday("KR", date(2026, 9, 6), "close") is True
    assert recorded["job"] == "report_close:KR"


def test_skip_if_holiday_false_on_a_normal_trading_day(monkeypatch):
    monkeypatch.setattr(report_cli, "_report_session_calendar", lambda: StaticSessionCalendar())
    monkeypatch.setattr(
        report_cli, "record_run",
        lambda *a, **k: pytest.fail("정상 개장일에는 스킵 기록을 남기면 안 된다"),
    )

    assert report_cli._skip_if_holiday("KR", date(2026, 9, 4), "open") is False


def test_skip_if_holiday_fails_open_when_calendar_construction_blows_up(monkeypatch, capsys):
    """판정 자체가 터지면 **빌드를 진행한다**(fail-open) — 오탐으로 스킵하면
    그날 리포트가 통째로 안 나간다."""

    def _boom():
        raise RuntimeError("credentials exploded")

    monkeypatch.setattr(report_cli, "_report_session_calendar", _boom)

    assert report_cli._skip_if_holiday("KR", date(2026, 9, 4), "open") is False
    assert "개장일 판정 실패" in capsys.readouterr().err


# ── main() 배선 — build가 실제로 네트워크 수집을 건너뛰는가 ────────────────

def test_main_build_open_session_skips_on_holiday_without_collecting(tmp_path, monkeypatch):
    monkeypatch.setattr(report_cli, "_report_session_calendar", lambda: StaticSessionCalendar())
    monkeypatch.setattr(
        report_cli, "record_run", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "quant.adapters.notify.telegram.TelegramNotifier.from_env",
        classmethod(lambda cls: type("N", (), {"send": lambda self, *a, **k: None})()),
    )

    def _boom(*a, **k):
        raise AssertionError("휴장일엔 뉴스 수집(collect)이 호출되면 안 된다")

    monkeypatch.setattr(report_cli, "collect", _boom)
    monkeypatch.setattr(report_cli, "build_sources", _boom)

    rc = report_cli.main([
        "build", "--market", "KR", "--date", "2026-09-06", "--root", str(tmp_path),
    ])

    assert rc == 3  # EXIT_SKIPPED — 래퍼 스크립트가 '발행' 알림을 내지 않도록 0 과 구분(2026-09-06)
    # 스킵 경로는 out/ 아래 engine.json 을 만들지 않는다.
    assert not (tmp_path / "out").exists()


def test_main_build_close_session_skips_on_holiday_after_kr_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(report_cli, "_report_session_calendar", lambda: StaticSessionCalendar())
    monkeypatch.setattr(report_cli, "record_run", lambda *a, **k: None)
    monkeypatch.setattr(
        "quant.adapters.notify.telegram.TelegramNotifier.from_env",
        classmethod(lambda cls: type("N", (), {"send": lambda self, *a, **k: None})()),
    )

    def _boom(*a, **k):
        raise AssertionError("휴장일엔 마감 스냅샷 수집이 호출되면 안 된다")

    monkeypatch.setattr(report_cli, "_collect_snapshot", _boom)

    rc = report_cli.main([
        "build", "--market", "KR", "--session", "close", "--date", "2026-09-06",
        "--root", str(tmp_path),
    ])

    assert rc == 3  # EXIT_SKIPPED — 래퍼 스크립트가 '발행' 알림을 내지 않도록 0 과 구분(2026-09-06)
