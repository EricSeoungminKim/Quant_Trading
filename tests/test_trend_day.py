"""`TrendDayPureStrategy`(15분봉 추세일 지속) 규칙 고정.

레거시 쌍둥이가 없는 **신규 전략**이라 동치 비교 대상이 없다. `StrategySnapshot`을
손으로 조립해 `decide()`를 직접 부른다 — `tests/test_orb_rvol.py` 와 같은 방식이다.

## 기준 시나리오 (US, 2026-01-05 월요일, now=10:15 ET)

- 오늘 15분봉 3개(09:30/09:45/10:00). 개장 레인지(첫 2봉) 고가 **102.5** /
  저가 **98.5** → 폭 4.0.
- 일봉 20개는 고가 102 / 저가 98 / 종가 100 → **ATR14 = 4.0**, 평균 거래량
  2,000,000(US 하한 1,000,000 통과), 전일 종가 100.0.
- 4.0 > 0.8 × 4.0 = 3.2 → **추세일 게이트 통과**(딱 걸치지 않게 여유를 뒀다).
- 세션 VWAP = 세 봉 전형가의 거래량가중평균(거래량 동일) = **101.667**.
- 마지막 완성봉 종가 103.2 > OR 고가 102.5 이고 > VWAP → 진입.
- 손절 = VWAP − 0.25 × 4.0 = **100.667**. 진입가 103.5 기준 약 273bp
  (기본 `min_stop_bp` 40 통과).
- 국면 대리 지수 QQQ 일봉은 100→119 로 오르는 20개 → 종가 119 > 20일선 109.5.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.strategy.trend_day import TrendDayPureStrategy, TrendDayShell

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DAY = date(2026, 1, 5)    # 월요일
PREV = date(2026, 1, 2)   # 금요일
US_SYM = "TSTU"
KR_SYM = "005930"
US_PROXY = "QQQ"
KR_PROXY = "069500"

OR_HIGH = 102.5
OR_LOW = 98.5
ATR14 = 4.0
VWAP = 101.666666666  # 세 봉 전형가 평균(거래량 동일)
STOP = VWAP - 0.25 * ATR14
ENTRY_PRICE = 103.5
_COLUMNS = ["open", "high", "low", "close", "volume"]


# ============================================================ 합성 봉 조립


def _tz(market: str) -> ZoneInfo:
    return NY if market == "US" else KST


def _open_time(market: str) -> dtime:
    return dtime(9, 30) if market == "US" else dtime(9, 0)


def _bars_15m(
    market: str = "US", *, n_bars: int = 3, or_high: float = OR_HIGH,
    or_low: float = OR_LOW, last_close: float = 103.2, day_open: float = 100.0,
    volume: float = 1000.0, empty: bool = False,
) -> pd.DataFrame:
    """오늘 15분봉만 조립한다(과거 세션 봉은 이 전략이 보지 않는다 — 개장
    레인지·VWAP·돌파 판정 전부 **당일 세션** 안에서 끝난다).

    첫 두 봉이 개장 레인지(`or_minutes=30` ÷ 15분)를 만들고, 세 번째 봉이
    돌파 판정 대상이다. `or_high`/`or_low` 는 두 봉에 나눠 심는다.
    """
    tz, open_t = _tz(market), _open_time(market)
    if empty:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], tz=tz))
    start = datetime.combine(DAY, open_t, tzinfo=tz)
    rows = [
        # 09:30 — 저가 쪽. 전형가 (102 + 98.5 + 101.5)/3
        {"open": day_open, "high": 102.0, "low": or_low, "close": 101.5, "volume": volume},
        # 09:45 — 고가 쪽. 전형가 (102.5 + 100 + 102)/3
        {"open": 101.5, "high": or_high, "low": 100.0, "close": 102.0, "volume": volume},
        # 10:00 — 돌파봉. 전형가 (103.5 + 101.8 + 103.2)/3
        {"open": 102.0, "high": 103.5, "low": 101.8, "close": last_close, "volume": volume},
    ][:n_bars]
    idx = [start + timedelta(minutes=15 * i) for i in range(len(rows))]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def _daily(*, n: int = 20, high: float = 102.0, low: float = 98.0,
           close: float = 100.0, volume: float = 2e6, market: str = "US") -> pd.DataFrame:
    tz = _tz(market)
    if n <= 0:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], tz=tz))
    idx = pd.date_range(end=datetime.combine(PREV, dtime(0, 0), tzinfo=tz),
                        periods=n, freq="1D")
    return pd.DataFrame(
        {"open": [close] * n, "high": [high] * n, "low": [low] * n,
         "close": [close] * n, "volume": [volume] * n},
        index=idx,
    )


def _proxy_daily(*, n: int = 20, rising: bool = True, market: str = "US") -> pd.DataFrame:
    """국면 대리 지수 일봉. `rising=True` 면 종가가 20일선 위(상승 국면)."""
    tz = _tz(market)
    if n <= 0:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], tz=tz))
    closes = [100.0 + i for i in range(n)] if rising else [100.0 - i for i in range(n)]
    idx = pd.date_range(end=datetime.combine(PREV, dtime(0, 0), tzinfo=tz),
                        periods=n, freq="1D")
    return pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
         "close": closes, "volume": [1e7] * n},
        index=idx,
    )


def _snap(
    *, market: str = "US", specs: dict[str, dict] | None = None,
    now: datetime | None = None, mtc: float = 165.0, cadence: float = 15.0,
    lots: dict | None = None, market_open: bool = True,
    proxy: dict | None = None, proxy_missing: bool = False,
) -> StrategySnapshot:
    """`specs`: 심볼 → {price, bars15(kwargs), daily(kwargs)}."""
    tz = _tz(market)
    if now is None:
        now = datetime.combine(
            DAY, dtime(10, 15) if market == "US" else dtime(9, 45), tzinfo=tz
        )
    specs = specs or {US_SYM if market == "US" else KR_SYM: {"price": ENTRY_PRICE}}
    bars: dict[tuple[str, str], pd.DataFrame] = {}
    quotes: dict[str, Quote] = {}
    for symbol, spec in specs.items():
        bars[(symbol, "15m")] = _bars_15m(market, **spec.get("bars15", {}))
        bars[(symbol, "1d")] = _daily(market=market, **spec.get("daily", {}))
        price = spec.get("price")
        if price is not None:
            quotes[symbol] = Quote(symbol=symbol, ts=now, price=price)
    if not proxy_missing:
        proxy_sym = US_PROXY if market == "US" else KR_PROXY
        bars[(proxy_sym, "1d")] = _proxy_daily(market=market, **(proxy or {}))
    return StrategySnapshot(
        now=now,
        market_open={market: market_open},
        minutes_to_close={market: mtc},
        cadence_minutes=cadence,
        bars=bars,
        quotes=quotes,
        lots=lots if lots is not None else {},
    )


def _strategy(symbols=(US_SYM,), **params) -> TrendDayPureStrategy:
    return TrendDayPureStrategy(list(symbols), dict(params))


def _lot(entry: float = ENTRY_PRICE, stop: float | None = STOP,
         session: str | None = None) -> dict:
    return {
        "entry": entry, "stop": stop,
        "session": session or DAY.isoformat(),
        "entered_at": datetime.combine(DAY, dtime(10, 15), tzinfo=NY).isoformat(),
        "strategy": "trend_day",
    }


# ============================================================ 계약 / 생성자


def test_requirements_declares_15m_daily_and_regime_proxy_bars():
    s = _strategy(symbols=(US_SYM, KR_SYM))
    needs = s.requirements()
    keys = {(sym, interval) for sym, interval, _ in needs.bars}
    assert keys == {
        (US_SYM, "15m"), (US_SYM, "1d"), (KR_SYM, "15m"), (KR_SYM, "1d"),
        (US_PROXY, "1d"), (KR_PROXY, "1d"),
    }
    counts = {(sym, interval): n for sym, interval, n in needs.bars}
    assert counts[(US_SYM, "15m")] == 26 * 2 + 10   # 두 세션치 + 여유
    assert counts[(US_SYM, "1d")] == 14 + 5
    assert counts[(US_PROXY, "1d")] == 20 + 5
    # 거래 심볼이 US 뿐이면 KR 대리 지수는 요청하지 않는다(콜드 페치 절약).
    assert {sym for sym, _, _ in _strategy().requirements().bars} == {US_SYM, US_PROXY}
    assert set(needs.quotes) == {US_SYM, KR_SYM}   # 대리 지수는 사지 않는다
    assert needs.needs_positions
    assert not needs.fetch_when_closed


@pytest.mark.parametrize("params", [
    {"or_minutes": 0},
    {"or_minutes": 20},        # 15의 배수가 아니다 — 봉 경계에 안 맞는다
    {"or_atr_mult": 0},
    {"stop_atr_mult": 0},
    {"atr_period": 0},
    {"avg_volume_days": 0},
    {"regime_ma_days": 1},
    {"entry_window_min": 30},  # or_minutes 이하
    {"eod_exit_min": 0},
    {"target_weight": 0},
    {"target_weight": 1.5},
])
def test_invalid_params_raise(params):
    with pytest.raises(ValueError):
        _strategy(**params)


# ============================================================ 진입


def test_entry_fires_on_trend_day_breakout_above_or_high_and_vwap():
    s = _strategy()
    decision = s.decide(_snap(), {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.symbol == US_SYM
    assert sig.strategy_id == "trend_day"
    assert sig.target_weight == pytest.approx(0.5)
    # 손절은 **진입가가 아니라 VWAP** 기준이다 — 이 전략의 핵심 설계.
    assert sig.stop == pytest.approx(STOP, abs=1e-6)
    assert sig.state_update["entry"] == pytest.approx(ENTRY_PRICE)
    assert sig.state_update["stop"] == pytest.approx(STOP, abs=1e-6)
    assert sig.state_update["session"] == DAY.isoformat()
    assert sig.state_update["strategy"] == "trend_day"
    assert decision.next_state["entries_today"][US_SYM] == DAY.isoformat()
    assert US_SYM not in decision.next_state["last_reject"]


def test_no_entry_when_opening_range_is_narrower_than_atr_threshold():
    """개장 레인지가 `or_atr_mult × ATR14` 이하면 추세일이 아니다 — 이 게이트가
    이 전략의 존재 이유(적고 큰 거래)라 반드시 고정한다."""
    s = _strategy()
    # OR 폭을 4.0 → 1.0 으로 좁힌다(0.8×ATR=3.2 미만).
    snap = _snap(specs={US_SYM: {"price": ENTRY_PRICE,
                                 "bars15": {"or_high": 101.0, "or_low": 100.0}}})
    decision = s.decide(snap, {})
    assert decision.signals == ()
    assert "추세일 아님" in decision.next_state["last_reject"][US_SYM]


def test_no_entry_when_last_close_is_below_vwap_even_if_above_or_high():
    """OR 고가만 넘고 VWAP 아래면 진입하지 않는다(둘 다 요구한다).

    OR 고가를 아주 낮춰 "돌파는 했지만 VWAP 아래"인 상태를 만든다."""
    s = _strategy(or_atr_mult=0.01)
    snap = _snap(specs={US_SYM: {"price": ENTRY_PRICE,
                                 "bars15": {"or_high": 99.0, "last_close": 99.5}}})
    decision = s.decide(snap, {})
    assert decision.signals == ()
    # "아직 돌파 전"은 정상 대기라 사유를 남기지 않는다.
    assert US_SYM not in decision.next_state["last_reject"]


def test_missing_15m_bars_records_reject_reason():
    s = _strategy()
    snap = _snap(specs={US_SYM: {"price": ENTRY_PRICE, "bars15": {"empty": True}}})
    decision = s.decide(snap, {})
    assert decision.signals == ()
    assert decision.next_state["last_reject"][US_SYM] == "당일 15분봉 확인 불가"


def test_missing_daily_bars_records_reject_reason():
    s = _strategy()
    snap = _snap(specs={US_SYM: {"price": ENTRY_PRICE, "daily": {"n": 0}}})
    decision = s.decide(snap, {})
    assert decision.signals == ()
    assert "평균 거래량" in decision.next_state["last_reject"][US_SYM]


# ============================================================ 국면 / 갭 게이트


def test_down_regime_blocks_every_entry_in_that_market():
    """대리 지수가 20일선 아래면 그 시장은 통째로 쉰다 — 소유자 요구
    "하락장에서는 조금만 잃고"의 코드 표현."""
    s = _strategy()
    decision = s.decide(_snap(proxy={"rising": False}), {})
    assert decision.signals == ()
    assert "하락 국면" in decision.next_state["last_reject"][US_SYM]


def test_missing_regime_proxy_is_a_reject_not_a_pass():
    """확인 불가는 통과가 아니라 거부다 — 국면을 모른 채 넓은 손절 전략을 켜는
    것이 이 전략에서 가장 비싼 실수다."""
    s = _strategy()
    decision = s.decide(_snap(proxy_missing=True), {})
    assert decision.signals == ()
    assert "국면 확인 불가" in decision.next_state["last_reject"][US_SYM]


def test_gap_down_blocks_entry_and_can_be_switched_off():
    """당일 시가 < 전일 종가면 진입하지 않는다. `require_gap_up: false` 면 통과."""
    gapped = {US_SYM: {"price": ENTRY_PRICE, "bars15": {"day_open": 99.0}}}
    blocked = _strategy().decide(_snap(specs=gapped), {})
    assert blocked.signals == ()
    assert "갭다운" in blocked.next_state["last_reject"][US_SYM]

    allowed = _strategy(require_gap_up=False).decide(_snap(specs=gapped), {})
    assert len(allowed.signals) == 1


def test_no_entry_before_opening_range_closes():
    """개장 레인지가 닫히기 전(경과 < or_minutes)에는 판정 자체를 하지 않는다 —
    정상 대기라 거부 사유도 남기지 않는다."""
    s = _strategy()
    now = datetime.combine(DAY, dtime(9, 45), tzinfo=NY)   # 개장 후 15분
    decision = s.decide(_snap(now=now, specs={US_SYM: {"price": ENTRY_PRICE,
                                                       "bars15": {"n_bars": 1}}}), {})
    assert decision.signals == ()
    assert decision.next_state["last_reject"] == {}


def test_no_entry_after_entry_window_closes():
    s = _strategy()
    now = datetime.combine(DAY, dtime(15, 30), tzinfo=NY)   # 개장 후 360분 > 330
    decision = s.decide(_snap(now=now, mtc=30.0), {})
    assert decision.signals == ()
    assert "진입창 종료" in decision.next_state["last_reject"][US_SYM]


def test_kr_after_continuous_session_ends_makes_no_entry():
    """KR 연속매매는 15:20 에 끝난다 — 그 뒤엔 체결될 수 없는 진입을 내지 않는다."""
    s = _strategy(symbols=(KR_SYM,))
    now = datetime.combine(DAY, dtime(15, 25), tzinfo=KST)
    snap = _snap(market="KR", now=now, mtc=5.0,
                 specs={KR_SYM: {"price": ENTRY_PRICE}})
    assert s.decide(snap, {}).signals == ()


def test_one_entry_per_symbol_per_session():
    s = _strategy()
    state = {"session_date": {"US": DAY.isoformat()},
             "entries_today": {US_SYM: DAY.isoformat()}}
    decision = s.decide(_snap(), state)
    assert decision.signals == ()
    assert decision.next_state["last_reject"][US_SYM] == "1일 1회 진입 소진"


# ============================================================ 보유 관리


def test_stop_exit_when_price_falls_to_stop():
    s = _strategy()
    snap = _snap(specs={US_SYM: {"price": STOP - 0.1}},
                 lots={US_SYM: _lot()})
    decision = s.decide(snap, {})
    assert len(decision.signals) == 1
    sig = decision.signals[0]
    assert sig.action is SignalAction.EXIT_LONG
    assert "손절" in sig.reason


def test_eod_flatten_exits_before_close():
    s = _strategy()
    # mtc - cadence < eod_exit_min(3) → 마감 임박
    snap = _snap(mtc=5.0, cadence=15.0, specs={US_SYM: {"price": ENTRY_PRICE + 5}},
                 lots={US_SYM: _lot()})
    decision = s.decide(snap, {})
    assert len(decision.signals) == 1
    assert decision.signals[0].action is SignalAction.EXIT_LONG
    assert "EoD 청산" in decision.signals[0].reason


def test_lot_without_stop_still_time_exits():
    """손절가가 없는 lot(구버전 잔재 등)도 EoD 청산은 반드시 걸린다."""
    s = _strategy()
    snap = _snap(mtc=5.0, cadence=15.0, specs={US_SYM: {"price": ENTRY_PRICE}},
                 lots={US_SYM: _lot(stop=None)})
    decision = s.decide(snap, {})
    assert len(decision.signals) == 1
    assert "EoD 청산" in decision.signals[0].reason


def test_overnight_carry_is_liquidated_on_session_roll():
    s = _strategy()
    snap = _snap(specs={US_SYM: {"price": ENTRY_PRICE}},
                 lots={US_SYM: _lot(session=PREV.isoformat())})
    decision = s.decide(snap, {})
    assert len(decision.signals) == 1
    assert "오버나잇 금지" in decision.signals[0].reason


def test_held_symbol_gets_no_new_entry_signal():
    s = _strategy()
    snap = _snap(specs={US_SYM: {"price": ENTRY_PRICE}}, lots={US_SYM: _lot()})
    actions = {sig.action for sig in s.decide(snap, {}).signals}
    assert SignalAction.ENTER_LONG not in actions


# ============================================================ 순수성 / 배선


def test_decide_does_not_mutate_state_or_lots():
    s = _strategy()
    state = {"session_date": {"US": PREV.isoformat()},
             "entries_today": {US_SYM: PREV.isoformat()},
             "last_reject": {US_SYM: "예전 사유"}}
    lots = {US_SYM: _lot()}
    state_copy, lots_copy = copy.deepcopy(state), copy.deepcopy(lots)
    s.decide(_snap(lots=lots), state)
    assert state == state_copy
    assert lots == lots_copy


def test_session_roll_clears_day_scoped_state():
    s = _strategy()
    state = {"session_date": {"US": PREV.isoformat()},
             "entries_today": {US_SYM: PREV.isoformat()},
             "last_reject": {US_SYM: "예전 사유"}}
    decision = s.decide(_snap(), state)
    assert decision.next_state["session_date"]["US"] == DAY.isoformat()
    assert len(decision.signals) == 1   # 어제 소진분이 지워져 오늘 다시 진입한다


def test_shell_wires_the_pure_strategy():
    shell = TrendDayShell([US_SYM], {}, market="US", id="trend_day")
    assert shell.id == "trend_day"
    assert shell.symbols == [US_SYM]
