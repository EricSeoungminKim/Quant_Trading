"""`Scalp1mPureStrategy`(순수함수 계약, quant.core.strategy_api)가 기존
`Scalp1mStrategy`와 **같은 신호**를 내는지 증명한다 — 엔진 분리 설계 Phase A,
donchian 파일럿 다음 이전 대상(scalp_1m, 가변 상태 최다).

세 층위(`test_donchian_pure.py`와 동일 구조)로 검증한다. 전 층위에서
`Scalp1mPureShell.on_cycle(ctx)`(= `Strategy` Protocol 그대로)를 쓴다 —
`StrategySnapshot`을 손으로 조립하지 않고, `PureStrategyShell`이 실제로
`ctx`에서 스냅샷을 만드는 전체 경로(requirements() → snapshot 조립 → decide())를
그대로 태워 legacy `Scalp1mStrategy.on_cycle(ctx)`와 나란히 비교한다 — 이쪽이
손으로 만든 StrategySnapshot을 넘기는 것보다 강한 증명이다(shell 배선 자체의
버그도 잡는다).

1. 단일 사이클 신호 동치 — 패턴 A(서지 있음/없음/L1 무효), 패턴 B(A 사용 후만
   평가), 세션 진입 2회 상한, 진입창 밖, 관리(손절/EoD/오버나잇/60선/부분익절),
   프리마켓(관찰 마킹/가속 진입/직접 진입/유동성 가드), 추세 게이트(block/shadow/off).
2. 다중 사이클 동치 — 진입 → 체결 시뮬레이션 → +1.5R 부분익절 → 60선 이탈 청산,
   세션 롤 오버나잇 강제청산, 세션당 2회 상한(A 소진 후 B).
3. "백테스트 규모" 동치 — `run_backtest`는 쓰지 않는다(아래 사유). 대신 여러
   거래일에 걸친 합성 사이클 시퀀스를 legacy/pure 양쪽에 동일하게 흘려
   총 진입 신호 수(entry count)가 일치하는지 확인한다.

**층위 3에서 `run_backtest`를 안 쓰는 이유**: donchian_pure는 `config/settings.yaml`의
"donchian" 블록을 복사해 "donchian_pure" 블록만 추가하는 방식으로 `run_backtest`를
그대로 썼다. scalp_1m은 그 방식이 성립하지 않는다 — (a) `settings.yaml`의 scalp_1m
`symbols: []`이다(관심종목 유니버스가 런타임에 채워지는 구조라 정적 백테스트
심볼이 없다), (b) 모듈 docstring 자체가 "Toss 1분봉은 4거래일 롤링만 제공하므로
백테스트 표본이 없다 — paper 번인이 유일한 검증 경로"라고 명시한다. 이 저장소에
scalp_1m용 `run_backtest` 선례 자체가 없다(grep 결과 0건). 근거 없이 새 백테스트
경로를 만드는 대신, 같은 목적(다중 세션 규모에서의 수치 동치)을 합성 다중일
시퀀스로 대체한다.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Position, Quote, SignalAction
from quant.core.ports import Context
from quant.trade.strategy.scalp_1m import Scalp1mPureShell, Scalp1mStrategy

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DAY1 = date(2026, 1, 5)  # 월요일
DAY2 = date(2026, 1, 6)
US_OPEN = dtime(9, 30)
KR_OPEN = dtime(9, 0)
KR_PRE_OPEN = dtime(8, 0)
US_PRE_OPEN = dtime(8, 0)
KR_SYMBOL = "005930"
US_PRE_SYMBOL = "SOXL"

LEGACY_ID = "scalp_1m"
PURE_ID = "scalp_1m_pure"


# ============================================================ 페이크 인프라
# test_scalp_1m.py와 인터페이스는 같지만, should_flatten을 실제 공식
# (mtc - cadence < flatten_minutes, quant/core/clock.py)으로 구현한다 —
# legacy는 ctx.clock.should_flatten을 직접 부르고, pure는 snap.minutes_to_close/
# cadence_minutes로 같은 공식을 재현하므로(Scalp1mPureStrategy._should_flatten),
# 두 경로가 같은 공식을 공유해야 동치성이 의미가 있다(donchian_pure 선례와 동일).

class FakeClock:
    def __init__(self, now, open_markets=frozenset({"US"}), minutes_to_close=300.0,
                 cadence_minutes=5.0 / 60):
        self._now = now
        self._open = open_markets
        self._mtc = minutes_to_close
        self._cadence = cadence_minutes

    def now(self):
        return self._now

    def is_market_open(self, market):
        return market in self._open

    def minutes_to_close(self, market):
        return self._mtc

    def cadence_minutes(self):
        return self._cadence

    def should_flatten(self, market, flatten_minutes):
        mtc = self.minutes_to_close(market)
        return mtc is not None and mtc - self._cadence < flatten_minutes


class FakeDataFeed:
    def __init__(self, quotes, bars=None, daily_bars=None):
        self._quotes = quotes
        self._bars = bars or {}
        self._daily_bars = daily_bars or {}

    def quote(self, symbol):
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(NY), price=self._quotes[symbol])

    def history(self, symbol, interval, n):
        df = self._daily_bars.get(symbol) if interval == "1d" else self._bars.get(symbol)
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

    def place_order(self, order):
        raise NotImplementedError


def _ctx(quotes, now, bars=None, positions=None, open_markets=frozenset({"US"}),
          minutes_to_close=300.0, daily_bars=None):
    return Context(
        clock=FakeClock(now, open_markets, minutes_to_close),
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


def _strats(symbols, params, market="US"):
    legacy = Scalp1mStrategy(list(symbols), params, market=market, id=LEGACY_ID)
    pure = Scalp1mPureShell(list(symbols), params, market=market, id=PURE_ID)
    return legacy, pure


def _sig_key(sig):
    """비교용 튜플 — strategy_id는 두 구현이 별도 이름으로 등록돼 의도적으로
    다르므로 제외(donchian_pure의 관례와 동일). reason은 포함(엄격 비교)."""
    return (
        sig.symbol, sig.action, sig.target_weight,
        sig.exit_fraction, sig.reason, sig.stop, sig.target, sig.state_update,
    )


def _keys(signals):
    return [_sig_key(s) for s in signals]


def _now_within_window(minutes_after_open=5.0, day=DAY1):
    return datetime.combine(day, US_OPEN, tzinfo=NY) + timedelta(minutes=minutes_after_open)


def _kr_now(t, day=DAY1):
    return datetime.combine(day, t, tzinfo=KST)


def _us_now(t, day=DAY1):
    return datetime.combine(day, t, tzinfo=NY)


# ============================================================ 봉 구성 헬퍼 (test_scalp_1m.py와 동일 정의)

def _warmup(before_ts, n, *, close=100.0, volume=1000.0):
    idx = [before_ts - timedelta(minutes=n - i) for i in range(n)]
    rows = [{"open": close, "high": close + 0.1, "low": close - 0.1, "close": close, "volume": volume}
            for _ in range(n)]
    return idx, rows


def _pattern_a_bars(*, surge=True, l1_breach_open=False, warmup_n=25):
    open_ts = datetime.combine(DAY1, US_OPEN, tzinfo=NY)
    idx, rows = _warmup(open_ts, warmup_n)
    p1_vol = 3500.0 if surge else 1000.0
    l1_low = 99.0 if l1_breach_open else 100.5
    session_rows = [
        {"open": 100.0, "high": 102.0, "low": 99.9, "close": 101.8, "volume": p1_vol},
        {"open": 101.8, "high": 101.9, "low": l1_low, "close": 100.6, "volume": 1000.0},
        {"open": 100.6, "high": 102.5, "low": 100.5, "close": 102.3, "volume": 1200.0},
    ]
    session_idx = [open_ts + timedelta(minutes=i) for i in range(3)]
    idx += session_idx
    rows += session_rows
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=NY))


def _flat_ma_bars(n=65, *, close=100.0, last_close=None, day=DAY1):
    open_ts = datetime.combine(day, US_OPEN, tzinfo=NY)
    idx = [open_ts + timedelta(minutes=i - n) for i in range(n)]
    rows = [{"open": close, "high": close + 0.1, "low": close - 0.1, "close": close, "volume": 1000.0}
            for _ in range(n)]
    if last_close is not None:
        rows[-1] = {"open": close, "high": max(close, last_close) + 0.1,
                    "low": min(close, last_close) - 0.1, "close": last_close, "volume": 1000.0}
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=NY))


def _lot_position(strategy_id, entry=100.0, stop=97.0, *, session=DAY1.isoformat(),
                    partial_taken=False, symbol="AAA"):
    return Position(symbol=symbol, qty=10, avg_cost=entry, meta={
        "lots": {strategy_id: {"qty": 10.0, "entry": entry, "stop": stop,
                                 "session": session, "partial_taken": partial_taken}},
    })


def _seed_open(pure, symbol, entry=100.0, stop=97.0, *, session=DAY1.isoformat(),
               partial_taken=False):
    """`Scalp1mPureShell`의 내부 `_state["open"]`을 직접 채운다 — legacy가
    `Position.meta["lots"][id]`에서 entry/stop/session/partial_taken을 읽는
    것과 달리, pure는 그 정보를 **next_state로만** 들고 있으므로(클래스
    docstring "이 순수 버전은 Position.meta에 아무것도 쓰지 않는다") 관리
    시나리오를 단일 사이클에서 재현하려면 `Position.meta`가 아니라 여기를
    채워야 한다. 심볼에 대응하는 `Position`은 qty>0이기만 하면 된다(shell이
    `snap.lots`에 심볼을 채우는 유일한 조건 — 내용은 pure가 다시 읽지 않는다)."""
    pure._state = {**pure._state, "open": {
        **pure._state.get("open", {}),
        symbol: {"entry": entry, "stop": stop, "session": session, "partial_taken": partial_taken},
    }}


def _kr_premarket_entry_bars(*, surge=True, l1_breach_open=False, notional_ok=True, warmup_n=25, day=DAY1):
    pre_open_ts = _kr_now(KR_PRE_OPEN, day)
    idx, rows = _warmup(pre_open_ts, warmup_n)
    p1_vol = 3500.0 if surge else 1000.0
    l1_low = 79000.0 if l1_breach_open else 80500.0
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


def _us_premarket_entry_bars(*, surge=True, notional_ok=True, warmup_n=25, day=DAY1):
    """US 프리마켓 직접 진입용 봉(KR 헬퍼와 동일 구조, 달러 단위).

    2026-08-26: 프리마켓 직접 진입 동치 검증을 KR → US 로 옮기면서 추가했다.
    한국장에는 연속 프리마켓이 없어(08:30~09:00 동시호가는 09:00 일괄 체결)
    KR 경로로는 진입 자체가 일어나지 않는다."""
    pre_open_ts = _us_now(dtime(8, 0), day)
    idx, rows = _warmup(pre_open_ts, warmup_n)
    p1_vol = 3500.0 if surge else 1000.0
    last_vol = 200_000.0 if notional_ok else 1.0
    session_rows = [
        {"open": 100.0, "high": 101.0, "low": 99.9, "close": 100.8, "volume": p1_vol},
        {"open": 100.8, "high": 100.9, "low": 100.5, "close": 100.6, "volume": 1000.0},
        {"open": 100.6, "high": 101.5, "low": 100.5, "close": 101.3, "volume": last_vol},
    ]
    idx += [pre_open_ts + timedelta(minutes=i) for i in range(3)]
    rows += session_rows
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=NY))


def _kr_premarket_confirm_bars(*, surge=True, breach_after=False, warmup_n=25, day=DAY1):
    pre_open_ts = _kr_now(KR_PRE_OPEN, day)
    idx, rows = _warmup(pre_open_ts, warmup_n)
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


def _daily_bars(rows_close, *, band=0.5):
    rows = [{"open": c, "high": c + band, "low": c - band, "close": c, "volume": 1000.0} for c in rows_close]
    idx = [DAY1 - timedelta(days=len(rows_close) - i) for i in range(len(rows_close))]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def _daily_bars_uptrend(n=40):
    return _daily_bars([100.0 + i for i in range(n)], band=0.5)


def _daily_bars_sideways(n=40):
    return _daily_bars([100.0 + 0.5 * math.sin(i / 3.0) for i in range(n)], band=0.3)


# ============================================================ 층위 1 — 단일 사이클 동치

@pytest.mark.parametrize("surge,l1_breach", [(True, False), (False, False), (True, True)])
def test_pattern_a_entry_equivalence(surge, l1_breach):
    """서지 있음(진입), 서지 없음(거부), L1이 시가 아래로 뚫림(거부) 세 경로."""
    legacy, pure = _strats(["AAA"], _params())
    bars = {"AAA": _pattern_a_bars(surge=surge, l1_breach_open=l1_breach)}
    now = _now_within_window(3.0)

    ctx_legacy = _ctx({"AAA": 102.4}, now, bars=bars)
    ctx_pure = _ctx({"AAA": 102.4}, now, bars=bars)

    sig_legacy = legacy.on_cycle(ctx_legacy)
    sig_pure = pure.on_cycle(ctx_pure)
    assert _keys(sig_legacy) == _keys(sig_pure)


def test_pattern_a_stop_math_equivalence():
    legacy, pure = _strats(["AAA"], _params(stop_buffer_pct=0.3, stop_hard_cap_pct=3.0))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    now = _now_within_window(3.0)
    [sig_legacy] = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    [sig_pure] = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    assert sig_legacy.stop == pytest.approx(sig_pure.stop)
    assert sig_legacy.target_weight == pytest.approx(sig_pure.target_weight)


def test_pattern_b_evaluated_only_after_pattern_a_used_equivalence():
    """A를 아직 안 썼으면 B는 평가되지 않는다 — 양쪽 다 신호 없음."""
    legacy, pure = _strats(["AAA"], _params())
    touch = _flat_ma_bars(n=64, close=100.0, last_close=99.85)
    confirm_ts = touch.index[-1] + timedelta(minutes=1)
    confirm = pd.DataFrame(
        [{"open": 99.9, "high": 100.4, "low": 99.85, "close": 100.3, "volume": 1000.0}],
        index=pd.DatetimeIndex([confirm_ts], tz=NY),
    )
    bars_df = pd.concat([touch, confirm])
    now = confirm_ts + timedelta(seconds=30)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 100.35}, now, bars={"AAA": bars_df}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 100.35}, now, bars={"AAA": bars_df}))
    assert sig_legacy == [] and sig_pure == []


def test_pattern_b_entry_after_pattern_a_used_equivalence():
    open_ts = datetime.combine(DAY1, US_OPEN, tzinfo=NY)
    warmup_idx, warmup_rows = _warmup(open_ts, 59)
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

    legacy, pure = _strats(["AAA"], _params())
    legacy._pattern_a_used["AAA"] = True
    legacy._session_date["US"] = DAY1
    # pure 쪽은 decide()의 next_state를 통해서만 상태를 주입할 수 있다 — 첫
    # 사이클(패턴 A 사용 처리)을 먼저 재현한 뒤 이 사이클을 돌린다.
    inner = pure.inner
    inner_state = {"pattern_a_used": {"AAA": True}, "session_date": {"US": DAY1}}
    pure._state = inner_state

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 100.35}, now, bars={"AAA": bars_df}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 100.35}, now, bars={"AAA": bars_df}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and sig_legacy[0].action == SignalAction.ENTER_LONG
    assert "패턴B" in sig_legacy[0].reason


def test_session_entry_cap_equivalence():
    legacy, pure = _strats(["AAA"], _params())
    legacy._pattern_a_used["AAA"] = True
    legacy._pattern_b_used["AAA"] = True
    legacy._session_date["US"] = DAY1
    pure._state = {
        "pattern_a_used": {"AAA": True}, "pattern_b_used": {"AAA": True},
        "session_date": {"US": DAY1},
    }
    bars = {"AAA": _pattern_a_bars(surge=True)}
    now = _now_within_window(3.0)
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    sig_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    assert sig_legacy == [] and sig_pure == []


def test_no_entry_outside_window_equivalence():
    legacy, pure = _strats(["AAA"], _params(entry_window_minutes_after_open=90))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    now = _now_within_window(95.0)
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    sig_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    assert sig_legacy == [] and sig_pure == []


def test_entry_window_zero_all_session_equivalence():
    """0 = 전 세션 대기(2026-08-26 소유자 지시) — 개장 95분 뒤 진입도 두 구현이
    같아야 한다."""
    legacy, pure = _strats(["AAA"], _params(entry_window_minutes_after_open=0))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    now = _now_within_window(95.0)
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    sig_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and sig_legacy[0].action == SignalAction.ENTER_LONG


def test_structure_stop_mode_equivalence():
    """stop_mode=structure(2026-08-26 구조층 재작업) — 구조 손절도 두 구현이
    같아야 한다."""
    legacy, pure = _strats(["AAA"], _params(stop_mode="structure"))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    now = _now_within_window(3.0)
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    sig_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1
    assert sig_legacy[0].stop == sig_pure[0].stop


def test_williams_gate_block_equivalence():
    legacy, pure = _strats(["AAA"], _params(williams_gate_mode="block"))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    now = _now_within_window(3.0)
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    sig_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    assert sig_legacy == [] and sig_pure == []


def test_repeated_same_bar_cycles_do_not_duplicate_equivalence():
    legacy, pure = _strats(["AAA"], _params())
    bars = {"AAA": _pattern_a_bars(surge=True)}
    now = _now_within_window(3.0)

    first_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    first_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    assert _keys(first_legacy) == _keys(first_pure)
    assert len(first_legacy) == 1

    second_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    second_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    assert second_legacy == [] and second_pure == []


# ---------------- 관리(management) 동치 — 손절/EoD/오버나잇/60선/부분익절

def test_stop_loss_exit_equivalence():
    legacy, pure = _strats(["AAA"], _params())
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, stop=97.0)
    pos_pure = _lot_position(PURE_ID, entry=100.0, stop=97.0)  # qty>0만 shell에 필요
    _seed_open(pure, "AAA", entry=100.0, stop=97.0)
    now = _now_within_window(5.0)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 96.9}, now, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 96.9}, now, positions={"AAA": pos_pure}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "손절" in sig_legacy[0].reason


def test_eod_flatten_exit_equivalence():
    legacy, pure = _strats(["AAA"], _params())
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, stop=97.0)
    pos_pure = _lot_position(PURE_ID, entry=100.0, stop=97.0)
    _seed_open(pure, "AAA", entry=100.0, stop=97.0)
    now = _now_within_window(5.0)
    # flatten_before_close_minutes=1, cadence=5/60 -> mtc=0.5면 0.5-0.083<1 True
    sig_legacy = legacy.on_cycle(
        _ctx({"AAA": 101.0}, now, positions={"AAA": pos_legacy}, minutes_to_close=0.5))
    sig_pure = pure.on_cycle(
        _ctx({"AAA": 101.0}, now, positions={"AAA": pos_pure}, minutes_to_close=0.5))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "EoD" in sig_legacy[0].reason


def test_session_roll_overnight_exit_equivalence():
    legacy, pure = _strats(["AAA"], _params())
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, stop=97.0, session=DAY1.isoformat())
    pos_pure = _lot_position(PURE_ID, entry=100.0, stop=97.0, session=DAY1.isoformat())
    _seed_open(pure, "AAA", entry=100.0, stop=97.0, session=DAY1.isoformat())
    now = datetime.combine(DAY2, dtime(10, 30), tzinfo=NY)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos_pure}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "오버나잇" in sig_legacy[0].reason


def test_partial_take_profit_equivalence():
    legacy, pure = _strats(["AAA"], _params(partial_take_r=1.5, partial_fraction=0.5))
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, stop=97.0)  # R=3 -> target=104.5
    pos_pure = _lot_position(PURE_ID, entry=100.0, stop=97.0)
    _seed_open(pure, "AAA", entry=100.0, stop=97.0)
    now = _now_within_window(5.0)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 104.6}, now, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 104.6}, now, positions={"AAA": pos_pure}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and sig_legacy[0].action == SignalAction.SCALE_OUT
    assert sig_legacy[0].state_update == {"partial_taken": True}


def test_breakeven_trail_exit_equivalence():
    """본전 이동+트레일(2026-08-27)이 두 구현에서 같은 사이클에 같은 이유로
    발동해야 한다 — 상태 키(hi/r0/stop 상향)가 next_state 로만 흐르는 pure 와
    lot in-place 인 legacy 가 갈라지면 여기서 잡는다."""
    legacy, pure = _strats(["AAA"], _params(breakeven_at_bp=50, trail_bp=70))
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, stop=97.0)
    pos_pure = _lot_position(PURE_ID, entry=100.0, stop=97.0)
    _seed_open(pure, "AAA", entry=100.0, stop=97.0)
    now = _now_within_window(5.0)

    # 사이클 1: +200bp — 고수위 형성, 청산 없음(스탑은 101.286 으로 상향)
    assert legacy.on_cycle(_ctx({"AAA": 102.0}, now, positions={"AAA": pos_legacy})) == []
    assert pure.on_cycle(_ctx({"AAA": 102.0}, now, positions={"AAA": pos_pure})) == []
    # 사이클 2: +120bp 되돌림 — 양쪽 다 이익보호 청산
    now2 = _now_within_window(6.0)
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 101.2}, now2, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 101.2}, now2, positions={"AAA": pos_pure}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "이익보호" in sig_legacy[0].reason
    assert "이익보호" in sig_pure[0].reason


def test_partial_take_profit_does_not_refire_equivalence():
    legacy, pure = _strats(["AAA"], _params(partial_take_r=1.5, partial_fraction=0.5))
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, stop=97.0, partial_taken=True)
    pos_pure = _lot_position(PURE_ID, entry=100.0, stop=97.0, partial_taken=True)
    _seed_open(pure, "AAA", entry=100.0, stop=97.0, partial_taken=True)
    now = _now_within_window(5.0)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 104.6}, now, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 104.6}, now, positions={"AAA": pos_pure}))
    assert sig_legacy == [] and sig_pure == []


def test_ma60_close_below_exits_equivalence():
    legacy, pure = _strats(["AAA"], _params(ma_period=60))
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, stop=97.0)
    pos_pure = _lot_position(PURE_ID, entry=100.0, stop=97.0)
    _seed_open(pure, "AAA", entry=100.0, stop=97.0)
    bars = {"AAA": _flat_ma_bars(n=65, close=100.0, last_close=95.0)}
    now = _now_within_window(5.0)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 97.5}, now, bars=bars, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 97.5}, now, bars=bars, positions={"AAA": pos_pure}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "60선" in sig_legacy[0].reason


def test_holds_above_ma60_equivalence():
    legacy, pure = _strats(["AAA"], _params(ma_period=60))
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, stop=97.0)
    pos_pure = _lot_position(PURE_ID, entry=100.0, stop=97.0)
    _seed_open(pure, "AAA", entry=100.0, stop=97.0)
    bars = {"AAA": _flat_ma_bars(n=65, close=100.0, last_close=100.2)}
    now = _now_within_window(5.0)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 100.5}, now, bars=bars, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 100.5}, now, bars=bars, positions={"AAA": pos_pure}))
    assert sig_legacy == [] and sig_pure == []


# ---------------- 프리마켓 동치

def test_premarket_confirmation_marks_symbol_equivalence():
    legacy, pure = _strats([KR_SYMBOL], _params(), market="US")
    bars = {KR_SYMBOL: _kr_premarket_confirm_bars(surge=True, breach_after=False)}
    now = _kr_now(dtime(8, 50, 30))

    sig_legacy = legacy.on_cycle(_ctx({KR_SYMBOL: 80700.0}, now, bars=bars, open_markets=frozenset()))
    sig_pure = pure.on_cycle(_ctx({KR_SYMBOL: 80700.0}, now, bars=bars, open_markets=frozenset()))
    assert sig_legacy == [] and sig_pure == []
    assert legacy._premarket_confirmed.get(KR_SYMBOL) == pytest.approx(81000.0)
    assert pure._state["premarket_confirmed"].get(KR_SYMBOL) == pytest.approx(81000.0)


def test_premarket_direct_entry_equivalence():
    """2026-08-26: KR → US. 한국장은 연속 프리마켓이 없어 직접 진입이 구조적으로
    불가하다 — 동치 검증은 실제로 진입이 일어나는 US 에서 한다."""
    legacy, pure = _strats([US_PRE_SYMBOL], _params(premarket_min_volume_usd=50_000))
    bars = {US_PRE_SYMBOL: _us_premarket_entry_bars(surge=True, notional_ok=True)}
    now = _us_now(dtime(8, 2, 30))

    sig_legacy = legacy.on_cycle(_ctx({US_PRE_SYMBOL: 101.3}, now, bars=bars, open_markets=frozenset()))
    sig_pure = pure.on_cycle(_ctx({US_PRE_SYMBOL: 101.3}, now, bars=bars, open_markets=frozenset()))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "프리마켓" in sig_legacy[0].reason


def test_kr_premarket_no_direct_entry_equivalence():
    """KR 프리마켓은 두 구현 모두 침묵해야 한다(체결될 수 없는 주문은 안 낸다)."""
    legacy, pure = _strats([KR_SYMBOL], _params(premarket_min_volume_krw=50_000_000))
    bars = {KR_SYMBOL: _kr_premarket_entry_bars(surge=True, notional_ok=True)}
    now = _kr_now(dtime(8, 2, 30))

    sig_legacy = legacy.on_cycle(_ctx({KR_SYMBOL: 81300.0}, now, bars=bars, open_markets=frozenset()))
    sig_pure = pure.on_cycle(_ctx({KR_SYMBOL: 81300.0}, now, bars=bars, open_markets=frozenset()))
    assert sig_legacy == [] and sig_pure == []


def test_premarket_direct_entry_rejected_below_liquidity_guard_equivalence():
    legacy, pure = _strats([KR_SYMBOL], _params(premarket_min_volume_krw=50_000_000))
    bars = {KR_SYMBOL: _kr_premarket_entry_bars(surge=True, notional_ok=False)}
    now = _kr_now(dtime(8, 2, 30))

    sig_legacy = legacy.on_cycle(_ctx({KR_SYMBOL: 81300.0}, now, bars=bars, open_markets=frozenset()))
    sig_pure = pure.on_cycle(_ctx({KR_SYMBOL: 81300.0}, now, bars=bars, open_markets=frozenset()))
    assert sig_legacy == [] and sig_pure == []


def test_regular_session_accelerated_entry_equivalence():
    """프리마켓 확인 심볼은 정규장에서 P1 재형성을 기다리지 않는다."""
    legacy, pure = _strats([KR_SYMBOL], _params())
    legacy._premarket_confirmed[KR_SYMBOL] = 81000.0
    legacy._session_date["KR"] = DAY1  # market="US" 인스턴스지만 심볼은 KR 추론됨(market_of_symbol)
    pure._state = {"premarket_confirmed": {KR_SYMBOL: 81000.0}, "session_date": {"KR": DAY1}}

    reg_open_ts = _kr_now(KR_OPEN)
    bars = pd.DataFrame(
        [
            {"open": 80900.0, "high": 80950.0, "low": 80900.0, "close": 80920.0, "volume": 1000.0},
            {"open": 80920.0, "high": 81200.0, "low": 80850.0, "close": 81100.0, "volume": 1200.0},
        ],
        index=pd.DatetimeIndex([reg_open_ts, reg_open_ts + timedelta(minutes=1)], tz=KST),
    )
    now = reg_open_ts + timedelta(minutes=1, seconds=30)

    sig_legacy = legacy.on_cycle(
        _ctx({KR_SYMBOL: 81150.0}, now, bars={KR_SYMBOL: bars}, open_markets=frozenset({"KR"})))
    sig_pure = pure.on_cycle(
        _ctx({KR_SYMBOL: 81150.0}, now, bars={KR_SYMBOL: bars}, open_markets=frozenset({"KR"})))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "패턴A" in sig_legacy[0].reason


# ---------------- 추세/변동성 게이트 동치

def test_trend_gate_block_mode_rejects_equivalence():
    legacy, pure = _strats(["AAA"], _params(trend_gate_mode="block"))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    daily = {"AAA": _daily_bars_sideways()}
    now = _now_within_window(3.0)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars, daily_bars=daily))
    sig_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars, daily_bars=daily))
    assert sig_legacy == [] and sig_pure == []


def test_trend_gate_shadow_mode_does_not_block_equivalence():
    legacy, pure = _strats(["AAA"], _params())  # 기본 shadow
    bars = {"AAA": _pattern_a_bars(surge=True)}
    daily = {"AAA": _daily_bars_sideways()}  # 차단 후보(횡보)지만 shadow는 안 막음
    now = _now_within_window(3.0)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars, daily_bars=daily))
    sig_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars, daily_bars=daily))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1
    assert "게이트:차단후보" in sig_legacy[0].reason


def test_trend_gate_off_mode_equivalence():
    legacy, pure = _strats(["AAA"], _params(trend_gate_mode="off"))
    bars = {"AAA": _pattern_a_bars(surge=True)}
    now = _now_within_window(3.0)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    sig_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1
    assert "게이트" not in sig_legacy[0].reason


def test_trend_gate_allows_when_uptrend_equivalence():
    legacy, pure = _strats(["AAA"], _params())
    bars = {"AAA": _pattern_a_bars(surge=True)}
    daily = {"AAA": _daily_bars_uptrend()}
    now = _now_within_window(3.0)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars, daily_bars=daily))
    sig_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars, daily_bars=daily))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1
    assert "게이트:통과" in sig_legacy[0].reason


# ============================================================ 층위 2 — 다중 사이클 동치
# legacy.on_cycle(ctx)와 pure.on_cycle(ctx)를 "체결 시뮬레이션"으로 연결한 여러
# 사이클에 걸쳐 나란히 구동한다 — donchian_pure의 층위 2와 동일 기법.

def test_multi_cycle_entry_partial_take_ma60_exit_equivalence():
    """진입(패턴 A) -> 체결 -> +1.5R 부분익절 -> 60선 이탈 전량 청산."""
    params = _params(partial_take_r=1.5, partial_fraction=0.5, ma_period=60)
    legacy, pure = _strats(["AAA"], params)

    legacy_positions: dict[str, Position] = {}
    pure_positions: dict[str, Position] = {}

    now = _now_within_window(3.0)
    bars_entry = {"AAA": _pattern_a_bars(surge=True)}

    # cycle 1: 진입
    sig_legacy1 = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars_entry, positions=legacy_positions))
    sig_pure1 = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars_entry, positions=pure_positions))
    assert _keys(sig_legacy1) == _keys(sig_pure1)
    assert len(sig_legacy1) == 1 and sig_legacy1[0].action == SignalAction.ENTER_LONG
    stop0 = sig_legacy1[0].stop

    # "체결" 시뮬레이션 — 두 broker에 각자의 전략 id로 랏을 채운다.
    entry_price = 102.4
    legacy_positions["AAA"] = Position(symbol="AAA", qty=10.0, avg_cost=entry_price, meta={
        "lots": {LEGACY_ID: {"qty": 10.0, "entry": entry_price, "stop": stop0,
                               "pattern": "A", "session": DAY1.isoformat(), "partial_taken": False}},
    })
    pure_positions["AAA"] = Position(symbol="AAA", qty=10.0, avg_cost=entry_price, meta={
        "lots": {PURE_ID: {"qty": 10.0, "entry": entry_price, "stop": stop0,
                             "pattern": "A", "session": DAY1.isoformat(), "partial_taken": False}},
    })

    r = entry_price - stop0
    target_1_5r = entry_price + 1.5 * r

    # cycle 2: 부분 익절 트리거 (가격이 target_1_5r 이상, MA60 위 유지되도록 평탄봉 사용)
    bars_flat = {"AAA": _flat_ma_bars(n=65, close=100.0, last_close=target_1_5r + 0.1)}
    now2 = now + timedelta(minutes=1)
    sig_legacy2 = legacy.on_cycle(
        _ctx({"AAA": target_1_5r + 0.1}, now2, bars=bars_flat, positions=legacy_positions))
    sig_pure2 = pure.on_cycle(
        _ctx({"AAA": target_1_5r + 0.1}, now2, bars=bars_flat, positions=pure_positions))
    assert _keys(sig_legacy2) == _keys(sig_pure2)
    assert len(sig_legacy2) == 1 and sig_legacy2[0].action == SignalAction.SCALE_OUT

    # 체결 반영 — legacy Position.meta lot에 state_update 적용(loop._execute_signal 동치).
    legacy_positions["AAA"].meta["lots"][LEGACY_ID].update(sig_legacy2[0].state_update)
    pure_positions["AAA"].meta["lots"][PURE_ID].update(sig_pure2[0].state_update)

    # cycle 3: 60선 이탈 -> 전량 청산. 현재가는 stop(~100.2, l1=100.5 기준) 위에
    # 둬야 손절 분기보다 60선 분기가 먼저 걸린다 — 봉 종가(last_close)는 quote와
    # 무관하게 MA 아래로 마감시킨다.
    bars_below_ma = {"AAA": _flat_ma_bars(n=65, close=100.0, last_close=95.0)}
    now3 = now2 + timedelta(minutes=1)
    sig_legacy3 = legacy.on_cycle(
        _ctx({"AAA": 101.0}, now3, bars=bars_below_ma, positions=legacy_positions))
    sig_pure3 = pure.on_cycle(
        _ctx({"AAA": 101.0}, now3, bars=bars_below_ma, positions=pure_positions))
    assert _keys(sig_legacy3) == _keys(sig_pure3)
    assert len(sig_legacy3) == 1 and "60선" in sig_legacy3[0].reason


def test_multi_cycle_session_cap_a_then_b_equivalence():
    """패턴 A로 진입 -> 청산(EoD) -> 같은 세션에서 패턴 B 재진입 -> 세션당 2회 상한 확인."""
    params = _params(ma_period=60)
    legacy, pure = _strats(["AAA"], params)

    legacy_positions: dict[str, Position] = {}
    pure_positions: dict[str, Position] = {}

    now = _now_within_window(3.0)
    bars_entry = {"AAA": _pattern_a_bars(surge=True)}
    sig_legacy1 = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars_entry, positions=legacy_positions))
    sig_pure1 = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars_entry, positions=pure_positions))
    assert _keys(sig_legacy1) == _keys(sig_pure1)
    assert legacy._pattern_a_used.get("AAA") is True
    assert pure._state["pattern_a_used"].get("AAA") is True

    # EoD 청산으로 A 포지션을 정리한다(체결 시뮬레이션 없이 mtc를 좁혀 신호만
    # 확인 — 세션 상한 로직은 pattern_a_used/b_used 플래그로 이미 결정되므로
    # 포지션 진행 여부와 무관).
    stop0 = sig_legacy1[0].stop
    entry_price = 102.4
    legacy_positions["AAA"] = Position(symbol="AAA", qty=10.0, avg_cost=entry_price, meta={
        "lots": {LEGACY_ID: {"qty": 10.0, "entry": entry_price, "stop": stop0,
                               "pattern": "A", "session": DAY1.isoformat(), "partial_taken": False}},
    })
    pure_positions["AAA"] = Position(symbol="AAA", qty=10.0, avg_cost=entry_price, meta={
        "lots": {PURE_ID: {"qty": 10.0, "entry": entry_price, "stop": stop0,
                             "pattern": "A", "session": DAY1.isoformat(), "partial_taken": False}},
    })
    now2 = now + timedelta(minutes=1)
    sig_legacy2 = legacy.on_cycle(
        _ctx({"AAA": 103.0}, now2, positions=legacy_positions, minutes_to_close=0.5))
    sig_pure2 = pure.on_cycle(
        _ctx({"AAA": 103.0}, now2, positions=pure_positions, minutes_to_close=0.5))
    assert _keys(sig_legacy2) == _keys(sig_pure2)
    assert "EoD" in sig_legacy2[0].reason
    # 청산 체결 반영 — 포지션을 닫는다(수량 0).
    legacy_positions["AAA"].qty = 0.0
    legacy_positions["AAA"].meta["lots"].pop(LEGACY_ID, None)
    pure_positions["AAA"].qty = 0.0
    pure_positions["AAA"].meta["lots"].pop(PURE_ID, None)

    # 패턴 B 재진입 시도 — 터치봉/확인봉을 세 번째 사이클에 제공.
    open_ts = datetime.combine(DAY1, US_OPEN, tzinfo=NY)
    warmup_idx, warmup_rows = _warmup(open_ts, 59)
    touch_ts = open_ts
    confirm_ts = open_ts + timedelta(minutes=1)
    touch_confirm = pd.DataFrame(
        [
            {"open": 100.0, "high": 100.1, "low": 99.85, "close": 99.85, "volume": 1000.0},
            {"open": 99.9, "high": 100.4, "low": 99.85, "close": 100.3, "volume": 1000.0},
        ],
        index=pd.DatetimeIndex([touch_ts, confirm_ts], tz=NY),
    )
    bars_b = pd.concat([
        pd.DataFrame(warmup_rows, index=pd.DatetimeIndex(warmup_idx, tz=NY)), touch_confirm,
    ])
    now3 = confirm_ts + timedelta(seconds=30)
    sig_legacy3 = legacy.on_cycle(
        _ctx({"AAA": 100.35}, now3, bars={"AAA": bars_b}, positions=legacy_positions))
    sig_pure3 = pure.on_cycle(
        _ctx({"AAA": 100.35}, now3, bars={"AAA": bars_b}, positions=pure_positions))
    assert _keys(sig_legacy3) == _keys(sig_pure3)
    assert len(sig_legacy3) == 1 and "패턴B" in sig_legacy3[0].reason
    assert legacy._pattern_b_used.get("AAA") is True
    assert pure._state["pattern_b_used"].get("AAA") is True

    # 이제 A/B 둘 다 소진 — 4번째 사이클에서 어떤 진입 시도도 신호가 없어야 한다.
    legacy_positions["AAA"].meta["lots"].pop(LEGACY_ID, None)
    legacy_positions["AAA"].qty = 0.0
    pure_positions["AAA"].meta["lots"].pop(PURE_ID, None)
    pure_positions["AAA"].qty = 0.0
    now4 = now3 + timedelta(minutes=1)
    sig_legacy4 = legacy.on_cycle(
        _ctx({"AAA": 102.4}, now4, bars=bars_entry, positions=legacy_positions))
    sig_pure4 = pure.on_cycle(
        _ctx({"AAA": 102.4}, now4, bars=bars_entry, positions=pure_positions))
    assert sig_legacy4 == [] and sig_pure4 == []


# ============================================================ 층위 3 — 다중 세션 규모 동치
# run_backtest 대체(파일 docstring 사유). 3거래일에 걸쳐 legacy/pure를 동일한
# 합성 사이클 시퀀스로 나란히 구동해 총 진입 신호 수가 일치하는지 확인한다.

def test_multi_session_entry_count_equivalence():
    params = _params()
    legacy, pure = _strats(["AAA"], params)
    legacy_positions: dict[str, Position] = {}
    pure_positions: dict[str, Position] = {}

    legacy_entries = 0
    pure_entries = 0
    for day in (DAY1, DAY2, date(2026, 1, 7)):
        bars = {"AAA": _pattern_a_bars(surge=True, warmup_n=25)}
        # _pattern_a_bars는 DAY1 고정 인덱스라 날짜별로 재라벨링한다.
        shift = day - DAY1
        bars["AAA"].index = bars["AAA"].index + shift
        now = _now_within_window(3.0, day=day)

        sig_legacy = legacy.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars, positions=legacy_positions))
        sig_pure = pure.on_cycle(_ctx({"AAA": 102.4}, now, bars=bars, positions=pure_positions))
        assert _keys(sig_legacy) == _keys(sig_pure), f"day={day} 신호 불일치"
        legacy_entries += sum(1 for s in sig_legacy if s.action == SignalAction.ENTER_LONG)
        pure_entries += sum(1 for s in sig_pure if s.action == SignalAction.ENTER_LONG)

        # 그날 안에 EoD로 청산해 다음날 세션 롤이 오버나잇 없이 깨끗하게 시작되게 한다.
        if sig_legacy:
            stop0 = sig_legacy[0].stop
            legacy_positions["AAA"] = Position(symbol="AAA", qty=10.0, avg_cost=102.4, meta={
                "lots": {LEGACY_ID: {"qty": 10.0, "entry": 102.4, "stop": stop0,
                                       "pattern": "A", "session": day.isoformat(), "partial_taken": False}},
            })
            pure_positions["AAA"] = Position(symbol="AAA", qty=10.0, avg_cost=102.4, meta={
                "lots": {PURE_ID: {"qty": 10.0, "entry": 102.4, "stop": stop0,
                                     "pattern": "A", "session": day.isoformat(), "partial_taken": False}},
            })
            now_eod = now + timedelta(minutes=1)
            legacy.on_cycle(_ctx({"AAA": 103.0}, now_eod, positions=legacy_positions, minutes_to_close=0.5))
            pure.on_cycle(_ctx({"AAA": 103.0}, now_eod, positions=pure_positions, minutes_to_close=0.5))
            legacy_positions["AAA"].qty = 0.0
            legacy_positions["AAA"].meta["lots"].pop(LEGACY_ID, None)
            pure_positions["AAA"].qty = 0.0
            pure_positions["AAA"].meta["lots"].pop(PURE_ID, None)

    assert legacy_entries == pure_entries
    assert legacy_entries == 3  # 세션마다 A로 1회씩, EoD 청산 후 다음날 재무장


def test_take_profit_bps_exit_equivalence():
    """전량 익절(2026-08-21 추가)도 두 구현이 같은 판단을 해야 한다 —
    순수 구현이 파라미터 위임 복사에서 빠지면 조용히 갈린다."""
    legacy, pure = _strats(["AAA"], _params(take_profit_bps=100))
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, stop=97.0)
    pos_pure = _lot_position(PURE_ID, entry=100.0, stop=97.0)
    _seed_open(pure, "AAA", entry=100.0, stop=97.0)
    now = _now_within_window(5.0)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos_pure}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "익절" in sig_legacy[0].reason
