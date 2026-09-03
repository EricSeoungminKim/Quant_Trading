"""`OpenReversalPureStrategy`(전일 패자 개장 매수) 규칙 고정.

레거시 쌍둥이가 없는 **신규 전략**이라 동치 비교 대상이 없다. `StrategySnapshot`을
손으로 조립해 `decide()`를 직접 부른다 — `vol_breakout` 테스트와 같은 방식이다.

## 기준 시나리오 (KR, 2026-01-05 월요일 09:07 KST — 개장 후 7분)

- 일봉 마지막 두 개(전전일 → 전일) 종가:
  005930 10,000 → 9,700(**−3.0%**) / 035420 10,000 → 9,950(−0.5%) /
  000660 10,000 → 10,100(+1.0%).
- `bottom_k` 기본 3이라 세 종목 다 하위 3위 안이지만,
  `min_prev_drop_pct`(2.0%)를 넘는 것은 005930 하나뿐이다.
- 오늘 시가는 전일 종가와 같게 둔다(갭 0%) → `max_gap_down_pct`(3.0%) 통과.
- 진입가 9,700이면 손절 = 9,700 × 0.98 = **9,506**.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.strategy.open_reversal import OpenReversalPureStrategy, OpenReversalShell

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
DAY = date(2026, 1, 5)    # 월요일
PREV = date(2026, 1, 2)   # 금요일
LOSER = "005930"
MID = "035420"
WINNER = "000660"

PRIOR_CLOSE = 10000.0
LOSER_PREV_CLOSE = 9700.0     # -3.0%
MID_PREV_CLOSE = 9950.0       # -0.5%
WINNER_PREV_CLOSE = 10100.0   # +1.0%
_COLUMNS = ["open", "high", "low", "close", "volume"]

ENTRY_NOW = datetime.combine(DAY, dtime(9, 7), tzinfo=KST)


# ============================================================ 합성 봉 조립


def _daily(*, prev_close: float, prior_close: float = PRIOR_CLOSE,
           n: int = 10, market: str = "KR") -> pd.DataFrame:
    """마지막 두 행이 (전전일, 전일) 종가. 그 앞은 판정에 쓰이지 않는다."""
    tz = KST if market == "KR" else NY
    if n <= 0:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], tz=tz))
    idx = pd.date_range(end=datetime.combine(PREV, dtime(0, 0), tzinfo=tz),
                        periods=n, freq="1D")
    closes = [prior_close] * n
    if n >= 2:
        closes[-1] = prev_close
    return pd.DataFrame(
        {"open": closes, "high": [c * 1.01 for c in closes],
         "low": [c * 0.99 for c in closes], "close": closes,
         "volume": [1e6] * n},
        index=idx,
    )


def _bars_5m(*, today_open: float, n: int = 3, market: str = "KR",
             empty: bool = False) -> pd.DataFrame:
    tz = KST if market == "KR" else NY
    open_t = dtime(9, 0) if market == "KR" else dtime(9, 30)
    if empty or n <= 0:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], tz=tz))
    start = datetime.combine(DAY, open_t, tzinfo=tz)
    idx = pd.DatetimeIndex([start + timedelta(minutes=5 * i) for i in range(n)])
    return pd.DataFrame(
        {"open": [today_open] * n, "high": [today_open * 1.005] * n,
         "low": [today_open * 0.995] * n, "close": [today_open] * n,
         "volume": [1000.0] * n},
        index=idx,
    )


def _snap(
    *, market: str = "KR", specs: dict[str, dict] | None = None,
    now: datetime | None = None, mtc: float = 383.0, cadence: float = 5.0,
    lots: dict | None = None, market_open: bool = True,
) -> StrategySnapshot:
    """`specs`: 심볼 → {price, prev_close, today_open, daily(kwargs), bars5(kwargs)}."""
    tz = KST if market == "KR" else NY
    now = now or ENTRY_NOW
    specs = specs or {}
    bars: dict[tuple[str, str], pd.DataFrame] = {}
    quotes: dict[str, Quote] = {}
    for symbol, spec in specs.items():
        prev_close = spec["prev_close"]
        bars[(symbol, "1d")] = _daily(prev_close=prev_close, market=market,
                                      **spec.get("daily", {}))
        bars[(symbol, "5m")] = _bars_5m(
            today_open=spec.get("today_open", prev_close), market=market,
            **spec.get("bars5", {}),
        )
        price = spec.get("price")
        if price is not None:
            quotes[symbol] = Quote(symbol=symbol, ts=now, price=price)
    return StrategySnapshot(
        now=now,
        market_open={market: market_open},
        minutes_to_close={market: mtc},
        cadence_minutes=cadence,
        bars=bars,
        quotes=quotes,
        lots=lots if lots is not None else {},
    )


def _three_symbol_specs(**loser_over) -> dict[str, dict]:
    loser = {"price": LOSER_PREV_CLOSE, "prev_close": LOSER_PREV_CLOSE}
    loser.update(loser_over)
    return {
        LOSER: loser,
        MID: {"price": MID_PREV_CLOSE, "prev_close": MID_PREV_CLOSE},
        WINNER: {"price": WINNER_PREV_CLOSE, "prev_close": WINNER_PREV_CLOSE},
    }


def _strategy(symbols=(LOSER, MID, WINNER), **params) -> OpenReversalPureStrategy:
    return OpenReversalPureStrategy(list(symbols), dict(params))


def _lot(entry: float = LOSER_PREV_CLOSE, stop: float | None = LOSER_PREV_CLOSE * 0.98,
         session: str | None = None) -> dict:
    return {
        "entry": entry, "stop": stop,
        "session": session or DAY.isoformat(),
        "entered_at": ENTRY_NOW.isoformat(),
        "strategy": "open_reversal",
    }


# ============================================================ 계약 / 생성자


def test_requirements_declares_daily_and_5m_bars():
    s = _strategy(symbols=(LOSER, MID))
    needs = s.requirements()
    assert {(sym, interval) for sym, interval, _ in needs.bars} == {
        (LOSER, "1d"), (LOSER, "5m"), (MID, "1d"), (MID, "5m")
    }
    counts = {(sym, interval): n for sym, interval, n in needs.bars}
    assert counts[(LOSER, "1d")] == 10
    assert counts[(LOSER, "5m")] == 12   # 개장 첫 봉까지 닿는 최소치
    assert set(needs.quotes) == {LOSER, MID}
    assert needs.needs_positions
    assert not needs.fetch_when_closed


@pytest.mark.parametrize("params", [
    {"entry_window_min": 0},
    {"bottom_k": 0},
    {"min_prev_drop_pct": -1},
    {"max_gap_down_pct": 0},
    {"stop_pct": 0},
    {"stop_pct": 100},
    {"eod_exit_min": 0},
    {"target_weight": 0},
    {"target_weight": 1.5},
])
def test_invalid_params_rejected(params):
    with pytest.raises(ValueError):
        OpenReversalPureStrategy([LOSER], params)


# ============================================================ ① 전일 패자 진입


def test_entry_signal_for_yesterdays_biggest_loser():
    d = _strategy().decide(_snap(specs=_three_symbol_specs()), {})
    assert len(d.signals) == 1
    sig = d.signals[0]
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.symbol == LOSER
    assert sig.target_weight == pytest.approx(0.5)
    assert sig.stop == pytest.approx(LOSER_PREV_CLOSE * 0.98)
    assert sig.state_update["entry"] == pytest.approx(LOSER_PREV_CLOSE)
    assert sig.state_update["session"] == DAY.isoformat()
    assert sig.state_update["strategy"] == "open_reversal"
    assert d.next_state["entries_today"][LOSER] == DAY.isoformat()
    assert "-3.00%" in sig.reason


def test_shallow_prev_drop_is_rejected():
    d = _strategy().decide(_snap(specs=_three_symbol_specs()), {})
    for symbol in (MID, WINNER):
        assert "낙폭" in d.next_state["last_reject"][symbol]


def test_bottom_k_gate_keeps_only_the_worst_names():
    d = _strategy(bottom_k=1).decide(_snap(specs=_three_symbol_specs()), {})
    assert [s.symbol for s in d.signals] == [LOSER]
    for symbol in (MID, WINNER):
        assert "하위 1위 밖" in d.next_state["last_reject"][symbol]


# ============================================================ ② 갭 게이트


def test_falling_knife_gap_down_is_rejected():
    """전일 종가 대비 −4% 갭하락 — 반전이 아니라 악재 지속일 가능성이 크다."""
    specs = _three_symbol_specs(today_open=LOSER_PREV_CLOSE * 0.96,
                                price=LOSER_PREV_CLOSE * 0.96)
    d = _strategy().decide(_snap(specs=specs), {})
    assert d.signals == ()
    assert "떨어지는 칼" in d.next_state["last_reject"][LOSER]


def test_mild_gap_down_still_enters():
    specs = _three_symbol_specs(today_open=LOSER_PREV_CLOSE * 0.99,
                                price=LOSER_PREV_CLOSE * 0.99)
    d = _strategy().decide(_snap(specs=specs), {})
    assert len(d.signals) == 1
    assert "-1.00%" in d.signals[0].reason  # 갭 표시


# ============================================================ ③ 데이터 결손 / 진입창


def test_insufficient_daily_bars_are_rejected():
    specs = _three_symbol_specs(daily={"n": 1})
    d = _strategy().decide(_snap(specs=specs), {})
    assert d.signals == ()
    assert "전일 수익률 확인 불가" in d.next_state["last_reject"][LOSER]


def test_missing_session_open_is_rejected():
    specs = _three_symbol_specs(bars5={"empty": True})
    d = _strategy().decide(_snap(specs=specs), {})
    assert d.signals == ()
    assert "당일 세션 시가 확인 불가" in d.next_state["last_reject"][LOSER]


def test_entry_window_closes_after_configured_minutes():
    late = datetime.combine(DAY, dtime(9, 20), tzinfo=KST)  # 개장 후 20분
    d = _strategy().decide(_snap(specs=_three_symbol_specs(), now=late), {})
    assert d.signals == ()
    assert "진입창 종료" in d.next_state["last_reject"][LOSER]


def test_no_quote_blocks_entry():
    specs = _three_symbol_specs(price=None)
    d = _strategy().decide(_snap(specs=specs), {})
    assert d.signals == ()
    assert "현재가 없음" in d.next_state["last_reject"][LOSER]


# ============================================================ ④ 청산 레일


def test_eod_flatten_exit():
    late = datetime.combine(DAY, dtime(15, 18), tzinfo=KST)
    snap = _snap(specs=_three_symbol_specs(), now=late, mtc=12.0, cadence=5.0,
                 lots={LOSER: _lot()})
    d = _strategy().decide(snap, {})
    exits = [s for s in d.signals if s.action is SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "EoD 청산" in exits[0].reason


def test_no_eod_flatten_when_remaining_time_is_ample():
    snap = _snap(specs=_three_symbol_specs(), lots={LOSER: _lot()})
    d = _strategy().decide(snap, {})
    assert [s for s in d.signals if s.action is SignalAction.EXIT_LONG] == []


def test_stop_loss_exit():
    stop = LOSER_PREV_CLOSE * 0.98
    specs = _three_symbol_specs(price=stop - 1)
    d = _strategy().decide(_snap(specs=specs, lots={LOSER: _lot(stop=stop)}), {})
    exits = [s for s in d.signals if s.action is SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "손절" in exits[0].reason


def test_eod_flatten_exit_even_when_lot_has_no_stop():
    late = datetime.combine(DAY, dtime(15, 18), tzinfo=KST)
    snap = _snap(specs=_three_symbol_specs(), now=late, mtc=12.0,
                 lots={LOSER: _lot(stop=None)})
    d = _strategy().decide(snap, {})
    exits = [s for s in d.signals if s.action is SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "EoD 청산" in exits[0].reason


def test_session_rollover_forces_exit_as_overnight_safety_net():
    snap = _snap(specs=_three_symbol_specs(), lots={LOSER: _lot(session=PREV.isoformat())})
    d = _strategy().decide(snap, {})
    exits = [s for s in d.signals if s.action is SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "오버나잇 금지" in exits[0].reason


# ============================================================ ⑤ 하루 1회 / 재시작


def test_one_entry_per_symbol_per_day():
    exhausted = {
        "session_date": {"KR": DAY.isoformat()},
        "entries_today": {LOSER: DAY.isoformat()},
        "last_reject": {},
    }
    d = _strategy().decide(_snap(specs=_three_symbol_specs()), exhausted)
    assert d.signals == ()
    assert d.next_state["last_reject"][LOSER] == "1일 1회 진입 소진"


def test_session_roll_resets_daily_gate():
    stale = {
        "session_date": {"KR": PREV.isoformat()},
        "entries_today": {LOSER: PREV.isoformat()},
        "last_reject": {LOSER: "낡은 사유"},
    }
    d = _strategy().decide(_snap(specs=_three_symbol_specs()), stale)
    assert len(d.signals) == 1
    assert d.next_state["session_date"]["KR"] == DAY.isoformat()


def test_open_lot_survives_process_restart():
    stop = LOSER_PREV_CLOSE * 0.98
    specs = _three_symbol_specs(price=stop - 1)
    restarted = OpenReversalPureStrategy([LOSER, MID, WINNER], {})
    d = restarted.decide(_snap(specs=specs, lots={LOSER: _lot(stop=stop)}), {})
    exits = [s for s in d.signals if s.action is SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "손절" in exits[0].reason


def test_no_duplicate_entry_after_restart_while_holding():
    restarted = OpenReversalPureStrategy([LOSER, MID, WINNER], {})
    d = restarted.decide(_snap(specs=_three_symbol_specs(), lots={LOSER: _lot()}), {})
    assert d.signals == ()


def test_closed_market_produces_no_signals():
    snap = _snap(specs=_three_symbol_specs(), market_open=False)
    assert _strategy().decide(snap, {}).signals == ()


# ============================================================ 순수성 / 껍질


def test_decide_does_not_mutate_input_state():
    state = {"session_date": {"KR": DAY.isoformat()}, "entries_today": {}, "last_reject": {}}
    before = copy.deepcopy(state)
    _strategy().decide(_snap(specs=_three_symbol_specs()), state)
    assert state == before


def test_decide_does_not_mutate_snapshot_lots():
    lot = _lot()
    before = copy.deepcopy(lot)
    _strategy().decide(_snap(specs=_three_symbol_specs(), lots={LOSER: lot}), {})
    assert lot == before


def test_shell_satisfies_strategy_protocol_wiring():
    shell = OpenReversalShell([LOSER], {}, market="KR", id="open_reversal")
    assert shell.id == "open_reversal"
    assert shell.symbols == [LOSER]
    assert hasattr(shell, "on_cycle")


def test_registered_in_strategy_registry():
    from quant.trade.strategy import STRATEGY_REGISTRY, TAG_ASSIGNMENT

    assert STRATEGY_REGISTRY["open_reversal"] is OpenReversalShell
    assert "open_reversal" in TAG_ASSIGNMENT["*"]
