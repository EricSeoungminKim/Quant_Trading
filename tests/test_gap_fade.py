"""`GapFadePureStrategy` — 갭하락 되돌림(gap fade) 5분봉 전략의 규칙 고정.

레거시 쌍둥이가 없는 신규 전략이라 `pullback_impulse`/`mr_vwap_quiet`와 같은
스타일로 규칙 자체를 고정한다: 합성 5분봉으로 갭/안정화/손절/목표/시간청산/EoD
레일이 독립적으로 작동하는지, 순수 계약(`decide()`가 `state`를 mutate하지
않는다)이 지켜지는지, 그리고 장중 재시작(2026-08-28 실사고)을 견디는지.

**열린 랏의 방어선은 `next_state`가 아니라 `snap.lots`로 다닌다** — 관리 테스트는
방어선을 `lots=`로 주입한다(`test_pullback_impulse.py`와 동일 설계).
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.indicators.trend_gate import atr_ratio
from quant.trade.strategy.gap_fade import GapFadePureStrategy, GapFadeShell

NY = ZoneInfo("America/New_York")
DAY = date(2026, 8, 27)          # 목
PREV = date(2026, 8, 26)         # 수
US_SYM = "SOXL"
KR_SYM = "005930"

PREV_CLOSE = 100.00
GAP_OPEN = 98.00  # (100-98)/100 * 1e4 = 200bp 하락 갭 — 기본 [100,400]bp 통과.

# ATR 워밍업 — 전 거래일 오후의 잔잔한 20봉(TR 0.2 고정, pullback_impulse와 동일 관례).
WARMUP = [(100.0, 100.1, 99.9, 100.0, 500.0)] * 20

# 정상 시나리오: 09:30~09:40 세션 3봉.
#   09:30 시가 98.00(갭 200bp) → 첫 봉은 하락(적삼) → 두 번째 봉이 양봉 마감(안정화,
#   09:35 시작, 저가 97.85) → 세 번째 봉(09:40)이 진입 평가 대상.
_STANDARD = [
    (98.00, 98.05, 97.80, 97.90, 1000.0),   # 09:30 적삼 — 당일 저가 97.80
    (97.90, 98.30, 97.85, 98.20, 1200.0),   # 09:35 양봉 — 안정화봉(저가 97.85)
    (98.20, 98.35, 98.10, 98.25, 900.0),    # 09:40 — 진입 평가 대상
]
DAY_LOW = 97.80
STAB_LOW = 97.85
QUOTE = 98.25
TARGET = GAP_OPEN + (PREV_CLOSE - GAP_OPEN) * 0.5  # 99.00


# ============================================================ 합성 봉 조립


def _frame(day: date, start: dtime, rows, tz) -> pd.DataFrame:
    idx = pd.DatetimeIndex([
        datetime.combine(day, start, tzinfo=tz) + timedelta(minutes=5 * i)
        for i in range(len(rows))
    ])
    return pd.DataFrame(
        {
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [r[4] for r in rows],
        },
        index=idx,
    )


def _bars(rows=None, *, tz=NY, start=dtime(9, 30)) -> pd.DataFrame:
    """워밍업(전 거래일) + 오늘 세션 봉. `history()`가 주는 연속 시계열 모사."""
    rows = _STANDARD if rows is None else rows
    return pd.concat([
        _frame(PREV, dtime(13, 0), WARMUP, tz),
        _frame(DAY, start, rows, tz),
    ])


def _daily_bars(prev_close: float = PREV_CLOSE, n: int = 5, *, tz=NY) -> pd.DataFrame:
    """전일 종가 확보용 일봉 — 마지막 행의 종가만 의미가 있다(장중엔
    `_filter_completed_bars`가 오늘 일봉을 잘라내므로 마지막 행이 곧 전일 종가,
    모듈 docstring "전일 종가 획득 경로" 절)."""
    idx = pd.DatetimeIndex([
        datetime.combine(PREV - timedelta(days=n - 1 - i), dtime(16, 0), tzinfo=tz)
        for i in range(n)
    ])
    closes = [prev_close - 1.0] * (n - 1) + [prev_close]
    return pd.DataFrame(
        {
            "open": closes, "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes], "close": closes,
            "volume": [10000.0] * n,
        },
        index=idx,
    )


def _snap(now, bars5, quotes, *, daily=None, lots=None, mtc=120.0, cadence=0.1,
          markets=("US",)) -> StrategySnapshot:
    bars = {(s, "5m"): df for s, df in bars5.items()}
    if daily is not None:
        bars.update({(s, "1d"): df for s, df in daily.items()})
    return StrategySnapshot(
        now=now,
        market_open={m: True for m in markets},
        minutes_to_close={m: mtc for m in markets},
        cadence_minutes=cadence,
        bars=bars,
        quotes={s: Quote(symbol=s, ts=now, price=p) for s, p in quotes.items()},
        lots=lots or {},
    )


def _strategy(symbols=(US_SYM,), **params) -> GapFadePureStrategy:
    return GapFadePureStrategy(list(symbols), dict(params))


def _us_now(h=9, m=45) -> datetime:
    return datetime.combine(DAY, dtime(h, m), tzinfo=NY)


_DEFAULT_DAILY = object()


def _decide(strategy, rows=None, *, price=QUOTE, state=None, now=None, lots=None,
            daily=_DEFAULT_DAILY, symbol=US_SYM):
    daily_bars = _daily_bars() if daily is _DEFAULT_DAILY else daily
    snap = _snap(
        now or _us_now(),
        {symbol: _bars(rows)},
        {symbol: price},
        daily=({symbol: daily_bars} if daily_bars is not None else {}),
        lots=lots,
    )
    return strategy.decide(snap, state or {})


def _atr_abs(rows=None) -> float:
    """전략이 쓰는 것과 같은 순수 지표로 계산한 ATR 절대값(14기간)."""
    bars = _bars(rows)
    ratio = atr_ratio(bars, 14)
    assert ratio is not None
    return ratio * float(bars["close"].iloc[-1])


# ============================================================ 계약


def test_requirements_declares_5m_and_1d_bars():
    s = _strategy(symbols=(US_SYM, KR_SYM))
    needs = s.requirements()
    intervals = {(sym, interval) for sym, interval, _ in needs.bars}
    assert intervals == {
        (US_SYM, "5m"), (KR_SYM, "5m"), (US_SYM, "1d"), (KR_SYM, "1d"),
    }
    five_m_counts = {count for sym, interval, count in needs.bars if interval == "5m"}
    assert all(c >= 78 + 14 for c in five_m_counts)
    assert set(needs.quotes) == {US_SYM, KR_SYM}
    assert needs.needs_positions


@pytest.mark.parametrize("params", [
    {"gap_min_bp": -1},
    {"gap_min_bp": 0},
    {"gap_max_bp": 100, "gap_min_bp": 100},   # 상한이 하한보다 커야 한다
    {"gap_max_bp": 50, "gap_min_bp": 100},
    {"entry_window_min": 0},
    {"entry_window_min": -1},
    {"fill_ratio": 0},
    {"fill_ratio": 1.5},
    {"atr_buffer_mult": -0.1},
    {"atr_period": 1},
    {"min_stop_bp": -1},
    {"max_hold_min": 0},
    {"flatten_before_close_minutes": 0},
    {"flatten_before_close_minutes": -1},
    {"target_weight": 0},
    {"target_weight": 1.5},
])
def test_invalid_params_rejected(params):
    with pytest.raises(ValueError):
        GapFadePureStrategy([US_SYM], params)


def test_default_params_are_documented_values():
    s = _strategy()
    assert s.gap_min_bp == 100.0
    assert s.gap_max_bp == 400.0
    assert s.entry_window_min == 30.0
    assert s.fill_ratio == 0.5
    assert s.atr_buffer_mult == 0.3
    assert s.min_stop_bp == 40.0
    assert s.max_hold_min == 120.0
    assert s.flatten_minutes == 5.0


# ============================================================ ① 정상 진입


def test_entry_after_gap_and_stabilization():
    d = _decide(_strategy())
    assert len(d.signals) == 1
    sig = d.signals[0]
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.symbol == US_SYM
    assert sig.target_weight == pytest.approx(0.5)

    expected_stop = min(DAY_LOW, STAB_LOW) - 0.3 * _atr_abs()
    assert sig.stop == pytest.approx(expected_stop)
    assert sig.stop < QUOTE
    assert sig.target == pytest.approx(TARGET)

    # 방어선의 정본은 state_update — 루프가 체결 확인 후 lot에 영속한다.
    assert sig.state_update["entry"] == pytest.approx(QUOTE)
    assert sig.state_update["stop"] == pytest.approx(expected_stop)
    assert sig.state_update["target"] == pytest.approx(TARGET)
    assert sig.state_update["entered_at"] == _us_now().isoformat()
    assert sig.state_update["session"] == DAY.isoformat()
    assert sig.state_update["strategy"] == "gap_fade"

    assert d.next_state["taken"][US_SYM] == DAY.isoformat()


def test_one_entry_per_symbol_per_day():
    first = _decide(_strategy())
    assert len(first.signals) == 1

    exhausted = {
        "session_date": {"US": DAY.isoformat()},
        "taken": {US_SYM: DAY.isoformat()},
        "last_reject": {},
    }
    again = _decide(_strategy(), state=exhausted)
    assert again.signals == ()


# ============================================================ ② 갭 미달/상회


@pytest.mark.parametrize("open_price", [99.50, 95.00])  # 50bp(미달) / 500bp(초과)
def test_no_entry_when_gap_outside_range(open_price):
    rows = [(open_price, open_price + 0.05, open_price - 0.05, open_price + 0.02, 500.0)]
    d = _decide(_strategy(), rows, price=open_price + 0.02, now=_us_now(9, 35))
    assert d.signals == ()
    assert "갭 조건 불충족" in d.next_state["last_reject"][US_SYM]


# ============================================================ ③ 안정화 없음


def test_no_entry_while_waiting_for_stabilization_within_window():
    """개장 15분, 아직 양봉이 안 나왔다 — entry_window(30분) 안이므로 '대기중'이지
    '포기'가 아니다."""
    rows = [
        (98.00, 98.05, 97.90, 97.95, 1000.0),   # 09:30 적삼
        (97.95, 98.00, 97.85, 97.90, 1000.0),   # 09:35 적삼
    ]
    d = _decide(_strategy(), rows, price=97.90, now=_us_now(9, 45))
    assert d.signals == ()
    assert "안정화 대기중" in d.next_state["last_reject"][US_SYM]


def test_gives_up_after_entry_window_elapses_without_stabilization():
    """entry_window(30분)가 지나도록 양봉이 한 번도 없었다 — 그날은 포기(별도
    상태 없이 시간창 자체가 다시 평가해도 계속 거부한다)."""
    rows = [
        (98.00, 98.05, 97.90, 97.95, 1000.0),   # 09:30
        (97.95, 98.00, 97.85, 97.90, 1000.0),   # 09:35
        (97.90, 97.95, 97.80, 97.85, 1000.0),   # 09:40
        (97.85, 97.90, 97.75, 97.80, 1000.0),   # 09:45
        (97.80, 97.85, 97.70, 97.75, 1000.0),   # 09:50
        (97.75, 97.80, 97.65, 97.70, 1000.0),   # 09:55
    ]
    d = _decide(_strategy(), rows, price=97.70, now=_us_now(10, 5))
    assert d.signals == ()
    assert "entry_window" in d.next_state["last_reject"][US_SYM]
    assert "초과" in d.next_state["last_reject"][US_SYM]


# ============================================================ ④ 목표 도달 청산


def _lot(entered_at=None, entry=QUOTE, stop=97.0, target=TARGET, session=None):
    return {
        "entry": entry, "stop": stop, "target": target,
        "session": session or DAY.isoformat(),
        "entered_at": (entered_at or _us_now(10, 0)).isoformat(),
        "strategy": "gap_fade",
    }


def _held_state():
    return {
        "session_date": {"US": DAY.isoformat()},
        "taken": {US_SYM: DAY.isoformat()},
        "last_reject": {},
    }


def test_target_reached_exit():
    d = _decide(_strategy(), price=TARGET + 0.10, state=_held_state(), lots={US_SYM: _lot()})
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert d.signals[0].exit_fraction == 1.0
    assert "목표" in d.signals[0].reason


def test_stop_loss_exit():
    d = _decide(_strategy(), price=96.90, state=_held_state(), lots={US_SYM: _lot(stop=97.0)})
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert "손절" in d.signals[0].reason


# ============================================================ ⑤ min_stop 게이트


def test_min_stop_gate_rejects_degenerate_stop():
    """당일 저가/안정화봉 저가가 진입가 바로 아래라 손절폭이 최소치(40bp) 미만이면
    진입하지 않는다 — pullback_impulse의 2026-08-29 결함 수리와 같은 게이트."""
    rows = [
        (98.00, 98.02, 97.95, 97.97, 1000.0),   # 09:30 적삼(당일 저가 97.95)
        (97.97, 98.05, 97.96, 98.03, 1000.0),   # 09:35 양봉(안정화봉 저가 97.96)
        (98.03, 98.06, 98.01, 98.05, 900.0),    # 09:40 진입 평가 대상
    ]
    # atr_buffer_mult=0으로 손절 버퍼를 없애 base_low 자체와 진입가 차이만 남긴다.
    strat = _strategy(atr_buffer_mult=0.0, min_stop_bp=40.0)
    d = _decide(strat, rows, price=98.05)
    assert d.signals == ()
    reason = d.next_state["last_reject"][US_SYM]
    assert "손절폭" in reason and "40" in reason


# ============================================================ ⑥ 시간 청산 + 마감 청산


def test_time_exit_after_max_hold_minutes():
    lots = {US_SYM: _lot(entered_at=_us_now(10, 0))}
    held_ok = _decide(_strategy(), price=98.50, state=_held_state(),
                       now=_us_now(10, 0) + timedelta(minutes=119), lots=lots)
    assert held_ok.signals == ()

    timed_out = _decide(_strategy(), price=98.50, state=_held_state(),
                         now=_us_now(10, 0) + timedelta(minutes=120), lots=lots)
    assert len(timed_out.signals) == 1
    assert "시간 청산" in timed_out.signals[0].reason


def test_eod_flatten_exit_overrides_time_exit():
    """마감 임박(잔여 4분, flatten=5분)이면 시간 청산 조건과 무관하게 EoD로
    청산한다 — 판단 주기(cadence)와 상호작용해 청산 창을 놓치지 않는지 확인."""
    lots = {US_SYM: _lot(entered_at=_us_now(10, 0))}
    snap = _snap(_us_now(15, 56), {US_SYM: _bars()}, {US_SYM: 98.50}, daily={},
                 lots=lots, mtc=4.0, cadence=0.1)
    d = _strategy().decide(snap, _held_state())
    assert len(d.signals) == 1
    assert "EoD 청산" in d.signals[0].reason


def test_session_roll_forces_exit_no_overnight():
    d = _decide(_strategy(), price=98.50, state=_held_state(),
                lots={US_SYM: _lot(session=PREV.isoformat())})
    assert len(d.signals) == 1
    assert "오버나잇 금지" in d.signals[0].reason


# ============================================================ ⑦ 전일 종가 확인 불가


def test_no_entry_when_prior_close_unavailable():
    d = _decide(_strategy(), daily=None)
    assert d.signals == ()
    assert d.next_state["last_reject"][US_SYM] == "전일 종가 확인 불가"


def test_no_entry_when_prior_close_is_empty_frame():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    d = _decide(_strategy(), daily=empty)
    assert d.signals == ()
    assert d.next_state["last_reject"][US_SYM] == "전일 종가 확인 불가"


# ============================================================ ⑧ lots 재시작 생존


@pytest.mark.parametrize("price,now_offset,fragment", [
    (96.90, timedelta(minutes=30), "손절"),
    (TARGET + 0.10, timedelta(minutes=30), "목표"),
    (98.50, timedelta(minutes=120), "시간 청산"),
])
def test_open_lot_survives_process_restart(price, now_offset, fragment):
    """**장중 재시작(2026-08-28 실제 사건)** — 껍질 상태도 전략 인스턴스도 통째로
    버린 뒤, 브로커 포지션의 lot만으로 손절·목표·시간청산이 그대로 나온다.
    `test_pullback_impulse.py`의 동명 테스트와 같은 취지다."""
    entered = _decide(_strategy())
    lot = dict(entered.signals[0].state_update)
    entered_at = datetime.fromisoformat(lot["entered_at"])

    restarted = GapFadePureStrategy([US_SYM], {})
    after = restarted.decide(
        _snap(entered_at + now_offset, {US_SYM: _bars()}, {US_SYM: price}, daily={},
              lots={US_SYM: lot}),
        {},
    )
    assert len(after.signals) == 1, "재시작 후 관리에서 빠졌다 — 손절이 사라진다"
    assert after.signals[0].action is SignalAction.EXIT_LONG
    assert fragment in after.signals[0].reason


def test_no_duplicate_entry_after_restart_while_holding():
    lot = dict(_decide(_strategy()).signals[0].state_update)
    restarted = GapFadePureStrategy([US_SYM], {})
    after = restarted.decide(
        _snap(_us_now(10, 30), {US_SYM: _bars()}, {US_SYM: 98.50}, daily={},
              lots={US_SYM: lot}),
        {},
    )
    assert after.signals == ()


# ============================================================ ⑨ 순수성 / 배선


def test_decide_does_not_mutate_input_state():
    strategy = _strategy()
    state = _held_state()
    snapshot_before = copy.deepcopy(state)

    strategy.decide(
        _snap(_us_now(10, 30), {US_SYM: _bars()}, {US_SYM: 96.90}, daily={},
              lots={US_SYM: _lot()}),
        state,
    )
    assert state == snapshot_before


def test_decide_does_not_mutate_snapshot_lots():
    lot = _lot()
    before = copy.deepcopy(lot)
    _decide(_strategy(), price=96.90, state=_held_state(), lots={US_SYM: lot})
    assert lot == before


def test_shell_satisfies_strategy_protocol_wiring():
    shell = GapFadeShell([US_SYM], {}, market="US", id="gap_fade")
    assert shell.id == "gap_fade"
    assert shell.symbols == [US_SYM]
    assert hasattr(shell, "on_cycle")
