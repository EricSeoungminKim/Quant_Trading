"""Clock 테스트 — 특히 마감 전 강제청산이 판단 주기를 건너뛰지 않는지.

배경(2026-07-28 발견): 원래 구현은 `minutes_to_close <= flatten_minutes`로
청산 시점을 판정했다. 15분봉 백테스트에서 미국장의 마지막 판단 시점은
15:45(잔여 15분)이고 그 다음 판단은 장 마감 이후라, flatten_before_close_minutes=10
이면 조건이 수학적으로 절대 성립하지 않았다. 오버나이트 금지가 무력화됐고,
10초마다 도는 라이브는 정상 작동해서 백테스트와 라이브가 다르게 행동했다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from quant.core.clock import SimClock, WallClock

NY = ZoneInfo("America/New_York")


def _at(hour: int, minute: int) -> datetime:
    """2026-01-05(월) 미국 동부 시각."""
    return datetime(2026, 1, 5, hour, minute, tzinfo=NY)


def test_session_open_and_close_boundaries():
    assert SimClock(_at(9, 30)).is_market_open("US")
    assert SimClock(_at(15, 59)).is_market_open("US")
    assert not SimClock(_at(16, 0)).is_market_open("US")   # 마감 시각은 이미 닫힘
    assert not SimClock(_at(9, 29)).is_market_open("US")


def test_weekend_is_closed():
    saturday = datetime(2026, 1, 10, 12, 0, tzinfo=NY)
    assert not SimClock(saturday).is_market_open("US")
    assert SimClock(saturday).minutes_to_close("US") is None


def test_flatten_fires_on_the_last_15m_decision_point():
    """이 테스트가 회귀 방지의 핵심이다.

    15분봉 리플레이의 마지막 장중 판단 시점은 15:45(잔여 15분)이다.
    naive 조건(15 <= 10)은 거짓이라 청산이 영원히 발동하지 않았다."""
    clock = SimClock(_at(15, 45), cadence_minutes=15)
    assert clock.minutes_to_close("US") == pytest.approx(15.0)
    assert clock.should_flatten("US", flatten_minutes=10) is True
    # naive 조건이 왜 실패했는지 명시적으로 박아둔다
    assert not (clock.minutes_to_close("US") <= 10)


def test_flatten_does_not_fire_while_another_decision_point_remains():
    """15:30에는 아직 15:45라는 판단 기회가 남아 있으므로 청산하지 않는다."""
    clock = SimClock(_at(15, 30), cadence_minutes=15)
    assert clock.minutes_to_close("US") == pytest.approx(30.0)
    assert clock.should_flatten("US", flatten_minutes=10) is False


def test_live_cadence_flattens_close_to_the_configured_window():
    """라이브(10초 주기)에서는 설정값에 거의 정확히 붙어서 발동해야 한다."""
    live = WallClock(poll_seconds=10)
    assert live.cadence_minutes() == pytest.approx(10 / 60)

    class _Frozen(WallClock):
        def __init__(self, now, poll_seconds=10):
            super().__init__(poll_seconds)
            self._frozen = now

        def now(self):
            return self._frozen

    assert _Frozen(_at(15, 49)).should_flatten("US", 10) is False   # 잔여 11분
    assert _Frozen(_at(15, 50)).should_flatten("US", 10) is True    # 잔여 10분


def test_backtest_and_live_agree_on_intent_despite_different_cadence():
    """같은 flatten_minutes 설정이 두 환경 모두에서 '마감 전 청산'을 보장한다.

    시각까지 같을 수는 없다(판단 주기가 다르므로). 보장해야 하는 것은
    '마감 전에 반드시 한 번은 발동한다'이다."""
    for cadence in (1, 5, 15, 30):
        fired = [
            m for m in range(0, 390)
            if SimClock(_at(9, 30) + timedelta(minutes=m),
                        cadence_minutes=cadence).should_flatten("US", 10)
        ]
        assert fired, f"cadence={cadence}분에서 마감 전 청산이 한 번도 발동하지 않음"


def test_market_closed_never_reports_flatten():
    """장이 닫힌 뒤에는 청산 신호를 만들지 않는다 — 이미 늦었고, 다음 세션에
    엉뚱한 시각에 발동하면 안 된다."""
    assert SimClock(_at(16, 30), cadence_minutes=15).should_flatten("US", 10) is False
    assert SimClock(_at(8, 0), cadence_minutes=15).should_flatten("US", 10) is False


def test_kr_session_window():
    kst = ZoneInfo("Asia/Seoul")
    assert SimClock(datetime(2026, 1, 5, 9, 0, tzinfo=kst)).is_market_open("KR")
    assert not SimClock(datetime(2026, 1, 5, 15, 30, tzinfo=kst)).is_market_open("KR")
    clock = SimClock(datetime(2026, 1, 5, 15, 15, tzinfo=kst), cadence_minutes=15)
    assert clock.should_flatten("KR", flatten_minutes=10) is True
