"""종가배팅(close_bet) — 2026-08-25 전략 4종 체제 ③, 2026-08-26 마감 동시호가 반영.

계약: CLOSE_BET 태그 + 진입 창(15:15~15:19 — 연속 거래가 끝나는 동시호가(15:20)
직전, 양봉·마감강도 차트 확인 후 그 자리에서 진입) + 동시호가 구간(15:20~15:30)에는
절대 진입하지 않는다. 다음날 손절(-1%) < 익절(+2%) 비대칭 + 시초 30분 데드라인.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.ports import Context
from quant.core.models import Position, Quote, SignalAction
from quant.trade.strategy.close_bet import CloseBetStrategy

KST = ZoneInfo("Asia/Seoul")
DAY1 = date(2026, 8, 25)
DAY2 = date(2026, 8, 26)


class FakeClock:
    def __init__(self, now, kr_open=True):
        self._now = now
        self._kr_open = kr_open

    def now(self):
        return self._now

    def is_market_open(self, market):
        return self._kr_open if market == "KR" else False

    def minutes_to_close(self, market):
        return 30.0

    def cadence_minutes(self):
        return 1.0

    def should_flatten(self, market, m):
        return False


class FakeFeed:
    def __init__(self, bars, quotes):
        self._bars = bars
        self._quotes = quotes

    def history(self, symbol, interval, n):
        df = self._bars.get(symbol)
        return df if df is not None else pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"])

    def quote(self, symbol):
        p = self._quotes.get(symbol)
        return Quote(symbol=symbol, ts=datetime.now(KST), price=p) if p else None


class FakeBroker:
    def __init__(self, positions=None):
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self):
        return 10_000_000.0


def _day_bars(day, opens, highs, lows, closes):
    n = len(closes)
    idx = pd.DatetimeIndex(
        [datetime.combine(day, dtime(9, i % 60), tzinfo=KST) for i in range(n)])
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": [1000.0] * n}, index=idx)


def _strong_close_bars(day=DAY1):
    """양봉 + 고가 근처: 시가 100 → 저가 99 → 고가 110, 현재가는 quote 몫."""
    n = 40
    closes = [100.0 + i * 0.25 for i in range(n)]
    return _day_bars(day, [100.0] * n, [110.0] * n, [99.0] * n, closes)


def _ctx(now, bars, quotes, positions=None, kr_open=True):
    return Context(clock=FakeClock(now, kr_open), data=FakeFeed(bars, quotes),
                   broker=FakeBroker(positions))


def _strat(**over):
    params = {"stop_pct": 1.0, "take_profit_pct": 2.0,
              "exit_deadline_minutes_after_open": 30}
    params.update(over)
    return CloseBetStrategy(["005930"], params, tags_of={"005930": ["CLOSE_BET"]})


ENTRY_TIME = datetime.combine(DAY1, dtime(15, 17), tzinfo=KST)


def test_enters_tagged_strong_close_in_window():
    s = _strat()
    # 현재가 109 → 강도 (109-99)/(110-99)=0.91, 양봉(>100)
    sigs = s.on_cycle(_ctx(ENTRY_TIME, {"005930": _strong_close_bars()}, {"005930": 109.0}))
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.action == SignalAction.ENTER_LONG
    assert sig.stop == pytest.approx(109.0 * 0.99)
    assert sig.target == pytest.approx(109.0 * 1.02)
    assert "종가배팅" in sig.reason


def test_no_entry_outside_window():
    s = _strat()
    early = datetime.combine(DAY1, dtime(14, 30), tzinfo=KST)
    assert s.on_cycle(_ctx(early, {"005930": _strong_close_bars()}, {"005930": 109.0})) == []


def test_no_entry_during_closing_auction():
    """15:20~15:30 은 동시호가 — entry_end 를 그 안까지 잘못 넓혀도(설정 실수)
    in_continuous_session 가드가 진입을 막는다. 이 구간의 '현재가'는 우리 데이터
    모델에서 실재하는 체결가가 아니다(quant.core.session.in_continuous_session
    의 원칙과 동일 — 2026-08-26 scalp_1m 프리마켓 수리와 같은 결)."""
    s = _strat(entry_end_hhmm=[15, 30])  # 의도적으로 동시호가까지 창을 늘려 봄
    during_auction = datetime.combine(DAY1, dtime(15, 25), tzinfo=KST)
    assert s.on_cycle(_ctx(during_auction, {"005930": _strong_close_bars()}, {"005930": 109.0})) == []


def test_no_entry_without_tag():
    s = CloseBetStrategy(["005930"], {}, tags_of={})
    assert s.on_cycle(_ctx(ENTRY_TIME, {"005930": _strong_close_bars()}, {"005930": 109.0})) == []


def test_rejects_weak_close_and_red_candle():
    s = _strat()
    # 마감 강도 부족: 현재가 101 → (101-99)/11 = 0.18
    assert s.on_cycle(_ctx(ENTRY_TIME, {"005930": _strong_close_bars()}, {"005930": 101.0})) == []
    assert "마감 강도" in s.last_reject["005930"]
    # 음봉: 현재가 99.5 < 시가 100
    s2 = _strat()
    assert s2.on_cycle(_ctx(ENTRY_TIME, {"005930": _strong_close_bars()}, {"005930": 99.5})) == []
    assert "양봉 아님" in s2.last_reject["005930"]


def test_once_per_day():
    s = _strat()
    ctx = _ctx(ENTRY_TIME, {"005930": _strong_close_bars()}, {"005930": 109.0})
    assert len(s.on_cycle(ctx)) == 1
    assert s.on_cycle(ctx) == []


def _held(entry=109.0, session=DAY1):
    return Position(symbol="005930", qty=10, avg_cost=entry, meta={
        "lots": {"close_bet": {"qty": 10, "avg_cost": entry, "entry": entry,
                               "stop": entry * 0.99, "target": entry * 1.02,
                               "session": session.isoformat()}}})


def test_holds_through_entry_day_close():
    """진입 당일엔 어떤 가격이든 안 판다 — 오버나이트가 이 전략이다."""
    s = _strat()
    late = datetime.combine(DAY1, dtime(15, 25), tzinfo=KST)
    sigs = s.on_cycle(_ctx(late, {}, {"005930": 106.0}, {"005930": _held()}))
    assert sigs == []


def test_next_day_stop_on_break_below_close():
    s = _strat()
    nxt = datetime.combine(DAY2, dtime(9, 5), tzinfo=KST)
    sigs = s.on_cycle(_ctx(nxt, {}, {"005930": 107.0}, {"005930": _held()}))  # -1.8%
    assert len(sigs) == 1 and "손절" in sigs[0].reason


def test_next_day_take_profit_on_gap():
    s = _strat()
    nxt = datetime.combine(DAY2, dtime(9, 3), tzinfo=KST)
    sigs = s.on_cycle(_ctx(nxt, {}, {"005930": 112.0}, {"005930": _held()}))  # +2.75%
    assert len(sigs) == 1 and "익절" in sigs[0].reason


def test_next_day_deadline_exit_when_neither_hit():
    s = _strat()
    at_29 = datetime.combine(DAY2, dtime(9, 29), tzinfo=KST)
    assert s.on_cycle(_ctx(at_29, {}, {"005930": 109.5}, {"005930": _held()})) == []
    at_31 = datetime.combine(DAY2, dtime(9, 31), tzinfo=KST)
    sigs = s.on_cycle(_ctx(at_31, {}, {"005930": 109.5}, {"005930": _held()}))
    assert len(sigs) == 1 and "정리" in sigs[0].reason


def test_asymmetry_lose_small_win_big():
    """방어 계약: 손절폭 < 익절폭 이 기본값에서 성립해야 한다."""
    s = _strat()
    assert s.stop_pct < s.take_profit_pct


def test_afternoon_entry_works_well_before_the_close():
    """진입 창은 **오후장**이지 종가 근접이 아니다 (2026-08-26 소유자 정정:
    "종가 단타가 아니라 오후장에 구매해서 다음날 아침 상승갭을 미리 예상하고
    판단해서 진입하는거야. 즉 다음날 아침에 팔아야해"). 15:05 — 창 초입 —
    에도 조건이 서면 진입한다. 같은 날 15:15 로 좁혔던 것은 "종가 단일가에
    최대한 가깝게"라는 잘못 읽은 의도의 과최적화였다 — 소유자 원 스펙은
    "15:00~15:20 선정"이다."""
    s = _strat()
    t = datetime.combine(DAY1, dtime(15, 5), tzinfo=KST)
    sigs = s.on_cycle(_ctx(t, {"005930": _strong_close_bars()}, {"005930": 109.0}))
    assert len(sigs) == 1
    assert sigs[0].action == SignalAction.ENTER_LONG
