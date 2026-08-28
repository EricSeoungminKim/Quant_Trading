"""`CloseBetPureStrategy`(순수함수 계약, quant.core.strategy_api)가 기존
`CloseBetStrategy`와 **같은 신호**를 내는지 증명한다 — 엔진 분리 설계 Phase A,
donchian(파일럿) → scalp_1m 다음 이전 대상.

**이 전략은 오버나이트다.** 다른 전략들의 "세션 롤 강제청산" 레일과 정반대로
오후장(15:00~15:19)에 사서 **다음 거래일 아침**에 판다(익절 +2% / 손절 -1% /
시초 30분 데드라인). 그래서 여기서 진짜로 증명해야 하는 것은 단일 사이클 동치가
아니라 **세션 경계를 넘는 상태의 왕복**이다:

    당일 15:17 진입 신호 → (루프가 체결 후 lot 에 state_update 적용) →
    당일 15:25 보유 유지 → 익일 09:0x 청산

`CloseBetPureStrategy`는 이 왕복을 두 갈래로 나눠 다룬다 — 하루 안에서만 사는
값(`_entered_date`/`last_reject`)만 `next_state`로 다니고, 밤을 넘겨야 하는
값(entry/stop/target/session)은 `Signal.state_update` → `Position.meta["lots"]` →
`StrategySnapshot.lots` 경로로 다닌다. 아래 `test_overnight_state_survives_*`
두 개가 그 설계를 고정한다(특히 "껍질 상태를 통째로 날려도(프로세스 재시작)
익일 청산이 그대로 나온다" 쪽 — `next_state`에 방어선을 넣었다면 실패한다).

전 층위에서 `CloseBetPureShell.on_cycle(ctx)`(= `Strategy` Protocol 그대로)를
쓴다 — `StrategySnapshot`을 손으로 조립하지 않고 껍질이 실제로 `ctx`에서
스냅샷을 만드는 전체 경로(requirements() → 스냅샷 조립 → decide())를 태워
legacy `CloseBetStrategy.on_cycle(ctx)`와 나란히 비교한다(shell 배선 버그도 잡힌다).
`tests/test_scalp_1m_pure.py`와 같은 구조다.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Position, Quote, SignalAction
from quant.core.ports import Context
from quant.trade.strategy.close_bet import CloseBetPureShell, CloseBetStrategy

KST = ZoneInfo("Asia/Seoul")
DAY1 = date(2026, 8, 25)  # 월
DAY2 = date(2026, 8, 26)  # 화
SYMBOL = "005930"

LEGACY_ID = "close_bet"
PURE_ID = "close_bet_pure"

ENTRY_TIME = datetime.combine(DAY1, dtime(15, 17), tzinfo=KST)


# ============================================================ 페이크 인프라
# test_close_bet.py와 같은 인터페이스 + 껍질이 요구하는 minutes_to_close/
# cadence_minutes 를 갖춘다(PureStrategyShell 이 스냅샷을 만들 때 부른다).

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
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df.tail(n)

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


def _params(**over):
    params = {"stop_pct": 1.0, "take_profit_pct": 2.0,
              "exit_deadline_minutes_after_open": 30}
    params.update(over)
    return params


def _pair(**over):
    """같은 파라미터의 (legacy, pure-shell) 한 쌍."""
    tags = {SYMBOL: ["CLOSE_BET"]}
    legacy = CloseBetStrategy([SYMBOL], _params(**over), id=LEGACY_ID, tags_of=tags)
    pure = CloseBetPureShell([SYMBOL], _params(**over), id=PURE_ID, tags_of=tags)
    return legacy, pure


def _held(strategy_id, entry=109.0, session=DAY1):
    """진입이 체결돼 lot 이 채워진 포지션 — 전략 id 별로 lot 키가 다르다."""
    return Position(symbol=SYMBOL, qty=10, avg_cost=entry, meta={
        "lots": {strategy_id: {"qty": 10, "avg_cost": entry, "entry": entry,
                               "stop": entry * 0.99, "target": entry * 1.02,
                               "session": session.isoformat()}}})


def _fingerprint(signals):
    """전략 id 를 제외한 신호의 관측 가능한 전부 — 동치 비교의 기준."""
    return [
        (s.symbol, s.action, s.target_weight, s.exit_fraction,
         None if s.stop is None else round(s.stop, 9),
         None if s.target is None else round(s.target, 9),
         s.reason, _scrub(s.state_update))
        for s in signals
    ]


def _scrub(state_update):
    if not state_update:
        return None
    out = dict(state_update)
    out.pop("strategy", None)  # 전략 id 자체는 다를 수밖에 없다
    return tuple(sorted(out.items()))


def _both(legacy, pure, now, bars, quotes, positions_of=None, kr_open=True):
    """같은 세계를 legacy/pure 양쪽에 흘리고 두 신호 목록을 돌려준다.

    `positions_of`: strategy_id → {symbol: Position}. lot 키가 전략 id 라서
    포지션은 양쪽에 따로 만들어야 한다."""
    positions_of = positions_of or {}
    ls = legacy.on_cycle(_ctx(now, bars, quotes, positions_of.get(LEGACY_ID), kr_open))
    ps = pure.on_cycle(_ctx(now, bars, quotes, positions_of.get(PURE_ID), kr_open))
    return ls, ps


def _assert_equivalent(ls, ps):
    assert _fingerprint(ls) == _fingerprint(ps)
    assert all(s.strategy_id == LEGACY_ID for s in ls)
    assert all(s.strategy_id == PURE_ID for s in ps)


# ============================================================ 1) 단일 사이클 동치

def test_entry_in_window_equivalent():
    legacy, pure = _pair()
    ls, ps = _both(legacy, pure, ENTRY_TIME,
                   {SYMBOL: _strong_close_bars()}, {SYMBOL: 109.0})
    _assert_equivalent(ls, ps)
    assert len(ps) == 1 and ps[0].action == SignalAction.ENTER_LONG
    assert ps[0].stop == pytest.approx(109.0 * 0.99)
    assert ps[0].target == pytest.approx(109.0 * 1.02)


def test_entry_at_window_start_equivalent():
    """진입 창은 **오후장**이지 종가 근접이 아니다(2026-08-26 소유자 정정)."""
    legacy, pure = _pair()
    t = datetime.combine(DAY1, dtime(15, 5), tzinfo=KST)
    ls, ps = _both(legacy, pure, t, {SYMBOL: _strong_close_bars()}, {SYMBOL: 109.0})
    _assert_equivalent(ls, ps)
    assert len(ps) == 1


def test_no_entry_before_window_equivalent():
    legacy, pure = _pair()
    early = datetime.combine(DAY1, dtime(14, 30), tzinfo=KST)
    ls, ps = _both(legacy, pure, early, {SYMBOL: _strong_close_bars()}, {SYMBOL: 109.0})
    _assert_equivalent(ls, ps)
    assert ps == []


def test_no_entry_after_window_equivalent():
    legacy, pure = _pair()
    late = datetime.combine(DAY1, dtime(15, 19, 1), tzinfo=KST)
    ls, ps = _both(legacy, pure, late, {SYMBOL: _strong_close_bars()}, {SYMBOL: 109.0})
    _assert_equivalent(ls, ps)
    assert ps == []


def test_closing_auction_guard_equivalent():
    """15:20~15:30 은 동시호가 — entry_end 를 그 안까지 잘못 넓혀도
    `in_continuous_session` 가드가 양쪽에서 똑같이 진입을 막는다."""
    legacy, pure = _pair(entry_end_hhmm=[15, 30])
    during_auction = datetime.combine(DAY1, dtime(15, 25), tzinfo=KST)
    ls, ps = _both(legacy, pure, during_auction,
                   {SYMBOL: _strong_close_bars()}, {SYMBOL: 109.0})
    _assert_equivalent(ls, ps)
    assert ps == []


def test_no_entry_without_tag_equivalent():
    legacy = CloseBetStrategy([SYMBOL], _params(), id=LEGACY_ID, tags_of={})
    pure = CloseBetPureShell([SYMBOL], _params(), id=PURE_ID, tags_of={})
    ls, ps = _both(legacy, pure, ENTRY_TIME,
                   {SYMBOL: _strong_close_bars()}, {SYMBOL: 109.0})
    _assert_equivalent(ls, ps)
    assert ps == []


def test_no_entry_market_closed_equivalent():
    legacy, pure = _pair()
    ls, ps = _both(legacy, pure, ENTRY_TIME, {SYMBOL: _strong_close_bars()},
                   {SYMBOL: 109.0}, kr_open=False)
    _assert_equivalent(ls, ps)
    assert ps == []


@pytest.mark.parametrize("price", [101.0, 99.5])
def test_no_signal_on_weak_or_red_close_equivalent(price):
    """무신호 동치 — 마감강도 부족(101 → 0.18) / 음봉(99.5 < 시가 100)."""
    legacy, pure = _pair()
    ls, ps = _both(legacy, pure, ENTRY_TIME, {SYMBOL: _strong_close_bars()},
                   {SYMBOL: price})
    _assert_equivalent(ls, ps)
    assert ps == []


def test_no_signal_without_quote_or_bars_equivalent():
    legacy, pure = _pair()
    ls, ps = _both(legacy, pure, ENTRY_TIME, {}, {})  # 봉도 시세도 없다
    _assert_equivalent(ls, ps)
    assert ps == []


def test_once_per_day_equivalent():
    """하루 1회 게이트 — legacy 는 `self._entered_date`, pure 는
    `next_state["entered_date"]`. 같은 인스턴스로 두 번 돌린다."""
    legacy, pure = _pair()
    bars, quotes = {SYMBOL: _strong_close_bars()}, {SYMBOL: 109.0}
    ls1, ps1 = _both(legacy, pure, ENTRY_TIME, bars, quotes)
    _assert_equivalent(ls1, ps1)
    assert len(ps1) == 1

    ls2, ps2 = _both(legacy, pure, ENTRY_TIME, bars, quotes)
    _assert_equivalent(ls2, ps2)
    assert ps2 == []


# ============================================================ 2) 세션을 넘긴 다음날 아침

@pytest.mark.parametrize("minute,price,keyword", [
    (5, 107.0, "손절"),     # -1.8% → stop(=107.91) 이탈
    (3, 112.0, "익절"),     # +2.75% → target(=111.18) 돌파
    (31, 109.5, "정리"),    # 시초 30분 데드라인 — 익절도 손절도 아닌 채로
])
def test_next_morning_exit_equivalent(minute, price, keyword):
    """세션을 넘긴 다음날 아침 청산 3종. 진입일(DAY1) lot 을 들고 DAY2 아침."""
    legacy, pure = _pair()
    nxt = datetime.combine(DAY2, dtime(9, minute), tzinfo=KST)
    ls, ps = _both(legacy, pure, nxt, {}, {SYMBOL: price},
                   {LEGACY_ID: {SYMBOL: _held(LEGACY_ID)},
                    PURE_ID: {SYMBOL: _held(PURE_ID)}})
    _assert_equivalent(ls, ps)
    assert len(ps) == 1
    assert ps[0].action == SignalAction.EXIT_LONG and ps[0].exit_fraction == 1.0
    assert keyword in ps[0].reason


def test_next_morning_before_deadline_holds_equivalent():
    """데드라인 전(09:29)이고 익절/손절 어느 쪽도 아니면 아직 안 판다."""
    legacy, pure = _pair()
    at_29 = datetime.combine(DAY2, dtime(9, 29), tzinfo=KST)
    ls, ps = _both(legacy, pure, at_29, {}, {SYMBOL: 109.5},
                   {LEGACY_ID: {SYMBOL: _held(LEGACY_ID)},
                    PURE_ID: {SYMBOL: _held(PURE_ID)}})
    _assert_equivalent(ls, ps)
    assert ps == []


def test_holds_through_entry_day_close_equivalent():
    """진입 당일엔 어떤 가격이든 안 판다 — 오버나이트가 이 전략이다.
    (lot["session"] == 오늘 → 관리 스킵)"""
    legacy, pure = _pair()
    late = datetime.combine(DAY1, dtime(15, 25), tzinfo=KST)
    ls, ps = _both(legacy, pure, late, {}, {SYMBOL: 106.0},
                   {LEGACY_ID: {SYMBOL: _held(LEGACY_ID)},
                    PURE_ID: {SYMBOL: _held(PURE_ID)}})
    _assert_equivalent(ls, ps)
    assert ps == []


def test_no_management_when_market_closed_equivalent():
    """장 밖 시세는 세션 밖 잔가일 수 있다 — 양쪽 다 판정하지 않는다."""
    legacy, pure = _pair()
    pre = datetime.combine(DAY2, dtime(8, 30), tzinfo=KST)
    ls, ps = _both(legacy, pure, pre, {}, {SYMBOL: 100.0},
                   {LEGACY_ID: {SYMBOL: _held(LEGACY_ID)},
                    PURE_ID: {SYMBOL: _held(PURE_ID)}}, kr_open=False)
    _assert_equivalent(ls, ps)
    assert ps == []


# ============================================================ 3) state 왕복 (당일 진입 → 익일 청산)

def _apply_fill(positions, strategy_id, signal, qty=10.0):
    """`loop._execute_signal`의 체결 후 처리 축약 — state_update 를 lot 에 적용한다
    (`quant/trade/loop.py`: `pos.ensure_lot(sid).update(signal.state_update)`)."""
    pos = positions.get(signal.symbol)
    if pos is None:
        pos = Position(symbol=signal.symbol, qty=0.0, avg_cost=0.0, meta={})
        positions[signal.symbol] = pos
    price = float(signal.state_update["entry"])
    pos.qty += qty
    pos.avg_cost = price
    lot = pos.ensure_lot(strategy_id)
    lot.update({"qty": qty, "avg_cost": price})
    lot.update(signal.state_update)
    return positions


def test_state_roundtrip_entry_day_to_next_morning_equivalent():
    """당일 진입 → 체결 반영 → 당일 늦은 사이클(보유) → 익일 아침 익절.
    같은 인스턴스로 4사이클을 연속으로 흘려 legacy/pure 가 매 사이클 동치인지 본다."""
    legacy, pure = _pair()
    pos_of = {LEGACY_ID: {}, PURE_ID: {}}

    # (1) DAY1 15:17 — 진입
    ls, ps = _both(legacy, pure, ENTRY_TIME, {SYMBOL: _strong_close_bars()},
                   {SYMBOL: 109.0}, pos_of)
    _assert_equivalent(ls, ps)
    assert len(ps) == 1 and ps[0].action == SignalAction.ENTER_LONG
    _apply_fill(pos_of[LEGACY_ID], LEGACY_ID, ls[0])
    _apply_fill(pos_of[PURE_ID], PURE_ID, ps[0])

    # 체결로 넘어간 값이 실제로 lot 에 남았는가 — 이게 밤을 넘는 경로다.
    pure_lot = pos_of[PURE_ID][SYMBOL].lot(PURE_ID)
    assert pure_lot["entry"] == pytest.approx(109.0)
    assert pure_lot["session"] == DAY1.isoformat()
    assert pure_lot["stop"] == pytest.approx(109.0 * 0.99)

    # (2) DAY1 15:18 — 하루 1회 게이트로 재진입 없음 + 진입 당일이라 청산도 없음
    t2 = datetime.combine(DAY1, dtime(15, 18), tzinfo=KST)
    ls, ps = _both(legacy, pure, t2, {SYMBOL: _strong_close_bars()},
                   {SYMBOL: 109.0}, pos_of)
    _assert_equivalent(ls, ps)
    assert ps == []

    # (3) DAY1 15:25 — 동시호가, 급락해도 안 판다(오버나이트)
    t3 = datetime.combine(DAY1, dtime(15, 25), tzinfo=KST)
    ls, ps = _both(legacy, pure, t3, {}, {SYMBOL: 105.0}, pos_of)
    _assert_equivalent(ls, ps)
    assert ps == []

    # (4) DAY2 09:03 — 시초 갭 +2.75% → 익절. 세션이 바뀌었다는 판정은
    #     lot["session"](DAY1) vs 오늘(DAY2) 비교에서 나온다.
    t4 = datetime.combine(DAY2, dtime(9, 3), tzinfo=KST)
    ls, ps = _both(legacy, pure, t4, {}, {SYMBOL: 112.0}, pos_of)
    _assert_equivalent(ls, ps)
    assert len(ps) == 1 and "익절" in ps[0].reason


def test_next_state_carries_only_intraday_values():
    """`next_state`에는 **하루 안에서만 사는 값**만 담긴다 — 방어선(entry/stop/
    target/session)은 여기 없다(클래스 docstring 매핑 표 3번)."""
    _, pure = _pair()
    pure.on_cycle(_ctx(ENTRY_TIME, {SYMBOL: _strong_close_bars()}, {SYMBOL: 109.0}))
    state = pure._state
    assert set(state) == {"entered_date", "last_reject"}
    assert state["entered_date"] == {SYMBOL: DAY1.isoformat()}
    # 어느 중첩 dict 에도 방어선 필드가 새어 들어가 있지 않다.
    nested_keys = {k for v in state.values() if isinstance(v, dict) for k in v}
    assert nested_keys <= {SYMBOL}
    assert not ({"entry", "stop", "target", "session"} & (set(state) | nested_keys))


def test_overnight_state_survives_process_restart():
    """**밤을 넘는 값이 `next_state`에 있었다면 실패하는 테스트.**

    진입 → 체결 → 껍질 상태를 통째로 버린다(= 밤 사이 프로세스 재시작). 그래도
    익일 아침 손절이 그대로 나와야 한다 — 방어선이 `Position.meta["lots"]`에
    영속돼 `snap.lots`로 되돌아오기 때문이다."""
    legacy, pure = _pair()
    pos_of = {LEGACY_ID: {}, PURE_ID: {}}
    ls, ps = _both(legacy, pure, ENTRY_TIME, {SYMBOL: _strong_close_bars()},
                   {SYMBOL: 109.0}, pos_of)
    _apply_fill(pos_of[LEGACY_ID], LEGACY_ID, ls[0])
    _apply_fill(pos_of[PURE_ID], PURE_ID, ps[0])

    # 재시작: 새 인스턴스(= next_state 유실). 포지션만 살아남는다.
    legacy2, pure2 = _pair()
    assert pure2._state == {}

    nxt = datetime.combine(DAY2, dtime(9, 5), tzinfo=KST)
    ls2, ps2 = _both(legacy2, pure2, nxt, {}, {SYMBOL: 107.0}, pos_of)
    _assert_equivalent(ls2, ps2)
    assert len(ps2) == 1 and "손절" in ps2[0].reason


def test_entered_date_gate_releases_next_day():
    """하루 1회 게이트는 날짜가 바뀌면 저절로 풀린다 — 세션 경계를 넘길 필요가
    없다는 매핑 표 1번 주장을 고정한다."""
    legacy, pure = _pair()
    ls, ps = _both(legacy, pure, ENTRY_TIME, {SYMBOL: _strong_close_bars()},
                   {SYMBOL: 109.0})
    _assert_equivalent(ls, ps)
    assert len(ps) == 1

    # 포지션 없이 다음날 오후장(현실에서는 아침에 청산된 뒤) — 다시 진입 가능.
    day2_entry = datetime.combine(DAY2, dtime(15, 17), tzinfo=KST)
    ls2, ps2 = _both(legacy, pure, day2_entry,
                     {SYMBOL: _strong_close_bars(DAY2)}, {SYMBOL: 109.0})
    _assert_equivalent(ls2, ps2)
    assert len(ps2) == 1 and ps2[0].state_update["session"] == DAY2.isoformat()


def test_pure_decide_does_not_mutate_input_state():
    """순수 계약 — 넘겨받은 state 를 in-place 로 고치지 않는다."""
    from quant.core.strategy_api import StrategySnapshot

    inner = CloseBetPureShell([SYMBOL], _params(), id=PURE_ID,
                              tags_of={SYMBOL: ["CLOSE_BET"]}).inner
    snap = StrategySnapshot(
        now=ENTRY_TIME, market_open={"KR": True}, minutes_to_close={"KR": 3.0},
        cadence_minutes=1.0, bars={(SYMBOL, "1m"): _strong_close_bars()},
        quotes={SYMBOL: Quote(symbol=SYMBOL, ts=ENTRY_TIME, price=109.0)}, lots={},
    )
    state = {"entered_date": {}, "last_reject": {}}
    decision = inner.decide(snap, state)
    assert len(decision.signals) == 1
    assert state == {"entered_date": {}, "last_reject": {}}  # 원본 그대로
    assert decision.next_state["entered_date"] == {SYMBOL: DAY1.isoformat()}
