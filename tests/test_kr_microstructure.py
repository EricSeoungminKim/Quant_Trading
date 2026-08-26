"""한국장 미시구조 — 연속 거래 구간과 동시호가 (2026-08-26 소유자 교정).

소유자가 알려준 실제 구조:

- **장 시작 동시호가 08:30~09:00**: 30분간 주문만 모으고 **체결하지 않는다**.
  09:00 정각에 하나의 시가로 일괄 체결.
- **장전 시간외 종가 08:30~08:40**: 전일 종가 고정가로만 거래(가격 발견 없음).
- **장 마감 동시호가 15:20~15:30**: 주문을 모아 15:30 종가로 일괄 체결.

따라서 **가격이 발견되는 연속 거래는 09:00~15:20 뿐이다.**

## 이 파일이 잡는 실사고 (2026-08-26)

엔진이 KR 프리마켓 창(08:00~08:50)에서 직접 진입해 **08:27·08:46 에 체결**을
기록했다. 두 시각 모두 한국장에서는 체결이 불가능하다 — 08:30 이전엔 거래
자체가 없고, 08:30~09:00 은 주문만 모은다. 그런데 09:00 정각 시가가 갭으로
열리면서 손절선(-1.0%)을 2.8% 지나쳐 **의도한 -1% 손실이 -3.8% 가 됐다**
(000720: 진입 129,200 / 손절선 127,900 / 실제 청산 124,369).

즉 그 체결은 **애초에 존재할 수 없는 거래**였고, 손익도 실재하지 않는다.
페이퍼 브로커는 데이터피드가 준 가격이면 무엇이든 체결시키므로 이런 구조적
오류를 스스로 잡지 못한다 — 세션 모델이 잡아야 한다.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

from quant.core.session import continuous_window, in_continuous_session

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
DAY = date(2026, 8, 26)  # 수요일


def _kst(h: int, m: int) -> datetime:
    return datetime.combine(DAY, dtime(h, m), tzinfo=KST)


# ---------------------------------------------------------------- 연속 거래 구간

def test_kr_continuous_window_ends_at_1520_not_1530():
    """연속 거래는 09:00~15:20. 15:20~15:30 은 동시호가라 가격 발견이 없다."""
    open_t, close_t = continuous_window("KR")
    assert open_t == dtime(9, 0)
    assert close_t == dtime(15, 20)


def test_us_continuous_window_is_the_full_session():
    """US 는 09:30~16:00 이 통째로 연속 거래다(마감 동시호가 개념이 다르다)."""
    assert continuous_window("US") == (dtime(9, 30), dtime(16, 0))


# ---------------------------------------------------------------- 시점 판정

def test_premarket_hours_are_not_continuous_session():
    """실사고 시각들: 08:27(거래 없음)·08:46(동시호가 접수 중) 둘 다 거래 불가."""
    assert not in_continuous_session("KR", _kst(8, 27))
    assert not in_continuous_session("KR", _kst(8, 46))
    assert not in_continuous_session("KR", _kst(8, 59))


def test_regular_hours_are_continuous_session():
    assert in_continuous_session("KR", _kst(9, 0))
    assert in_continuous_session("KR", _kst(12, 0))
    assert in_continuous_session("KR", _kst(15, 19))


def test_closing_auction_is_not_continuous_session():
    """15:20~15:30 은 주문 접수 구간 — 이때의 '현재가'로 체결을 모델링하면
    실재하지 않는 손익이 만들어진다."""
    assert not in_continuous_session("KR", _kst(15, 20))
    assert not in_continuous_session("KR", _kst(15, 25))
    assert not in_continuous_session("KR", _kst(15, 30))


def test_weekend_is_never_continuous():
    sat = datetime.combine(date(2026, 8, 29), dtime(11, 0), tzinfo=KST)
    assert not in_continuous_session("KR", sat)


def test_us_continuous_session_uses_new_york_time():
    """시장별 tz 로 판정한다 — KST 로 US 를 재면 종일 어긋난다."""
    assert in_continuous_session("US", datetime.combine(DAY, dtime(10, 0), tzinfo=NY))
    assert not in_continuous_session("US", datetime.combine(DAY, dtime(8, 0), tzinfo=NY))


# ---------------------------------------------------------------- 마감 청산과 동시호가

def test_kr_minutes_to_close_measures_to_continuous_end():
    """`minutes_to_close` 는 **연속 거래가 끝나는 시점**까지 잰다 (2026-08-26).

    KR 명목 마감은 15:30 이지만 15:20~15:30 은 동시호가다 — 15:30 기준으로 재면
    마감 청산(flatten_before_close_minutes=1)이 15:29, 즉 동시호가 한복판에 걸려
    그 시각의 '현재가'(예상체결가)로 실재하지 않는 체결을 만든다. 소비처 전수
    (EoD 청산·마감 임박 신규진입 금지) 모두 "연속으로 체결 가능한 남은 시간"을
    원하므로 여기 한 곳에서 클램프한다."""
    from quant.core.clock import SimClock

    clock = SimClock(_kst(15, 10), cadence_minutes=5 / 60)
    mtc = clock.minutes_to_close("KR")
    assert mtc is not None and abs(mtc - 10.0) < 1e-9, \
        f"15:10 → 연속 거래 끝(15:20)까지 10분이어야 하는데 {mtc}"


def test_kr_should_flatten_fires_before_auction_not_inside_it():
    from quant.core.clock import SimClock

    # 15:19: 연속 거래 잔여 1분 — 지금이 마지막 체결 기회, 청산해야 한다.
    assert SimClock(_kst(15, 19), cadence_minutes=5 / 60).should_flatten("KR", 1.0)
    # 15:25(동시호가 안): 이미 연속 거래 밖 — 청산 신호를 만들어도 체결가가 허구다.
    assert not SimClock(_kst(15, 25), cadence_minutes=5 / 60).should_flatten("KR", 1.0)
    # 15:00: 아직 여유 — 청산하지 않는다.
    assert not SimClock(_kst(15, 0), cadence_minutes=5 / 60).should_flatten("KR", 1.0)


def test_us_minutes_to_close_unchanged():
    """US 는 정규장 전체가 연속 거래 — 종전과 동일하게 16:00 까지 잰다."""
    from quant.core.clock import SimClock

    now = datetime.combine(DAY, dtime(15, 30), tzinfo=NY)
    mtc = SimClock(now, cadence_minutes=5 / 60).minutes_to_close("US")
    assert mtc is not None and abs(mtc - 30.0) < 1e-9
