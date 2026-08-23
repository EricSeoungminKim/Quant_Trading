from datetime import date

import pytest

from quant.adapters.env import get_key
from quant.collect.sources.calendar import et_to_kst_hhmm, fetch_calendar, parse_fomc, to_dday


def test_to_dday_sorts_ascending_and_labels_dday():
    events = [
        {"name": "B", "date": "2026-08-20", "source": "x"},
        {"name": "A", "date": "2026-08-14", "source": "x"},
    ]
    out = to_dday(events, date(2026, 8, 12))
    assert [e["name"] for e in out] == ["A", "B"]
    assert out[0]["dday"] == "모레"
    assert out[1]["dday"] == "D-8"


def test_to_dday_today_and_tomorrow_labels():
    events = [
        {"name": "Today", "date": "2026-08-12", "source": "x"},
        {"name": "Tomorrow", "date": "2026-08-13", "source": "x"},
    ]
    out = to_dday(events, date(2026, 8, 12))
    by_name = {e["name"]: e for e in out}
    assert by_name["Today"]["dday"] == "오늘"
    assert by_name["Tomorrow"]["dday"] == "내일"


def test_to_dday_drops_past_events():
    events = [{"name": "Past", "date": "2026-08-11", "source": "x"}]
    assert to_dday(events, date(2026, 8, 12)) == []


def test_to_dday_flags_high_impact():
    # FRED 실제 릴리즈명을 그대로 쓴다 — 판정이 정확 일치이므로 가공된 이름
    # ("Consumer Price Index (CPI)" 같은)으로 테스트하면 실제와 어긋난다.
    events = [
        {"name": "Consumer Price Index", "date": "2026-08-14", "source": "x"},
        {"name": "Wholesale Trade", "date": "2026-08-14", "source": "x"},
    ]
    out = to_dday(events, date(2026, 8, 12))
    by_name = {e["name"]: e for e in out}
    assert by_name["Consumer Price Index"]["high_impact"] is True
    assert by_name["Wholesale Trade"]["high_impact"] is False


def test_substring_lookalikes_are_not_high_impact():
    """실측 오분류(2026-08-12). 부분 문자열로 판정하면 이 둘이 최상단을 차지한다."""
    events = [
        {"name": "Research Consumer Price Index", "date": "2026-08-14", "source": "x"},
        {"name": "Debt to Gross Domestic Product Ratios", "date": "2026-08-14", "source": "x"},
    ]
    out = to_dday(events, date(2026, 8, 12))
    assert all(e["high_impact"] is False for e in out)


def test_et_to_kst_hhmm_during_edt():
    # 8월은 서머타임(EDT, UTC-4) — KST(UTC+9)와 13시간 차이.
    assert et_to_kst_hhmm(date(2026, 8, 12), 8, 30) == "21:30"


def test_et_to_kst_hhmm_during_est():
    # 서머타임 회귀 방지: 1월은 표준시(EST, UTC-5) — KST와 14시간 차이.
    # 이 한 시간 차를 놓치면 "장전 리포트"가 실제 발표 이후로 밀린다.
    assert et_to_kst_hhmm(date(2026, 1, 15), 8, 30) == "22:30"


def test_et_to_kst_hhmm_crosses_midnight():
    # 14:00 ET + 13시간 = 다음날 새벽 3시.
    assert et_to_kst_hhmm(date(2026, 8, 12), 14, 0) == "03:00"


def test_to_dday_attaches_weekday_time_kst_freq_and_day_flags():
    events = [
        {"name": "Consumer Price Index", "date": "2026-08-12", "source": "x"},
        {"name": "Unemployment Insurance Weekly Claims Report", "date": "2026-08-13", "source": "x"},
    ]
    out = to_dday(events, date(2026, 8, 12))
    by_name = {e["name"]: e for e in out}

    cpi = by_name["Consumer Price Index"]
    assert cpi["weekday"] == "수"
    assert cpi["time_kst"] == "21:30"
    assert cpi["freq"] == "월간"
    assert cpi["is_today"] is True
    assert cpi["is_tomorrow"] is False

    claims = by_name["Unemployment Insurance Weekly Claims Report"]
    assert claims["weekday"] == "목"
    assert claims["is_today"] is False
    assert claims["is_tomorrow"] is True


def test_to_dday_weekday_is_single_korean_char():
    events = [{"name": "X", "date": "2026-08-12", "source": "x"}]
    out = to_dday(events, date(2026, 8, 12))
    assert out[0]["weekday"] in "월화수목금토일"
    assert len(out[0]["weekday"]) == 1


def test_to_dday_time_kst_empty_when_release_unmapped():
    events = [{"name": "Wholesale Trade", "date": "2026-08-12", "source": "x"}]
    out = to_dday(events, date(2026, 8, 12))
    assert out[0]["time_kst"] == ""


def test_parse_fomc_uses_range_end_date():
    html = "<p>August 18-19, 2026</p>"
    events = parse_fomc(html)
    assert events == [
        {"name": "FOMC Meeting", "date": "2026-08-19", "source": "Federal Reserve"}
    ]


def test_parse_fomc_dedupes_same_date():
    html = "<p>August 18-19, 2026</p><p>August 18-19, 2026</p>"
    assert len(parse_fomc(html)) == 1


@pytest.mark.live
def test_fetch_calendar_returns_events_with_expected_keys():
    if not get_key("FRED_API_KEY"):
        pytest.skip("FRED_API_KEY 미설정")
    result = fetch_calendar(date.today())
    assert result["events"]
    for ev in result["events"]:
        assert "dday" in ev
        assert "days_ahead" in ev
        assert "high_impact" in ev


def test_near_days_use_korean_words_and_rest_keep_dday():
    """사흘 안쪽은 우리말이 즉시 읽힌다. 그 밖은 D-N 이 간결하다."""
    events = [
        {"name": "a", "date": "2026-08-12", "source": "x"},
        {"name": "b", "date": "2026-08-13", "source": "x"},
        {"name": "c", "date": "2026-08-14", "source": "x"},
        {"name": "d", "date": "2026-08-15", "source": "x"},
        {"name": "e", "date": "2026-08-26", "source": "x"},
    ]
    labels = [e["dday"] for e in to_dday(events, date(2026, 8, 12))]
    assert labels == ["오늘", "내일", "모레", "D-3", "D-14"]
