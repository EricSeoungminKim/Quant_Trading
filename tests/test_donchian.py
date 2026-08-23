"""DonchianStrategy 단위 테스트: 진입/청산 로직, look-ahead 방지."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.ports import Context
from quant.core.models import Position, Quote, SignalAction
from quant.trade.strategy.donchian import DonchianStrategy

NY = ZoneInfo("America/New_York")


class FakeClock:
    def __init__(self, now: datetime, minutes_to_close: float | None = 120.0,
                 cadence_minutes: float = 15.0):
        self._now = now
        self._minutes_to_close = minutes_to_close
        self._cadence = cadence_minutes

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return self._minutes_to_close

    def cadence_minutes(self) -> float:
        return self._cadence

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        mtc = self.minutes_to_close(market)
        return mtc is not None and mtc - self._cadence < flatten_minutes


class FakeDataFeed:
    def __init__(self, bars: dict[str, pd.DataFrame], quotes: dict[str, float]):
        self._bars = bars
        self._quotes = quotes

    def quote(self, symbol: str) -> Quote | None:
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(NY), price=self._quotes[symbol])

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return self._bars.get(symbol, pd.DataFrame()).tail(n)


class FakeBroker:
    def __init__(self, positions: dict[str, Position]):
        self._positions = positions

    def place_order(self, order):
        raise NotImplementedError

    def positions(self) -> dict[str, Position]:
        return self._positions

    def cash(self) -> float:
        return 1_000_000.0


def _make_bars(n: int, base: float = 100.0, breakout: bool = False, low_volume: bool = False) -> pd.DataFrame:
    rows = []
    ts0 = datetime(2026, 1, 5, 9, 30, tzinfo=NY)
    for i in range(n):
        price = base + i * 0.01
        rows.append({
            "ts": ts0 + timedelta(minutes=15 * i), "open": price, "high": price + 0.05,
            "low": price - 0.05, "close": price, "volume": 1000.0,
        })
    if breakout:
        last_close = rows[-1]["close"] + 5.0
        rows[-1].update(
            close=last_close, high=last_close + 0.1, low=last_close - 1.0,
            volume=1.0 if low_volume else 5000.0,
        )
    return pd.DataFrame(rows).set_index("ts")


def _params(**overrides) -> dict:
    p = dict(
        interval_minutes=15, lookback_bars=40, volume_mult=1.5, stop_fallback_pct=1.5,
        risk_reward=2.0, max_concurrent_names=1, flatten_before_close_minutes=10,
        min_session_bars_before_entry=0,
    )
    p.update(overrides)
    return p


def _ctx(bars: pd.DataFrame, now: datetime) -> Context:
    price = float(bars["close"].iloc[-1])
    return Context(
        clock=FakeClock(now),
        data=FakeDataFeed({"TQQQ": bars}, {"TQQQ": price}),
        broker=FakeBroker({}),
    )


def test_breakout_entry_fires():
    bars = _make_bars(41, breakout=True)
    strat = DonchianStrategy(symbols=["TQQQ"], params=_params(), market="US")
    signals = strat.on_cycle(_ctx(bars, datetime(2026, 1, 5, 20, 0, tzinfo=NY)))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG
    assert signals[0].symbol == "TQQQ"


def test_no_entry_without_volume_confirmation():
    bars = _make_bars(41, breakout=True, low_volume=True)
    strat = DonchianStrategy(symbols=["TQQQ"], params=_params(), market="US")
    signals = strat.on_cycle(_ctx(bars, datetime(2026, 1, 5, 20, 0, tzinfo=NY)))
    assert signals == []


def test_no_entry_without_breakout():
    bars = _make_bars(41, breakout=False)
    strat = DonchianStrategy(symbols=["TQQQ"], params=_params(), market="US")
    signals = strat.on_cycle(_ctx(bars, datetime(2026, 1, 5, 20, 0, tzinfo=NY)))
    assert signals == []


def test_stop_target_math():
    bars = _make_bars(41, breakout=True)
    strat = DonchianStrategy(symbols=["TQQQ"], params=_params(), market="US")
    [signal] = strat.on_cycle(_ctx(bars, datetime(2026, 1, 5, 20, 0, tzinfo=NY)))

    last = bars.iloc[-1]
    entry = float(last["close"])
    expected_stop = max(float(last["low"]), entry * (1 - 1.5 / 100))
    expected_target = entry + 2.0 * (entry - expected_stop)

    assert signal.stop == pytest.approx(expected_stop)
    assert signal.target == pytest.approx(expected_target)


def test_same_day_reentry_allowed_by_default():
    bars1 = _make_bars(41, breakout=True)
    bars2 = _make_bars(42, breakout=True)
    strat = DonchianStrategy(symbols=["TQQQ"], params=_params(), market="US")

    signals1 = strat.on_cycle(_ctx(bars1, datetime(2026, 1, 5, 20, 0, tzinfo=NY)))
    assert len(signals1) == 1

    signals2 = strat.on_cycle(_ctx(bars2, datetime(2026, 1, 5, 20, 30, tzinfo=NY)))
    assert len(signals2) == 1
    assert signals2[0].action == SignalAction.ENTER_LONG


def test_same_day_reentry_blocked_when_disabled():
    bars1 = _make_bars(41, breakout=True)
    bars2 = _make_bars(42, breakout=True)
    strat = DonchianStrategy(
        symbols=["TQQQ"], params=_params(allow_same_day_reentry=False), market="US"
    )

    signals1 = strat.on_cycle(_ctx(bars1, datetime(2026, 1, 5, 20, 0, tzinfo=NY)))
    assert len(signals1) == 1
    assert signals1[0].action == SignalAction.ENTER_LONG

    signals2 = strat.on_cycle(_ctx(bars2, datetime(2026, 1, 5, 20, 30, tzinfo=NY)))
    assert signals2 == []  # 같은 거래일 재진입 차단


def test_no_lookahead_in_backtest_history():
    from quant.adapters.data.stub import StubDataFeed

    feed = StubDataFeed(["TQQQ"], seed=42, days=20)
    full = feed.bars_1m["TQQQ"]
    cutoff = full.index[len(full) // 2]
    feed.set_now(cutoff)

    bars = feed.history("TQQQ", "1m", 100_000)
    assert not bars.empty
    assert (bars.index <= cutoff).all()

    bars_15m = feed.history("TQQQ", "15m", 100_000)
    assert (bars_15m.index <= cutoff).all()


def _narrow_breakout_bars(n: int = 41, base: float = 100.0) -> pd.DataFrame:
    """돌파봉의 저가가 종가에 거의 붙어 있는 케이스 — 손절폭 하한이 없으면
    stop이 진입가에 붙어 r0가 0에 수렴한다."""
    rows = []
    ts0 = datetime(2026, 1, 5, 9, 30, tzinfo=NY)
    for i in range(n):
        price = base + i * 0.01
        rows.append({
            "ts": ts0 + timedelta(minutes=15 * i), "open": price, "high": price + 0.05,
            "low": price - 0.05, "close": price, "volume": 1000.0,
        })
    last_close = rows[-1]["close"] + 5.0
    rows[-1].update(
        close=last_close, high=last_close + 0.02,
        low=last_close - 0.01,  # 종가 대비 약 1bp
        volume=5000.0,
    )
    return pd.DataFrame(rows).set_index("ts")


def test_stop_min_bps_widens_a_too_tight_stop():
    bars = _narrow_breakout_bars()
    now = datetime(2026, 1, 5, 20, 0, tzinfo=NY)

    unfloored = DonchianStrategy(symbols=["TQQQ"], params=_params(), market="US")
    sig_unfloored = unfloored.on_cycle(_ctx(bars, now))[0]
    entry = float(bars["close"].iloc[-1])
    # 하한이 없으면 손절폭이 왕복 거래비용(14bp) 수준으로 좁아진다
    assert (entry - sig_unfloored.stop) / entry < 0.0015

    floored = DonchianStrategy(
        symbols=["TQQQ"], params=_params(stop_min_bps=30), market="US",
    )
    sig_floored = floored.on_cycle(_ctx(bars, now))[0]
    assert (entry - sig_floored.stop) / entry == pytest.approx(0.0030, rel=1e-6)
    # 손절폭이 벌어지면 목표가도 그만큼 멀어진다 (risk_reward 배수 유지)
    assert sig_floored.target - entry == pytest.approx(2.0 * (entry - sig_floored.stop), rel=1e-6)


def test_stop_min_bps_does_not_tighten_an_already_wide_stop():
    """하한은 좁은 손절만 벌린다 — 이미 충분히 넓은 손절은 건드리지 않는다."""
    bars = _make_bars(41, breakout=True)
    now = datetime(2026, 1, 5, 20, 0, tzinfo=NY)
    entry = float(bars["close"].iloc[-1])

    base_sig = DonchianStrategy(symbols=["TQQQ"], params=_params(), market="US").on_cycle(_ctx(bars, now))[0]
    floored_sig = DonchianStrategy(
        symbols=["TQQQ"], params=_params(stop_min_bps=30), market="US",
    ).on_cycle(_ctx(bars, now))[0]

    assert (entry - base_sig.stop) / entry > 0.0030   # 이미 하한보다 넓음
    assert floored_sig.stop == base_sig.stop
