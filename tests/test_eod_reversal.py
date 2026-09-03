"""`EodReversalPureStrategy`(장 막판 일중 반전) 규칙 고정.

레거시 쌍둥이가 없는 **신규 전략**이라 동치 비교 대상이 없다. `StrategySnapshot`을
손으로 조립해 `decide()`를 직접 부른다 — `vol_breakout` 테스트와 같은 방식이다.

## 기준 시나리오 (KR, 2026-01-05 월요일 14:35 KST)

- KR 연속매매는 15:20 종료 → 남은 시간 45분 = `eval_minutes_before_close`(45)
  경계 → **평가창이 막 열린 시점**이다.
- 관심종목 3개: 005930 −3.0% / 035420 +0.5% / 000660 +1.0%.
  `bottom_pct`(20%) × 3종목 → 하위 **1종목**(005930)만 후보.
- 1분봉 300개(09:00~13:59), 봉당 거래량 300 → 세션 누적 거래대금 ≈ 9억원으로
  유동성 밴드(1억~1,000억) 안이다.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.strategy.eod_reversal import EodReversalPureStrategy, EodReversalShell

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
DAY = date(2026, 1, 5)    # 월요일
PREV = date(2026, 1, 2)   # 금요일
LOSER = "005930"
MID = "035420"
WINNER = "000660"
US_SYM = "TSTU"

SESSION_OPEN = 10000.0
LOSER_LAST = 9700.0       # -3.0%
MID_LAST = 10050.0        # +0.5%
WINNER_LAST = 10100.0     # +1.0%
BAR_VOLUME = 300.0        # 300봉 × 1만원 × 300주 ≈ 9억원
_COLUMNS = ["open", "high", "low", "close", "volume"]

# 14:35 KST — 연속매매 종료(15:20)까지 45분.
EVAL_NOW = datetime.combine(DAY, dtime(14, 35), tzinfo=KST)


# ============================================================ 합성 봉 조립


def _bars_1m(
    market: str = "KR", *, open_px: float = SESSION_OPEN, last_px: float = LOSER_LAST,
    n: int = 300, volume: float = BAR_VOLUME, empty: bool = False,
) -> pd.DataFrame:
    """오늘 세션 1분봉 n개. 첫 봉 시가 = `open_px`, 마지막 봉 종가 = `last_px`
    (그 사이는 선형)."""
    tz = KST if market == "KR" else NY
    open_t = dtime(9, 0) if market == "KR" else dtime(9, 30)
    if empty or n <= 0:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], tz=tz))
    start = datetime.combine(DAY, open_t, tzinfo=tz)
    idx = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(n)])
    step = (last_px - open_px) / max(n - 1, 1)
    closes = [open_px + step * i for i in range(n)]
    opens = [open_px] + closes[:-1]
    return pd.DataFrame(
        {"open": opens,
         "high": [max(o, c) for o, c in zip(opens, closes)],
         "low": [min(o, c) for o, c in zip(opens, closes)],
         "close": closes,
         "volume": [volume] * n},
        index=idx,
    )


def _snap(
    *, market: str = "KR", specs: dict[str, dict] | None = None,
    now: datetime | None = None, mtc: float | None = None, cadence: float = 5.0,
    lots: dict | None = None, market_open: bool = True,
) -> StrategySnapshot:
    """`specs`: 심볼 → {price, bars(kwargs)}. `mtc`를 안 주면 명목 마감(KR 15:30)
    까지의 실제 잔여시간을 계산해 넣는다."""
    tz = KST if market == "KR" else NY
    if now is None:
        now = EVAL_NOW if market == "KR" else datetime.combine(DAY, dtime(15, 15), tzinfo=NY)
    if mtc is None:
        close_t = dtime(15, 30) if market == "KR" else dtime(16, 0)
        close_dt = datetime.combine(now.astimezone(tz).date(), close_t, tzinfo=tz)
        mtc = (close_dt - now.astimezone(tz)).total_seconds() / 60
    specs = specs or {}
    bars: dict[tuple[str, str], pd.DataFrame] = {}
    quotes: dict[str, Quote] = {}
    for symbol, spec in specs.items():
        bars[(symbol, "1m")] = _bars_1m(market, **spec.get("bars", {}))
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


def _three_symbol_specs(*, loser_last: float = LOSER_LAST, **loser_bars) -> dict[str, dict]:
    return {
        LOSER: {"price": loser_last, "bars": {"last_px": loser_last, **loser_bars}},
        MID: {"price": MID_LAST, "bars": {"last_px": MID_LAST}},
        WINNER: {"price": WINNER_LAST, "bars": {"last_px": WINNER_LAST}},
    }


def _strategy(symbols=(LOSER, MID, WINNER), **params) -> EodReversalPureStrategy:
    return EodReversalPureStrategy(list(symbols), dict(params))


def _lot(entry: float = LOSER_LAST, stop: float | None = LOSER_LAST * 0.985,
         session: str | None = None) -> dict:
    return {
        "entry": entry, "stop": stop,
        "session": session or DAY.isoformat(),
        "entered_at": EVAL_NOW.isoformat(),
        "strategy": "eod_reversal",
    }


# ============================================================ 계약 / 생성자


def test_requirements_declares_only_1m_bars():
    s = _strategy(symbols=(LOSER, MID))
    needs = s.requirements()
    assert {(sym, interval) for sym, interval, _ in needs.bars} == {(LOSER, "1m"), (MID, "1m")}
    assert {n for _, _, n in needs.bars} == {390 + 10}
    assert set(needs.quotes) == {LOSER, MID}
    assert needs.needs_positions
    assert not needs.fetch_when_closed


@pytest.mark.parametrize("params", [
    {"eval_minutes_before_close": 0},
    {"bottom_pct": 0},
    {"bottom_pct": 101},
    {"min_drop_pct": -1},
    {"min_turnover_krw": -1},
    {"max_turnover_krw": 1e8, "min_turnover_krw": 1e8},   # 상한 <= 하한
    {"stop_pct": 0},
    {"stop_pct": 100},
    {"eod_exit_min": 0},
    {"target_weight": 0},
    {"target_weight": 1.5},
    {"eval_minutes_before_close": 2, "eod_exit_min": 2},  # 평가창 = 청산창
    {"markets": []},
    {"markets": ["JP"]},
    {"min_session_bars": 0},
])
def test_invalid_params_rejected(params):
    with pytest.raises(ValueError):
        EodReversalPureStrategy([LOSER], params)


# ============================================================ ① 하위 분위 진입


def test_entry_signal_for_the_worst_performer_in_the_eval_window():
    d = _strategy().decide(_snap(specs=_three_symbol_specs()), {})
    assert len(d.signals) == 1
    sig = d.signals[0]
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.symbol == LOSER
    assert sig.target_weight == pytest.approx(0.5)
    assert sig.stop == pytest.approx(LOSER_LAST * (1 - 0.015))
    assert sig.state_update["entry"] == pytest.approx(LOSER_LAST)
    assert sig.state_update["session"] == DAY.isoformat()
    assert sig.state_update["strategy"] == "eod_reversal"
    assert d.next_state["entries_today"][LOSER] == DAY.isoformat()
    assert "-3.00%" in sig.reason


def test_symbols_outside_the_bottom_quantile_are_rejected():
    d = _strategy().decide(_snap(specs=_three_symbol_specs()), {})
    for symbol in (MID, WINNER):
        assert "하위 20% 밖" in d.next_state["last_reject"][symbol]


def test_no_entry_before_the_eval_window_opens():
    """13:00 KST — 마감까지 140분. 아직 평가창 전이라 사유도 남기지 않는다."""
    early = datetime.combine(DAY, dtime(13, 0), tzinfo=KST)
    d = _strategy().decide(_snap(specs=_three_symbol_specs(), now=early), {})
    assert d.signals == ()
    assert d.next_state["last_reject"] == {}


# ============================================================ ② 낙폭 / 유동성 게이트


def test_shallow_drop_is_rejected():
    d = _strategy().decide(_snap(specs=_three_symbol_specs(loser_last=9900.0)), {})
    assert d.signals == ()
    assert "낙폭" in d.next_state["last_reject"][LOSER]


def test_turnover_below_band_is_rejected():
    d = _strategy().decide(_snap(specs=_three_symbol_specs(volume=0.01)), {})
    assert d.signals == ()
    assert "하한" in d.next_state["last_reject"][LOSER]


def test_turnover_above_band_is_rejected():
    """상한은 "효과가 남아 있는가"를 자른다 — 대형주에서는 반전이 약하다."""
    d = _strategy().decide(_snap(specs=_three_symbol_specs(volume=1e6)), {})
    assert d.signals == ()
    assert "상한" in d.next_state["last_reject"][LOSER]


def test_missing_session_bars_are_rejected():
    d = _strategy().decide(_snap(specs=_three_symbol_specs(empty=True)), {})
    assert d.signals == ()
    assert "세션 1분봉 확인 불가" in d.next_state["last_reject"][LOSER]


def test_too_few_session_bars_are_rejected():
    d = _strategy().decide(_snap(specs=_three_symbol_specs(n=5)), {})
    assert d.signals == ()
    assert "세션 1분봉 확인 불가" in d.next_state["last_reject"][LOSER]


def test_us_symbols_are_rejected_by_default_market_gate():
    """거래대금 밴드가 원 단위라 기본값은 KR 전용이다."""
    strategy = _strategy(symbols=(US_SYM,))
    now = datetime.combine(DAY, dtime(15, 20), tzinfo=NY)  # 마감 40분 전
    snap = _snap(market="US", now=now,
                 specs={US_SYM: {"price": 97.0, "bars": {"open_px": 100.0, "last_px": 97.0}}})
    d = strategy.decide(snap, {})
    assert d.signals == ()
    assert "미허용 시장" in d.next_state["last_reject"][US_SYM]


# ============================================================ ③ 청산 레일


def test_eod_flatten_exit():
    late = datetime.combine(DAY, dtime(15, 18), tzinfo=KST)  # 연속매매 종료 2분 전
    snap = _snap(specs=_three_symbol_specs(), now=late, cadence=5.0,
                 lots={LOSER: _lot()})
    d = _strategy().decide(snap, {})
    exits = [s for s in d.signals if s.action is SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "EoD 청산" in exits[0].reason


def test_no_new_entry_inside_the_flatten_window():
    """들어가자마자 EoD 청산이 나가는 왕복은 만들지 않는다."""
    late = datetime.combine(DAY, dtime(15, 18), tzinfo=KST)
    d = _strategy().decide(_snap(specs=_three_symbol_specs(), now=late), {})
    assert d.signals == ()


def test_no_eod_flatten_when_remaining_time_is_ample():
    snap = _snap(specs=_three_symbol_specs(), lots={LOSER: _lot()})
    d = _strategy().decide(snap, {})
    assert [s for s in d.signals if s.action is SignalAction.EXIT_LONG] == []


def test_stop_loss_exit():
    stop = LOSER_LAST * 0.985
    specs = _three_symbol_specs()
    specs[LOSER]["price"] = stop - 1
    d = _strategy().decide(_snap(specs=specs, lots={LOSER: _lot(stop=stop)}), {})
    exits = [s for s in d.signals if s.action is SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "손절" in exits[0].reason


def test_eod_flatten_exit_even_when_lot_has_no_stop():
    late = datetime.combine(DAY, dtime(15, 18), tzinfo=KST)
    snap = _snap(specs=_three_symbol_specs(), now=late, lots={LOSER: _lot(stop=None)})
    d = _strategy().decide(snap, {})
    assert len(d.signals) == 1
    assert "EoD 청산" in d.signals[0].reason


def test_session_rollover_forces_exit_as_overnight_safety_net():
    snap = _snap(specs=_three_symbol_specs(), lots={LOSER: _lot(session=PREV.isoformat())})
    d = _strategy().decide(snap, {})
    exits = [s for s in d.signals if s.action is SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "오버나잇 금지" in exits[0].reason


# ============================================================ ④ 하루 1회 / 재시작


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
    stop = LOSER_LAST * 0.985
    specs = _three_symbol_specs()
    specs[LOSER]["price"] = stop - 1
    restarted = EodReversalPureStrategy([LOSER, MID, WINNER], {})
    d = restarted.decide(_snap(specs=specs, lots={LOSER: _lot(stop=stop)}), {})
    exits = [s for s in d.signals if s.action is SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "손절" in exits[0].reason


def test_no_duplicate_entry_after_restart_while_holding():
    restarted = EodReversalPureStrategy([LOSER, MID, WINNER], {})
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
    shell = EodReversalShell([LOSER], {}, market="KR", id="eod_reversal")
    assert shell.id == "eod_reversal"
    assert shell.symbols == [LOSER]
    assert hasattr(shell, "on_cycle")


def test_registered_in_strategy_registry():
    from quant.trade.strategy import STRATEGY_REGISTRY, TAG_ASSIGNMENT

    assert STRATEGY_REGISTRY["eod_reversal"] is EodReversalShell
    assert "eod_reversal" in TAG_ASSIGNMENT["*"]
