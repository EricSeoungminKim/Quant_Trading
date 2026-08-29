"""`VolBreakoutPureStrategy`(Larry Williams 변동성 돌파) 규칙 고정.

레거시 쌍둥이가 없는 **신규 전략**이라 동치 비교 대상이 없다. `StrategySnapshot`을
손으로 조립해 `decide()`를 직접 부른다 — `mr_vwap_quiet`/`pullback_impulse` 테스트와
같은 방식이다.

## 기준 시나리오

- 당일 세션 시가 100.0, 전일 고저 110.0/100.0(범위 10.0), k=0.5 →
  트리거 = 100.0 + 0.5*10.0 = **105.0**.
- 트리거에서 진입 시 손절 = entry − 0.5*0.5*10.0 = entry − 2.5. 진입가 105.0이면
  손절 102.5, 손절폭 ≈238bp — 기본 `min_stop_bp`(40) 를 넉넉히 통과한다.
- min_stop_bp 게이트는 별도의 "전일 범위가 아주 좁은" 시나리오로 확인한다
  (전일 범위 0.10 → 손절폭 ≈2.5bp).
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.strategy.vol_breakout import VolBreakoutPureStrategy, VolBreakoutShell

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DAY = date(2026, 1, 5)    # 월요일
PREV = date(2026, 1, 2)   # 금요일
US_SYM = "TSTU"
KR_SYM = "005930"

SESSION_OPEN = 100.0
PREV_HIGH = 110.0
PREV_LOW = 100.0
PREV_RANGE = PREV_HIGH - PREV_LOW  # 10.0
TRIGGER = SESSION_OPEN + 0.5 * PREV_RANGE  # 105.0


# ============================================================ 합성 봉 조립


def _five_min_bars(market: str = "US", *, session_open: float = SESSION_OPEN,
                    n: int = 3) -> pd.DataFrame:
    """오늘 세션 첫 봉 시가가 `session_open`인 5분봉 n개. 값은 시가 판정에만
    쓰이므로 이후 봉은 임의값이다."""
    tz = NY if market == "US" else KST
    open_t = dtime(9, 30) if market == "US" else dtime(9, 0)
    start = datetime.combine(DAY, open_t, tzinfo=tz)
    idx = pd.date_range(start=start, periods=n, freq="5min")
    opens = [session_open] + [session_open + 0.5 * i for i in range(1, n)]
    return pd.DataFrame(
        {
            "open": opens,
            "high": [o + 0.3 for o in opens],
            "low": [o - 0.3 for o in opens],
            "close": opens,
            "volume": [1000.0] * n,
        },
        index=idx,
    )


def _daily_bars(*, high: float = PREV_HIGH, low: float = PREV_LOW,
                 tz=NY, missing: bool = False) -> pd.DataFrame:
    """마지막 완성 일봉 = 전일 세션(고가/저가가 이 전략이 쓰는 값)."""
    if missing:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    idx = pd.date_range(end=datetime.combine(PREV, dtime(0, 0), tzinfo=tz), periods=3, freq="1D")
    mid = (high + low) / 2
    return pd.DataFrame(
        {"open": [mid] * 3, "high": [high] * 3, "low": [low] * 3,
         "close": [mid] * 3, "volume": [1e6] * 3},
        index=idx,
    )


def _snap(
    *, symbol: str = US_SYM, market: str = "US", price: float,
    now: datetime | None = None, session_open: float = SESSION_OPEN,
    prev_high: float = PREV_HIGH, prev_low: float = PREV_LOW,
    daily_missing: bool = False, mtc: float = 195.0, cadence: float = 5.0,
    lots: dict | None = None, market_open: bool = True,
) -> StrategySnapshot:
    tz = NY if market == "US" else KST
    if now is None:
        now = datetime.combine(DAY, dtime(10, 0), tzinfo=tz)
    bars5 = _five_min_bars(market, session_open=session_open)
    daily = _daily_bars(high=prev_high, low=prev_low, tz=tz, missing=daily_missing)
    return StrategySnapshot(
        now=now,
        market_open={market: market_open},
        minutes_to_close={market: mtc},
        cadence_minutes=cadence,
        bars={(symbol, "5m"): bars5, (symbol, "1d"): daily},
        quotes={symbol: Quote(symbol=symbol, ts=now, price=price)},
        lots=lots if lots is not None else {},
    )


def _strategy(symbols=(US_SYM,), **params) -> VolBreakoutPureStrategy:
    return VolBreakoutPureStrategy(list(symbols), dict(params))


def _lot(entry: float = TRIGGER, stop: float = TRIGGER - 2.5,
         session: str | None = None, entered_at: datetime | None = None) -> dict:
    tz = NY
    return {
        "entry": entry, "stop": stop,
        "session": session or DAY.isoformat(),
        "entered_at": (entered_at or datetime.combine(DAY, dtime(10, 0), tzinfo=tz)).isoformat(),
        "strategy": "vol_breakout",
    }


# ============================================================ 계약 / 생성자


def test_requirements_declares_5m_and_daily_bars():
    s = _strategy(symbols=(US_SYM, KR_SYM))
    needs = s.requirements()
    intervals = {(sym, interval) for sym, interval, _ in needs.bars}
    assert intervals == {(US_SYM, "5m"), (US_SYM, "1d"), (KR_SYM, "5m"), (KR_SYM, "1d")}
    assert set(needs.quotes) == {US_SYM, KR_SYM}
    assert needs.needs_positions


@pytest.mark.parametrize("params", [
    {"k": 0},
    {"k": -0.1},
    {"min_stop_bp": -1},
    {"eod_exit_min": 0},
    {"eod_exit_min": -1},
    {"target_weight": 0},
    {"target_weight": 1.5},
])
def test_invalid_params_rejected(params):
    with pytest.raises(ValueError):
        VolBreakoutPureStrategy([US_SYM], params)


# ============================================================ ① 트리거 돌파 시 진입


def test_entry_signal_when_price_breaks_above_trigger():
    d = _strategy().decide(_snap(price=TRIGGER), {})
    assert len(d.signals) == 1
    sig = d.signals[0]
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.symbol == US_SYM
    assert sig.target_weight == pytest.approx(0.5)

    expected_stop = TRIGGER - 0.5 * 0.5 * PREV_RANGE
    assert sig.stop == pytest.approx(expected_stop)
    assert sig.stop < TRIGGER

    assert sig.state_update["entry"] == pytest.approx(TRIGGER)
    assert sig.state_update["stop"] == pytest.approx(expected_stop)
    assert sig.state_update["session"] == DAY.isoformat()
    assert sig.state_update["strategy"] == "vol_breakout"
    assert d.next_state["entries_today"][US_SYM] == DAY.isoformat()


def test_entry_signal_when_price_well_above_trigger():
    d = _strategy().decide(_snap(price=TRIGGER + 3.0), {})
    assert len(d.signals) == 1
    assert d.signals[0].state_update["entry"] == pytest.approx(TRIGGER + 3.0)


# ============================================================ ② 트리거 미달 시 무진입


def test_no_entry_when_price_below_trigger():
    d = _strategy().decide(_snap(price=TRIGGER - 0.01), {})
    assert d.signals == ()
    assert US_SYM not in d.next_state["entries_today"]


# ============================================================ ③ 전일 데이터 없으면 거부


def test_no_entry_when_daily_bars_missing():
    d = _strategy().decide(_snap(price=TRIGGER, daily_missing=True), {})
    assert d.signals == ()
    assert "전일 고저 확인 불가" in d.next_state["last_reject"][US_SYM]


def test_no_entry_when_prev_range_is_degenerate():
    """고가<=저가인 데이터 결손 — 계산 불가로 취급해 거부한다."""
    d = _strategy().decide(_snap(price=TRIGGER, prev_high=100.0, prev_low=100.0), {})
    assert d.signals == ()
    assert "전일 범위" in d.next_state["last_reject"][US_SYM]


# ============================================================ ④ min_stop_bp 게이트


def test_min_stop_bp_gate_rejects_narrow_range():
    """전일 범위 0.10 짜리 조용한 날 — 손절폭이 사실상 2.5bp 로 좁아
    `min_stop_bp`(기본 40) 미만이면 진입하지 않는다(`pullback_impulse`와 같은
    게이트 패턴)."""
    narrow_low, narrow_high = 99.95, 100.05
    trigger = SESSION_OPEN + 0.5 * (narrow_high - narrow_low)  # 100.05
    d = _strategy().decide(
        _snap(price=trigger, prev_high=narrow_high, prev_low=narrow_low), {}
    )
    assert d.signals == ()
    reason = d.next_state["last_reject"][US_SYM]
    assert "손절폭" in reason and "최소" in reason


def test_min_stop_bp_gate_can_be_disabled():
    narrow_low, narrow_high = 99.95, 100.05
    trigger = SESSION_OPEN + 0.5 * (narrow_high - narrow_low)
    d = _strategy(min_stop_bp=0).decide(
        _snap(price=trigger, prev_high=narrow_high, prev_low=narrow_low), {}
    )
    assert len(d.signals) == 1


# ============================================================ ⑤ 마감 N분 전 청산


def test_eod_flatten_exit():
    """다음 판단 시점(now + cadence)이 마감 청산 창 안으로 들어오면 손절과
    무관하게 전량 청산한다(clock.py `_should_flatten` 재현)."""
    snap = _snap(price=TRIGGER + 1.0, mtc=6.0, cadence=5.0, lots={US_SYM: _lot()})
    d = _strategy().decide(snap, {})
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert "EoD 청산" in d.signals[0].reason


def test_no_eod_flatten_when_remaining_time_is_ample():
    snap = _snap(price=TRIGGER + 1.0, mtc=60.0, cadence=5.0, lots={US_SYM: _lot()})
    d = _strategy().decide(snap, {})
    assert d.signals == ()


def test_stop_loss_exit():
    stop = TRIGGER - 2.5
    snap = _snap(price=stop - 0.01, mtc=195.0, lots={US_SYM: _lot(stop=stop)})
    d = _strategy().decide(snap, {})
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert "손절" in d.signals[0].reason


def test_eod_flatten_exit_even_when_lot_has_no_stop():
    """방어선이 반쪽인 랏(stop 없음)이라도 EoD 청산은 걸린다 — 하드레일(손절)
    판정만 건너뛴다("지어내지 않는다"), 오버나잇 금지는 지켜진다."""
    snap = _snap(price=TRIGGER + 1.0, mtc=6.0, cadence=5.0,
                 lots={US_SYM: _lot(stop=None)})
    d = _strategy().decide(snap, {})
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert "EoD 청산" in d.signals[0].reason


def test_session_rollover_forces_exit_even_when_lot_has_no_stop():
    """방어선이 반쪽인 랏(stop 없음)이라도 세션 롤(오버나잇 금지) 강제청산은
    걸린다."""
    snap = _snap(price=TRIGGER + 1.0,
                 lots={US_SYM: _lot(stop=None, session=PREV.isoformat())})
    d = _strategy().decide(snap, {})
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert "오버나잇 금지" in d.signals[0].reason


# ============================================================ ⑥ 하루 1회 제한


def test_one_entry_per_symbol_per_day():
    first = _strategy().decide(_snap(price=TRIGGER), {})
    assert len(first.signals) == 1

    exhausted_state = {
        "session_date": {"US": DAY.isoformat()},
        "entries_today": {US_SYM: DAY.isoformat()},
        "last_reject": {},
    }
    again = _strategy().decide(_snap(price=TRIGGER + 5.0), exhausted_state)
    assert again.signals == ()
    assert again.next_state["last_reject"][US_SYM] == "1일 1회 진입 소진"


def test_session_roll_resets_daily_gate():
    stale_state = {
        "session_date": {"US": PREV.isoformat()},
        "entries_today": {US_SYM: PREV.isoformat()},
        "last_reject": {},
    }
    d = _strategy().decide(_snap(price=TRIGGER), stale_state)
    assert len(d.signals) == 1
    assert d.next_state["session_date"]["US"] == DAY.isoformat()


# ============================================================ ⑦ lots 경유 재시작 생존


def test_open_lot_survives_process_restart():
    """장중 재시작(2026-08-28 실제 사건) — 새 인스턴스, 빈 `next_state`. 브로커
    포지션의 lot만으로 손절 판단이 그대로 나와야 한다."""
    stop = TRIGGER - 2.5
    lot = _lot(entry=TRIGGER, stop=stop)
    restarted = VolBreakoutPureStrategy([US_SYM], {})
    snap = _snap(price=stop - 0.01, lots={US_SYM: lot})
    d = restarted.decide(snap, {})
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert "손절" in d.signals[0].reason


def test_no_duplicate_entry_after_restart_while_holding():
    lot = _lot(entry=TRIGGER, stop=TRIGGER - 2.5)
    restarted = VolBreakoutPureStrategy([US_SYM], {})
    snap = _snap(price=TRIGGER + 1.0, mtc=195.0, lots={US_SYM: lot})
    d = restarted.decide(snap, {})
    assert d.signals == ()  # 손절/목표 어디에도 안 걸림 → 관리 신호도 진입 신호도 없음


# ============================================================ ⑧ 관리 우선순위 / KR 동시호가


def test_session_rollover_forces_exit_as_overnight_safety_net():
    d = _strategy().decide(
        _snap(price=TRIGGER + 1.0, lots={US_SYM: _lot(session=PREV.isoformat())}), {}
    )
    assert len(d.signals) == 1
    assert "오버나잇 금지" in d.signals[0].reason


def test_kr_no_entry_after_continuous_close():
    """KR 연속매매는 15:20 종료 — 그 뒤엔 현재가로 체결할 수 없다."""
    strategy = _strategy(symbols=(KR_SYM,))
    blocked = strategy.decide(
        _snap(symbol=KR_SYM, market="KR", price=TRIGGER,
              now=datetime.combine(DAY, dtime(15, 25), tzinfo=KST)),
        {},
    )
    assert blocked.signals == ()


# ============================================================ 순수성


def test_decide_does_not_mutate_input_state():
    strategy = _strategy()
    state = {
        "session_date": {"US": DAY.isoformat()},
        "entries_today": {}, "last_reject": {},
    }
    snapshot_before = copy.deepcopy(state)
    strategy.decide(_snap(price=TRIGGER - 1.0), state)
    assert state == snapshot_before


def test_decide_does_not_mutate_snapshot_lots():
    lot = _lot()
    before = copy.deepcopy(lot)
    _strategy().decide(_snap(price=TRIGGER + 1.0, lots={US_SYM: lot}), {})
    assert lot == before


def test_shell_satisfies_strategy_protocol_wiring():
    shell = VolBreakoutShell([US_SYM], {}, market="US", id="vol_breakout")
    assert shell.id == "vol_breakout"
    assert shell.symbols == [US_SYM]
    assert hasattr(shell, "on_cycle")
