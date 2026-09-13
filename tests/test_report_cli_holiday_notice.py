"""휴장일 안내 한 줄(2026-09-07) — 09-06·09-07 이틀 연속 안내가 조용히 사라진 사고의 회귀 테스트.

발송은 셸(run_report.sh)이 하고 파이썬은 문구만 만든다. 여기서는 문구·침묵 계약만 잰다.
외부 캘린더 공급자는 명시적으로 주입한다. 실제 API 자격증명/IP 허용 여부를
단위 테스트의 휴장 판정 근거로 사용하지 않는다.
"""

from __future__ import annotations

from datetime import date

import pytest

from quant.apps import report_cli
from quant.core.session import TossSessionCalendar


@pytest.fixture
def calendar_calls(monkeypatch):
    calls = []
    payloads = {
        date(2026, 9, 7): {"today": {"regularMarket": None}},
        date(2026, 9, 8): {"today": {"regularMarket": {"startTime": "09:30", "endTime": "16:00"}}},
    }

    class _CalendarClient:
        def market_calendar(self, market, day):
            assert market == "US"
            calls.append((market, day))
            return payloads[day]

    calendar = TossSessionCalendar(_CalendarClient())
    monkeypatch.setattr(report_cli, "_report_session_calendar", lambda: calendar)
    return calls


def test_holiday_prints_one_line_with_next_open(capsys, calendar_calls):
    rc = report_cli.main(["holiday-notice", "--market", "US", "--date", "2026-09-07"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out.startswith("📰 US 휴장일 — 리포트 없음(다음 개장 2026-09-08")
    assert calendar_calls == [("US", date(2026, 9, 7)), ("US", date(2026, 9, 8))]


def test_trading_day_prints_nothing(capsys, calendar_calls):
    rc = report_cli.main(["holiday-notice", "--market", "US", "--date", "2026-09-08"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""
    assert calendar_calls == [("US", date(2026, 9, 8))]


@pytest.mark.parametrize("provider_state", ["unconfigured", "unavailable"])
def test_holiday_notice_uses_static_fallback_when_provider_cannot_answer(capsys, monkeypatch, provider_state):
    # 실제 공급자 조립·Toss 파서·정적 폴백을 유지하고 외부 자격증명/API 경계만 주입한다.
    from quant.adapters import env
    from quant.adapters.brokers.toss import client as toss_client

    monkeypatch.setattr(env, "get_key", lambda name: "" if provider_state == "unconfigured" else "test-only")
    client_creations = []
    calendar_requests = []

    class _UnavailableClient:
        def market_calendar(self, market, day):
            calendar_requests.append((market, day))
            raise RuntimeError("calendar unavailable")

    def make_client(**kwargs):
        client_creations.append(True)
        return _UnavailableClient()

    monkeypatch.setattr(toss_client, "TossClient", make_client)
    rc = report_cli.main(["holiday-notice", "--market", "US", "--date", "2026-09-07"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "📰 US 휴장일 — 리포트 없음(다음 개장 2026-09-08)"
    assert len(client_creations) == (0 if provider_state == "unconfigured" else 1)
    assert calendar_requests == ([] if provider_state == "unconfigured" else [
        ("US", date(2026, 9, 7)), ("US", date(2026, 9, 8)),
    ])


def test_text_builder_says_미확인_when_no_next_open_found():
    """다음 개장일을 못 찾으면(긴 연휴 등) 날짜를 지어내지 않는다."""

    class _NeverOpen:
        def session(self, market, now):
            return None

    text = report_cli.holiday_notice_text("KR", date(2026, 9, 7), _NeverOpen())
    assert "미확인" in text
