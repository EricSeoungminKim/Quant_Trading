"""`IntradayMomentumPureStrategy` — Zarattini·Aziz·Barbon(2024) 노이즈 밴드 이탈
추종을 롱 온리 계좌용으로 각색한 신규 전략의 규칙 고정.

레거시 쌍둥이가 없으므로 여기서 고정하는 것은 규칙 그 자체다: 노이즈 밴드
이탈이 방향별로 옳은 ETF를 사는지, "확인 불가면 거부" 게이트가 실제로
발동하는지, VWAP 역크로스/손절/EoD/오버나잇 청산 레일이 신호를 내는지, 하루
같은 방향 재진입 상한이 걸리는지, 그리고 열린 랏의 방어선(방향 포함)이
`snap.lots`만으로 장중 재시작을 견디는지.

**열린 랏의 방어선은 `next_state`가 아니라 `snap.lots`로 다닌다** — 관리 테스트는
방어선을 `state`가 아니라 스냅샷의 `lots=`로 주입한다(`mr_vwap_quiet`/
`pullback_impulse`와 같은 패턴).
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.strategy.intraday_momentum import (
    IntradayMomentumPureStrategy,
    IntradayMomentumShell,
    _session_slice,
    noise_band,
)
from quant.trade.strategy.mr_vwap_quiet import session_vwap_bands

NY = ZoneInfo("America/New_York")
SIGNAL, LONG, SHORT = "QQQ", "TQQQ", "SQQQ"
TODAY = date(2026, 8, 27)
PREV_DAYS = [
    date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 24),
    date(2026, 8, 25), date(2026, 8, 26),
]  # 5거래일 — 기본 min_lookback_days와 정확히 일치

# 과거 각 날: 09:30/09:35/09:40 세 봉. 시가 100.00, 09:40 종가 100.50
# → |100.50/100.00 - 1| = 0.005. 5일 전부 동일값이므로 σ=0.005 정확히.
HIST_ROWS = [
    (100.00, 100.12, 99.95, 100.05, 1000.0),
    (100.05, 100.30, 100.00, 100.20, 1200.0),
    (100.20, 100.55, 100.15, 100.50, 1500.0),
]
DAY_OPEN = 100.00
SIGMA = 0.005
UPPER = DAY_OPEN * (1 + SIGMA)   # 100.50
LOWER = DAY_OPEN * (1 - SIGMA)   # 99.50


def _today_rows(last_close: float) -> list[tuple]:
    hi = max(100.20, last_close) + 0.10
    lo = min(100.20, last_close) - 0.10
    return [
        (100.00, 100.12, 99.95, 100.05, 1000.0),
        (100.05, 100.30, 100.00, 100.20, 1200.0),
        (100.20, hi, lo, last_close, 1500.0),
    ]


def _frame(day: date, start: dtime, rows, tz=NY) -> pd.DataFrame:
    idx = pd.DatetimeIndex([
        datetime.combine(day, start, tzinfo=tz) + timedelta(minutes=5 * i)
        for i in range(len(rows))
    ])
    return pd.DataFrame(
        {
            "open": [r[0] for r in rows], "high": [r[1] for r in rows],
            "low": [r[2] for r in rows], "close": [r[3] for r in rows],
            "volume": [r[4] for r in rows],
        },
        index=idx,
    )


def _bars(last_close: float = 100.00, hist_days=None) -> pd.DataFrame:
    hist_days = PREV_DAYS if hist_days is None else hist_days
    frames = [_frame(d, dtime(9, 30), HIST_ROWS) for d in hist_days]
    frames.append(_frame(TODAY, dtime(9, 30), _today_rows(last_close)))
    return pd.concat(frames)


def _snap(now, bars_df, quotes: dict, *, lots=None, mtc=120.0, cadence=0.1) -> StrategySnapshot:
    return StrategySnapshot(
        now=now,
        market_open={"US": True},
        minutes_to_close={"US": mtc},
        cadence_minutes=cadence,
        bars={(SIGNAL, "5m"): bars_df},
        quotes={s: Quote(symbol=s, ts=now, price=p) for s, p in quotes.items()},
        lots=lots or {},
    )


def _strategy(**params) -> IntradayMomentumPureStrategy:
    return IntradayMomentumPureStrategy([], dict(params))


def _now(h=9, m=45) -> datetime:
    return datetime.combine(TODAY, dtime(h, m), tzinfo=NY)


def _decide(strategy, last_close=100.00, *, quotes=None, now=None, state=None,
            lots=None, hist_days=None, mtc=120.0, cadence=0.1):
    quotes = quotes or {LONG: 50.0, SHORT: 30.0}
    snap = _snap(now or _now(), _bars(last_close, hist_days), quotes,
                 lots=lots, mtc=mtc, cadence=cadence)
    return strategy.decide(snap, state or {})


def _vwap_for(last_close: float, hist_days=None) -> float:
    """전략이 실제로 쓰는 것과 같은 순수 함수로 계산한 신호 심볼 VWAP."""
    bars = _bars(last_close, hist_days)
    sess = _session_slice(bars, "US", TODAY)
    vwap_series, _lower, _upper = session_vwap_bands(sess, band_k=1.0)
    return float(vwap_series.iloc[-1])


# ============================================================ 계약


def test_requirements_declares_signal_symbol_5m_bars_and_three_quotes():
    s = _strategy()
    needs = s.requirements()
    assert needs.bars == ((SIGNAL, "5m", s._lookback_bars),)
    assert s._lookback_bars >= (14 + 1) * 78 + 10
    assert set(needs.quotes) == {SIGNAL, LONG, SHORT}
    assert needs.needs_positions


def test_symbols_built_from_signal_long_short_ignoring_symbols_arg():
    s = IntradayMomentumPureStrategy(["ZZZZ"], {})
    assert s.symbols == [SIGNAL, LONG, SHORT]


@pytest.mark.parametrize("params", [
    {"lookback_days": 0},
    {"min_lookback_days": 0},
    {"min_lookback_days": 20, "lookback_days": 14},
    {"band_mult": 0},
    {"stop_pct": 0},
    {"min_stop_bp": -1},
    {"max_same_direction_entries_per_day": 0},
    {"flatten_before_close_minutes": 0},
    {"target_weight": 0},
    {"target_weight": 1.5},
    {"long_symbol": "TQQQ", "short_symbol": "TQQQ"},
    {"signal_symbol": "005930"},  # KR — long/short 기본값(US)과 시장 불일치
])
def test_invalid_params_rejected(params):
    with pytest.raises(ValueError):
        IntradayMomentumPureStrategy([], params)


# ============================================================ ① 상방 이탈


def test_upward_band_breakout_buys_long_symbol():
    d = _decide(_strategy(), last_close=100.60)
    assert len(d.signals) == 1
    sig = d.signals[0]
    assert sig.symbol == LONG
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.state_update["direction"] == "long"
    assert "long" in sig.reason
    assert d.next_state["entries_today"]["long"] == 1


# ============================================================ ② 하방 이탈


def test_downward_band_breakout_buys_short_symbol():
    d = _decide(_strategy(), last_close=99.40)
    assert len(d.signals) == 1
    sig = d.signals[0]
    assert sig.symbol == SHORT
    assert sig.action is SignalAction.ENTER_LONG  # 인버스 ETF를 "매수"
    assert sig.state_update["direction"] == "short"
    assert d.next_state["entries_today"]["short"] == 1


# ============================================================ ③ 밴드 안


def test_no_entry_when_close_inside_band():
    d = _decide(_strategy(), last_close=100.00)
    assert d.signals == ()


def test_band_boundaries_match_hand_computed_sigma():
    band = noise_band(_bars(100.00), "US", TODAY, band_mult=1.0,
                       lookback_days=14, min_lookback_days=5)
    assert band is not None
    day_open, upper, lower, days_used = band
    assert day_open == pytest.approx(DAY_OPEN)
    assert upper == pytest.approx(UPPER)
    assert lower == pytest.approx(LOWER)
    assert days_used == 5


# ============================================================ ④ lookback 부족


def test_insufficient_lookback_days_rejects_entry():
    """과거 거래일이 2개뿐(기본 min_lookback_days=5 미만)이면 밴드 자체를
    계산하지 않는다 — "확인 불가는 통과가 아니라 거부다"."""
    band = noise_band(_bars(100.60, hist_days=PREV_DAYS[:2]), "US", TODAY,
                       band_mult=1.0, lookback_days=14, min_lookback_days=5)
    assert band is None

    d = _decide(_strategy(), last_close=100.60, hist_days=PREV_DAYS[:2])
    assert d.signals == ()


def test_reduced_min_lookback_days_allows_entry_with_fewer_history_days():
    """실측(Toss 5분봉 ~4거래일) 대응 시나리오 — min_lookback_days를 낮추면
    같은 얕은 히스토리에서도 정상 진입한다(게이트가 파라미터로 동작함을 확인)."""
    strategy = _strategy(min_lookback_days=2)
    d = _decide(strategy, last_close=100.60, hist_days=PREV_DAYS[:2])
    assert len(d.signals) == 1


# ============================================================ ⑤ VWAP 역크로스 청산


def test_vwap_reverse_cross_exits_long_position():
    vwap = _vwap_for(100.00)
    lot = {
        "entry": 50.0, "stop": 45.0, "direction": "long",
        "session": TODAY.isoformat(), "entered_at": _now(9, 40).isoformat(),
    }
    d = _decide(
        _strategy(), last_close=100.00,
        quotes={SIGNAL: vwap - 0.50, LONG: 51.0, SHORT: 30.0},
        lots={LONG: lot},
    )
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert d.signals[0].symbol == LONG
    assert "VWAP 역크로스" in d.signals[0].reason


def test_vwap_reverse_cross_exits_short_position():
    """숏 레인이라도 보유 포지션은 (인버스 ETF의) **매수**이므로 손절은 항상
    entry **아래**에 있다 — 방향과 무관하게 stop < entry."""
    vwap = _vwap_for(100.00)
    lot = {
        "entry": 30.0, "stop": 29.55, "direction": "short",
        "session": TODAY.isoformat(), "entered_at": _now(9, 40).isoformat(),
    }
    d = _decide(
        _strategy(), last_close=100.00,
        quotes={SIGNAL: vwap + 0.50, LONG: 50.0, SHORT: 29.60},
        lots={SHORT: lot},
    )
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert d.signals[0].symbol == SHORT
    assert "VWAP 역크로스" in d.signals[0].reason


def test_no_exit_when_signal_price_stays_on_correct_side_of_vwap():
    vwap = _vwap_for(100.00)
    lot = {
        "entry": 50.0, "stop": 45.0, "direction": "long",
        "session": TODAY.isoformat(), "entered_at": _now(9, 40).isoformat(),
    }
    d = _decide(
        _strategy(), last_close=100.00,
        quotes={SIGNAL: vwap + 0.50, LONG: 51.0, SHORT: 30.0},
        lots={LONG: lot},
    )
    assert d.signals == ()


# ============================================================ ⑥ 마감 5분 전 청산


def test_eod_flatten_exit_regardless_of_price():
    """마감 임박(잔여 1분 미만 < flatten_before_close_minutes 기본 5)이면
    손절/VWAP과 무관하게 전량 청산한다. cadence_minutes를 빼는 상호작용
    (모듈 docstring)도 여기서 함께 검증된다: mtc(1.0) - cadence(0.1) = 0.9 < 5."""
    lot = {
        "entry": 50.0, "stop": 45.0, "direction": "long",
        "session": TODAY.isoformat(), "entered_at": _now(9, 40).isoformat(),
    }
    d = _decide(
        _strategy(), last_close=100.00,
        quotes={SIGNAL: 100.00, LONG: 51.0, SHORT: 30.0},
        lots={LONG: lot}, mtc=1.0, cadence=0.1,
    )
    assert len(d.signals) == 1
    assert "EoD 청산" in d.signals[0].reason


def test_no_eod_flatten_when_close_is_not_imminent():
    lot = {
        "entry": 50.0, "stop": 45.0, "direction": "long",
        "session": TODAY.isoformat(), "entered_at": _now(9, 40).isoformat(),
    }
    vwap = _vwap_for(100.00)
    d = _decide(
        _strategy(), last_close=100.00,
        quotes={SIGNAL: vwap + 0.10, LONG: 51.0, SHORT: 30.0},
        lots={LONG: lot}, mtc=120.0, cadence=0.1,
    )
    assert d.signals == ()


def test_session_roll_forces_overnight_exit():
    lot = {
        "entry": 50.0, "stop": 45.0, "direction": "long",
        "session": PREV_DAYS[-1].isoformat(), "entered_at": _now(9, 40).isoformat(),
    }
    vwap = _vwap_for(100.00)
    d = _decide(
        _strategy(), last_close=100.00,
        quotes={SIGNAL: vwap + 0.10, LONG: 51.0, SHORT: 30.0},
        lots={LONG: lot},
    )
    assert len(d.signals) == 1
    assert "오버나잇 금지" in d.signals[0].reason


# ============================================================ ⑦ min_stop 게이트


def test_min_stop_gate_rejects_shallow_stop():
    """stop_pct를 0.1%(=10bp)로 낮추면 기본 min_stop_bp(40bp) 게이트에
    걸려 진입하지 않는다 — `pullback_impulse.py`의 같은 계열 방어."""
    strategy = _strategy(stop_pct=0.1)
    d = _decide(strategy, last_close=100.60)
    assert d.signals == ()


def test_default_stop_pct_clears_min_stop_gate():
    """기본 stop_pct(1.5%=150bp)는 기본 min_stop_bp(40bp)를 넉넉히 넘는다 —
    위 거부가 다른 이유가 아니라 게이트 때문임을 대조로 확인."""
    d = _decide(_strategy(), last_close=100.60)
    assert len(d.signals) == 1
    stop = d.signals[0].state_update["stop"]
    entry = d.signals[0].state_update["entry"]
    assert (entry - stop) / entry * 1e4 == pytest.approx(150.0, rel=1e-6)


# ============================================================ ⑧ 랏 재시작 생존


def test_open_lot_survives_process_restart_with_direction():
    """장중 재시작(2026-08-28 실사건 계열) — 껍질 상태·전략 인스턴스를 통째로
    버리고 **브로커 lot만** 넘겨도 방향(direction)까지 포함해 손절이 그대로
    나온다."""
    entered = _decide(_strategy(), last_close=100.60)
    lot = dict(entered.signals[0].state_update)
    assert lot["direction"] == "long"

    restarted = IntradayMomentumPureStrategy([], {})
    after = restarted.decide(
        _snap(_now(10, 0), _bars(100.00), {LONG: lot["stop"] - 0.01, SHORT: 30.0,
                                             SIGNAL: 100.00},
              lots={LONG: lot}),
        {},
    )
    assert len(after.signals) == 1
    assert after.signals[0].action is SignalAction.EXIT_LONG
    assert "손절" in after.signals[0].reason


def test_restarted_instance_uses_lot_direction_for_vwap_cross_short():
    """`next_state`(entries_today 등)는 재시작으로 비어 있어도, lot 안의
    `direction="short"`만으로 VWAP 역크로스 판정이 정확히 동작한다."""
    vwap = _vwap_for(100.00)
    lot = {
        "entry": 30.0, "stop": 29.55, "direction": "short",
        "session": TODAY.isoformat(), "entered_at": _now(9, 40).isoformat(),
    }
    restarted = IntradayMomentumPureStrategy([], {})
    after = restarted.decide(
        _snap(_now(10, 0), _bars(100.00),
              {SIGNAL: vwap + 0.50, LONG: 50.0, SHORT: 29.60},
              lots={SHORT: lot}),
        {},
    )
    assert len(after.signals) == 1
    assert after.signals[0].symbol == SHORT
    assert "VWAP 역크로스" in after.signals[0].reason


def test_no_duplicate_entry_after_restart_while_holding():
    lot = dict(_decide(_strategy(), last_close=100.60).signals[0].state_update)
    restarted = IntradayMomentumPureStrategy([], {})
    after = restarted.decide(
        _snap(_now(9, 50), _bars(100.60), {SIGNAL: 100.00, LONG: 51.0, SHORT: 30.0},
              lots={LONG: lot}),
        {},
    )
    # 보유 중엔 관리만 하고 진입 재평가를 하지 않는다 — 관리 조건 어디에도
    # 안 걸리면 신호 없음(중복 진입도 없음).
    assert all(s.action is not SignalAction.ENTER_LONG for s in after.signals)


# ============================================================ ⑨ 하루 재진입 상한


def test_same_direction_entry_capped_per_day():
    state = {
        "session_date": {"US": TODAY.isoformat()},
        "entries_today": {"long": 2},
    }
    blocked = _decide(_strategy(), last_close=100.60, state=state)
    assert blocked.signals == ()

    state_under_cap = {
        "session_date": {"US": TODAY.isoformat()},
        "entries_today": {"long": 1},
    }
    allowed = _decide(_strategy(), last_close=100.60, state=state_under_cap)
    assert len(allowed.signals) == 1
    assert allowed.next_state["entries_today"]["long"] == 2


def test_opposite_direction_reentry_not_blocked_by_same_direction_cap():
    """같은 방향 상한(long=2)에 걸려도 반대 방향(short) 진입은 막히지 않는다."""
    state = {
        "session_date": {"US": TODAY.isoformat()},
        "entries_today": {"long": 2},
    }
    d = _decide(_strategy(), last_close=99.40, state=state)  # 하방 이탈 → short
    assert len(d.signals) == 1
    assert d.signals[0].symbol == SHORT


def test_session_roll_resets_daily_entry_counts():
    state = {
        "session_date": {"US": PREV_DAYS[-1].isoformat()},
        "entries_today": {"long": 2},
    }
    d = _decide(_strategy(), last_close=100.60, state=state)
    assert len(d.signals) == 1
    assert d.next_state["entries_today"]["long"] == 1


# ============================================================ ⑩ 순수성 / 배선


def test_decide_does_not_mutate_input_state():
    strategy = _strategy()
    state = {
        "session_date": {"US": TODAY.isoformat()},
        "entries_today": {"long": 1},
    }
    before = copy.deepcopy(state)
    _decide(strategy, last_close=100.00, state=state)
    assert state == before


def test_decide_does_not_mutate_snapshot_lots():
    lot = {
        "entry": 50.0, "stop": 45.0, "direction": "long",
        "session": TODAY.isoformat(), "entered_at": _now(9, 40).isoformat(),
    }
    before = copy.deepcopy(lot)
    _decide(_strategy(), last_close=100.00,
            quotes={SIGNAL: 100.00, LONG: 51.0, SHORT: 30.0}, lots={LONG: lot})
    assert lot == before


def test_shell_satisfies_strategy_protocol_wiring():
    shell = IntradayMomentumShell([], {}, market="US", id="intraday_momentum")
    assert shell.id == "intraday_momentum"
    assert shell.symbols == [SIGNAL, LONG, SHORT]
    assert hasattr(shell, "on_cycle")


def test_no_entry_outside_continuous_session():
    """연속 거래 시간 밖(장 시작 전)에는 밴드가 이탈해도 진입하지 않는다."""
    d = _decide(_strategy(), last_close=100.60, now=_now(9, 0))
    assert d.signals == ()
