from datetime import date, datetime, timedelta

import pytest

from quant.core.report_clock import KST, publish_at, session_window


def _kst(dt):
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def test_kr_publishes_one_hour_before_0900():
    assert _kst(publish_at("KR", date(2026, 8, 12))) == "2026-08-12 08:00"


def test_kr_is_unaffected_by_us_dst():
    assert _kst(publish_at("KR", date(2026, 1, 15))) == "2026-01-15 08:00"


def test_us_summer_time_is_2130_kst():
    # EDT (UTC-4): 08:30 ET -> 12:30 UTC -> 21:30 KST
    assert _kst(publish_at("US", date(2026, 8, 12))) == "2026-08-12 20:00"


def test_us_standard_time_is_2230_kst():
    # EST (UTC-5): 08:30 ET -> 13:30 UTC -> 22:30 KST
    assert _kst(publish_at("US", date(2026, 1, 15))) == "2026-01-15 21:00"


@pytest.mark.parametrize(
    "session,expected",
    [
        (date(2026, 3, 6), "2026-03-06 21:00"),    # DST 시작 직전 (금)
        (date(2026, 3, 9), "2026-03-09 20:00"),    # DST 시작 직후 (월)
        (date(2026, 10, 30), "2026-10-30 20:00"),  # DST 종료 직전 (금)
        (date(2026, 11, 2), "2026-11-02 21:00"),   # DST 종료 직후 (월)
    ],
)
def test_us_dst_transitions(session, expected):
    assert _kst(publish_at("US", session)) == expected


def test_unknown_market_raises():
    with pytest.raises(ValueError, match="market"):
        publish_at("JP", date(2026, 8, 12))


def test_window_starts_at_previous_publish():
    now = datetime(2026, 8, 12, 8, 0, tzinfo=KST)
    prev = datetime(2026, 8, 11, 8, 0, tzinfo=KST)
    start, end = session_window(now, prev)
    assert start == prev and end == now


def test_window_spans_weekend():
    monday = datetime(2026, 8, 17, 8, 0, tzinfo=KST)
    friday = datetime(2026, 8, 14, 8, 0, tzinfo=KST)
    start, end = session_window(monday, friday)
    assert (end - start) == timedelta(days=3)


def test_window_defaults_to_24h_when_no_previous():
    now = datetime(2026, 8, 12, 8, 0, tzinfo=KST)
    start, end = session_window(now, None)
    assert (end - start) == timedelta(hours=24)


def test_window_ignores_future_previous():
    """직전 스냅샷이 미래면(시계 오류·수동 실행) 24시간으로 떨어진다."""
    now = datetime(2026, 8, 12, 8, 0, tzinfo=KST)
    future = datetime(2026, 8, 13, 8, 0, tzinfo=KST)
    start, end = session_window(now, future)
    assert (end - start) == timedelta(hours=24)


def test_us_lead_is_longer_than_kr_for_cron_buffer():
    """US 는 21:40 엔진 크론을 양쪽 DST 체제에서 모두 피해야 한다."""
    from quant.core.report_clock import LEAD

    assert LEAD["US"] > LEAD["KR"]
    assert (LEAD["US"] - LEAD["KR"]).total_seconds() == 5400


def test_us_publish_never_lands_in_engine_cron_window():
    """엔진이 21:40 에 US 관심종목을 초기화한다. 서머타임·표준시 **양쪽 모두**
    그 구간(21:20~22:00 KST)을 피해야 한다 — '2시간 전'은 표준시에 21:30 이라
    오히려 더 위험했다."""
    for session in (date(2026, 8, 12), date(2026, 1, 15),
                    date(2026, 3, 9), date(2026, 11, 2)):
        hhmm = publish_at("US", session).strftime("%H:%M")
        assert not ("21:20" <= hhmm <= "22:00"), f"{session} → {hhmm}"
