"""결함주입 5/6 — 시간: tz-naive 타임스탬프, 세션 경계 정각, US DST 전환(2026-11-01),
KR 공휴일(추석 2026-09-24/25)."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from quant.adapters.execution.paper import PaperBroker
from quant.core.clock import SimClock
from quant.core.models import Order, Side
from quant.core.portfolio.portfolio import Portfolio
from quant.core.session import StaticSessionCalendar, in_continuous_session

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")


# --------------------------------------------------------- 1. tz-naive 타임스탬프

class _NaiveQuote:
    def __init__(self, symbol, price, ts):
        self.symbol = symbol
        self.price = price
        self.ts = ts


def test_paper_broker_accepts_naive_quote_timestamp_without_crashing():
    """어댑터 버그로 tz-naive 시각이 quote에 섞여 들어와도 체결 자체는 죽지 않는다
    (paper.py 스코프 확인). 이 시각이 원장(quant/control/ledger.py)까지 흘러가면
    그쪽은 이미 방어적으로 UTC를 보정한다(`ts.tzinfo is None: ts = ts.replace(
    tzinfo=UTC)`, ledger.py 여러 곳) — 이 저장소 범위(quant/trade 이하) 밖이라
    수정 대상은 아니지만, 경계에서 크래시가 없다는 것만 여기서 확인한다."""
    class _Feed:
        def quote(self, symbol):
            return _NaiveQuote(symbol, 100.0, datetime(2026, 1, 5, 10, 0))  # naive

    pf = Portfolio(cash=1_000_000.0)
    b = PaperBroker(data=_Feed(), portfolio=pf, market_of={"TQQQ": "US"})
    state = b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=1, strategy_id="s"))
    assert state.fill is not None
    assert state.fill.ts.tzinfo is None  # 있는 그대로 통과시킨다 — 위조하지 않는다


# ------------------------------------------------------- 2. 세션 경계 정각(15:30:00)

def test_kr_market_closes_exactly_at_15_30_00():
    calendar = StaticSessionCalendar()
    at_close = datetime(2026, 1, 5, 15, 30, 0, tzinfo=KST)
    just_before = datetime(2026, 1, 5, 15, 29, 59, tzinfo=KST)
    session = calendar.session("KR", at_close)
    assert session is not None
    assert not (session.open <= at_close < session.close)  # 정각은 이미 닫힘
    assert session.open <= just_before < session.close


def test_kr_continuous_trading_window_closes_exactly_at_15_20_00():
    """15:20:00 정각부터는 장 마감 동시호가 — 연속 거래가 아니다(경계 포함 여부가
    실거래 체결 가능성을 가른다, session.py 모듈 docstring 참고)."""
    assert in_continuous_session("KR", datetime(2026, 1, 5, 15, 19, 59, tzinfo=KST))
    assert not in_continuous_session("KR", datetime(2026, 1, 5, 15, 20, 0, tzinfo=KST))


def test_kr_continuous_trading_window_opens_exactly_at_09_00_00():
    assert in_continuous_session("KR", datetime(2026, 1, 5, 9, 0, 0, tzinfo=KST))
    assert not in_continuous_session("KR", datetime(2026, 1, 5, 8, 59, 59, tzinfo=KST))


def test_us_market_closes_exactly_at_16_00_00():
    calendar = StaticSessionCalendar()
    session = calendar.session("US", datetime(2026, 1, 5, 16, 0, 0, tzinfo=NY))
    at_close = datetime(2026, 1, 5, 16, 0, 0, tzinfo=NY)
    assert not (session.open <= at_close < session.close)


# --------------------------------------------------------- 3. US DST 전환(11/1)

def test_session_hours_correct_across_us_dst_fallback_2026_11_01():
    """2026-11-01(일)이 서머타임 종료일(EDT→EST) — 실제 거래일인 전후 금/월요일에
    로컬 개장 시각(09:30 America/New_York)이 그대로 유지되는지, 그리고 UTC로
    환산한 오프셋이 실제로 한 시간 이동하는지 둘 다 확인한다. zoneinfo가 이걸
    자동으로 처리하므로 이 테스트는 회귀 방지용(수동 UTC 오프셋 하드코딩이
    섞여 들어오면 깨진다)."""
    before = datetime(2026, 10, 30, 9, 30, tzinfo=NY)  # 금요일, EDT(UTC-4)
    after = datetime(2026, 11, 2, 9, 30, tzinfo=NY)     # 월요일, EST(UTC-5)

    assert SimClock(before).is_market_open("US")
    assert SimClock(after).is_market_open("US")

    # UTC 오프셋이 실제로 한 시간 이동했다 — zoneinfo가 DST를 반영하고 있다는 증거.
    assert before.utcoffset().total_seconds() / 3600 == -4
    assert after.utcoffset().total_seconds() / 3600 == -5
    # 09:30 로컬 개장은 두 날 모두 UTC로 각각 13:30/14:30 — 시스템이 로컬시각
    # 기준으로 세션을 판정하는 한(session.py `local = now.astimezone(tz)`) 이
    # 전환은 저절로 처리된다.
    assert before.astimezone(UTC).hour == 13
    assert after.astimezone(UTC).hour == 14


def test_should_flatten_unaffected_by_dst_transition_when_expressed_in_local_time():
    clock_before = SimClock(datetime(2026, 10, 30, 15, 55, tzinfo=NY), cadence_minutes=15)
    clock_after = SimClock(datetime(2026, 11, 2, 15, 55, tzinfo=NY), cadence_minutes=15)
    assert clock_before.should_flatten("US", flatten_minutes=10) is True
    assert clock_after.should_flatten("US", flatten_minutes=10) is True


# ------------------------------------------------ 4. KR 공휴일(추석 2026-09-24/25)

def test_static_calendar_now_recognizes_kr_thanksgiving_holiday():
    """실측 확인(오너 지시 항목): 결함주입 세션 착수 시점에는 `StaticSessionCalendar`가
    요일만 보고 2026-09-24(목, 추석 연휴)/09-25(금, 추석)를 '개장'으로 오판했다 —
    표에 없는 공휴일은 여전히 못 걸러낸다는 것이 알려진 설계 경계였다. 이 세션
    도중 `quant/core/session.py`에 `_STATIC_HOLIDAYS`(2026-09-07 추가, KR 추석/
    개천절/한글날/크리스마스, US 추수감사절/크리스마스)가 반영돼 이제는 정확히
    걸러진다 — 회귀 방지로 고정한다. 남는 잔여 위험은 docstring이 명시한 그대로다:
    표에 없는 대체휴일·임시공휴일, 그리고 표를 매년 갱신하지 않으면 다시 조용히
    틀린다는 것(라이브는 `TossSessionCalendar`가 우선이라 이 폴백 표가 실제로
    쓰이는 건 그 조회가 실패했을 때뿐)."""
    calendar = StaticSessionCalendar()
    for holiday in (datetime(2026, 9, 24, 10, 0, tzinfo=KST), datetime(2026, 9, 25, 10, 0, tzinfo=KST)):
        session = calendar.session("KR", holiday)
        assert session is None, f"{holiday.date()}는 추석 연휴 — 휴장으로 판정돼야 한다"


def test_static_calendar_holiday_table_is_a_finite_snapshot_not_a_general_solution():
    """잔여 위험의 정량화: 표에 없는 해(예: 2027년 추석)는 여전히 못 잡는다 —
    이건 하드코딩된 연도별 스냅샷이지 알고리즘적 음력 공휴일 계산이 아니다."""
    calendar = StaticSessionCalendar()
    # 2027년은 표에 없다 — 같은 요일 패턴(평일)이면 그대로 '개장' 오판이 재발한다.
    session = calendar.session("KR", datetime(2027, 9, 24, 10, 0, tzinfo=KST))
    assert session is not None  # 알려진 잔여 갭 — 표를 매년 갱신해야 한다는 그 주장
