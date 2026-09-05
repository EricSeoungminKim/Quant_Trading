"""Scalp1mStrategy — 1분봉 스캘프(패턴 A/B + 부분 익절 + 60선 트레일) 테스트.

합성 1분봉 시퀀스로 스펙(docs/superpowers/specs/2026-08-18-scalp-1m-design.md)의
진입/청산 규칙을 고정한다. news_scalp/intraday_scan의 테스트 패턴(FakeClock/
FakeDataFeed/FakeBroker)을 따른다.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.ports import Context
from quant.core.models import Position, Quote, SignalAction
from quant.trade.strategy.scalp_1m import Scalp1mStrategy

NY = ZoneInfo("America/New_York")
DAY1 = date(2026, 1, 5)  # 월요일 — 주말 게이트에 안 걸린다
DAY2 = date(2026, 1, 6)
US_OPEN = dtime(9, 30)

KST = ZoneInfo("Asia/Seoul")
KR_OPEN = dtime(9, 0)
KR_PRE_OPEN = dtime(8, 0)


class FakeClock:
    def __init__(self, now: datetime, open_markets=frozenset({"US"}), flatten_markets=frozenset()):
        self._now = now
        self._open = open_markets
        self._flatten = flatten_markets

    def now(self):
        return self._now

    def is_market_open(self, market):
        return market in self._open

    def minutes_to_close(self, market):
        return 300.0

    def cadence_minutes(self):
        return 5.0 / 60

    def should_flatten(self, market, flatten_minutes):
        return market in self._flatten


class FakeDataFeed:
    def __init__(
        self, quotes: dict[str, float], bars: dict[str, pd.DataFrame] | None = None,
        daily_bars: dict[str, pd.DataFrame] | None = None,
    ):
        self._quotes = quotes
        self._bars = bars or {}
        # 일봉("1d") — trend_gate(추세/변동성 게이트) 전용. 기본 비어 있음 →
        # 조회하면 빈 DataFrame(게이트 폴백=통과, quant/trade/indicators/
        # trend_gate.py 모듈 docstring "게이트 부재" 절과 동일 원칙) — 기존
        # 테스트는 daily_bars를 안 주므로 게이트가 항상 통과해 결과가 그대로다.
        self._daily_bars = daily_bars or {}
        # 분 경계 캐시 검증용 — 1m(그 외 간격) history() 호출마다 심볼을 기록한다
        # (호출 횟수 카운팅). "1d" 호출은 별도 리스트(daily_history_calls)에
        # 기록해 기존 `history_calls.count(symbol)` 단언이 trend_gate 조회로
        # 영향받지 않도록 분리한다.
        self.history_calls: list[str] = []
        self.daily_history_calls: list[str] = []

    def quote(self, symbol):
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(NY), price=self._quotes[symbol])

    def history(self, symbol, interval, n):
        if interval == "1d":
            self.daily_history_calls.append(symbol)
            df = self._daily_bars.get(symbol)
        else:
            self.history_calls.append(symbol)
            df = self._bars.get(symbol)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df.tail(n)


class FakeBroker:
    def __init__(self, positions=None):
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self):
        return 1_000_000.0


def _ctx(quotes, now, bars=None, positions=None, open_markets=frozenset({"US"}), flatten_markets=frozenset(),
          daily_bars=None):
    return Context(
        clock=FakeClock(now, open_markets, flatten_markets),
        data=FakeDataFeed(quotes, bars, daily_bars),
        broker=FakeBroker(positions),
    )


def _params(**over):
    p = dict(
        entry_window_minutes_after_open=90, volume_surge_mult=3.0, volume_surge_lookback=20,
        ma_period=60, ma_tolerance_pct=0.2, stop_buffer_pct=0.3, stop_hard_cap_pct=3.0,
        partial_take_r=1.5, partial_fraction=0.5, flatten_before_close_minutes=1,
    )
    p.update(over)
    return p


def _now_within_window(minutes_after_open: float = 5.0, day=DAY1) -> datetime:
    return datetime.combine(day, US_OPEN, tzinfo=NY) + timedelta(minutes=minutes_after_open)


# ============================================================ 봉 구성 헬퍼

def _warmup(tz, before_ts, n, *, close=100.0, volume=1000.0):
    idx = [before_ts - timedelta(minutes=n - i) for i in range(n)]
    rows = [{"open": close, "high": close + 0.1, "low": close - 0.1, "close": close, "volume": volume}
            for _ in range(n)]
    return idx, rows


def _pattern_a_bars(*, surge=True, l1_breach_open=False, warmup_n=25):
    """패턴 A 시퀀스: [P1(서지)봉, 되돌림(L1)봉, 재돌파봉]. 시가 100.0."""
    open_ts = datetime.combine(DAY1, US_OPEN, tzinfo=NY)
    idx, rows = _warmup(NY, open_ts, warmup_n)
    p1_vol = 3500.0 if surge else 1000.0
    l1_low = 99.0 if l1_breach_open else 100.5  # 시가(100.0) 아래로 뚫리면 무효
    session_rows = [
        {"open": 100.0, "high": 102.0, "low": 99.9, "close": 101.8, "volume": p1_vol},   # P1봉
        {"open": 101.8, "high": 101.9, "low": l1_low, "close": 100.6, "volume": 1000.0},  # L1봉
        {"open": 100.6, "high": 102.5, "low": 100.5, "close": 102.3, "volume": 1200.0},  # 재돌파봉
    ]
    session_idx = [open_ts + timedelta(minutes=i) for i in range(3)]
    idx += session_idx
    rows += session_rows
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=NY))


def _flat_ma_bars(n=65, *, close=100.0, last_close=None, day=DAY1):
    """MA60 워밍업이 끝난 평탄한 봉 n개(마지막 봉만 last_close로 대체 가능)."""
    open_ts = datetime.combine(day, US_OPEN, tzinfo=NY)
    idx = [open_ts + timedelta(minutes=i - n) for i in range(n)]
    rows = [{"open": close, "high": close + 0.1, "low": close - 0.1, "close": close, "volume": 1000.0}
            for _ in range(n)]
    if last_close is not None:
        rows[-1] = {"open": close, "high": max(close, last_close) + 0.1,
                    "low": min(close, last_close) - 0.1, "close": last_close, "volume": 1000.0}
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=NY))


def _lot_position(entry=100.0, stop=97.0, *, session=DAY1.isoformat(), partial_taken=False):
    return Position(symbol="AAA", qty=10, avg_cost=entry, meta={
        "lots": {"scalp_1m": {"qty": 10.0, "entry": entry, "stop": stop,
                               "session": session, "partial_taken": partial_taken}},
    })


# ============================================================ 패턴 A — 서지 있음/없음, L1 유효성

def test_pattern_a_enters_on_breakout_with_volume_surge():
    strat = Scalp1mStrategy(["AAA"], _params())
    bars = {"AAA": _pattern_a_bars(surge=True)}
    signals = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.ENTER_LONG
    assert "패턴A" in sig.reason
    assert sig.stop is not None and sig.stop < 102.4
    assert strat._pattern_a_used.get("AAA") is True


def test_pattern_a_rejected_without_volume_surge():
    strat = Scalp1mStrategy(["AAA"], _params())
    bars = {"AAA": _pattern_a_bars(surge=False)}
    signals = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    assert signals == []


def test_pattern_a_invalidated_when_l1_breaches_open():
    strat = Scalp1mStrategy(["AAA"], _params())
    bars = {"AAA": _pattern_a_bars(surge=True, l1_breach_open=True)}
    signals = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    assert signals == []


def test_pattern_a_stop_uses_l1_buffer_and_hard_cap():
    strat = Scalp1mStrategy(["AAA"], _params(stop_buffer_pct=0.3, stop_hard_cap_pct=3.0))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    [sig] = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    l1 = 100.5
    expected = max(l1 * (1 - 0.3 / 100), 102.4 * (1 - 3.0 / 100))
    assert sig.stop == pytest.approx(expected)


# ============================================================ 패턴 B — 60선 지지 반등(재진입)

def test_pattern_b_enters_after_pattern_a_already_used():
    """터치봉/확인봉은 정규장 개장(09:30) 이후에 있어야 한다 — `_session_bars`가
    오늘 날짜의 봉을 세션 시간으로도 거르기 때문(모듈 docstring "프리마켓" 절 —
    KR 프리마켓 봉이 정규장 봉과 섞이는 걸 막는 수정, 2026-08-18). MA60 워밍업
    59개는 개장 전(같은 날짜, `full_bars` 연속 계산용 — today_bars 필터 대상 아님)."""
    strat = Scalp1mStrategy(["AAA"], _params())
    strat._pattern_a_used["AAA"] = True  # A가 이미 이 세션에 쓰였다 — B가 "재진입"으로 평가된다.
    strat._session_date["US"] = DAY1

    open_ts = datetime.combine(DAY1, US_OPEN, tzinfo=NY)
    warmup_idx, warmup_rows = _warmup(NY, open_ts, 59)
    touch_ts = open_ts  # 정규장 첫 봉을 터치봉으로
    confirm_ts = open_ts + timedelta(minutes=1)
    touch_confirm = pd.DataFrame(
        [
            {"open": 100.0, "high": 100.1, "low": 99.85, "close": 99.85, "volume": 1000.0},  # 터치봉: 저가 MA60(-0.2%) 안
            {"open": 99.9, "high": 100.4, "low": 99.85, "close": 100.3, "volume": 1000.0},    # 확인봉: 양봉
        ],
        index=pd.DatetimeIndex([touch_ts, confirm_ts], tz=NY),
    )
    bars_df = pd.concat([
        pd.DataFrame(warmup_rows, index=pd.DatetimeIndex(warmup_idx, tz=NY)),
        touch_confirm,
    ])

    now = confirm_ts + timedelta(seconds=30)
    signals = strat.on_cycle(_ctx({"AAA": 100.35}, now, bars={"AAA": bars_df}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG
    assert "패턴B" in signals[0].reason
    assert strat._pattern_b_used.get("AAA") is True


def test_pattern_b_not_evaluated_before_pattern_a_used():
    """A를 아직 쓰지 않았으면 B는 평가되지 않는다(스펙: B는 "재진입·후속 진입용")."""
    strat = Scalp1mStrategy(["AAA"], _params())
    touch = _flat_ma_bars(n=64, close=100.0, last_close=99.85)
    confirm_ts = touch.index[-1] + timedelta(minutes=1)
    confirm = pd.DataFrame(
        [{"open": 99.9, "high": 100.4, "low": 99.85, "close": 100.3, "volume": 1000.0}],
        index=pd.DatetimeIndex([confirm_ts], tz=NY),
    )
    bars_df = pd.concat([touch, confirm])
    now = confirm_ts + timedelta(seconds=30)
    signals = strat.on_cycle(_ctx({"AAA": 100.35}, now, bars={"AAA": bars_df}))
    assert signals == []


def test_session_entry_cap_two_after_both_patterns_used():
    """세션당 심볼당 총 진입 2회 상한(A 1회 + B 1회) — 둘 다 쓰였으면 더 이상 진입 없음."""
    strat = Scalp1mStrategy(["AAA"], _params())
    strat._pattern_a_used["AAA"] = True
    strat._pattern_b_used["AAA"] = True
    strat._session_date["US"] = DAY1  # 세션 롤 리셋(신규 세션 감지)이 위 플래그를 지우지 않게 고정
    bars = {"AAA": _pattern_a_bars(surge=True)}
    signals = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    assert signals == []


# ============================================================ 진입창

def test_no_entry_outside_entry_window():
    strat = Scalp1mStrategy(["AAA"], _params(entry_window_minutes_after_open=90))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    late = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(95.0), bars=bars))
    assert late == []


def test_entry_window_zero_waits_all_session():
    """소유자 지시(2026-08-26): "단타 스캘핑은 언제든 해도 좋아 — 언제든 시그널을
    계속 대기하는 거야". 0 = 진입창 없음(전 세션 대기) — 개장 95분 뒤에도
    패턴이 서면 진입한다."""
    strat = Scalp1mStrategy(["AAA"], _params(entry_window_minutes_after_open=0))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    late = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(95.0), bars=bars))
    assert len(late) == 1 and late[0].action == SignalAction.ENTER_LONG


def test_entry_window_zero_still_blocks_before_open():
    """전-세션 모드여도 개장 전(경과 음수)은 정규장 진입 경로가 아니다 —
    07:00 NY는 프리마켓 창(08:00~)보다도 앞이라 관찰 자체가 없어야 한다."""
    strat = Scalp1mStrategy(["AAA"], _params(entry_window_minutes_after_open=0))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    early = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(-150.0), bars=bars))
    assert early == []


def test_entry_window_zero_lookback_covers_full_session():
    """전-세션 모드는 조회 봉 수도 세션 전체(390분)를 덮어야 한다 — 90분 기준
    그대로면 오후 패턴 판정에 필요한 봉이 잘린다."""
    strat = Scalp1mStrategy(["AAA"], _params(entry_window_minutes_after_open=0, lookback_bars=1))
    assert strat._lookback_bars >= 60 + 20 + 390


# ============================================================ 구조층 (2026-08-26 재작업)

def test_structure_stop_mode_uses_swing_support():
    """stop_mode=structure — 손절이 패턴 기준가(L1)가 아니라 최근 스윙 저점
    (지지) 아래에 놓인다. _pattern_a_bars 의 지지는 워밍업 저가 99.9 (L1 저가
    100.5 는 마지막 wing 3봉이라 미확정 — 스윙 판정 제외)."""
    strat = Scalp1mStrategy(["AAA"], _params(stop_mode="structure", stop_buffer_pct=0.3))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    [sig] = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    assert sig.action == SignalAction.ENTER_LONG
    assert sig.stop == pytest.approx(99.9 * (1 - 0.3 / 100))
    assert "구조손절" in sig.reason


def test_structure_stop_mode_rejects_when_no_support_below():
    """지지(스윙 저점)가 전부 진입가 위 — "손절선을 정할 수 없는 자리"는
    진입하지 않는다(structure.py 손절 철학)."""
    strat = Scalp1mStrategy(["AAA"], _params(stop_mode="structure"))
    open_ts = datetime.combine(DAY1, US_OPEN, tzinfo=NY)
    idx, rows = _warmup(NY, open_ts, 25, close=110.0)  # 워밍업 저가 109.9 > 진입가
    session_rows = [
        {"open": 100.0, "high": 102.0, "low": 99.9, "close": 101.8, "volume": 3500.0},
        {"open": 101.8, "high": 101.9, "low": 100.5, "close": 100.6, "volume": 1000.0},
        {"open": 100.6, "high": 102.5, "low": 100.5, "close": 102.3, "volume": 1200.0},
    ]
    idx += [open_ts + timedelta(minutes=i) for i in range(3)]
    rows += session_rows
    bars_df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=NY))
    signals = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars={"AAA": bars_df}))
    assert signals == []
    assert "구조 지지 없음" in strat.last_reject["AAA"]


def test_williams_gate_shadow_notes_overbought_but_enters():
    """shadow — 재돌파 직후는 정의상 과매수 부근(W%R≈-8): 진입은 막지 않고
    사유에 차단 후보 노트만 싣는다(표본 축적 → block 승격 판단, trend_gate 관례)."""
    strat = Scalp1mStrategy(["AAA"], _params(williams_gate_mode="shadow"))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    [sig] = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    assert sig.action == SignalAction.ENTER_LONG
    assert "W%R" in sig.reason and "차단후보" in sig.reason


def test_williams_gate_block_rejects_overbought_entry():
    strat = Scalp1mStrategy(["AAA"], _params(williams_gate_mode="block"))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    signals = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    assert signals == []
    assert "과매수" in strat.last_reject["AAA"]


def test_williams_gate_off_by_default_keeps_reason_clean():
    strat = Scalp1mStrategy(["AAA"], _params())
    bars = {"AAA": _pattern_a_bars(surge=True)}
    [sig] = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    assert "W%R" not in sig.reason


# ============================================================ 5초 루프 상호작용

def test_repeated_cycles_on_same_completed_bar_do_not_duplicate_entry():
    """같은 완성봉(같은 데이터)으로 on_cycle을 반복 호출해도(5초 폴링 재평가)
    두 번째 이후엔 중복 진입 신호가 나지 않는다 — 신호 생성 시점에 즉시 세팅되는
    _pattern_a_used 플래그가 다음 사이클을 막는다(체결 확인을 기다리지 않음)."""
    strat = Scalp1mStrategy(["AAA"], _params())
    bars = {"AAA": _pattern_a_bars(surge=True)}
    now = _now_within_window(3.0)
    first = strat.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    assert len(first) == 1
    second = strat.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    assert second == [], "같은 1분봉 안에서 반복 호출되어도 중복 주문이 나면 안 된다"


# ============================================================ 청산 — 손절/EoD/세션롤

def test_stop_loss_exits_full_position():
    strat = Scalp1mStrategy(["AAA"], _params())
    pos = _lot_position(entry=100.0, stop=97.0)
    signals = strat.on_cycle(_ctx({"AAA": 96.9}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert signals[0].exit_fraction == 1.0
    assert "손절" in signals[0].reason


def test_eod_flatten_exits_position():
    strat = Scalp1mStrategy(["AAA"], _params())
    pos = _lot_position(entry=100.0, stop=97.0)
    signals = strat.on_cycle(_ctx({"AAA": 101.0}, _now_within_window(5.0), positions={"AAA": pos},
                                   flatten_markets=frozenset({"US"})))
    assert len(signals) == 1
    assert "EoD" in signals[0].reason


def test_session_roll_forces_exit_overnight():
    strat = Scalp1mStrategy(["AAA"], _params())
    pos = _lot_position(entry=100.0, stop=97.0, session=DAY1.isoformat())
    now = datetime.combine(DAY2, dtime(10, 30), tzinfo=NY)
    signals = strat.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos}))
    assert len(signals) == 1
    assert "오버나잇 금지" in signals[0].reason


def test_restart_recovery_uses_avg_cost_and_hard_cap_stop():
    strat = Scalp1mStrategy(["AAA"], _params(stop_hard_cap_pct=3.0))
    pos = Position(symbol="AAA", qty=10, avg_cost=100.0, meta={})
    signals = strat.on_cycle(_ctx({"AAA": 96.9}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert "손절" in signals[0].reason


# ============================================================ 절반 익절 — 1회만

def test_partial_take_profit_fires_once_at_1_5r():
    strat = Scalp1mStrategy(["AAA"], _params(partial_take_r=1.5, partial_fraction=0.5))
    pos = _lot_position(entry=100.0, stop=97.0)  # R = 3.0 -> target = 100 + 1.5*3 = 104.5
    signals = strat.on_cycle(_ctx({"AAA": 104.6}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.SCALE_OUT
    assert sig.exit_fraction == pytest.approx(0.5)
    assert sig.state_update == {"partial_taken": True}


def test_partial_take_profit_does_not_refire_once_flagged():
    strat = Scalp1mStrategy(["AAA"], _params(partial_take_r=1.5, partial_fraction=0.5))
    pos = _lot_position(entry=100.0, stop=97.0, partial_taken=True)
    signals = strat.on_cycle(_ctx({"AAA": 104.6}, _now_within_window(5.0), positions={"AAA": pos}))
    assert signals == []


# ---------------------------------- D7: 1주 lot의 부분청산이 <1주로 내림될 때 ---
# 실측(2026-09-03): scalp_1m 096770이 보유 1주 x 0.5 = 0.5주를 부분청산하려다
# risk 레이어가 매 사이클 "부분매도 수량 <1주"로 거부했다. partial_taken은
# 체결 시에만 세팅되므로 같은 신호가 22초간 60회 재발화했다. KR은 항상 정수
# 매도만 허용되므로 소수점 매도 구제(US 연속세션)가 없다 — 전량 청산으로
# 대체돼야 한다.

def _kr_lot_position(symbol="096770", entry=100.0, stop=97.0, qty=1.0, *,
                      session=DAY1.isoformat(), partial_taken=False):
    return Position(symbol=symbol, qty=qty, avg_cost=entry, meta={
        "lots": {"scalp_1m": {"qty": qty, "entry": entry, "stop": stop,
                               "session": session, "partial_taken": partial_taken}},
    })


def _kr_now_within_window(minutes_after_open: float = 5.0, day=DAY1) -> datetime:
    return datetime.combine(day, KR_OPEN, tzinfo=KST) + timedelta(minutes=minutes_after_open)


def test_partial_take_profit_becomes_full_exit_when_1_share_lot_cannot_be_split():
    """KR은 소수점 매도가 없다 — floor(1주 x 0.5)=0이면 부분청산 대신 전량
    청산으로 대체해 반복 거부/재발화를 막는다."""
    strat = Scalp1mStrategy(["096770"], _params(partial_take_r=1.5, partial_fraction=0.5))
    pos = _kr_lot_position(qty=1.0, entry=100.0, stop=97.0)  # R=3.0 -> target=104.5
    ctx = _ctx({"096770": 104.6}, _kr_now_within_window(5.0), positions={"096770": pos},
               open_markets=frozenset({"KR"}))

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.EXIT_LONG
    assert sig.exit_fraction == pytest.approx(1.0)
    assert sig.state_update == {"partial_taken": True}
    assert "분할 불가" in sig.reason


def test_partial_take_profit_stays_scale_out_when_kr_lot_is_large_enough():
    """보유가 충분히 크면(floor(qty*fraction)>=1) 기존 부분청산 그대로 — 회귀 방지."""
    strat = Scalp1mStrategy(["096770"], _params(partial_take_r=1.5, partial_fraction=0.5))
    pos = _kr_lot_position(qty=10.0, entry=100.0, stop=97.0)
    ctx = _ctx({"096770": 104.6}, _kr_now_within_window(5.0), positions={"096770": pos},
               open_markets=frozenset({"KR"}))

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    assert signals[0].action == SignalAction.SCALE_OUT
    assert signals[0].exit_fraction == pytest.approx(0.5)


def test_partial_take_profit_stays_scale_out_for_us_continuous_session_even_with_1_share():
    """US 연속세션 중에는 소수점 매도가 허용되므로(2026-09-02 risk 레이어 규칙)
    1주짜리 부분청산도 그대로 부분청산이다 — 전량 대체로 과잉 반응하지 않는다."""
    strat = Scalp1mStrategy(["AAA"], _params(partial_take_r=1.5, partial_fraction=0.5))
    pos = _lot_position(entry=100.0, stop=97.0)  # AAA, qty=10 in helper — 여기선 lot qty만 본다
    # lot qty를 1로 좁혀 실제 <1주 시나리오를 재현한다.
    pos.meta["lots"]["scalp_1m"]["qty"] = 1.0
    signals = strat.on_cycle(_ctx({"AAA": 104.6}, _now_within_window(5.0), positions={"AAA": pos}))

    assert len(signals) == 1
    assert signals[0].action == SignalAction.SCALE_OUT
    assert signals[0].exit_fraction == pytest.approx(0.5)


# ============================================================ 잔량 트레일 — 60선 이탈

def test_ma60_close_below_exits_full_remaining():
    strat = Scalp1mStrategy(["AAA"], _params(ma_period=60))
    pos = _lot_position(entry=100.0, stop=97.0)
    bars = {"AAA": _flat_ma_bars(n=65, close=100.0, last_close=95.0)}  # 종가가 MA60 아래로 마감
    # 현재가(97.5)는 하드 손절(97.0) 위 — 손절이 아니라 60선 트레일 경로를 격리해 검증한다.
    signals = strat.on_cycle(_ctx({"AAA": 97.5}, _now_within_window(5.0), bars=bars, positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert signals[0].exit_fraction == 1.0
    assert "60선" in signals[0].reason


def test_holds_above_ma60_with_no_other_exit_trigger():
    strat = Scalp1mStrategy(["AAA"], _params(ma_period=60))
    pos = _lot_position(entry=100.0, stop=97.0)
    bars = {"AAA": _flat_ma_bars(n=65, close=100.0, last_close=100.2)}  # 종가가 MA60 위
    signals = strat.on_cycle(_ctx({"AAA": 100.5}, _now_within_window(5.0), bars=bars, positions={"AAA": pos}))
    assert signals == []


# ============================================================ 생성자 검증

def test_invalid_entry_window_raises():
    # 0은 2026-08-26부터 "전 세션 대기"라는 유효한 값이다 — 음수만 거부한다.
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(entry_window_minutes_after_open=-1))


def test_invalid_volume_surge_mult_raises():
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(volume_surge_mult=0))


def test_invalid_ma_period_raises():
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(ma_period=0))


def test_invalid_partial_fraction_raises():
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(partial_fraction=1.0))


def test_invalid_partial_take_r_raises():
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(partial_take_r=0))


def test_invalid_kr_entry_open_delay_min_raises():
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(kr_entry_open_delay_min=-1))


def test_kr_entry_open_delay_min_defaults_to_30():
    """모듈 docstring "KR 개장 초반 진입 지연 게이트" 절 — 기본값 30(2026-09-02
    원장×문헌 교차확인, 하위호환용 0이 아니라 명시적으로 켜진 기본값)."""
    strat = Scalp1mStrategy(["AAA"], {})
    assert strat.kr_entry_open_delay_min == 30


# ============================================================ entry_patterns (패턴 A/B 개별 온오프, 2026-09-04)
#
# 원장 151 트립(2026-08-18~09-04, data/state/trades.jsonl) 재생 근거는 모듈
# docstring의 생성자 주석 참고 — 기본값(둘 다 켜짐)은 기존 동작과 동일해야 한다.

def test_entry_patterns_defaults_to_both_enabled():
    strat = Scalp1mStrategy(["AAA"], _params())
    assert strat.entry_patterns == frozenset({"A", "B"})
    assert strat.pattern_a_enabled is True
    assert strat.pattern_b_enabled is True


def test_entry_patterns_accepts_comma_string_and_list_equivalently():
    strat_list = Scalp1mStrategy(["AAA"], _params(entry_patterns=["B"]))
    strat_str = Scalp1mStrategy(["AAA"], _params(entry_patterns="b"))
    assert strat_list.entry_patterns == strat_str.entry_patterns == frozenset({"B"})
    strat_csv = Scalp1mStrategy(["AAA"], _params(entry_patterns="a, B"))
    assert strat_csv.entry_patterns == frozenset({"A", "B"})


def test_entry_patterns_default_matches_explicit_both_enabled():
    """entry_patterns 키가 없을 때와 명시적으로 ["A","B"]를 줄 때 신호가
    완전히 동일해야 한다 — "기본값은 오늘과 100% 동일"이라는 주장의 근거."""
    bars = {"AAA": _pattern_a_bars(surge=True)}
    strat_default = Scalp1mStrategy(["AAA"], _params())
    strat_explicit = Scalp1mStrategy(["AAA"], _params(entry_patterns=["A", "B"]))
    sig_default = strat_default.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    sig_explicit = strat_explicit.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    assert len(sig_default) == len(sig_explicit) == 1
    assert sig_default[0].reason == sig_explicit[0].reason
    assert sig_default[0].stop == pytest.approx(sig_explicit[0].stop)


def test_entry_patterns_b_only_suppresses_pattern_a_entry():
    """패턴 A가 완성된 봉이어도 entry_patterns=["B"]면 A로 진입하지 않는다 —
    기본값이면 이 봉 시퀀스는 패턴 A로 즉시 진입한다
    (test_pattern_a_enters_on_breakout_with_volume_surge 참고). A가 꺼지면
    바로 B를 평가하는데, 이 봉(28개, MA60 워밍업 60개 미만)으로는 B도
    성립하지 않아 "패턴B 미충족"으로 거부된다 — A로 새지 않는다는 확인."""
    strat = Scalp1mStrategy(["AAA"], _params(entry_patterns=["B"]))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    signals = strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars))
    assert signals == []
    assert strat._pattern_a_used.get("AAA", False) is False
    assert strat.last_reject.get("AAA") == "패턴B 미충족"


def test_entry_patterns_a_only_suppresses_pattern_b_entry():
    """A를 이미 쓴 뒤에도 entry_patterns=["A"]면 B로 진입하지 않는다 — 기본값
    이면 test_pattern_b_enters_after_pattern_a_already_used가 진입시키는
    동일한 봉 시퀀스."""
    strat = Scalp1mStrategy(["AAA"], _params(entry_patterns=["A"]))
    strat._pattern_a_used["AAA"] = True
    strat._session_date["US"] = DAY1

    open_ts = datetime.combine(DAY1, US_OPEN, tzinfo=NY)
    warmup_idx, warmup_rows = _warmup(NY, open_ts, 59)
    touch_ts = open_ts
    confirm_ts = open_ts + timedelta(minutes=1)
    touch_confirm = pd.DataFrame(
        [
            {"open": 100.0, "high": 100.1, "low": 99.85, "close": 99.85, "volume": 1000.0},
            {"open": 99.9, "high": 100.4, "low": 99.85, "close": 100.3, "volume": 1000.0},
        ],
        index=pd.DatetimeIndex([touch_ts, confirm_ts], tz=NY),
    )
    bars_df = pd.concat([
        pd.DataFrame(warmup_rows, index=pd.DatetimeIndex(warmup_idx, tz=NY)),
        touch_confirm,
    ])
    now = confirm_ts + timedelta(seconds=30)
    signals = strat.on_cycle(_ctx({"AAA": 100.35}, now, bars={"AAA": bars_df}))
    assert signals == []
    assert strat._pattern_b_used.get("AAA", False) is False
    assert strat.last_reject.get("AAA") == "세션 진입 상한(A+B) 도달"


def test_entry_patterns_disables_premarket_pattern_a_path():
    """entry_patterns=["B"]면 프리마켓 직접 진입(패턴 A 전용) 경로도 막힌다 —
    기본값이면 이 봉 시퀀스는 프리마켓에서 즉시 진입한다
    (test_kr_premarket_never_enters_directly_even_on_perfect_pattern의 대칭
    US 버전, 여기서는 유동성/시장 제약 없이 순수 entry_patterns 효과만 본다)."""
    strat = Scalp1mStrategy([US_PRE_SYMBOL], _params(entry_patterns=["B"]))
    bars = {US_PRE_SYMBOL: _us_premarket_entry_bars(surge=True, notional_ok=True)}
    now = _us_now(dtime(8, 2, 30))
    ctx = _us_ctx({US_PRE_SYMBOL: 102.3}, now, bars=bars)

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert strat.last_reject.get(US_PRE_SYMBOL) == "패턴A 비활성(entry_patterns)"


def test_entry_patterns_empty_raises():
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(entry_patterns=[]))


def test_entry_patterns_unknown_value_raises():
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(entry_patterns=["C"]))


# ---------------- entry_patterns 시장별 매핑(dict 폼, 2026-09-05 소유자 위임 결정)
#
# 원장 151 트립(2026-08-18~09-04)을 시장별로 쪼개면 KR 패턴A -86.8bp(n=58,
# 최악) vs KR 패턴B -1.8bp(n=30, 훨씬 낫다) — US는 혼재라 그대로(A+B) 둔다.
# {KR: "B", US: "A,B"} 같은 dict를 받아 시장별로 다르게 적용한다.

def test_entry_patterns_dict_missing_market_defaults_to_both():
    """dict에 없는 시장은 기본값(둘 다 켜짐 — 동작 보존)."""
    strat = Scalp1mStrategy(["AAA"], _params(entry_patterns={"KR": "B"}))
    assert strat._patterns_for("KR") == frozenset({"B"})
    assert strat._patterns_for("US") == frozenset({"A", "B"})


def test_entry_patterns_dict_form_resolves_per_market():
    """entry_patterns={KR: "B", US: "A,B"}이면 KR 심볼은 패턴 A를 건너뛰고
    바로 B를 평가하고(이 봉 시퀀스는 MA60 워밍업이 60개 미만이라 B도 미충족
    — 결국 무신호, "세션 진입 상한" 이 아니라 "패턴B 미충족"으로 거부되는
    것이 A를 건너뛰었다는 증거), US 심볼은 기존과 동일하게 패턴 A로 즉시
    진입한다 — 같은 인스턴스라도 시장별 분기가 서로를 오염시키지 않는다."""
    patterns = {"KR": "B", "US": "A,B"}

    kr_strat = Scalp1mStrategy([KR_SYMBOL], _params(entry_patterns=patterns, kr_entry_open_delay_min=0))
    kr_bars = {KR_SYMBOL: _kr_pattern_a_bars()}
    kr_now = _kr_now(KR_OPEN) + timedelta(minutes=1, seconds=30)
    kr_signals = kr_strat.on_cycle(_kr_ctx({KR_SYMBOL: 81300.0}, kr_now, bars=kr_bars, kr_open=True))
    assert kr_signals == []
    assert kr_strat._pattern_a_used.get(KR_SYMBOL, False) is False
    assert kr_strat.last_reject.get(KR_SYMBOL) == "패턴B 미충족"

    us_strat = Scalp1mStrategy(["AAA"], _params(entry_patterns=patterns))
    us_bars = {"AAA": _pattern_a_bars(surge=True)}
    us_signals = us_strat.on_cycle(_ctx({"AAA": 102.4}, _now_within_window(3.0), bars=us_bars))
    assert len(us_signals) == 1
    assert us_signals[0].action == SignalAction.ENTER_LONG
    assert "패턴A" in us_signals[0].reason


def test_entry_patterns_dict_invalid_pattern_value_raises():
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(entry_patterns={"KR": "C"}))


def test_entry_patterns_dict_empty_market_set_raises():
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(entry_patterns={"KR": []}))


# ============================================================ 랏 소유권

def test_does_not_manage_another_strategys_position():
    strat = Scalp1mStrategy(["AAA"], _params())
    foreign = Position(symbol="AAA", qty=10, avg_cost=100.0,
                        meta={"strategy": "orb_scan", "stop": 90.0})
    assert strat._owns(foreign) is False
    mine = Position(symbol="AAA", qty=10, avg_cost=100.0,
                     meta={"strategy": "scalp_1m", "stop": 90.0})
    assert strat._owns(mine) is True


# ============================================================ 조회 최적화 (2026-08-18)

def test_minute_cache_reuses_bars_within_same_minute():
    """같은 분 안의 반복 사이클(5초 폴링)은 history를 재조회하지 않는다."""
    strat = Scalp1mStrategy(["AAA"], _params())
    feed = FakeDataFeed({"AAA": 102.4}, {"AAA": _pattern_a_bars(surge=False)})  # 미충족 -> 매 사이클 재평가
    now = _now_within_window(3.0)
    ctx = Context(clock=FakeClock(now), data=feed, broker=FakeBroker({}))

    strat.on_cycle(ctx)
    strat.on_cycle(ctx)
    strat.on_cycle(ctx)

    assert feed.history_calls.count("AAA") == 1


def test_closed_market_position_skips_management_entirely():
    """심볼의 시장이 닫혀 있으면 포지션 관리 자체를 건너뛴다(history/quote 조회 0회)."""
    strat = Scalp1mStrategy(["AAA"], _params())
    pos = _lot_position(entry=100.0, stop=97.0)
    feed = FakeDataFeed({"AAA": 96.9})  # 평가됐다면 손절 트리거될 가격
    ctx = Context(clock=FakeClock(_now_within_window(5.0), open_markets=frozenset()),
                  data=feed, broker=FakeBroker({"AAA": pos}))

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert feed.history_calls == []


def test_outside_entry_window_with_no_position_makes_zero_queries():
    """진입창이 지났고 보유 포지션도 없으면 history 조회가 아예 없다."""
    strat = Scalp1mStrategy(["AAA"], _params(entry_window_minutes_after_open=90))
    feed = FakeDataFeed({"AAA": 102.4}, {"AAA": _pattern_a_bars(surge=True)})
    ctx = Context(clock=FakeClock(_now_within_window(95.0)), data=feed, broker=FakeBroker({}))

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert feed.history_calls == []


def test_outside_entry_window_with_position_keeps_minute_boundary_management():
    """진입창이 지나도 보유 중이면 청산 관리(60선 트레일)는 분 경계로 계속된다."""
    strat = Scalp1mStrategy(["AAA"], _params(entry_window_minutes_after_open=90, ma_period=60))
    pos = _lot_position(entry=100.0, stop=97.0)
    bars = {"AAA": _flat_ma_bars(n=65, close=100.0, last_close=95.0)}  # 종가가 MA60 아래
    now = _now_within_window(95.0)
    feed = FakeDataFeed({"AAA": 97.5}, bars)
    ctx = Context(clock=FakeClock(now), data=feed, broker=FakeBroker({"AAA": pos}))

    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert "60선" in signals[0].reason
    assert feed.history_calls.count("AAA") == 1

    strat.on_cycle(ctx)  # 같은 분 반복 호출 -> 재조회 없음
    assert feed.history_calls.count("AAA") == 1


def test_new_minute_triggers_refetch_and_correct_signal():
    """새 분에 들어오면 재조회하고, 새 데이터로 신호 판정이 정상 동작한다."""
    strat = Scalp1mStrategy(["AAA"], _params())
    feed = FakeDataFeed({"AAA": 102.4}, {"AAA": _pattern_a_bars(surge=False)})  # 1차: 패턴 미충족
    now = _now_within_window(3.0)
    ctx1 = Context(clock=FakeClock(now), data=feed, broker=FakeBroker({}))

    first = strat.on_cycle(ctx1)
    assert first == []
    assert feed.history_calls.count("AAA") == 1

    strat.on_cycle(ctx1)  # 같은 분 재호출 -> 캐시
    assert feed.history_calls.count("AAA") == 1

    feed._bars["AAA"] = _pattern_a_bars(surge=True)  # 2차: 새 분에 패턴 충족 데이터로 교체
    now2 = now + timedelta(minutes=1)
    ctx2 = Context(clock=FakeClock(now2), data=feed, broker=FakeBroker({}))

    second = strat.on_cycle(ctx2)
    assert feed.history_calls.count("AAA") == 2
    assert len(second) == 1
    assert second[0].action == SignalAction.ENTER_LONG


# ============================================================ 프리마켓 (2026-08-18)

KR_SYMBOL = "005930"


def _kr_now(t: dtime, day=DAY1) -> datetime:
    return datetime.combine(day, t, tzinfo=KST)


def _kr_ctx(quotes, now, bars=None, positions=None, *, kr_open=False, daily_bars=None):
    """KR 프리마켓 테스트용 컨텍스트. `kr_open=False`(기본)는 실제 `WallClock`과
    동일하게 08:00~09:00엔 `is_market_open("KR")`이 False임을 재현한다 —
    `_market_active`의 확장 판정 자체를 검증하려면 clock 레벨에서는 "닫힘"으로
    보고돼야 한다."""
    return Context(
        clock=FakeClock(now, open_markets=frozenset({"KR"}) if kr_open else frozenset()),
        data=FakeDataFeed(quotes, bars, daily_bars),
        broker=FakeBroker(positions),
    )


# ---------------- US 프리마켓 (2026-08-18 대칭 확장) — KR과 동일 헬퍼 패턴

US_PRE_SYMBOL = "SOXL"
US_PRE_OPEN = dtime(8, 0)


def _us_now(t: dtime, day=DAY1) -> datetime:
    return datetime.combine(day, t, tzinfo=NY)


def _us_ctx(quotes, now, bars=None, positions=None, *, us_open=False, daily_bars=None):
    """US 프리마켓 테스트용 컨텍스트. `us_open=False`(기본)는 실제 `WallClock`과
    동일하게 08:00~09:30 ET엔 `is_market_open("US")`이 False임을 재현한다."""
    return Context(
        clock=FakeClock(now, open_markets=frozenset({"US"}) if us_open else frozenset()),
        data=FakeDataFeed(quotes, bars, daily_bars),
        broker=FakeBroker(positions),
    )


def _us_premarket_entry_bars(*, surge=True, l1_breach_open=False, notional_ok=True,
                              warmup_n=25, day=DAY1):
    """08:00 ET부터 [P1(서지)봉, L1봉, 재돌파봉] — 프리마켓 패턴 A(직접 진입)
    전용. 시가 $100. `_kr_premarket_entry_bars`와 동일 구조(달러 단위만 다름)."""
    pre_open_ts = _us_now(US_PRE_OPEN, day)
    idx, rows = _warmup(NY, pre_open_ts, warmup_n)
    p1_vol = 3500.0 if surge else 1000.0
    l1_low = 99.0 if l1_breach_open else 100.5  # 시가(100.0) 아래로 뚫리면 무효
    last_vol = 2000.0 if notional_ok else 10.0  # notional_ok: 종가*거래량 >= $50k
    session_rows = [
        {"open": 100.0, "high": 102.0, "low": 99.9, "close": 101.8, "volume": p1_vol},
        {"open": 101.8, "high": 101.9, "low": l1_low, "close": 100.6, "volume": 1000.0},
        {"open": 100.6, "high": 102.5, "low": 100.5, "close": 102.3, "volume": last_vol},
    ]
    session_idx = [pre_open_ts + timedelta(minutes=i) for i in range(3)]
    idx += session_idx
    rows += session_rows
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=NY))


def _us_premarket_confirm_bars(*, surge=True, breach_after=False, warmup_n=25, day=DAY1):
    """08:00 ET부터 [서지 상승봉(P_pre 후보), 확인 대상 이후 봉] — "프리마켓
    확인" 마킹(관찰) 전용. `_kr_premarket_confirm_bars`와 동일 구조."""
    pre_open_ts = _us_now(US_PRE_OPEN, day)
    idx, rows = _warmup(NY, pre_open_ts, warmup_n)
    surge_vol = 3500.0 if surge else 1000.0
    low_after = 99.0 if breach_after else 100.2
    session_rows = [
        {"open": 100.0, "high": 101.0, "low": 99.9, "close": 100.8, "volume": surge_vol},
        {"open": 100.8, "high": 100.9, "low": low_after, "close": 100.7, "volume": 1000.0},
    ]
    session_idx = [pre_open_ts + timedelta(minutes=i) for i in range(2)]
    idx += session_idx
    rows += session_rows
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=NY))


def _kr_premarket_entry_bars(*, surge=True, l1_breach_open=False, notional_ok=True,
                              warmup_n=25, day=DAY1):
    """08:00부터 [P1(서지)봉, L1봉, 재돌파봉] — 프리마켓 패턴 A(직접 진입) 전용.
    시가 80000. 마지막(재돌파) 봉 거래량으로 유동성 가드를 조절한다."""
    pre_open_ts = _kr_now(KR_PRE_OPEN, day)
    idx, rows = _warmup(KST, pre_open_ts, warmup_n)
    p1_vol = 3500.0 if surge else 1000.0
    l1_low = 79000.0 if l1_breach_open else 80500.0  # 시가(80000) 아래로 뚫리면 무효
    last_vol = 200_000.0 if notional_ok else 10.0
    session_rows = [
        {"open": 80000.0, "high": 81000.0, "low": 79900.0, "close": 80800.0, "volume": p1_vol},
        {"open": 80800.0, "high": 80900.0, "low": l1_low, "close": 80600.0, "volume": 1000.0},
        {"open": 80600.0, "high": 81500.0, "low": 80500.0, "close": 81300.0, "volume": last_vol},
    ]
    session_idx = [pre_open_ts + timedelta(minutes=i) for i in range(3)]
    idx += session_idx
    rows += session_rows
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=KST))


def _kr_premarket_confirm_bars(*, surge=True, breach_after=False, warmup_n=25, day=DAY1):
    """08:00부터 [서지 상승봉(P_pre 후보), 확인 대상 이후 봉] — "프리마켓 확인"
    마킹(관찰) 전용. `breach_after=True`면 서지봉 이후 저가가 시가 아래로 뚫린다."""
    pre_open_ts = _kr_now(KR_PRE_OPEN, day)
    idx, rows = _warmup(KST, pre_open_ts, warmup_n)
    surge_vol = 3500.0 if surge else 1000.0
    low_after = 79000.0 if breach_after else 80200.0
    session_rows = [
        {"open": 80000.0, "high": 81000.0, "low": 79900.0, "close": 80800.0, "volume": surge_vol},
        {"open": 80800.0, "high": 80900.0, "low": low_after, "close": 80700.0, "volume": 1000.0},
    ]
    session_idx = [pre_open_ts + timedelta(minutes=i) for i in range(2)]
    idx += session_idx
    rows += session_rows
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=KST))


# ---------------- 관찰(1번) — "프리마켓 확인" 마킹

def test_premarket_confirmation_marks_symbol_at_blackout():
    """08:50(블랙아웃 진입)에 프리마켓 봉 전체를 놓고 확인을 1회 확정한다."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params())
    bars = {KR_SYMBOL: _kr_premarket_confirm_bars(surge=True, breach_after=False)}
    now = _kr_now(dtime(8, 50, 30))  # 블랙아웃 진입 직후
    ctx = _kr_ctx({KR_SYMBOL: 80700.0}, now, bars=bars)

    signals = strat.on_cycle(ctx)

    assert signals == []  # 블랙아웃엔 신규 진입 없음
    assert strat._premarket_confirmed.get(KR_SYMBOL) == pytest.approx(81000.0)


def test_premarket_confirmation_rejected_without_volume_surge():
    strat = Scalp1mStrategy([KR_SYMBOL], _params())
    bars = {KR_SYMBOL: _kr_premarket_confirm_bars(surge=False, breach_after=False)}
    now = _kr_now(dtime(8, 50, 30))
    ctx = _kr_ctx({KR_SYMBOL: 80700.0}, now, bars=bars)

    strat.on_cycle(ctx)

    assert KR_SYMBOL not in strat._premarket_confirmed


def test_premarket_confirmation_rejected_when_price_breaches_open_after_surge():
    strat = Scalp1mStrategy([KR_SYMBOL], _params())
    bars = {KR_SYMBOL: _kr_premarket_confirm_bars(surge=True, breach_after=True)}
    now = _kr_now(dtime(8, 50, 30))
    ctx = _kr_ctx({KR_SYMBOL: 80700.0}, now, bars=bars)

    strat.on_cycle(ctx)

    assert KR_SYMBOL not in strat._premarket_confirmed


def test_premarket_confirmation_evaluated_once_per_session():
    """08:50~09:00 사이 여러 사이클이 돌아도 확정 판정은 1회만(재계산 없음).
    같은 심볼에 대해 이후 데이터가 바뀌어도 최초 판정이 유지된다."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params())
    bars = {KR_SYMBOL: _kr_premarket_confirm_bars(surge=True, breach_after=False)}
    ctx = _kr_ctx({KR_SYMBOL: 80700.0}, _kr_now(dtime(8, 50, 30)), bars=bars)

    strat.on_cycle(ctx)
    assert strat._premarket_confirmed.get(KR_SYMBOL) == pytest.approx(81000.0)

    # 같은 세션의 이후 사이클(예: 08:55) — 데이터가 사라져도 이미 확정된 값 유지.
    ctx2 = _kr_ctx({KR_SYMBOL: 80700.0}, _kr_now(dtime(8, 55)), bars={KR_SYMBOL: pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"])})
    strat.on_cycle(ctx2)
    assert strat._premarket_confirmed.get(KR_SYMBOL) == pytest.approx(81000.0)


# ---------------- 관찰(1번) — 정규장 가속 진입(P_pre를 P1로 인정)

def test_regular_session_accelerated_entry_uses_premarket_high_as_p1():
    """프리마켓 확인 심볼은 정규장에서 P1 재형성을 기다리지 않는다 — 되돌림+
    재돌파 단 2봉만으로 진입한다(기존 패턴 A의 3봉 최소 요건보다 빠르다).

    kr_entry_open_delay_min=0 — 이 테스트는 가속 패턴 로직 자체(2026-08-18)를
    검증하는 것이지 개장 초반 지연 게이트(2026-09-02, 기본 30분)를 검증하는
    것이 아니다. 그 게이트는 별도 테스트(test_kr_entry_open_delay_gate_*)가
    고정한다."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params(kr_entry_open_delay_min=0))
    strat._premarket_confirmed[KR_SYMBOL] = 81000.0
    strat._session_date["KR"] = DAY1

    reg_open_ts = _kr_now(KR_OPEN)
    bars = pd.DataFrame(
        [
            # 정규장 첫 봉(=되돌림 후보) — 저가가 자기 시가와 같다(아래 꼬리 없음,
            # "시가 아래로 뚫리지 않음"의 가장 단순한 유효 케이스).
            {"open": 80900.0, "high": 80950.0, "low": 80900.0, "close": 80920.0, "volume": 1000.0},
            {"open": 80920.0, "high": 81200.0, "low": 80850.0, "close": 81100.0, "volume": 1200.0},   # 재돌파(P_pre=81000 위)
        ],
        index=pd.DatetimeIndex([reg_open_ts, reg_open_ts + timedelta(minutes=1)], tz=KST),
    )
    now = reg_open_ts + timedelta(minutes=1, seconds=30)
    ctx = _kr_ctx({KR_SYMBOL: 81150.0}, now, bars={KR_SYMBOL: bars}, kr_open=True)

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG
    assert "패턴A" in signals[0].reason
    assert strat._pattern_a_used.get(KR_SYMBOL) is True


def test_regular_session_accelerated_entry_invalidated_below_open():
    """되돌림이 정규장 시가 아래로 뚫리면 가속 경로도 기존 규칙대로 무효.

    kr_entry_open_delay_min=0 — 위 테스트와 같은 이유(가속 패턴 로직 검증,
    개장 초반 지연 게이트와 무관)."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params(kr_entry_open_delay_min=0))
    strat._premarket_confirmed[KR_SYMBOL] = 81000.0
    strat._session_date["KR"] = DAY1

    reg_open_ts = _kr_now(KR_OPEN)
    bars = pd.DataFrame(
        [
            {"open": 80900.0, "high": 80950.0, "low": 80800.0, "close": 80850.0, "volume": 1000.0},
            {"open": 80850.0, "high": 81200.0, "low": 79000.0, "close": 81100.0, "volume": 1200.0},  # 저가가 시가 아래
        ],
        index=pd.DatetimeIndex([reg_open_ts, reg_open_ts + timedelta(minutes=1)], tz=KST),
    )
    now = reg_open_ts + timedelta(minutes=1, seconds=30)
    ctx = _kr_ctx({KR_SYMBOL: 81150.0}, now, bars={KR_SYMBOL: bars}, kr_open=True)

    signals = strat.on_cycle(ctx)
    assert signals == []


# ---------------- 직접 진입(2번)

def test_kr_premarket_never_enters_directly_even_on_perfect_pattern():
    """**한국장에는 연속 프리마켓이 없다**(2026-08-26 소유자 교정).

    08:30 이전엔 거래 자체가 없고, 08:30~09:00 은 주문만 모아 09:00 정각에 하나의
    시가로 일괄 체결한다(장전 시간외 종가 08:30~08:40 도 전일 종가 고정이라 가격
    발견이 없다). 그래서 프리마켓 "체결"은 실재할 수 없는 거래다.

    실사고(2026-08-26): 엔진이 08:27·08:46 에 진입을 기록했고, 09:00 시가가 갭으로
    열리며 손절선(-1.0%)을 2.8% 지나쳐 **의도한 -1% 손실이 -3.8%** 가 됐다
    (000720: 진입 129,200 / 손절선 127,900 / 실제 청산 124,369, -165,356원).
    페이퍼 브로커는 피드가 준 가격이면 무엇이든 체결시키므로 이 구조적 오류를
    스스로 못 잡는다 — 전략이 막아야 한다.

    관찰(P_pre 마킹)은 그대로 둔다 — 그건 주문을 내지 않고, 정규장 진입의 재료일
    뿐이다."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params(premarket_min_volume_krw=50_000_000))
    bars = {KR_SYMBOL: _kr_premarket_entry_bars(surge=True, notional_ok=True)}
    now = _kr_now(dtime(8, 2, 30))  # 재돌파봉(08:02) 완성 직후 — 패턴은 완벽하다
    ctx = _kr_ctx({KR_SYMBOL: 81300.0}, now, bars=bars)

    signals = strat.on_cycle(ctx)

    assert signals == [], "패턴이 아무리 좋아도 체결될 수 없는 시각엔 주문하지 않는다"
    assert strat._pattern_a_used.get(KR_SYMBOL, False) is False, \
        "진입하지 않았으므로 세션 진입 슬롯도 쓰지 않는다"
    assert "체결" in (strat.last_reject.get(KR_SYMBOL) or ""), \
        "왜 걸렀는지가 사유로 남아야 한다"


def test_kr_premarket_liquidity_guard_is_moot_but_still_no_entry():
    """유동성 가드 이전에 구조적으로 막힌다 — 가드 통과 여부와 무관하게 진입 없음."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params(premarket_min_volume_krw=50_000_000))
    bars = {KR_SYMBOL: _kr_premarket_entry_bars(surge=True, notional_ok=False)}
    now = _kr_now(dtime(8, 2, 30))
    ctx = _kr_ctx({KR_SYMBOL: 81300.0}, now, bars=bars)

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert strat._pattern_a_used.get(KR_SYMBOL, False) is False


def test_premarket_direct_entry_disabled_by_flag():
    strat = Scalp1mStrategy([KR_SYMBOL], _params(premarket_entry=False))
    bars = {KR_SYMBOL: _kr_premarket_entry_bars(surge=True, notional_ok=True)}
    now = _kr_now(dtime(8, 2, 30))
    ctx = _kr_ctx({KR_SYMBOL: 81300.0}, now, bars=bars)

    signals = strat.on_cycle(ctx)
    assert signals == []


def test_premarket_direct_entry_invalidated_when_l1_breaches_open():
    strat = Scalp1mStrategy([KR_SYMBOL], _params())
    bars = {KR_SYMBOL: _kr_premarket_entry_bars(surge=True, l1_breach_open=True)}
    now = _kr_now(dtime(8, 2, 30))
    ctx = _kr_ctx({KR_SYMBOL: 81300.0}, now, bars=bars)

    signals = strat.on_cycle(ctx)
    assert signals == []


# ---------------- 08:50 컷 + 세션당 상한 합산 + EoD

def test_no_new_entry_during_blackout_08_50_to_09_00():
    """패턴 A가 완성돼 있어도 08:50 이후엔 신규 진입 신호가 나지 않는다(관리만)."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params())
    bars = {KR_SYMBOL: _kr_premarket_entry_bars(surge=True, notional_ok=True)}
    now = _kr_now(dtime(8, 52))
    ctx = _kr_ctx({KR_SYMBOL: 81300.0}, now, bars=bars)

    signals = strat.on_cycle(ctx)
    assert signals == []
    assert strat._pattern_a_used.get(KR_SYMBOL, False) is False


def test_entry_cap_combines_premarket_and_regular_session():
    """프리마켓 직접 진입(A)이 A 슬롯을 쓰면, 정규장에서는 가속 경로가 아니라
    곧바로 패턴 B 평가로 넘어간다(A 슬롯이 이미 소진됐으므로) — 상한이 합산임을
    확인한다.

    2026-08-26: KR → US 로 옮겼다. 한국장에는 연속 프리마켓이 없어 직접 진입
    자체가 구조적으로 불가능하다(`_PREMARKET_DIRECT_ENTRY_MARKETS`). 이 테스트가
    지키는 성질(프리마켓+정규장 진입 슬롯 합산)은 시장과 무관하다."""
    strat = Scalp1mStrategy([US_PRE_SYMBOL], _params())
    bars_pre = {US_PRE_SYMBOL: _us_premarket_entry_bars(surge=True, notional_ok=True)}
    ctx_pre = _us_ctx({US_PRE_SYMBOL: 102.3}, _us_now(dtime(8, 2, 30)), bars=bars_pre)
    entry_signals = strat.on_cycle(ctx_pre)
    assert len(entry_signals) == 1
    assert strat._pattern_a_used.get(US_PRE_SYMBOL) is True

    # 블랙아웃(09:25~09:30) 확정 사이클 — 이 시나리오와 무관해도 된다.
    strat.on_cycle(_us_ctx({US_PRE_SYMBOL: 102.3}, _us_now(dtime(9, 26)), bars=bars_pre))

    # 정규장 09:3x — A 슬롯이 이미 쓰였으므로 패턴 A 는 더 이상 평가되지 않는다.
    reg_open_ts = _us_now(US_OPEN)
    touch = pd.DataFrame(
        [{"open": 102.3, "high": 102.35, "low": 102.29, "close": 102.295, "volume": 1000.0}],
        index=pd.DatetimeIndex([reg_open_ts], tz=NY),
    )
    ctx_reg = _us_ctx({US_PRE_SYMBOL: 102.295}, reg_open_ts + timedelta(seconds=30),
                      bars={US_PRE_SYMBOL: touch}, us_open=True)
    signals = strat.on_cycle(ctx_reg)
    assert signals == []  # 패턴 B 미충족(봉 1개뿐) — A 가 재평가되지 않는다는 것만 확인
    assert strat.last_reject.get(US_PRE_SYMBOL) == "패턴B 미충족"


def test_premarket_entry_flattens_before_regular_close():
    """프리마켓에서 연 포지션도 EoD 강제청산 레일이 동일하게 적용된다."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params())
    pos = Position(symbol=KR_SYMBOL, qty=10, avg_cost=81300.0, meta={
        "lots": {"scalp_1m": {"qty": 10.0, "entry": 81300.0, "stop": 79000.0,
                               "session": DAY1.isoformat(), "partial_taken": False}},
    })
    now = _kr_now(dtime(15, 29))
    ctx = _kr_ctx({KR_SYMBOL: 81400.0}, now, positions={KR_SYMBOL: pos}, kr_open=True)
    ctx.clock._flatten = frozenset({"KR"})

    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert "EoD" in signals[0].reason


# ---------------- US 프리마켓 대칭 테스트 (2026-08-18) — KR 스위트와 동일 시나리오

def test_us_premarket_confirmation_marks_symbol_at_blackout():
    strat = Scalp1mStrategy([US_PRE_SYMBOL], _params())
    bars = {US_PRE_SYMBOL: _us_premarket_confirm_bars(surge=True, breach_after=False)}
    now = _us_now(dtime(9, 25, 30))  # 블랙아웃 진입 직후
    ctx = _us_ctx({US_PRE_SYMBOL: 100.7}, now, bars=bars)

    signals = strat.on_cycle(ctx)

    assert signals == []  # 블랙아웃엔 신규 진입 없음
    assert strat._premarket_confirmed.get(US_PRE_SYMBOL) == pytest.approx(101.0)


def test_us_premarket_confirmation_rejected_without_volume_surge():
    strat = Scalp1mStrategy([US_PRE_SYMBOL], _params())
    bars = {US_PRE_SYMBOL: _us_premarket_confirm_bars(surge=False, breach_after=False)}
    ctx = _us_ctx({US_PRE_SYMBOL: 100.7}, _us_now(dtime(9, 25, 30)), bars=bars)

    strat.on_cycle(ctx)

    assert US_PRE_SYMBOL not in strat._premarket_confirmed


def test_us_regular_session_accelerated_entry_uses_premarket_high_as_p1():
    strat = Scalp1mStrategy([US_PRE_SYMBOL], _params())
    strat._premarket_confirmed[US_PRE_SYMBOL] = 101.0
    strat._session_date["US"] = DAY1

    reg_open_ts = _us_now(US_OPEN)
    bars = pd.DataFrame(
        [
            {"open": 100.9, "high": 100.95, "low": 100.9, "close": 100.92, "volume": 1000.0},
            {"open": 100.92, "high": 101.2, "low": 100.85, "close": 101.1, "volume": 1200.0},
        ],
        index=pd.DatetimeIndex([reg_open_ts, reg_open_ts + timedelta(minutes=1)], tz=NY),
    )
    now = reg_open_ts + timedelta(minutes=1, seconds=30)
    ctx = _us_ctx({US_PRE_SYMBOL: 101.15}, now, bars={US_PRE_SYMBOL: bars}, us_open=True)

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG
    assert "패턴A" in signals[0].reason
    assert strat._pattern_a_used.get(US_PRE_SYMBOL) is True


def test_us_premarket_direct_entry_on_full_pattern_a_with_liquidity_guard_passed():
    strat = Scalp1mStrategy([US_PRE_SYMBOL], _params(premarket_min_volume_usd=50_000))
    bars = {US_PRE_SYMBOL: _us_premarket_entry_bars(surge=True, notional_ok=True)}
    now = _us_now(dtime(8, 2, 30))  # 재돌파봉(08:02) 완성 직후
    ctx = _us_ctx({US_PRE_SYMBOL: 102.3}, now, bars=bars)

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.ENTER_LONG
    assert "프리마켓" in sig.reason and "패턴A" in sig.reason
    assert strat._pattern_a_used.get(US_PRE_SYMBOL) is True


def test_us_premarket_direct_entry_rejected_below_liquidity_guard():
    strat = Scalp1mStrategy([US_PRE_SYMBOL], _params(premarket_min_volume_usd=50_000))
    bars = {US_PRE_SYMBOL: _us_premarket_entry_bars(surge=True, notional_ok=False)}
    now = _us_now(dtime(8, 2, 30))
    ctx = _us_ctx({US_PRE_SYMBOL: 102.3}, now, bars=bars)

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert strat._pattern_a_used.get(US_PRE_SYMBOL, False) is False


def test_us_premarket_direct_entry_disabled_by_flag():
    strat = Scalp1mStrategy([US_PRE_SYMBOL], _params(premarket_entry=False))
    bars = {US_PRE_SYMBOL: _us_premarket_entry_bars(surge=True, notional_ok=True)}
    now = _us_now(dtime(8, 2, 30))
    ctx = _us_ctx({US_PRE_SYMBOL: 102.3}, now, bars=bars)

    signals = strat.on_cycle(ctx)
    assert signals == []


def test_no_new_entry_during_us_blackout_09_25_to_09_30():
    """패턴 A가 완성돼 있어도 09:25 ET 이후엔 신규 진입 신호가 나지 않는다
    (관리만) — KR의 08:50~09:00 블랙아웃과 대칭."""
    strat = Scalp1mStrategy([US_PRE_SYMBOL], _params())
    bars = {US_PRE_SYMBOL: _us_premarket_entry_bars(surge=True, notional_ok=True)}
    now = _us_now(dtime(9, 27))
    ctx = _us_ctx({US_PRE_SYMBOL: 102.3}, now, bars=bars)

    signals = strat.on_cycle(ctx)
    assert signals == []
    assert strat._pattern_a_used.get(US_PRE_SYMBOL, False) is False


def test_us_premarket_min_volume_krw_does_not_gate_us_symbol():
    """US 심볼의 프리마켓 유동성 가드는 premarket_min_volume_usd만 본다 —
    premarket_min_volume_krw(원화 기준, 자릿수가 전혀 다름)를 잘못 적용하면
    달러 표시 거래대금이 항상 통과하거나 항상 막히는 사고가 난다."""
    strat = Scalp1mStrategy([US_PRE_SYMBOL], _params(
        premarket_min_volume_krw=50_000_000,  # 원화 기준 — US에는 적용되면 안 됨
        premarket_min_volume_usd=50_000,
    ))
    bars = {US_PRE_SYMBOL: _us_premarket_entry_bars(surge=True, notional_ok=True)}
    now = _us_now(dtime(8, 2, 30))
    ctx = _us_ctx({US_PRE_SYMBOL: 102.3}, now, bars=bars)

    signals = strat.on_cycle(ctx)
    assert len(signals) == 1  # 5천만원 기준이 아니라 $50k 기준으로 통과해야 한다


# ---------------- 확장 창 경계 — 정확한 발동 시각

def test_kr_window_state_boundaries():
    assert Scalp1mStrategy._premarket_window_state("KR", _kr_now(dtime(7, 59, 59))) == "closed"
    assert Scalp1mStrategy._premarket_window_state("KR", _kr_now(dtime(8, 0, 0))) == "premarket"
    assert Scalp1mStrategy._premarket_window_state("KR", _kr_now(dtime(8, 49, 59))) == "premarket"
    assert Scalp1mStrategy._premarket_window_state("KR", _kr_now(dtime(8, 50, 0))) == "blackout"
    assert Scalp1mStrategy._premarket_window_state("KR", _kr_now(dtime(8, 59, 59))) == "blackout"
    assert Scalp1mStrategy._premarket_window_state("KR", _kr_now(dtime(9, 0, 0))) == "closed"
    # 주말은 프리마켓 시간대라도 닫힘.
    saturday = date(2026, 1, 10)
    assert Scalp1mStrategy._premarket_window_state("KR", _kr_now(dtime(8, 10), day=saturday)) == "closed"


def test_us_window_state_boundaries():
    """US 프리마켓 창(ET 08:00~09:25)도 KR과 동일한 구조(premarket/blackout/closed)로
    판정된다(2026-08-18 대칭 확장)."""
    assert Scalp1mStrategy._premarket_window_state("US", _us_now(dtime(7, 59, 59))) == "closed"
    assert Scalp1mStrategy._premarket_window_state("US", _us_now(dtime(8, 0, 0))) == "premarket"
    assert Scalp1mStrategy._premarket_window_state("US", _us_now(dtime(9, 24, 59))) == "premarket"
    assert Scalp1mStrategy._premarket_window_state("US", _us_now(dtime(9, 25, 0))) == "blackout"
    assert Scalp1mStrategy._premarket_window_state("US", _us_now(dtime(9, 29, 59))) == "blackout"
    assert Scalp1mStrategy._premarket_window_state("US", _us_now(dtime(9, 30, 0))) == "closed"
    saturday = date(2026, 1, 10)
    assert Scalp1mStrategy._premarket_window_state("US", _us_now(dtime(8, 10), day=saturday)) == "closed"


def test_market_active_extends_both_kr_and_us_gates_during_premarket():
    """KR/US 둘 다 확장 창이 프리마켓 구간에서 시장을 "열림"으로 인정한다
    (2026-08-18 US 대칭 확장 — 이전에는 US가 확장 대상이 아니었다)."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params())
    ctx = _kr_ctx({KR_SYMBOL: 80000.0}, _kr_now(dtime(8, 10)))  # clock: KR 닫힘
    assert strat._market_active("KR", ctx) is True  # 확장 창이 열어준다

    us_ctx = Context(
        clock=FakeClock(_us_now(dtime(8, 30)), open_markets=frozenset()),  # clock: US 닫힘
        data=FakeDataFeed({}), broker=FakeBroker({}),
    )
    assert strat._market_active("US", us_ctx) is True  # US 확장 창도 열어준다


def test_pattern_b_rejects_confirm_bar_closing_below_ma60():
    """확인봉이 양봉이어도 종가가 60선 아래면 패턴 B 진입 금지 (2026-08-18 P0).

    실전 재현: 096770 10:28 — 양봉 조건만 보고 진입 → 종가<MA60 이라 다음
    사이클의 60선 트레일이 같은 봉으로 즉시 전량 청산, 동일 분 왕복(-198원,
    전액 수수료·호가 낙차). 진입 조건과 청산 조건은 상호 배타적이어야 한다 —
    '반등'은 60선 회복까지다.
    """
    strat = Scalp1mStrategy(["AAA"], _params())
    strat._pattern_a_used["AAA"] = True
    strat._session_date["US"] = DAY1

    open_ts = datetime.combine(DAY1, US_OPEN, tzinfo=NY)
    warmup_idx, warmup_rows = _warmup(NY, open_ts, 59)
    touch_ts = open_ts
    confirm_ts = open_ts + timedelta(minutes=1)
    touch_confirm = pd.DataFrame(
        [
            {"open": 100.0, "high": 100.1, "low": 99.85, "close": 99.85, "volume": 1000.0},
            # 확인봉: 양봉(99.5→99.8)이지만 종가가 MA60(~100) 아래 — 실전 결함 모양 그대로
            {"open": 99.5, "high": 99.9, "low": 99.45, "close": 99.8, "volume": 1000.0},
        ],
        index=pd.DatetimeIndex([touch_ts, confirm_ts], tz=NY),
    )
    bars_df = pd.concat([
        pd.DataFrame(warmup_rows, index=pd.DatetimeIndex(warmup_idx, tz=NY)),
        touch_confirm,
    ])

    now = confirm_ts + timedelta(seconds=30)
    signals = strat.on_cycle(_ctx({"AAA": 99.8}, now, bars={"AAA": bars_df}))
    assert signals == [], "종가<MA60 확인봉으로는 진입이 나오면 안 된다"
    assert strat._pattern_b_used.get("AAA") is not True


# ============================================================ 일봉 추세/변동성 게이트 (2026-08-19)
# QuantConnect #407(ADX+DI 추세 필터)/#478(ATR 변동성 억제기) 이식 배선. 계산부
# 자체(정확성/경계값)는 tests/test_trend_gate.py가 고정한다 — 여기서는 "게이트가
# 패턴 A/B/프리마켓 진입 직전에 실제로 걸리는지 + 세션당 1회만 조회하는지"만 본다.

def _daily_bars(rows_close: list[float], *, band: float = 0.5) -> pd.DataFrame:
    """일봉 히스토리 합성 — adx_di/atr_ratio는 값의 순서만 보므로 인덱스 간격은
    무관(날짜 그대로 하루 간격을 준다)."""
    rows = [{"open": c, "high": c + band, "low": c - band, "close": c, "volume": 1000.0} for c in rows_close]
    idx = [DAY1 - timedelta(days=len(rows_close) - i) for i in range(len(rows_close))]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def _daily_bars_uptrend(n: int = 40) -> pd.DataFrame:
    """뚜렷한 상승추세 + 좁은 밴드 — 추세 게이트(ADX>=25, +DI>-DI)·변동성 게이트
    (ATR/가격<=0.10) 둘 다 통과(tests/test_trend_gate.py에서 실측: ADX=100,
    ATR비율≈0.011)."""
    return _daily_bars([100.0 + i for i in range(n)], band=0.5)


def _daily_bars_sideways(n: int = 40) -> pd.DataFrame:
    """횡보(사인파 진동) — 추세 게이트 미충족(실측 ADX≈20.2 < 기본 adx_min 25)."""
    return _daily_bars([100.0 + 0.5 * math.sin(i / 3.0) for i in range(n)], band=0.3)


def _daily_bars_high_volatility(n: int = 40) -> pd.DataFrame:
    """상승추세(추세 게이트는 통과)이지만 밴드가 넓어 변동성 게이트 미충족
    (실측 ATR비율≈0.216 > 기본 max_atr_ratio 0.10)."""
    return _daily_bars([100.0 + i for i in range(n)], band=15.0)


def test_trend_gate_blocks_pattern_a_entry_on_low_adx():
    strat = Scalp1mStrategy(["AAA"], _params(trend_gate_mode="block"))
    bars = {"AAA": _pattern_a_bars(surge=True)}  # 게이트 없으면 진입했을 시퀀스
    ctx = _ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars,
               daily_bars={"AAA": _daily_bars_sideways()})
    signals = strat.on_cycle(ctx)
    assert signals == []
    assert "추세 게이트" in strat.last_reject["AAA"]
    assert strat._pattern_a_used.get("AAA") is not True


def test_trend_gate_blocks_pattern_a_entry_on_high_atr_ratio():
    strat = Scalp1mStrategy(["AAA"], _params(trend_gate_mode="block"))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    ctx = _ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars,
               daily_bars={"AAA": _daily_bars_high_volatility()})
    signals = strat.on_cycle(ctx)
    assert signals == []
    assert "변동성 과다" in strat.last_reject["AAA"]


def test_trend_gate_allows_entry_when_daily_trend_and_volatility_pass():
    strat = Scalp1mStrategy(["AAA"], _params())
    bars = {"AAA": _pattern_a_bars(surge=True)}
    ctx = _ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars,
               daily_bars={"AAA": _daily_bars_uptrend()})
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG


def test_trend_gate_disabled_bypasses_check():
    strat = Scalp1mStrategy(["AAA"], _params(trend_gate_enabled=False))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    # 게이트가 켜져 있었으면 거부됐을 일봉(횡보)이지만 꺼져 있으므로 무시된다.
    ctx = _ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars,
               daily_bars={"AAA": _daily_bars_sideways()})
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert ctx.data.daily_history_calls == []  # 비활성이면 일봉 조회 자체를 안 한다


def test_trend_gate_falls_back_to_pass_when_daily_history_missing():
    """일봉 조회 실패(빈 DataFrame, 기존 동작 보존) — daily_bars를 주지 않으면
    trend_gate_enabled=True(기본)여도 게이트가 통과한다. 기존 53개 테스트가
    daily_bars 없이도 무수정 통과하는 이유가 바로 이 폴백이다."""
    strat = Scalp1mStrategy(["AAA"], _params())
    bars = {"AAA": _pattern_a_bars(surge=True)}
    ctx = _ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars)  # daily_bars 없음
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert ctx.data.daily_history_calls == ["AAA"]  # 조회는 했지만(빈 결과) 통과 처리


def test_trend_gate_queries_daily_history_once_per_session():
    """세션당 심볼 1회만 일봉을 조회한다 — 반복 사이클(패턴 A 미충족으로 매번
    재평가되는 상황)에도 daily_history_calls가 늘지 않아야 한다."""
    strat = Scalp1mStrategy(["AAA"], _params())
    bars = {"AAA": _pattern_a_bars(surge=False)}  # 서지 없음 → 매 사이클 재평가(진입 없음)
    ctx = _ctx({"AAA": 102.4}, _now_within_window(3.0), bars=bars,
               daily_bars={"AAA": _daily_bars_uptrend()})

    strat.on_cycle(ctx)
    strat.on_cycle(ctx)
    strat.on_cycle(ctx)

    assert ctx.data.daily_history_calls.count("AAA") == 1


def test_trend_gate_blocks_premarket_direct_entry_on_low_adx():
    """2026-08-26: KR → US. 한국장은 프리마켓 직접 진입이 구조적으로 불가라
    추세 게이트까지 오지도 않는다(그건 별도 테스트가 지킨다). 이 테스트가 지키는
    성질(게이트가 프리마켓 진입 경로에서도 작동)은 US 로 확인한다."""
    strat = Scalp1mStrategy([US_PRE_SYMBOL],
                            _params(trend_gate_mode="block", premarket_min_volume_usd=50_000))
    bars = {US_PRE_SYMBOL: _us_premarket_entry_bars(surge=True, notional_ok=True)}
    ctx = _us_ctx({US_PRE_SYMBOL: 102.3}, _us_now(dtime(8, 2, 30)), bars=bars,
                  daily_bars={US_PRE_SYMBOL: _daily_bars_sideways()})
    signals = strat.on_cycle(ctx)
    assert signals == []
    assert "추세 게이트" in strat.last_reject[US_PRE_SYMBOL]


def test_trend_gate_shadow_records_verdict_without_blocking():
    """기본값 shadow: 게이트가 미충족이어도 **진입을 막지 않고** 판정만 사유에
    남긴다 (2026-08-19). 근거: 도입 당일 소급 측정에서 이 게이트가 최대 손실을
    못 걸렀고 소폭 승자도 함께 막았다 — 검증 전에 진입을 막으면 근거 없이
    전략을 바꾸는 것이다. 표본은 저널의 이 문자열로 쌓인다."""
    strat = Scalp1mStrategy(["AAA"], _params())  # 기본 = shadow
    assert strat.trend_gate_mode == "shadow"

    bars = _pattern_a_bars()
    daily = _flat_ma_bars(n=40, close=100.0)  # 횡보 → ADX 낮음(차단 후보)
    now = _now_within_window()
    signals = strat.on_cycle(
        _ctx({"AAA": 101.5}, now, bars={"AAA": bars}, daily_bars={"AAA": daily})
    )

    assert len(signals) == 1, "shadow 는 진입을 막지 않는다"
    assert "게이트:" in signals[0].reason
    assert strat.gate_verdict.get("AAA") is not None, "판정 자체는 기록된다"


# ==================================================== 전량 익절(take_profit_bps)
# 2026-08-21 실측 근거. 라이브 원장 종결 63건 중 1분봉 재생 가능한 27건에 사전
# 지정 청산 규칙을 재생했다(scalp_1m 분 18건):
#
#   현행(원장)              중앙 -77.7bp  평균 -58.0bp  승률 22%  합계 -1,044
#   전량 익절 +100/손절 -100  중앙 +38.5bp  평균  -5.1bp  승률 50%  합계    -92
#   절반 익절 +100/손절 -100  중앙 -46.8bp  평균 -67.5bp  승률  0%  합계 -1,215
#
# **절반 익절은 오히려 나빴다** — 남긴 절반이 마감까지 끌려가 되돌림을 그대로
# 맞는다(같은 표본에서 '마감까지 보유'는 평균 -194.8bp). 기존 `partial_fraction`은
# 코드가 `0 < x < 1`로 강제하므로 전량 익절을 표현할 수 없어, 전량 청산 경로를
# 따로 둔다. 0 = 비활성(기본)이라 미설정 시 동작이 지금과 100% 같다.
#
# 순서: 하드 손절 바로 다음, MA60 잔량 트레일보다 앞. 익절가는 하드 목표라
# 트레일 판정보다 우선한다.

def test_take_profit_bps_exits_full_position():
    strat = Scalp1mStrategy(["AAA"], _params(take_profit_bps=100))
    pos = _lot_position(entry=100.0, stop=97.0)
    signals = strat.on_cycle(_ctx({"AAA": 101.0}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.EXIT_LONG
    assert sig.exit_fraction == 1.0
    assert "익절" in sig.reason


def test_take_profit_bps_not_reached_does_not_exit():
    strat = Scalp1mStrategy(["AAA"], _params(take_profit_bps=100))
    pos = _lot_position(entry=100.0, stop=97.0)
    # +99bp — 문턱 바로 아래. 절반 익절(1.5R=104.5)도 아직 아니다.
    signals = strat.on_cycle(_ctx({"AAA": 100.99}, _now_within_window(5.0), positions={"AAA": pos}))
    assert signals == []


def test_take_profit_bps_absent_keeps_today_behaviour():
    """미설정(기본)이면 절반 익절 경로가 지금 그대로 살아 있어야 한다."""
    strat = Scalp1mStrategy(["AAA"], _params(partial_take_r=1.5, partial_fraction=0.5))
    pos = _lot_position(entry=100.0, stop=97.0)
    signals = strat.on_cycle(_ctx({"AAA": 104.6}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.SCALE_OUT


def test_take_profit_bps_takes_precedence_over_partial():
    """전량 익절이 켜져 있으면 절반 익절보다 먼저 발동한다(중복 청산 방지)."""
    strat = Scalp1mStrategy(["AAA"], _params(take_profit_bps=100, partial_take_r=1.5))
    pos = _lot_position(entry=100.0, stop=97.0)
    signals = strat.on_cycle(_ctx({"AAA": 104.6}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG


def test_stop_still_wins_over_take_profit():
    """손절과 익절이 동시에 참일 수는 없지만, 순서가 바뀌어 손절이 밀리면 안 된다."""
    strat = Scalp1mStrategy(["AAA"], _params(take_profit_bps=100))
    pos = _lot_position(entry=100.0, stop=97.0)
    signals = strat.on_cycle(_ctx({"AAA": 96.9}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert "손절" in signals[0].reason


def test_invalid_take_profit_bps_raises():
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(take_profit_bps=-1))


# ============================== 본전 이동 + 고수위 트레일(breakeven_at_bp/trail_bp)
# 2026-08-27 실측 근거(원장 66건): 패자 29건 중 12건이 +50bp 를 찍고도 손실
# (반납형), 승자 실현 중앙 +94bp vs 세션 MFE 중앙 +342bp(고정 TP 가 상방 절단).
# 반사실 시뮬 57트립·탐색 6회: BE50+트레일70 이 고정 TP100 대비 건당 +8.6bp.
# 0 = 비활성(기본) — 미설정이면 동작이 기존과 100% 같다.

def test_breakeven_move_raises_stop_to_entry_after_threshold():
    """+50bp 를 찍은 뒤 진입가로 되돌아오면 본전에서 나간다 — 반납형 절단."""
    strat = Scalp1mStrategy(["AAA"], _params(breakeven_at_bp=50))
    pos = _lot_position(entry=100.0, stop=97.0)
    # 사이클 1: +60bp — 본전 이동 발동, 청산은 없다
    assert strat.on_cycle(_ctx({"AAA": 100.6}, _now_within_window(5.0), positions={"AAA": pos})) == []
    # 사이클 2: 진입가 복귀 — 원래 스탑(97.0)이었으면 계속 보유했을 자리
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(6.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert "이익보호" in signals[0].reason


def test_trailing_stop_follows_high_water_after_arming():
    """고수위 −70bp 트레일 — 단 **이익 구간(+50bp)에 들어간 뒤에만** 작동한다."""
    strat = Scalp1mStrategy(["AAA"], _params(breakeven_at_bp=50, trail_bp=70))
    pos = _lot_position(entry=100.0, stop=97.0)
    assert strat.on_cycle(_ctx({"AAA": 102.0}, _now_within_window(5.0), positions={"AAA": pos})) == []
    # 고수위 102.0 → 무장 후 트레일 스탑 101.286. +120bp 로 되돌림 → 이익보호 청산
    signals = strat.on_cycle(_ctx({"AAA": 101.2}, _now_within_window(6.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert "이익보호" in signals[0].reason


def test_trail_does_not_tighten_initial_stop_before_profit():
    """**2026-08-28 수리한 계약**: 이익 구간 진입 전에는 트레일이 구조 손절을
    건드리지 않는다.

    직전 구현은 진입 직후부터 스탑을 `진입가 -trail_bp` 로 조여, 구조 손절이
    더 넓어도 무력화했다. 실거래 확증(096770): 구조손절 -111bp 가 트레일 때문에
    -70bp 로 조여져 청산됐고, 원래 손절선이었다면 살아남았다. 원장 실측은 더
    분명하다 — 손절당한 뒤 **76%(35/46)가 당일 진입가 위로 회복**(회복 폭 중앙
    +105bp). 진입이 틀린 게 아니라 손절이 노이즈에 걸린 것이다."""
    strat = Scalp1mStrategy(["AAA"], _params(breakeven_at_bp=50, trail_bp=70))
    pos = _lot_position(entry=100.0, stop=97.0)
    # -50bp 로 밀려도(트레일이 켜졌다면 -70bp 스탑에 걸렸을 자리) 구조 손절 97.0
    # 위이므로 계속 보유해야 한다.
    assert strat.on_cycle(_ctx({"AAA": 99.5}, _now_within_window(5.0), positions={"AAA": pos})) == []
    assert strat.on_cycle(_ctx({"AAA": 99.2}, _now_within_window(6.0), positions={"AAA": pos})) == []
    assert pos.meta["lots"]["scalp_1m"]["stop"] == 97.0, "이익 전에는 스탑이 움직이지 않는다"
    # 구조 손절 아래로 내려가면 그때는 손절
    signals = strat.on_cycle(_ctx({"AAA": 96.9}, _now_within_window(7.0), positions={"AAA": pos}))
    assert len(signals) == 1 and "손절" in signals[0].reason


def test_trail_stays_armed_after_pullback_below_threshold():
    """한 번 무장하면 되돌림으로 문턱 아래로 내려가도 풀리지 않는다 — 풀리면
    본전 보호가 사라져 이익을 반납하게 된다."""
    strat = Scalp1mStrategy(["AAA"], _params(breakeven_at_bp=50, trail_bp=70))
    pos = _lot_position(entry=100.0, stop=97.0)
    assert strat.on_cycle(_ctx({"AAA": 100.6}, _now_within_window(5.0), positions={"AAA": pos})) == []
    assert pos.meta["lots"]["scalp_1m"]["trail_armed"] is True
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(6.0), positions={"AAA": pos}))
    assert len(signals) == 1 and "이익보호" in signals[0].reason


def test_partial_target_survives_stop_raise():
    """스탑이 본전으로 올라와도 절반 익절 목표는 최초 R(r0) 기준을 유지한다 —
    entry-stop 재계산이면 R=0 이 되어 목표 자체가 사라지는 회귀를 막는다."""
    strat = Scalp1mStrategy(["AAA"], _params(breakeven_at_bp=50, partial_take_r=1.5))
    pos = _lot_position(entry=100.0, stop=97.0)
    assert strat.on_cycle(_ctx({"AAA": 100.6}, _now_within_window(5.0), positions={"AAA": pos})) == []
    signals = strat.on_cycle(_ctx({"AAA": 104.6}, _now_within_window(6.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.SCALE_OUT


def test_breakeven_trail_disabled_by_default_keeps_behaviour():
    """미설정(기본 0)이면 스탑이 절대 움직이지 않는다 — 기존 계약 보존."""
    strat = Scalp1mStrategy(["AAA"], _params())
    pos = _lot_position(entry=100.0, stop=97.0)
    assert strat.on_cycle(_ctx({"AAA": 102.0}, _now_within_window(5.0), positions={"AAA": pos})) == []
    assert pos.meta["lots"]["scalp_1m"]["stop"] == 97.0
    assert strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(6.0), positions={"AAA": pos})) == []


def test_invalid_breakeven_and_trail_raise():
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(breakeven_at_bp=-1))
    with pytest.raises(ValueError):
        Scalp1mStrategy(["AAA"], _params(trail_bp=-1))


# ============================================================ KR 개장 초반 진입 지연 게이트 (2026-09-02)


def _kr_pattern_a_bars(*, warmup_n=25, day=DAY1):
    """KR 버전 패턴 A 시퀀스 — `_pattern_a_bars`와 동일 구조(KST/KR_OPEN, 시가 80000)."""
    open_ts = _kr_now(KR_OPEN, day)
    idx, rows = _warmup(KST, open_ts, warmup_n)
    session_rows = [
        {"open": 80000.0, "high": 81000.0, "low": 79900.0, "close": 80800.0, "volume": 3500.0},  # P1봉(서지)
        {"open": 80800.0, "high": 80900.0, "low": 80500.0, "close": 80600.0, "volume": 1000.0},   # L1봉
        {"open": 80600.0, "high": 81500.0, "low": 80500.0, "close": 81300.0, "volume": 1200.0},   # 재돌파봉
    ]
    session_idx = [open_ts + timedelta(minutes=i) for i in range(3)]
    idx += session_idx
    rows += session_rows
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=KST))


def test_kr_entry_open_delay_gate_blocks_before_delay():
    """KR 정규장 개장 직후(5분 < 기본 30분)엔 패턴 A가 완성돼 있어도 신규
    진입이 나가지 않는다 — 모듈 docstring "KR 개장 초반 진입 지연 게이트" 절."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params())
    bars = {KR_SYMBOL: _kr_pattern_a_bars()}
    now = _kr_now(KR_OPEN) + timedelta(minutes=5)
    ctx = _kr_ctx({KR_SYMBOL: 81300.0}, now, bars=bars, kr_open=True)

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert strat._pattern_a_used.get(KR_SYMBOL, False) is False


def test_kr_entry_open_delay_gate_allows_after_delay():
    """같은 패턴 A가 30분 경과 후엔 정상 진입한다."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params())
    bars = {KR_SYMBOL: _kr_pattern_a_bars()}
    now = _kr_now(KR_OPEN) + timedelta(minutes=31)
    ctx = _kr_ctx({KR_SYMBOL: 81300.0}, now, bars=bars, kr_open=True)

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG


def test_kr_entry_open_delay_gate_zero_preserves_existing_behavior():
    """kr_entry_open_delay_min=0 — 하위호환/실험 롤백: 개장 직후에도 즉시 진입."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params(kr_entry_open_delay_min=0))
    bars = {KR_SYMBOL: _kr_pattern_a_bars()}
    now = _kr_now(KR_OPEN) + timedelta(minutes=1, seconds=30)
    ctx = _kr_ctx({KR_SYMBOL: 81300.0}, now, bars=bars, kr_open=True)

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG


def test_us_entry_unaffected_by_kr_entry_open_delay_gate():
    """US는 kr_entry_open_delay_min의 영향을 받지 않는다 — 기본값(30)이어도
    US 정규장 개장 직후(1.5분) 진입은 그대로 나간다."""
    strat = Scalp1mStrategy(["AAA"], _params())  # kr_entry_open_delay_min 기본 30
    bars = {"AAA": _pattern_a_bars(surge=True)}
    ctx = _ctx({"AAA": 102.4}, _now_within_window(1.5), bars=bars)

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG


def test_kr_entry_open_delay_gate_does_not_block_position_management():
    """진입은 지연 게이트에 막혀도, 이미 보유 중인 포지션의 손절 관리는
    시간대와 무관하게 동작한다 — "진입은 미뤄도 청산은 아니다" 비대칭."""
    strat = Scalp1mStrategy([KR_SYMBOL], _params())
    pos = Position(symbol=KR_SYMBOL, qty=10, avg_cost=81300.0, meta={
        "lots": {"scalp_1m": {"qty": 10.0, "entry": 81300.0, "stop": 81000.0,
                               "session": DAY1.isoformat(), "partial_taken": False}},
    })
    now = _kr_now(KR_OPEN) + timedelta(minutes=5)  # 지연 게이트 창 안(< 30분)
    ctx = _kr_ctx({KR_SYMBOL: 80900.0}, now, positions={KR_SYMBOL: pos}, kr_open=True)

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    assert "손절" in signals[0].reason
