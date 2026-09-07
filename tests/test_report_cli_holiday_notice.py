"""휴장일 안내 한 줄(2026-09-07) — 09-06·09-07 이틀 연속 안내가 조용히 사라진 사고의 회귀 테스트.

발송은 셸(run_report.sh)이 하고 파이썬은 문구만 만든다. 여기서는 문구·침묵 계약만 잰다.
"""

from __future__ import annotations

from datetime import date

from quant.apps import report_cli


def test_holiday_prints_one_line_with_next_open(capsys):
    rc = report_cli.main(["holiday-notice", "--market", "US", "--date", "2026-09-07"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out.startswith("📰 US 휴장일 — 리포트 없음(다음 개장 2026-09-08")


def test_trading_day_prints_nothing(capsys):
    rc = report_cli.main(["holiday-notice", "--market", "US", "--date", "2026-09-08"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_text_builder_says_미확인_when_no_next_open_found():
    """다음 개장일을 못 찾으면(긴 연휴 등) 날짜를 지어내지 않는다."""

    class _NeverOpen:
        def session(self, market, now):
            return None

    text = report_cli.holiday_notice_text("KR", date(2026, 9, 7), _NeverOpen())
    assert "미확인" in text
