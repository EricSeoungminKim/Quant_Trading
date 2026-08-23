"""거래 세션 캘린더 + Clock 통합 테스트.

여기서 막으려는 결함은 둘 다 실제로 터졌던 것이다:
1. 세션이 09:30~16:00으로 하드코딩돼 조기폐장일(13:00 마감)을 인식하지 못했다.
   실측 14세션에서 엔진이 장 마감 후에도 포지션을 관리하고 시간외로 청산 주문을 냈다.
2. 마감 시각에 `is_market_open`이 False가 되면서 `minutes_to_close`가 None을 반환했고
   `should_flatten`이 조용히 False가 됐다. 청산 창이 통째로 사라져 3배 레버리지 ETF
   포지션이 며칠씩 살아남았다(10년 백테스트에서 청산 17건).
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.clock import SimClock
from quant.core.session import (
    BarSessionCalendar,
    MultiMarketBarSessionCalendar,
    StaticSessionCalendar,
    TossSessionCalendar,
    _to_datetime,
)

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")


def _bar_index(day: date, first: str, last: str, minutes: int = 5) -> pd.DatetimeIndex:
    """day의 first~last(ET, 봉 시가 기준) 구간을 minutes 간격으로."""
    return pd.date_range(
        f"{day} {first}", f"{day} {last}", freq=f"{minutes}min", tz=NY
    ).tz_convert("UTC")


# --------------------------------------------------------- BarSessionCalendar


def test_bar_calendar_derives_early_close_from_the_data():
    """조기폐장일은 봉이 일찍 끊긴다 — 별도 캘린더 없이 데이터가 그대로 말해준다."""
    normal = _bar_index(date(2024, 11, 27), "09:30", "15:55")
    early = _bar_index(date(2024, 11, 29), "09:30", "12:55")  # 추수감사절 다음날 13:00 마감
    cal = BarSessionCalendar(normal.append(early), interval_minutes=5, market="US")

    normal_session = cal.session("US", datetime(2024, 11, 27, 12, 0, tzinfo=NY))
    assert normal_session.close == datetime(2024, 11, 27, 16, 0, tzinfo=NY)

    early_session = cal.session("US", datetime(2024, 11, 29, 12, 0, tzinfo=NY))
    assert early_session.close == datetime(2024, 11, 29, 13, 0, tzinfo=NY)


def test_bar_calendar_returns_none_for_a_day_with_no_bars():
    cal = BarSessionCalendar(_bar_index(date(2024, 11, 27), "09:30", "15:55"), 5, "US")
    # 추수감사절 — 봉이 없다.
    assert cal.session("US", datetime(2024, 11, 28, 12, 0, tzinfo=NY)) is None


# ------------------------------------------------------------- Clock 통합


def test_flatten_fires_on_early_close_day():
    """조기폐장일 12:55에 청산이 걸려야 한다. 16:00 고정이면 안 걸린다."""
    early = _bar_index(date(2024, 11, 29), "09:30", "12:55")
    clock = SimClock(
        now=datetime(2024, 11, 29, 12, 55, tzinfo=NY), cadence_minutes=5,
        calendar=BarSessionCalendar(early, 5, "US"),
    )
    assert clock.minutes_to_close("US") == pytest.approx(5.0)
    assert clock.should_flatten("US", flatten_minutes=1) is True

    # 같은 시각을 고정 시간표로 보면 마감까지 185분 남았다고 착각한다.
    static_clock = SimClock(
        now=datetime(2024, 11, 29, 12, 55, tzinfo=NY), cadence_minutes=5,
        calendar=StaticSessionCalendar(),
    )
    assert static_clock.minutes_to_close("US") == pytest.approx(185.0)
    assert static_clock.should_flatten("US", flatten_minutes=1) is False


def test_flatten_fires_at_the_last_in_session_cycle_not_after_close():
    """청산은 세션 **안의** 마지막 판단 시점에 걸려야 한다.

    마감 이후에 걸리면 라이브에서 시간외 호가창으로 주문이 나간다. 대신 세션 안의
    마지막 사이클(5분봉이면 15:55)에서는 반드시 걸려야 한다 — 여기서 놓치면 청산
    창이 통째로 사라져 3배 레버리지 포지션이 다음 날로 넘어간다.
    """
    idx = _bar_index(date(2024, 6, 3), "09:30", "15:55")
    cal = BarSessionCalendar(idx, 5, "US")

    last_in_session = SimClock(datetime(2024, 6, 3, 15, 55, tzinfo=NY), 5, calendar=cal)
    assert last_in_session.is_market_open("US") is True
    assert last_in_session.minutes_to_close("US") == pytest.approx(5.0)
    assert last_in_session.should_flatten("US", flatten_minutes=1) is True

    # flatten_minutes=0이면 이 마지막 창마저 닫힌다(mtc - cadence == 0).
    # 그래서 config는 반드시 1 이상이어야 한다.
    assert last_in_session.should_flatten("US", flatten_minutes=0) is False

    at_close = SimClock(datetime(2024, 6, 3, 16, 0, tzinfo=NY), 5, calendar=cal)
    assert at_close.is_market_open("US") is False
    assert at_close.should_flatten("US", flatten_minutes=10) is False


def test_no_flatten_on_a_day_without_a_session():
    idx = _bar_index(date(2024, 6, 3), "09:30", "15:55")
    clock = SimClock(datetime(2024, 6, 4, 12, 0, tzinfo=NY), 5,
                     calendar=BarSessionCalendar(idx, 5, "US"))
    assert clock.is_market_open("US") is False
    assert clock.should_flatten("US", flatten_minutes=10) is False


def test_market_open_boundaries():
    idx = _bar_index(date(2024, 6, 3), "09:30", "15:55")
    cal = BarSessionCalendar(idx, 5, "US")
    for hhmm, expected in [((9, 29), False), ((9, 30), True), ((15, 59), True), ((16, 0), False)]:
        clock = SimClock(datetime(2024, 6, 3, *hhmm, tzinfo=NY), 5, calendar=cal)
        assert clock.is_market_open("US") is expected, hhmm


# --------------------------------------------------------- MultiMarketBarSessionCalendar


def test_multi_market_calendar_dispatches_to_the_matching_market():
    """관심종목 유니버스가 KR/US 섞인 경우 각 시장의 봉으로 만든 캘린더에
    올바르게 위임해야 한다 — 심볼 하나의 시장만 아는 단일 캘린더로는 다른
    시장을 답할 수 없다(mean_reversion 등이 심볼별로 market_of_symbol을
    추론해 개별 조회하기 때문)."""
    us_idx = _bar_index(date(2024, 6, 3), "09:30", "15:55")
    kr_idx = pd.date_range(
        "2024-06-03 09:00", "2024-06-03 15:25", freq="5min", tz=KST
    ).tz_convert("UTC")
    cal = MultiMarketBarSessionCalendar({
        "US": BarSessionCalendar(us_idx, 5, "US"),
        "KR": BarSessionCalendar(kr_idx, 5, "KR"),
    })

    us_session = cal.session("US", datetime(2024, 6, 3, 12, 0, tzinfo=NY))
    assert us_session.close == datetime(2024, 6, 3, 16, 0, tzinfo=NY)

    kr_session = cal.session("KR", datetime(2024, 6, 3, 12, 0, tzinfo=KST))
    assert kr_session.close == datetime(2024, 6, 3, 15, 30, tzinfo=KST)


def test_multi_market_calendar_fails_loudly_for_an_unsupported_market():
    """이 백테스트가 봉을 갖고 있지 않은 시장을 물으면 추측하지 말고 명시적으로
    실패해야 한다 — 조용한 스킵은 run_cycle이 삼켜 '거래 0건'이 가짜 성공으로
    보인다(mean_reversion이 실측으로 이렇게 죽었다)."""
    cal = MultiMarketBarSessionCalendar({
        "US": BarSessionCalendar(_bar_index(date(2024, 6, 3), "09:30", "15:55"), 5, "US"),
    })
    with pytest.raises(ValueError, match="KR"):
        cal.session("KR", datetime(2024, 6, 3, 12, 0, tzinfo=KST))


# ------------------------------------------------------- TossSessionCalendar


class _FakeTossClient:
    def __init__(self, payload=None, error: Exception | None = None):
        self._payload = payload
        self._error = error
        self.calls = 0

    def market_calendar(self, market, date_=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._payload


def _payload(start: str | None, end: str | None) -> dict:
    regular = None if start is None else {"startTime": start, "endTime": end}
    return {"today": {"date": "2024-11-29", "regularMarket": regular}}


def _kr_payload(start: str | None, end: str | None) -> dict:
    """KR 응답 형태 — US와 달리 세션이 `integrated` 아래 중첩된다 (QUICKREF 참고)."""
    integrated = None if start is None else {"regularMarket": {"startTime": start, "endTime": end}}
    return {"today": {"date": "2024-11-29", "integrated": integrated}}


def test_toss_calendar_reads_early_close_and_caches():
    client = _FakeTossClient(_payload("2024-11-29T09:30:00-05:00", "2024-11-29T13:00:00-05:00"))
    cal = TossSessionCalendar(client)
    now = datetime(2024, 11, 29, 12, 0, tzinfo=NY)

    session = cal.session("US", now)
    assert session.close == datetime(2024, 11, 29, 13, 0, tzinfo=NY)

    # 같은 거래일은 다시 조회하지 않는다(응답이 영업일 단위로만 바뀐다).
    cal.session("US", datetime(2024, 11, 29, 12, 5, tzinfo=NY))
    assert client.calls == 1


def test_toss_calendar_treats_null_regular_market_as_holiday():
    cal = TossSessionCalendar(_FakeTossClient(_payload(None, None)))
    assert cal.session("US", datetime(2024, 11, 28, 12, 0, tzinfo=NY)) is None


def test_toss_calendar_falls_back_loudly_and_retries(caplog):
    """조회 실패를 휴장으로 읽으면 안 된다 — API 장애/IP 차단과 구분되지 않는다.

    폴백은 하되 (1) 경고를 남기고 (2) 실패를 캐시하지 않아 다음 사이클에 재시도한다.
    """
    client = _FakeTossClient(error=RuntimeError("403 access_denied"))
    cal = TossSessionCalendar(client)
    now = datetime(2024, 6, 3, 12, 0, tzinfo=NY)

    with caplog.at_level("WARNING"):
        session = cal.session("US", now)
    assert session is not None, "휴장으로 처리하면 안 된다 — 고정 시간표로 폴백해야 한다"
    assert session.close == datetime(2024, 6, 3, 16, 0, tzinfo=NY)
    assert "폴백" in caplog.text

    cal.session("US", now)
    assert client.calls == 2, "실패를 캐시하면 일시적 장애가 하루 종일 지속된다"


def test_toss_calendar_parses_kr_nested_integrated_shape():
    """KR 응답은 US와 달리 세션이 `integrated` 아래 중첩된다 — `today.regularMarket`을
    그대로 찾으면(US 파싱 로직) KR은 항상 휴장으로 오판한다."""
    client = _FakeTossClient(_kr_payload(
        "2024-11-29T09:00:00+09:00", "2024-11-29T15:30:00+09:00"))
    cal = TossSessionCalendar(client)
    now = datetime(2024, 11, 29, 12, 0, tzinfo=KST)

    session = cal.session("KR", now)
    assert session is not None
    assert session.open == datetime(2024, 11, 29, 9, 0, tzinfo=KST)
    assert session.close == datetime(2024, 11, 29, 15, 30, tzinfo=KST)


def test_toss_calendar_treats_kr_null_integrated_as_holiday():
    cal = TossSessionCalendar(_FakeTossClient(_kr_payload(None, None)))
    assert cal.session("KR", datetime(2024, 11, 28, 12, 0, tzinfo=KST)) is None


@pytest.mark.parametrize("value,expected_hour", [
    ("2024-11-29T13:00:00-05:00", 13),   # ISO + 오프셋
    ("13:00", 13),                        # 시각만
    ("13:00:00", 13),
])
def test_to_datetime_accepts_documented_and_plausible_formats(value, expected_hour):
    got = _to_datetime(value, date(2024, 11, 29), NY)
    assert got is not None and got.astimezone(NY).hour == expected_hour


def test_to_datetime_rejects_garbage():
    assert _to_datetime("장마감", date(2024, 11, 29), NY) is None
    assert _to_datetime(None, date(2024, 11, 29), NY) is None


def test_to_datetime_bare_time_uses_the_requested_day_not_today():
    """시각 전용 문자열의 날짜는 **요청한 거래일**이어야 한다.

    실제로 있었던 죽은 분기: pd.Timestamp("09:00:00")는 1970년이 아니라 호출
    시점의 오늘 날짜를 반환하므로 `ts.year == 1970` 휴리스틱이 절대 걸리지 않았고,
    bare 시각이 오면 세션 개장/마감이 조용히 오늘 날짜로 계산됐다 — 내일 캘린더를
    미리 조회하는 순간 마감 판정이 통째로 어긋나는 결함이다.
    """
    day = date(2024, 11, 29)  # 테스트 실행일과 무관한 과거 날짜
    got = _to_datetime("09:00:00", day, KST)
    assert got == datetime(2024, 11, 29, 9, 0, tzinfo=KST)
    got_short = _to_datetime("13:05", day, KST)
    assert got_short == datetime(2024, 11, 29, 13, 5, tzinfo=KST)
