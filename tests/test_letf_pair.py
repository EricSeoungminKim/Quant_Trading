"""`LetfPairPureStrategy`(레버리지 ETF 페어 전환, Family F1) 규칙 고정.

레거시 쌍둥이가 없는 **신규 전략**이라 동치 비교 대상이 없다. `StrategySnapshot`을
손으로 조립해 `decide()`를 직접 부른다 — `tests/test_trend_day.py`와 같은 방식이다.

## 기준 시나리오 (US, QQQ→TQQQ/SQQQ, 2026-01-05 월요일, now=10:15 ET)

지표 계산을 손으로 검산하기 쉽게 `n_fast=2/n_slow=4/n_atr=4`(스펙 기본값이 아니라
이 테스트 전용 축소값)를 쓴다. 봉은 전일(2026-01-02, 4개 완성봉, 고정가 100 —
EMA/ATR 워밍업용)과 당일(2026-01-05, 3개 완성봉)로 구성한다.

- **UP 시나리오**(`UP_TODAY_ROWS`): 당일 09:30/09:45/10:00 종가 100.5→102.5→105.5로
  강하게 상승. 계산 결과(모듈에서 직접 `_indicators()`로 검증): `atr≈2.0640625`,
  `vwap≈102.28889`, `close=105.5`, `strength≈0.6721`. ema_f>ema_s·close>vwap·
  strength>=0(기본 `k_min=0`)이므로 UP.
- **DOWN 시나리오**(`DOWN_TODAY_ROWS`): 대칭 하락(99.5→97.5→94.5).
  `atr≈2.0640625`, `vwap≈97.71111`, `close=94.5`, `strength≈-0.6721` → DOWN.
- **NEUTRAL/VWAP 시나리오**(`NEUTRAL_TODAY_ROWS`): EMA는 여전히 강세(strength≈
  +0.120>0)이지만 마지막 종가(101.3)가 세션 VWAP(≈101.722) 아래로 떨어져
  방향이 NEUTRAL로 떨어진다 — "EMA는 맞아도 VWAP 조건 하나로 막힌다"를 고정한다.
- 일봉(day_filter/win_table용) `_daily_flat`: 20개 평평한 고가102/저가98/종가100
  → `ATR14=4.0`, 전일종가 100.0. 당일 개장 30분(2봉) 레인지 = [99.5, 103.0] →
  폭 3.5 → OR/ATR14 = 0.875(기본 `or_atr_min=0.8` 통과, `or_atr_min=2.0`이면 불통과).
  `_daily_rising`(20개, 100→119 상승) → SMA20=109.5 < 종가119 → win_table 국면 "above".

손절 공식(진입 시): `entry × (1 − 3×stop_atr_mult×atr/close)` — 기본
`stop_atr_mult=1.5`, TQQQ 진입가 50.0 기준 예상 손절 ≈45.598(≈880bp, 기본
`min_stop_bp=40` 통과).
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.strategy.letf_pair import LetfPairPureStrategy, LetfPairShell

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
PREV = date(2026, 1, 2)   # 금요일
DAY = date(2026, 1, 5)    # 월요일
SIG, LONG, SHORT = "QQQ", "TQQQ", "SQQQ"
NOW = datetime.combine(DAY, dtime(10, 15), tzinfo=NY)
_COLUMNS = ["open", "high", "low", "close", "volume"]

LONG_PRICE = 50.0
SHORT_PRICE = 20.0
EXPECTED_ATR = 2.0640625000000012
EXPECTED_UP_VWAP = 102.28888888888889
EXPECTED_UP_CLOSE = 105.5
EXPECTED_UP_STRENGTH = 0.6721013822300759
EXPECTED_DOWN_CLOSE = 94.5
EXPECTED_LONG_STOP = LONG_PRICE * (1 - 3 * 1.5 * EXPECTED_ATR / EXPECTED_UP_CLOSE)  # ≈45.598
EXPECTED_SHORT_STOP = SHORT_PRICE * (1 - 3 * 1.5 * EXPECTED_ATR / EXPECTED_DOWN_CLOSE)

PREV_ROWS = [
    {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000.0}
    for _ in range(4)
]
UP_TODAY_ROWS = [
    {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 1000.0},
    {"open": 100.5, "high": 103.0, "low": 100.3, "close": 102.5, "volume": 1000.0},
    {"open": 102.5, "high": 106.0, "low": 102.3, "close": 105.5, "volume": 1000.0},
]
DOWN_TODAY_ROWS = [
    {"open": 100.0, "high": 100.5, "low": 99.0, "close": 99.5, "volume": 1000.0},
    {"open": 99.5, "high": 99.7, "low": 97.0, "close": 97.5, "volume": 1000.0},
    {"open": 97.5, "high": 97.7, "low": 94.0, "close": 94.5, "volume": 1000.0},
]
NEUTRAL_TODAY_ROWS = [
    {"open": 100.0, "high": 101.0, "low": 99.8, "close": 100.8, "volume": 1000.0},
    {"open": 100.8, "high": 104.0, "low": 100.5, "close": 103.5, "volume": 1000.0},
    {"open": 103.5, "high": 103.6, "low": 101.0, "close": 101.3, "volume": 1000.0},
]

BASE_PARAMS = dict(
    signal_symbol=SIG, long_symbol=LONG, short_symbol=SHORT,
    n_fast=2, n_slow=4, n_atr=4, k_min=0.0,
    warmup_min=0, no_entry_min=0, cooldown_bars=0,
)


# ============================================================ 합성 봉 조립

def _mk(rows: list[dict], day: date, tz=NY, open_t: dtime = dtime(9, 30), m: int = 15) -> pd.DataFrame:
    start = datetime.combine(day, open_t, tzinfo=tz)
    idx = [start + timedelta(minutes=m * i) for i in range(len(rows))]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def _bars(today_rows: list[dict] | None, *, empty: bool = False) -> pd.DataFrame:
    if empty:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], tz=NY))
    prev_df = _mk(PREV_ROWS, PREV)
    if today_rows is None:
        return prev_df
    return pd.concat([prev_df, _mk(today_rows, DAY)])


def _daily(*, n: int = 20, high: float = 102.0, low: float = 98.0,
           close: float = 100.0, volume: float = 2e6, empty: bool = False) -> pd.DataFrame:
    if empty or n <= 0:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], tz=NY))
    idx = pd.date_range(end=datetime.combine(PREV, dtime(0, 0), tzinfo=NY), periods=n, freq="1D")
    return pd.DataFrame(
        {"open": [close] * n, "high": [high] * n, "low": [low] * n,
         "close": [close] * n, "volume": [volume] * n},
        index=idx,
    )


def _daily_rising(*, n: int = 20) -> pd.DataFrame:
    """종가가 20일선 위(win_table 국면 "above")."""
    closes = [100.0 + i for i in range(n)]
    idx = pd.date_range(end=datetime.combine(PREV, dtime(0, 0), tzinfo=NY), periods=n, freq="1D")
    return pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
         "close": closes, "volume": [1e6] * n},
        index=idx,
    )


def _quotes(**overrides) -> dict[str, Quote]:
    values = {LONG: LONG_PRICE, SHORT: SHORT_PRICE, SIG: EXPECTED_UP_CLOSE}
    values.update(overrides)
    return {sym: Quote(symbol=sym, ts=NOW, price=price) for sym, price in values.items()}


def _snap(
    *, today_rows: list[dict] | None = UP_TODAY_ROWS, bars_empty: bool = False,
    daily: pd.DataFrame | None = None, quotes: dict[str, Quote] | None = None,
    now: datetime = NOW, mtc: float = 200.0, cadence: float = 15.0,
    lots: dict | None = None, market_open: bool = True,
) -> StrategySnapshot:
    bars = {(SIG, "15m"): _bars(today_rows, empty=bars_empty)}
    if daily is not None:
        bars[(SIG, "1d")] = daily
    return StrategySnapshot(
        now=now,
        market_open={"US": market_open},
        minutes_to_close={"US": mtc},
        cadence_minutes=cadence,
        bars=bars,
        quotes=quotes if quotes is not None else _quotes(),
        lots=lots if lots is not None else {},
    )


def _strategy(**overrides) -> LetfPairPureStrategy:
    win_table = overrides.pop("win_table", None)
    params = {**BASE_PARAMS, **overrides}
    return LetfPairPureStrategy([], params, market="US", win_table=win_table)


def _lot(entry: float = LONG_PRICE, stop: float | None = 10.0, direction: str = "long",
         session: str | None = None) -> dict:
    return {
        "entry": entry, "stop": stop, "direction": direction,
        "session": session or DAY.isoformat(),
        "entered_at": NOW.isoformat(), "strategy": "letf_pair",
    }


# ============================================================ 계약 / 생성자


def test_requirements_declares_15m_bars_quotes_and_positions():
    s = _strategy()   # 테스트 전용 축소값 n_slow=4/n_atr=4 → max(6*4, 4+4+10)=24
    needs = s.requirements()
    assert needs.bars == ((SIG, "15m", 24),)   # max(6*n_slow, n_slow+n_atr+10) — 6× 근거는 전략 _bar_count 주석
    assert set(needs.quotes) == {SIG, LONG, SHORT}
    assert needs.needs_positions
    assert not needs.fetch_when_closed


def test_requirements_bar_count_uses_6x_nslow_when_larger():
    # 스펙 기본값(n_slow=21, n_atr=14)에서는 6×n_slow(126)가 워밍업 여유(45)보다
    # 커서 그쪽이 하한을 결정한다. 3×n_slow(63)에서 6×로 올린 근거는 전략의
    # `_bar_count` 주석(교차검증에서 확인된 EMA/ATR 시드 편향).
    s = _strategy(n_fast=8, n_slow=21, n_atr=14)
    needs = s.requirements()
    assert needs.bars == ((SIG, "15m", 126),)   # max(6*21, 21+14+10) = max(126,45) = 126


def test_requirements_adds_daily_bars_only_when_day_filter_or_win_table():
    plain = _strategy().requirements()
    assert (SIG, "1d") not in {(sym, iv) for sym, iv, _ in plain.bars}

    with_day_filter = _strategy(day_filter=True).requirements()
    assert (SIG, "1d", 30) in with_day_filter.bars

    with_win_table = _strategy(win_table={"edges": {}, "buckets": {}}).requirements()
    assert (SIG, "1d", 30) in with_win_table.bars


@pytest.mark.parametrize("overrides", [
    {"n_fast": 4, "n_slow": 4},           # n_fast는 n_slow보다 작아야 한다
    {"n_fast": 5, "n_slow": 4},
    {"n_atr": 0},
    {"k_min": -0.1},
    {"stop_atr_mult": 0},
    {"trail_atr_mult": -0.1},
    {"min_stop_bp": -1},
    {"warmup_min": -1},
    {"no_entry_min": -1},
    {"eod_exit_min": 0},
    {"cooldown_bars": -1},
    {"max_entries_per_day": 0},
    {"gap_min": -0.1},
    {"or_atr_min": -0.1},
    {"target_weight": 0},
    {"target_weight": 1.5},
    {"interval_minutes": 0},
    {"long_symbol": SHORT},               # long == short
    {"short_symbol": ""},                 # 빈 심볼
    {"long_symbol": "005930"},            # 시장 불일치(KR)
    {"bar_interval_minutes": 5},          # interval_minutes(15 기본)와 불일치
])
def test_invalid_params_raise(overrides):
    with pytest.raises(ValueError):
        _strategy(**overrides)


# ============================================================ 진입


def test_up_entry_emits_enter_long_for_long_symbol_with_stop_in_state_update():
    s = _strategy()
    decision = s.decide(_snap(), {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.symbol == LONG
    assert sig.strategy_id == "letf_pair"
    assert sig.target_weight == pytest.approx(0.5)
    assert sig.stop == pytest.approx(EXPECTED_LONG_STOP, rel=1e-6)
    assert sig.state_update["entry"] == pytest.approx(LONG_PRICE)
    assert sig.state_update["stop"] == pytest.approx(EXPECTED_LONG_STOP, rel=1e-6)
    assert sig.state_update["direction"] == "long"
    assert sig.state_update["session"] == DAY.isoformat()
    assert sig.state_update["strategy"] == "letf_pair"
    assert decision.next_state["entries_today"] == 1
    assert decision.next_state["last_reject"] == {}


def test_down_entry_emits_enter_long_for_short_symbol():
    s = _strategy()
    decision = s.decide(_snap(today_rows=DOWN_TODAY_ROWS, quotes=_quotes(**{SIG: EXPECTED_DOWN_CLOSE})), {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.symbol == SHORT
    assert sig.stop == pytest.approx(EXPECTED_SHORT_STOP, rel=1e-6)
    assert sig.state_update["direction"] == "short"


def test_k_min_hysteresis_blocks_weak_signal():
    """|strength|≈0.672 < k_min=0.9 → 방향 자체가 NEUTRAL로 떨어져 무진입(정상
    대기, 사유 없음)."""
    s = _strategy(k_min=0.9)
    decision = s.decide(_snap(), {})
    assert decision.signals == ()


def test_bullish_ema_with_close_below_vwap_does_not_enter():
    """EMA는 여전히 강세(strength>0)지만 종가가 VWAP 아래면 UP이 성립하지 않는다
    — VWAP 조건은 EMA 조건과 독립적으로 반드시 충족돼야 한다."""
    s = _strategy()
    decision = s.decide(_snap(today_rows=NEUTRAL_TODAY_ROWS, quotes=_quotes(**{SIG: 101.3})), {})
    assert decision.signals == ()


def test_stop_too_tight_is_rejected():
    s = _strategy(min_stop_bp=100_000)
    decision = s.decide(_snap(), {})
    assert decision.signals == ()
    assert "손절폭" in decision.next_state["last_reject"][LONG]


def test_missing_bars_records_reject_reason():
    s = _strategy()
    decision = s.decide(_snap(bars_empty=True), {})
    assert decision.signals == ()
    assert decision.next_state["last_reject"][SIG] == "지표 계산 불가(데이터 부족)"


def test_no_entry_when_indicators_show_neutral_direction():
    """지표 자체가 완전한 NEUTRAL(강세도 약세도 아님)이면 정상 대기 — 사유
    없음. `NEUTRAL_TODAY_ROWS`는 이미 VWAP 조건 하나로 NEUTRAL이 되므로 재사용."""
    s = _strategy()
    decision = s.decide(_snap(today_rows=NEUTRAL_TODAY_ROWS, quotes=_quotes(**{SIG: 101.3})), {})
    assert decision.signals == ()
    assert SIG not in decision.next_state["last_reject"]


# -------------------------------------------------- 진입창(warmup/no_entry_min)


def test_no_entry_before_warmup_elapses():
    s = _strategy(warmup_min=60)
    decision = s.decide(_snap(), {})
    assert decision.signals == ()
    assert decision.next_state["last_reject"] == {}  # 정상 대기 — 사유 없음


def test_no_entry_within_no_entry_min_of_close():
    s = _strategy(no_entry_min=30)
    decision = s.decide(_snap(mtc=10.0), {})
    assert decision.signals == ()
    assert "진입창 종료" in decision.next_state["last_reject"][SIG]


# -------------------------------------------------- max_entries_per_day


def test_max_entries_per_day_blocks_further_entries():
    s = _strategy(max_entries_per_day=1)
    state = {"session_date": {"US": DAY.isoformat()}, "entries_today": 1,
             "last_reject": {}, "last_stop": {}}
    decision = s.decide(_snap(), state)
    assert decision.signals == ()
    assert "하루 진입 상한" in decision.next_state["last_reject"][SIG]


# -------------------------------------------------- 재진입 쿨다운


def test_cooldown_blocks_same_direction_reentry_before_bars_elapse():
    """전일 마지막 봉 시점에 롱 손절이 났다고 가정 — 당일 3개 봉(09:30/09:45/
    10:00)이 그 뒤에 오므로 elapsed=3. cooldown_bars=4면 아직 부족해 차단."""
    s = _strategy(cooldown_bars=4)
    state = {"last_stop": {"long": _bars(None).index[-1].isoformat()}}
    decision = s.decide(_snap(), state)
    assert decision.signals == ()
    assert "쿨다운" in decision.next_state["last_reject"][SIG]


def test_cooldown_allows_reentry_once_enough_bars_elapsed():
    s = _strategy(cooldown_bars=3)
    state = {"last_stop": {"long": _bars(None).index[-1].isoformat()}}
    decision = s.decide(_snap(), state)
    assert len(decision.signals) == 1


def test_cooldown_does_not_block_opposite_direction():
    """반대 방향 재진입은 쿨다운 없이 즉시 허용된다 — `last_stop`에 "short"만
    있고 지금은 "long" 방향이 뜬 상황."""
    s = _strategy(cooldown_bars=100)
    state = {"last_stop": {"short": _bars(None).index[-1].isoformat()}}
    decision = s.decide(_snap(), state)
    assert len(decision.signals) == 1


# -------------------------------------------------- day_filter


def test_day_filter_passes_via_opening_range_atr_ratio():
    """OR/ATR14=0.875 ≥ or_atr_min=0.8, 갭 조건은 통과 불가능하게 눌러 OR
    단독으로 통과함을 고정한다."""
    s = _strategy(day_filter=True, gap_min=100.0, or_atr_min=0.8)
    decision = s.decide(_snap(daily=_daily()), {})
    assert len(decision.signals) == 1


def test_day_filter_rejects_when_both_gap_and_or_conditions_fail():
    s = _strategy(day_filter=True, gap_min=100.0, or_atr_min=2.0)
    decision = s.decide(_snap(daily=_daily()), {})
    assert decision.signals == ()
    assert "day_filter" in decision.next_state["last_reject"][SIG]


def test_day_filter_passes_via_gap_when_or_condition_fails():
    gapped_rows = [dict(UP_TODAY_ROWS[0]), dict(UP_TODAY_ROWS[1]), dict(UP_TODAY_ROWS[2])]
    gapped_rows[0]["open"] = 105.0   # 전일종가(100) 대비 +5% 갭업
    s = _strategy(day_filter=True, gap_min=0.03, or_atr_min=2.0)
    decision = s.decide(_snap(today_rows=gapped_rows, daily=_daily()), {})
    assert len(decision.signals) == 1


def test_day_filter_rejects_when_daily_bars_missing():
    s = _strategy(day_filter=True)
    decision = s.decide(_snap(daily=_daily(empty=True)), {})
    assert decision.signals == ()
    assert "day_filter" in decision.next_state["last_reject"][SIG]


# -------------------------------------------------- win_table


def test_win_table_negative_bucket_rejects_entry():
    win_table = {
        "edges": {"strength": [0.3, 0.9]},   # |0.672| → t2
        "buckets": {"above|09:30-10:30|t2": {"n": 30, "mean_bp": -5.0}},
    }
    s = _strategy(win_table=win_table)
    decision = s.decide(_snap(daily=_daily_rising()), {})
    assert decision.signals == ()
    assert "win-table" in decision.next_state["last_reject"][LONG]


def test_win_table_missing_bucket_allows_entry():
    win_table = {"edges": {"strength": [0.3, 0.9]}, "buckets": {}}
    s = _strategy(win_table=win_table)
    decision = s.decide(_snap(daily=_daily_rising()), {})
    assert len(decision.signals) == 1


def test_win_table_bucket_below_n_threshold_allows_entry():
    """n<30이면 표본이 적어 판단하지 않는다(스펙 n≥30 조건) — 부호가 음수여도
    통과."""
    win_table = {
        "edges": {"strength": [0.3, 0.9]},
        "buckets": {"above|09:30-10:30|t2": {"n": 5, "mean_bp": -50.0}},
    }
    s = _strategy(win_table=win_table)
    decision = s.decide(_snap(daily=_daily_rising()), {})
    assert len(decision.signals) == 1


# ============================================================ 보유 관리


def test_flip_while_holding_long_emits_exit_and_enter_short_when_switch_true():
    s = _strategy(switch=True)
    lot = _lot(entry=48.0, stop=10.0, direction="long")
    decision = s.decide(
        _snap(today_rows=DOWN_TODAY_ROWS, quotes=_quotes(**{SIG: EXPECTED_DOWN_CLOSE}),
              lots={LONG: lot}),
        {},
    )
    actions = [(sig.action, sig.symbol) for sig in decision.signals]
    assert (SignalAction.EXIT_LONG, LONG) in actions
    assert (SignalAction.ENTER_LONG, SHORT) in actions
    assert len(decision.signals) == 2


def test_flip_while_holding_long_emits_only_exit_when_switch_false():
    s = _strategy(switch=False)
    lot = _lot(entry=48.0, stop=10.0, direction="long")
    decision = s.decide(
        _snap(today_rows=DOWN_TODAY_ROWS, quotes=_quotes(**{SIG: EXPECTED_DOWN_CLOSE}),
              lots={LONG: lot}),
        {},
    )
    assert len(decision.signals) == 1
    assert decision.signals[0].action is SignalAction.EXIT_LONG
    assert decision.signals[0].symbol == LONG


def test_flip_while_holding_short_emits_exit_and_enter_long_when_switch_true():
    s = _strategy(switch=True)
    lot = _lot(entry=18.0, stop=10.0, direction="short")  # SHORT_PRICE(20.0)보다 낮게 — 손절에 안 걸리게
    decision = s.decide(
        _snap(today_rows=UP_TODAY_ROWS, quotes=_quotes(**{SIG: EXPECTED_UP_CLOSE}),
              lots={SHORT: lot}),
        {},
    )
    actions = [(sig.action, sig.symbol) for sig in decision.signals]
    assert (SignalAction.EXIT_LONG, SHORT) in actions
    assert (SignalAction.ENTER_LONG, LONG) in actions


def test_exit_on_neutral_true_closes_position():
    s = _strategy(exit_on_neutral=True)
    lot = _lot(entry=48.0, stop=10.0, direction="long")
    decision = s.decide(
        _snap(today_rows=NEUTRAL_TODAY_ROWS, quotes=_quotes(**{SIG: 101.3}), lots={LONG: lot}),
        {},
    )
    assert len(decision.signals) == 1
    assert decision.signals[0].action is SignalAction.EXIT_LONG
    assert "중립" in decision.signals[0].reason


def test_exit_on_neutral_false_keeps_holding():
    s = _strategy(exit_on_neutral=False)
    lot = _lot(entry=48.0, stop=10.0, direction="long")
    decision = s.decide(
        _snap(today_rows=NEUTRAL_TODAY_ROWS, quotes=_quotes(**{SIG: 101.3}), lots={LONG: lot}),
        {},
    )
    assert decision.signals == ()


def test_stop_exit_when_price_falls_to_stop():
    s = _strategy()
    lot = _lot(entry=48.0, stop=45.0, direction="long")
    decision = s.decide(_snap(quotes=_quotes(**{LONG: 44.9}), lots={LONG: lot}), {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.action is SignalAction.EXIT_LONG
    assert "손절" in sig.reason


def test_eod_flatten_exits_before_close():
    s = _strategy()
    lot = _lot(entry=48.0, stop=10.0, direction="long")
    decision = s.decide(_snap(mtc=5.0, cadence=15.0, lots={LONG: lot}), {})
    assert len(decision.signals) == 1
    assert decision.signals[0].action is SignalAction.EXIT_LONG
    assert "EoD 청산" in decision.signals[0].reason


def test_overnight_false_liquidates_carried_lot_at_session_roll():
    s = _strategy(overnight=False)
    lot = _lot(entry=48.0, stop=10.0, direction="long", session=PREV.isoformat())
    decision = s.decide(_snap(lots={LONG: lot}), {})
    assert len(decision.signals) == 1
    assert "오버나잇 금지" in decision.signals[0].reason


def test_overnight_true_does_not_liquidate_carried_lot():
    s = _strategy(overnight=True)
    lot = _lot(entry=48.0, stop=10.0, direction="long", session=PREV.isoformat())
    decision = s.decide(_snap(mtc=200.0, lots={LONG: lot}), {})
    # 오버나잇 허용이면 세션 롤 강제청산도, EoD 청산도 걸리지 않는다(신호가
    # 반전되지 않는 한 계속 보유) — UP 시나리오라 방향은 여전히 long과 일치.
    assert decision.signals == ()


def test_held_symbol_gets_no_new_entry_signal():
    s = _strategy()
    lot = _lot(entry=48.0, stop=10.0, direction="long")
    actions = {sig.action for sig in s.decide(_snap(lots={LONG: lot}), {}).signals}
    # 보유 중(방향 유지, UP 시나리오)이므로 손절/EoD/반전 어느 것도 안 걸려 무신호.
    assert SignalAction.ENTER_LONG not in actions


# ============================================================ 순수성 / 배선


def test_decide_does_not_mutate_state_or_lots():
    s = _strategy()
    state = {
        "session_date": {"US": PREV.isoformat()}, "entries_today": 0,
        "last_reject": {SIG: "예전 사유"}, "last_stop": {"long": "2020-01-01T00:00:00-05:00"},
    }
    lots: dict = {}
    state_copy, lots_copy = copy.deepcopy(state), copy.deepcopy(lots)
    s.decide(_snap(lots=lots), state)
    assert state == state_copy
    assert lots == lots_copy


def test_session_roll_clears_day_scoped_state_and_reenables_entry():
    s = _strategy(max_entries_per_day=1)
    state = {"session_date": {"US": PREV.isoformat()}, "entries_today": 1,
             "last_reject": {SIG: "예전 사유"}, "last_stop": {}}
    decision = s.decide(_snap(), state)
    assert decision.next_state["session_date"]["US"] == DAY.isoformat()
    assert decision.next_state["entries_today"] == 1  # 오늘의 새 진입 1건으로 다시 채워짐
    assert len(decision.signals) == 1


def test_shell_wires_the_pure_strategy_and_passes_win_table_through():
    win_table = {"edges": {}, "buckets": {}}
    shell = LetfPairShell([], dict(BASE_PARAMS), market="US", id="letf_pair_qqq", win_table=win_table)
    assert shell.id == "letf_pair_qqq"
    assert set(shell.symbols) == {SIG, LONG, SHORT}
    assert shell.inner.win_table is win_table


# ============================================================ 손절/익절 규칙 확장
# (2026-09-05, 소유자 청산 규칙 확장 — stop_mode/pct 손절, 익절 사다리, 플로어 청산)
#
# tp1_pct=0.1 기준 목표가는 부동소수점 상 48.0*1.1=52.800000000000004다 — 테스트
# 퀴트가는 그 문턱을 확실히 넘도록(53.0) 여유를 둔다(경계값에 float 오차로
# 걸리지 않게).

_TP1_LOT = _lot(entry=48.0, stop=40.0, direction="long")


def test_pct_stop_mode_uses_fixed_percentage_from_entry():
    s = _strategy(stop_mode="pct", stop_pct=0.1)
    decision = s.decide(_snap(), {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.stop == pytest.approx(LONG_PRICE * (1 - 0.1))
    assert sig.state_update["stop"] == pytest.approx(LONG_PRICE * 0.9)


def test_tp1_partial_emits_scale_out_with_fraction_and_records_tp1_price():
    s = _strategy(tp1_pct=0.1, tp1_fraction=0.4)
    decision = s.decide(_snap(quotes=_quotes(**{LONG: 53.0}), lots={LONG: _TP1_LOT}), {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.action is SignalAction.SCALE_OUT
    assert sig.exit_fraction == pytest.approx(0.4)
    assert sig.state_update["tp1_done"] is True
    assert sig.state_update["tp1_price"] == pytest.approx(53.0)
    assert "부분 익절" in sig.reason


def test_tp2_full_exit_closes_remainder_even_without_prior_tp1():
    """가격이 1단계를 건너뛰고 곧장 2단계를 넘어도(갭 등) 잔량 전량 청산한다."""
    s = _strategy(tp1_pct=0.1, tp2_pct=0.2)
    decision = s.decide(_snap(quotes=_quotes(**{LONG: 48.0 * 1.2 + 0.5}), lots={LONG: _TP1_LOT}), {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.action is SignalAction.EXIT_LONG
    assert sig.exit_fraction == pytest.approx(1.0)
    assert "익절(2단계)" in sig.reason


def test_floor_exit_after_tp1_when_price_drops_below_tp1_price():
    s = _strategy(tp1_pct=0.1, tp1_fraction=0.4)  # tp_floor_exit는 사다리 켜짐 → 기본 true
    assert s.tp_floor_exit is True
    lot_after_tp1 = {**_TP1_LOT, "tp1_done": True, "tp1_price": 52.0}
    decision = s.decide(_snap(quotes=_quotes(**{LONG: 51.0}), lots={LONG: lot_after_tp1}), {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.action is SignalAction.EXIT_LONG
    assert "부분익절 가격 이탈" in sig.reason


def test_floor_exit_disabled_when_explicitly_turned_off():
    s = _strategy(tp1_pct=0.1, tp1_fraction=0.4, tp_floor_exit=False)
    lot_after_tp1 = {**_TP1_LOT, "tp1_done": True, "tp1_price": 52.0}
    decision = s.decide(_snap(quotes=_quotes(**{LONG: 51.0}), lots={LONG: lot_after_tp1}), {})
    assert decision.signals == ()


def test_tp_floor_to_entry_raises_stop_on_tp1_fill():
    s = _strategy(tp1_pct=0.1, tp1_fraction=0.4, tp_floor_to_entry=True)
    decision = s.decide(_snap(quotes=_quotes(**{LONG: 53.0}), lots={LONG: _TP1_LOT}), {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.action is SignalAction.SCALE_OUT
    assert sig.state_update["stop"] == pytest.approx(_TP1_LOT["entry"])


def test_tp1_pct_and_tp_atr_mult_are_mutually_exclusive():
    with pytest.raises(ValueError):
        _strategy(tp1_pct=0.1, tp_atr_mult=0.5)


def test_target_is_set_on_enter_signal_only_when_tp2_is_a_standalone_full_exit():
    """1단계 없이 2단계만 있으면 target을 실어 봉내 체결기가 쓸 수 있게 한다."""
    s = _strategy(tp2_pct=0.1)
    decision = s.decide(_snap(), {})
    assert decision.signals[0].target == pytest.approx(LONG_PRICE * 1.1)


def test_target_is_none_on_enter_signal_when_a_two_step_ladder_is_configured():
    s = _strategy(tp1_pct=0.05, tp2_pct=0.1)
    decision = s.decide(_snap(), {})
    assert decision.signals[0].target is None


def test_defaults_are_unchanged_by_the_new_exit_rule_params():
    """새 파라미터를 전혀 건드리지 않은 기존 시나리오는 이전과 완전히 같은
    Decision을 낸다 — `test_up_entry_emits_enter_long_...`과 같은 기준
    시나리오를 재사용해 stop/target/state_update를 다시 고정한다."""
    s = _strategy()
    assert s.stop_mode == "atr"
    assert s.tp1_pct == 0.0 and s.tp2_pct == 0.0 and s.tp_atr_mult == 0.0
    assert s.tp_floor_exit is False
    assert s.tp_floor_to_entry is False

    decision = s.decide(_snap(), {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.symbol == LONG
    assert sig.stop == pytest.approx(EXPECTED_LONG_STOP, rel=1e-6)
    assert sig.target is None
    assert sig.state_update["entry"] == pytest.approx(LONG_PRICE)
    assert sig.state_update["stop"] == pytest.approx(EXPECTED_LONG_STOP, rel=1e-6)


def test_defaults_are_unchanged_when_new_params_passed_explicitly_at_documented_defaults():
    s = _strategy(
        stop_mode="atr", stop_pct=0.03, tp1_pct=0.0, tp1_fraction=0.5,
        tp2_pct=0.0, tp_atr_mult=0.0, tp_floor_to_entry=False,
    )
    decision = s.decide(_snap(), {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.stop == pytest.approx(EXPECTED_LONG_STOP, rel=1e-6)
    assert sig.target is None
