"""소식통 배달 점검 — 소유자 조직도 역할 6, 2026-08-26.

중심 주장(health.py 테스트와 같은 관례): **"모른다"(unknown)는 "정상"이
아니다** — 로그 파일 자체가 없는 것과, 로그를 읽었는데 발송 흔적이 없는 것은
다른 사실이고 다르게 취급해야 한다. 그리고 **휴장일(주말) 판정이 새지 않아야
한다** — 화요일 점검이 일요일 산출물을 찾으면 안 된다.
"""
from __future__ import annotations

from datetime import date

from quant.control.delivery_check import (
    MISSING,
    UNKNOWN,
    ArtifactStatus,
    check_ai_trader,
    check_artifacts,
    check_log_traces,
    expected_artifacts,
    is_weekday,
)


def test_is_weekday():
    assert is_weekday(date(2026, 8, 25))  # 화
    assert not is_weekday(date(2026, 8, 22))  # 토
    assert not is_weekday(date(2026, 8, 23))  # 일


def test_expected_artifacts_on_a_normal_weekday_run():
    # 화(8/25) 실행 → 전날 월(8/24, 평일) 기준 KR/US 산출물 + 오늘 US_wrap.
    today = date(2026, 8, 25)
    expected = expected_artifacts(today)
    assert expected["KR_report.html"] == date(2026, 8, 24)
    assert expected["KR_engine.json"] == date(2026, 8, 24)
    assert expected["KR_close_engine.json"] == date(2026, 8, 24)
    assert expected["US_report.html"] == date(2026, 8, 24)
    assert expected["US_wrap.json"] == today


def test_expected_artifacts_empty_when_prior_day_is_weekend():
    # 월(8/24) 실행 → 전날은 일(8/23), 평일이 아니므로 아무것도 확인하지 않는다.
    today = date(2026, 8, 24)
    assert expected_artifacts(today) == {}


def test_check_artifacts_ok_when_present_and_nonzero():
    statuses = {"KR_report.html": ArtifactStatus(exists=True, size=1234)}
    assert check_artifacts(statuses) == []


def test_check_artifacts_missing_when_absent():
    statuses = {"KR_report.html": ArtifactStatus(exists=False, size=0)}
    findings = check_artifacts(statuses)
    assert len(findings) == 1
    assert findings[0].level == MISSING
    assert findings[0].check == "KR_report.html"


def test_check_artifacts_missing_when_zero_size():
    statuses = {"US_wrap.json": ArtifactStatus(exists=True, size=0)}
    findings = check_artifacts(statuses)
    assert findings[0].level == MISSING


def test_check_log_traces_ok_when_needle_present():
    target = date(2026, 8, 24)
    logs = {
        "own_brief_KR": [f"[{target.isoformat()} 08:12:03] [KR] 리포트 rc=0 후보: 000660"],
        "own_brief_US": [f"[{target.isoformat()} 21:50:01] [US] 리포트 rc=0 후보: AAPL"],
        "run_report_KR": [f"[{target.isoformat()} 07:32:10] [KR] 빌드 완료 (312초)"],
        "run_report_US": [f"[{target.isoformat()} 21:10:00] [US] 빌드 완료 (301초)"],
    }
    assert check_log_traces(logs, target) == []


def test_check_log_traces_unknown_when_file_missing():
    target = date(2026, 8, 24)
    logs = {
        "own_brief_KR": None, "own_brief_US": [], "run_report_KR": [], "run_report_US": [],
    }
    findings = check_log_traces(logs, target)
    assert any(f.check == "own_brief_KR" and f.level == UNKNOWN for f in findings)


def test_check_log_traces_missing_when_no_line_for_today():
    target = date(2026, 8, 24)
    logs = {
        "own_brief_KR": ["[2026-08-20 08:12:03] [KR] 리포트 rc=0 후보: 000660"],  # 다른 날짜
        "own_brief_US": [f"[{target.isoformat()} 21:50:01] [US] 리포트 rc=0 후보: AAPL"],
        "run_report_KR": [f"[{target.isoformat()} 07:32:10] [KR] 빌드 완료"],
        "run_report_US": [f"[{target.isoformat()} 21:10:00] [US] 빌드 완료"],
    }
    findings = check_log_traces(logs, target)
    assert len(findings) == 1
    assert findings[0].check == "own_brief_KR"
    assert findings[0].level == MISSING


def test_check_log_traces_does_not_cross_market_prefix():
    # KR 줄만 있고 US 줄이 없으면 US만 MISSING이어야 한다(브라켓 정확 대조).
    target = date(2026, 8, 24)
    logs = {
        "own_brief_KR": [f"[{target.isoformat()} 08:12:03] [KR] 리포트 rc=0 후보: 000660"],
        "own_brief_US": [f"[{target.isoformat()} 08:12:03] [KR] 리포트 rc=0 후보: 000660"],
        "run_report_KR": [f"[{target.isoformat()} 07:32:10] [KR] 빌드 완료"],
        "run_report_US": [f"[{target.isoformat()} 07:32:10] [KR] 빌드 완료"],
    }
    findings = check_log_traces(logs, target)
    checks = {f.check for f in findings}
    assert checks == {"own_brief_US", "run_report_US"}


def test_check_ai_trader_ok_when_no_pick_is_normal():
    # "조용한 게 기본값" — 픽 없음도 정상 종료 흔적이 있으면 OK.
    target = date(2026, 8, 24)
    lines = [f"[{target.isoformat()} 08:20:15] KR 픽 없음/결근 — 침묵"]
    assert check_ai_trader(lines, "KR", target) is None


def test_check_ai_trader_ok_when_pick_sent():
    target = date(2026, 8, 24)
    lines = [f"[{target.isoformat()} 08:20:15] KR 픽 발생:"]
    assert check_ai_trader(lines, "KR", target) is None


def test_check_ai_trader_missing_on_explicit_failure():
    target = date(2026, 8, 24)
    lines = [f"[{target.isoformat()} 08:20:15] KR 실패 exit=1 (결근 처리 — 판단 미기록)"]
    finding = check_ai_trader(lines, "KR", target)
    assert finding is not None
    assert finding.level == MISSING


def test_check_ai_trader_missing_when_job_never_ran():
    target = date(2026, 8, 24)
    lines = [f"[2026-08-20 08:20:15] KR 픽 발생:"]  # 다른 날짜뿐
    finding = check_ai_trader(lines, "KR", target)
    assert finding is not None
    assert finding.level == MISSING


def test_check_ai_trader_unknown_when_log_missing():
    finding = check_ai_trader(None, "KR", date(2026, 8, 24))
    assert finding is not None
    assert finding.level == UNKNOWN
