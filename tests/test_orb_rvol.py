"""`OrbRvolPureStrategy`(개장 레인지 돌파 + rvol 선별) 규칙 고정.

레거시 쌍둥이가 없는 **신규 전략**이라 동치 비교 대상이 없다. `StrategySnapshot`을
손으로 조립해 `decide()`를 직접 부른다 — `vol_breakout`/`mr_vwap_quiet` 테스트와
같은 방식이다.

## 기준 시나리오 (US, 2026-01-05 월요일)

- 오늘 개장 5분봉: 시가 100.0 / 고가 **101.0** / 저가 99.5 / 종가 100.8,
  거래량 2,000. 몸통 0.8 ÷ 레인지 1.5 = 53% → 도지 게이트(10%) 통과.
- 직전 14세션의 같은 개장 5분봉 거래량은 전부 1,000 → **rvol = 2.0**.
- 일봉 20개는 고가 102 / 저가 98 / 종가 100 → **ATR14 = 4.0**,
  평균 거래량 2,000,000(US 하한 1,000,000 통과).
- 진입가 101.5면 손절 = 101.5 − 0.10×4.0 = **101.1** (약 39bp — 기본
  `min_stop_bp`가 0(비활성)이라 통과한다. 왜 0인지는 모듈 docstring "논문 숫자" 2번).
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.strategy.orb_rvol import OrbRvolPureStrategy, OrbRvolShell

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DAY = date(2026, 1, 5)    # 월요일
PREV = date(2026, 1, 2)   # 금요일
US_SYM = "TSTU"
US_SYM2 = "TSTV"
KR_SYM = "005930"

OR_HIGH = 101.0
OR_LOW = 99.5
OR_OPEN = 100.0
OR_CLOSE = 100.8
PRIOR_VOLUME = 1000.0
TODAY_VOLUME = 2000.0      # rvol = 2.0
ATR14 = 4.0                # 아래 _daily() 기준
_COLUMNS = ["open", "high", "low", "close", "volume"]


# ============================================================ 합성 봉 조립


def _tz(market: str) -> ZoneInfo:
    return NY if market == "US" else KST


def _open_time(market: str) -> dtime:
    return dtime(9, 30) if market == "US" else dtime(9, 0)


def _bars_5m(
    market: str = "US", *, n_prior: int = 14, prior_volume: float = PRIOR_VOLUME,
    today_open: float = OR_OPEN, today_high: float = OR_HIGH,
    today_low: float = OR_LOW, today_close: float = OR_CLOSE,
    today_volume: float = TODAY_VOLUME, today_missing: bool = False,
) -> pd.DataFrame:
    """세션마다 개장 5분봉 1개(과거) + 오늘 개장봉 및 후속 2봉.

    전략은 세션별 **첫** 개장 이후 봉만 보므로, 과거 세션은 그 한 봉만 있으면
    충분하다 — 15세션 × 78봉을 합성하지 않는 이유다.
    """
    tz, open_t = _tz(market), _open_time(market)
    idx: list[datetime] = []
    rows: list[dict] = []
    if n_prior:
        for day in pd.bdate_range(end=DAY - timedelta(days=1), periods=n_prior).date:
            idx.append(datetime.combine(day, open_t, tzinfo=tz))
            rows.append({"open": 100.0, "high": 100.5, "low": 99.5,
                         "close": 100.3, "volume": prior_volume})
    if not today_missing:
        start = datetime.combine(DAY, open_t, tzinfo=tz)
        idx.append(start)
        rows.append({"open": today_open, "high": today_high, "low": today_low,
                     "close": today_close, "volume": today_volume})
        for k in (1, 2):
            idx.append(start + timedelta(minutes=5 * k))
            rows.append({"open": today_close, "high": today_close + 0.2,
                         "low": today_close - 0.2, "close": today_close,
                         "volume": 500.0})
    if not rows:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], tz=tz))
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


def _snap(
    *, market: str = "US", specs: dict[str, dict] | None = None,
    now: datetime | None = None, mtc: float = 195.0, cadence: float = 5.0,
    lots: dict | None = None, market_open: bool = True,
) -> StrategySnapshot:
    """`specs`: 심볼 → {price, bars5(kwargs), daily(kwargs)}."""
    tz = _tz(market)
    if now is None:
        now = datetime.combine(DAY, dtime(10, 0) if market == "US" else dtime(9, 30),
                               tzinfo=tz)
    specs = specs or {US_SYM: {}}
    bars: dict[tuple[str, str], pd.DataFrame] = {}
    quotes: dict[str, Quote] = {}
    for symbol, spec in specs.items():
        bars[(symbol, "5m")] = _bars_5m(market, **spec.get("bars5", {}))
        bars[(symbol, "1d")] = _daily(market=market, **spec.get("daily", {}))
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


def _strategy(symbols=(US_SYM,), **params) -> OrbRvolPureStrategy:
    return OrbRvolPureStrategy(list(symbols), dict(params))


def _lot(entry: float = OR_HIGH + 0.5, stop: float | None = OR_HIGH + 0.1,
         session: str | None = None) -> dict:
    return {
        "entry": entry, "stop": stop,
        "session": session or DAY.isoformat(),
        "entered_at": datetime.combine(DAY, dtime(10, 0), tzinfo=NY).isoformat(),
        "strategy": "orb_rvol",
    }


# ============================================================ 계약 / 생성자


def test_requirements_declares_5m_and_daily_bars_with_rvol_lookback():
    s = _strategy(symbols=(US_SYM, KR_SYM))
    needs = s.requirements()
    intervals = {(sym, interval) for sym, interval, _ in needs.bars}
    assert intervals == {(US_SYM, "5m"), (US_SYM, "1d"), (KR_SYM, "5m"), (KR_SYM, "1d")}
    counts = {(sym, interval): n for sym, interval, n in needs.bars}
    # (rvol_days + 1) 세션 × 78봉 + 여유 — 직전 14세션의 개장봉까지 닿아야 한다.
    assert counts[(US_SYM, "5m")] == (14 + 1) * 78 + 10
    assert counts[(US_SYM, "1d")] == 14 + 5
    assert set(needs.quotes) == {US_SYM, KR_SYM}
    assert needs.needs_positions
    assert not needs.fetch_when_closed


@pytest.mark.parametrize("params", [
    {"rvol_days": 0},
    {"rvol_min": -0.1},
    {"top_k": 0},
    {"entry_window_min": 0},
    {"stop_atr_frac": 0},
    {"atr_period": 0},
    {"avg_volume_days": 0},
    {"doji_body_frac": 1.0},
    {"doji_body_frac": -0.1},
    {"min_rvol_sessions": 0},
    {"eod_exit_min": 0},
    {"target_weight": 0},
    {"target_weight": 1.5},
    {"min_stop_bp": -1},
])
def test_invalid_params_rejected(params):
    with pytest.raises(ValueError):
        OrbRvolPureStrategy([US_SYM], params)


# ============================================================ ① OR 고가 돌파 진입


def test_entry_signal_when_price_breaks_above_opening_range_high():
    d = _strategy().decide(_snap(specs={US_SYM: {"price": OR_HIGH + 0.5}}), {})
    assert len(d.signals) == 1
    sig = d.signals[0]
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.symbol == US_SYM
    assert sig.target_weight == pytest.approx(0.5)

    entry = OR_HIGH + 0.5
    assert sig.stop == pytest.approx(entry - 0.10 * ATR14)
    assert sig.stop < entry
    assert sig.state_update["entry"] == pytest.approx(entry)
    assert sig.state_update["session"] == DAY.isoformat()
    assert sig.state_update["strategy"] == "orb_rvol"
    assert d.next_state["entries_today"][US_SYM] == DAY.isoformat()
    assert "rvol 2.00" in sig.reason


def test_no_entry_when_price_is_only_at_the_opening_range_high():
    """상향 **돌파**여야 한다 — 고가와 같으면 아직 아니다(대기, 사유 없음)."""
    d = _strategy().decide(_snap(specs={US_SYM: {"price": OR_HIGH}}), {})
    assert d.signals == ()
    assert US_SYM not in d.next_state["last_reject"]
    assert US_SYM not in d.next_state["entries_today"]


# ============================================================ ② 데이터 결손 / 도지


def test_no_entry_when_opening_bar_missing():
    d = _strategy().decide(
        _snap(specs={US_SYM: {"price": OR_HIGH + 0.5,
                              "bars5": {"today_missing": True}}}), {}
    )
    assert d.signals == ()
    assert "개장 5분봉 확인 불가" in d.next_state["last_reject"][US_SYM]


def test_doji_opening_bar_is_excluded():
    d = _strategy().decide(
        _snap(specs={US_SYM: {"price": OR_HIGH + 0.5,
                              "bars5": {"today_close": OR_OPEN + 0.05}}}), {}
    )
    assert d.signals == ()
    assert "도지" in d.next_state["last_reject"][US_SYM]


def test_zero_range_opening_bar_is_excluded():
    d = _strategy().decide(
        _snap(specs={US_SYM: {"price": OR_HIGH + 0.5,
                              "bars5": {"today_high": 100.0, "today_low": 100.0}}}), {}
    )
    assert d.signals == ()
    assert "범위 0" in d.next_state["last_reject"][US_SYM]


def test_atr_unavailable_blocks_entry():
    """일봉이 ATR 기간+1 개 미만이면 손절폭을 지어내지 않고 거부한다."""
    d = _strategy().decide(
        _snap(specs={US_SYM: {"price": OR_HIGH + 0.5, "daily": {"n": 5}}}), {}
    )
    assert d.signals == ()
    assert "ATR 계산 불가" in d.next_state["last_reject"][US_SYM]


# ============================================================ ③ rvol 게이트


def test_low_rvol_is_rejected():
    d = _strategy().decide(
        _snap(specs={US_SYM: {"price": OR_HIGH + 0.5,
                              "bars5": {"today_volume": 500.0}}}), {}
    )
    assert d.signals == ()
    reason = d.next_state["last_reject"][US_SYM]
    assert "rvol 0.50" in reason and "최소" in reason


def test_insufficient_rvol_sessions_is_rejected():
    """신규 편입 종목 — 기준일이 `min_rvol_sessions` 미만이면 그날은 보지 않는다."""
    d = _strategy().decide(
        _snap(specs={US_SYM: {"price": OR_HIGH + 0.5, "bars5": {"n_prior": 3}}}), {}
    )
    assert d.signals == ()
    assert "rvol 기준일 부족" in d.next_state["last_reject"][US_SYM]


def test_top_k_keeps_only_the_highest_rvol_names():
    """둘 다 돌파했지만 top_k=1 — rvol 이 높은 쪽만 in play 다."""
    snap = _snap(specs={
        US_SYM: {"price": OR_HIGH + 0.5, "bars5": {"today_volume": 5000.0}},   # rvol 5.0
        US_SYM2: {"price": OR_HIGH + 0.5, "bars5": {"today_volume": 1500.0}},  # rvol 1.5
    })
    d = _strategy(symbols=(US_SYM, US_SYM2), top_k=1).decide(snap, {})
    assert [s.symbol for s in d.signals] == [US_SYM]
    assert "상위 1위 밖" in d.next_state["last_reject"][US_SYM2]


# ============================================================ ④ 가격/유동성 필터


def test_min_price_filter_excludes_penny_names():
    d = _strategy(min_price={"US": 200.0}).decide(
        _snap(specs={US_SYM: {"price": OR_HIGH + 0.5}}), {}
    )
    assert d.signals == ()
    assert "저가주 제외" in d.next_state["last_reject"][US_SYM]


def test_min_avg_volume_filter_excludes_illiquid_names():
    d = _strategy().decide(
        _snap(specs={US_SYM: {"price": OR_HIGH + 0.5, "daily": {"volume": 1000.0}}}), {}
    )
    assert d.signals == ()
    assert "평균 거래량" in d.next_state["last_reject"][US_SYM]


# ============================================================ ⑤ 진입창 / min_stop_bp


def test_entry_window_closes_after_configured_minutes():
    late = datetime.combine(DAY, dtime(10, 35), tzinfo=NY)  # 개장 후 65분
    d = _strategy().decide(
        _snap(specs={US_SYM: {"price": OR_HIGH + 0.5}}, now=late), {}
    )
    assert d.signals == ()
    assert "진입창 종료" in d.next_state["last_reject"][US_SYM]


def test_min_stop_bp_gate_blocks_paper_thin_stop_when_enabled():
    """논문 손절(0.10×ATR14)은 39bp — 게이트를 100bp로 올리면 막힌다.
    기본값 0(비활성)은 첫 측정을 훼손하지 않기 위한 것이다."""
    d = _strategy(min_stop_bp=100).decide(
        _snap(specs={US_SYM: {"price": OR_HIGH + 0.5}}), {}
    )
    assert d.signals == ()
    assert "손절폭" in d.next_state["last_reject"][US_SYM]


# ============================================================ ⑥ 청산 레일


def test_eod_flatten_exit():
    snap = _snap(specs={US_SYM: {"price": OR_HIGH + 1.0}}, mtc=4.0, cadence=5.0,
                 lots={US_SYM: _lot()})
    d = _strategy().decide(snap, {})
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert "EoD 청산" in d.signals[0].reason


def test_no_eod_flatten_when_remaining_time_is_ample():
    snap = _snap(specs={US_SYM: {"price": OR_HIGH + 1.0}}, mtc=195.0, cadence=5.0,
                 lots={US_SYM: _lot()})
    assert _strategy().decide(snap, {}).signals == ()


def test_stop_loss_exit():
    stop = OR_HIGH + 0.1
    snap = _snap(specs={US_SYM: {"price": stop - 0.01}}, lots={US_SYM: _lot(stop=stop)})
    d = _strategy().decide(snap, {})
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert "손절" in d.signals[0].reason


def test_eod_flatten_exit_even_when_lot_has_no_stop():
    snap = _snap(specs={US_SYM: {"price": OR_HIGH + 1.0}}, mtc=4.0, cadence=5.0,
                 lots={US_SYM: _lot(stop=None)})
    d = _strategy().decide(snap, {})
    assert len(d.signals) == 1
    assert "EoD 청산" in d.signals[0].reason


def test_session_rollover_forces_exit_as_overnight_safety_net():
    snap = _snap(specs={US_SYM: {"price": OR_HIGH + 1.0}},
                 lots={US_SYM: _lot(session=PREV.isoformat())})
    d = _strategy().decide(snap, {})
    assert len(d.signals) == 1
    assert "오버나잇 금지" in d.signals[0].reason


# ============================================================ ⑦ 하루 1회 / 재시작


def test_one_entry_per_symbol_per_day():
    first = _strategy().decide(_snap(specs={US_SYM: {"price": OR_HIGH + 0.5}}), {})
    assert len(first.signals) == 1

    exhausted = {
        "session_date": {"US": DAY.isoformat()},
        "entries_today": {US_SYM: DAY.isoformat()},
        "last_reject": {},
    }
    again = _strategy().decide(_snap(specs={US_SYM: {"price": OR_HIGH + 3.0}}), exhausted)
    assert again.signals == ()
    assert again.next_state["last_reject"][US_SYM] == "1일 1회 진입 소진"


def test_session_roll_resets_daily_gate():
    stale = {
        "session_date": {"US": PREV.isoformat()},
        "entries_today": {US_SYM: PREV.isoformat()},
        "last_reject": {US_SYM: "낡은 사유"},
    }
    d = _strategy().decide(_snap(specs={US_SYM: {"price": OR_HIGH + 0.5}}), stale)
    assert len(d.signals) == 1
    assert d.next_state["session_date"]["US"] == DAY.isoformat()


def test_open_lot_survives_process_restart():
    """장중 재시작(2026-08-28 실사건) — 새 인스턴스, 빈 state. lot 만으로 손절이 나가야 한다."""
    stop = OR_HIGH + 0.1
    restarted = OrbRvolPureStrategy([US_SYM], {})
    snap = _snap(specs={US_SYM: {"price": stop - 0.01}}, lots={US_SYM: _lot(stop=stop)})
    d = restarted.decide(snap, {})
    assert len(d.signals) == 1
    assert "손절" in d.signals[0].reason


def test_no_duplicate_entry_after_restart_while_holding():
    restarted = OrbRvolPureStrategy([US_SYM], {})
    snap = _snap(specs={US_SYM: {"price": OR_HIGH + 3.0}}, lots={US_SYM: _lot()})
    assert restarted.decide(snap, {}).signals == ()


def test_kr_no_entry_after_continuous_close():
    """KR 연속매매는 15:20 종료 — 그 뒤엔 현재가로 체결할 수 없다."""
    strategy = _strategy(symbols=(KR_SYM,), min_price=0, min_avg_volume=0)
    snap = _snap(market="KR", specs={KR_SYM: {"price": OR_HIGH + 0.5}},
                 now=datetime.combine(DAY, dtime(15, 25), tzinfo=KST))
    assert strategy.decide(snap, {}).signals == ()


def test_closed_market_produces_no_signals():
    snap = _snap(specs={US_SYM: {"price": OR_HIGH + 0.5}}, market_open=False)
    assert _strategy().decide(snap, {}).signals == ()


# ============================================================ 순수성 / 껍질


def test_decide_does_not_mutate_input_state():
    state = {"session_date": {"US": DAY.isoformat()}, "entries_today": {}, "last_reject": {}}
    before = copy.deepcopy(state)
    _strategy().decide(_snap(specs={US_SYM: {"price": OR_HIGH + 0.5}}), state)
    assert state == before


def test_decide_does_not_mutate_snapshot_lots():
    lot = _lot()
    before = copy.deepcopy(lot)
    _strategy().decide(_snap(specs={US_SYM: {"price": OR_HIGH + 1.0}},
                             lots={US_SYM: lot}), {})
    assert lot == before


def test_shell_satisfies_strategy_protocol_wiring():
    shell = OrbRvolShell([US_SYM], {}, market="KR", id="orb_rvol")
    assert shell.id == "orb_rvol"
    assert shell.symbols == [US_SYM]
    assert hasattr(shell, "on_cycle")


def test_registered_in_strategy_registry():
    from quant.trade.strategy import STRATEGY_REGISTRY, TAG_ASSIGNMENT

    assert STRATEGY_REGISTRY["orb_rvol"] is OrbRvolShell
    assert "orb_rvol" in TAG_ASSIGNMENT["*"]
