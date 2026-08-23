"""`DonchianPureStrategy`(순수함수 계약, quant.core.strategy_api)가 기존
`DonchianStrategy`와 **같은 신호**를 내는지 증명한다 — 엔진 분리 설계 Phase A.

세 층위로 검증한다:
1. 단일 사이클 신호 동치 (`test_donchian.py`와 같은 시나리오를 `decide()`로 재현)
2. 여러 사이클(진입 -> 부분익절 -> 목표가 청산)에 걸친 신호+상태 동치
   (`DonchianStrategy.on_cycle`과 `DonchianPureShell.on_cycle`을 같은 ctx로 나란히 구동)
3. 백테스트 수치 동치 (n_trades/총수익률) — settings.yaml은 건드리지 않고,
   임시 복사본에 "donchian_pure" 블록만 추가해 같은 파라미터로 돌린다.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yaml

from quant.core.models import Position, Quote, SignalAction
from quant.core.ports import Context
from quant.core.strategy_api import StrategySnapshot
from quant.trade.strategy.donchian import DonchianPureShell, DonchianPureStrategy, DonchianStrategy

NY = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# 층위 1: 단일 사이클 — test_donchian.py의 fixture를 재사용해 decide()를 직접 부른다.
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, now: datetime, minutes_to_close: float | None = 120.0, cadence_minutes: float = 15.0):
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

    def set_quote(self, symbol: str, price: float) -> None:
        self._quotes[symbol] = price

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


def _ctx(bars: pd.DataFrame, now: datetime, positions: dict | None = None) -> Context:
    price = float(bars["close"].iloc[-1])
    return Context(
        clock=FakeClock(now),
        data=FakeDataFeed({"TQQQ": bars}, {"TQQQ": price}),
        broker=FakeBroker(positions or {}),
    )


def _pure_snapshot(bars: pd.DataFrame, now: datetime, quote_price: float | None = None,
                    lots: dict | None = None) -> StrategySnapshot:
    price = quote_price if quote_price is not None else float(bars["close"].iloc[-1])
    return StrategySnapshot(
        now=now,
        market_open={"US": True, "KR": True},
        minutes_to_close={"US": 120.0, "KR": 120.0},
        cadence_minutes=15.0,
        bars={("TQQQ", "15m"): bars},
        quotes={"TQQQ": Quote(symbol="TQQQ", ts=now, price=price)},
        lots=lots or {},
    )


def _sig_key(sig):
    """비교용 튜플. `strategy_id`는 두 구현이 레지스트리에 별도 이름
    ("donchian" vs "donchian_pure")으로 등록돼 의도적으로 다르므로 제외한다.
    reason(사람이 읽는 문구)은 두 구현이 문구를 조금 다르게 조립해도 의미는
    같을 수 있어 제외하지 않고 **포함**한다(엄격 비교가 목표)."""
    return (
        sig.symbol, sig.action, sig.target_weight,
        sig.exit_fraction, sig.reason, sig.stop, sig.target, sig.state_update,
    )


@pytest.mark.parametrize("breakout,low_volume", [(True, False), (True, True), (False, False)])
def test_single_cycle_entry_equivalence(breakout, low_volume):
    bars = _make_bars(41, breakout=breakout, low_volume=low_volume)
    now = datetime(2026, 1, 5, 20, 0, tzinfo=NY)

    legacy = DonchianStrategy(symbols=["TQQQ"], params=_params(), market="US")
    legacy_signals = legacy.on_cycle(_ctx(bars, now))

    pure = DonchianPureStrategy(symbols=["TQQQ"], params=_params(), market="US")
    snap = _pure_snapshot(bars, now)
    decision = pure.decide(snap, {})

    assert [_sig_key(s) for s in legacy_signals] == [_sig_key(s) for s in decision.signals]


def test_stop_target_math_equivalence():
    bars = _make_bars(41, breakout=True)
    now = datetime(2026, 1, 5, 20, 0, tzinfo=NY)

    legacy = DonchianStrategy(symbols=["TQQQ"], params=_params(), market="US")
    [legacy_sig] = legacy.on_cycle(_ctx(bars, now))

    pure = DonchianPureStrategy(symbols=["TQQQ"], params=_params(), market="US")
    [pure_sig] = pure.decide(_pure_snapshot(bars, now), {}).signals

    assert legacy_sig.stop == pytest.approx(pure_sig.stop)
    assert legacy_sig.target == pytest.approx(pure_sig.target)
    assert legacy_sig.target_weight == pytest.approx(pure_sig.target_weight)


def test_stop_min_bps_equivalence():
    """돌파봉 저가가 종가에 거의 붙은 케이스 — stop_min_bps 하한 적용까지 동치인지."""
    rows = []
    ts0 = datetime(2026, 1, 5, 9, 30, tzinfo=NY)
    for i in range(41):
        price = 100.0 + i * 0.01
        rows.append({
            "ts": ts0 + timedelta(minutes=15 * i), "open": price, "high": price + 0.05,
            "low": price - 0.05, "close": price, "volume": 1000.0,
        })
    last_close = rows[-1]["close"] + 5.0
    rows[-1].update(close=last_close, high=last_close + 0.02, low=last_close - 0.01, volume=5000.0)
    bars = pd.DataFrame(rows).set_index("ts")
    now = datetime(2026, 1, 5, 20, 0, tzinfo=NY)

    legacy = DonchianStrategy(symbols=["TQQQ"], params=_params(stop_min_bps=30), market="US")
    [legacy_sig] = legacy.on_cycle(_ctx(bars, now))

    pure = DonchianPureStrategy(symbols=["TQQQ"], params=_params(stop_min_bps=30), market="US")
    [pure_sig] = pure.decide(_pure_snapshot(bars, now), {}).signals

    assert legacy_sig.stop == pytest.approx(pure_sig.stop)
    assert legacy_sig.target == pytest.approx(pure_sig.target)


def test_same_day_reentry_blocked_equivalence():
    bars1 = _make_bars(41, breakout=True)
    bars2 = _make_bars(42, breakout=True)
    params = _params(allow_same_day_reentry=False)

    legacy = DonchianStrategy(symbols=["TQQQ"], params=params, market="US")
    now1 = datetime(2026, 1, 5, 20, 0, tzinfo=NY)
    now2 = datetime(2026, 1, 5, 20, 30, tzinfo=NY)
    legacy_s1 = legacy.on_cycle(_ctx(bars1, now1))
    legacy_s2 = legacy.on_cycle(_ctx(bars2, now2))

    pure = DonchianPureStrategy(symbols=["TQQQ"], params=params, market="US")
    d1 = pure.decide(_pure_snapshot(bars1, now1), {})
    d2 = pure.decide(_pure_snapshot(bars2, now2), d1.next_state)

    assert len(legacy_s1) == len(d1.signals) == 1
    assert legacy_s2 == []
    assert d2.signals == ()  # 같은 거래일 재진입 차단 — 동치


# ---------------------------------------------------------------------------
# 층위 2: 여러 사이클 — 진입 -> 부분익절(breakeven 이동) -> 목표가 청산.
# `DonchianStrategy.on_cycle(ctx)` 과 `DonchianPureShell.on_cycle(ctx)` 을
# **같은 broker positions dict** 로 나란히 구동해 신호 시퀀스가 일치하는지 본다.
# ---------------------------------------------------------------------------

def _pm_params(**overrides) -> dict:
    p = _params(
        stop_min_bps=30,
        position_mgmt={
            "initial_tranche_frac": 1.0,
            "scale_in_at_r": 0, "max_scale_ins": 0, "no_add_minutes_before_close": 60,
            "scale_out_at_r": 1.5, "scale_out_fraction": 0.3, "breakeven_after_scale_out": True,
        },
    )
    p.update(overrides)
    return p


def test_multi_cycle_entry_scaleout_target_equivalence():
    params = _pm_params()
    bars = _make_bars(41, breakout=True)
    now = datetime(2026, 1, 5, 20, 0, tzinfo=NY)

    legacy = DonchianStrategy(symbols=["TQQQ"], params=params, market="US", id="donchian")
    pure_shell = DonchianPureShell(symbols=["TQQQ"], params=params, market="US", id="donchian_pure")

    legacy_positions: dict[str, Position] = {}
    pure_positions: dict[str, Position] = {}

    data_legacy = FakeDataFeed({"TQQQ": bars}, {"TQQQ": float(bars["close"].iloc[-1])})
    data_pure = FakeDataFeed({"TQQQ": bars}, {"TQQQ": float(bars["close"].iloc[-1])})
    ctx_legacy = Context(clock=FakeClock(now), data=data_legacy, broker=FakeBroker(legacy_positions))
    ctx_pure = Context(clock=FakeClock(now), data=data_pure, broker=FakeBroker(pure_positions))

    # cycle 1: 진입
    sig_legacy = legacy.on_cycle(ctx_legacy)
    sig_pure = pure_shell.on_cycle(ctx_pure)
    assert [_sig_key(s) for s in sig_legacy] == [_sig_key(s) for s in sig_pure]
    assert len(sig_legacy) == 1 and sig_legacy[0].action == SignalAction.ENTER_LONG

    entry = float(bars["close"].iloc[-1])  # entry_price == 마지막 종가(breakout 봉)
    stop0 = sig_legacy[0].stop
    target = sig_legacy[0].target
    r0 = entry - stop0

    # "체결" 시뮬레이션: 두 broker에 동일한 포지션을 채운다 (avg_cost=entry, qty=100)
    legacy_positions["TQQQ"] = Position(symbol="TQQQ", qty=100.0, avg_cost=entry)
    pure_positions["TQQQ"] = Position(symbol="TQQQ", qty=100.0, avg_cost=entry)

    # cycle 2: scale_out 트리거 (+1.6R, 목표가 미달)
    price2 = entry + 1.6 * r0
    data_legacy.set_quote("TQQQ", price2)
    data_pure.set_quote("TQQQ", price2)
    sig_legacy2 = legacy.on_cycle(ctx_legacy)
    sig_pure2 = pure_shell.on_cycle(ctx_pure)
    assert [_sig_key(s) for s in sig_legacy2] == [_sig_key(s) for s in sig_pure2]
    assert len(sig_legacy2) == 1 and sig_legacy2[0].action == SignalAction.SCALE_OUT
    assert sig_legacy2[0].state_update == {"scaled_out": True, "stop": entry}

    # 체결 반영: 기존 loop._execute_signal과 동일하게 legacy 쪽 Position.meta lot에
    # state_update를 적용한다(순수판은 자체 next_state에 이미 반영돼 있다 — shell.py 참고).
    legacy_positions["TQQQ"].ensure_lot("donchian").update(sig_legacy2[0].state_update)

    # cycle 3: breakeven 이동 후 재차 조회 — 스케일아웃 재발동 없음, 아직 목표가 미달
    sig_legacy3 = legacy.on_cycle(ctx_legacy)
    sig_pure3 = pure_shell.on_cycle(ctx_pure)
    assert sig_legacy3 == []
    assert sig_pure3 == []

    # cycle 4: 목표가 도달
    price4 = target + 0.01
    data_legacy.set_quote("TQQQ", price4)
    data_pure.set_quote("TQQQ", price4)
    sig_legacy4 = legacy.on_cycle(ctx_legacy)
    sig_pure4 = pure_shell.on_cycle(ctx_pure)
    assert [_sig_key(s) for s in sig_legacy4] == [_sig_key(s) for s in sig_pure4]
    assert len(sig_legacy4) == 1 and sig_legacy4[0].action == SignalAction.EXIT_LONG
    assert "목표가 도달" in sig_legacy4[0].reason


# ---------------------------------------------------------------------------
# 층위 3: 백테스트 수치 — settings.yaml은 건드리지 않는다. 임시 복사본에
# "donchian_pure" 블록(class만 다르고 나머지는 donchian과 동일)을 추가해 돌린다.
# ---------------------------------------------------------------------------

def test_backtest_n_trades_and_return_equivalence(tmp_path):
    from quant.backtest import run_backtest

    with open("config/settings.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    donchian_cfg = copy.deepcopy(cfg["strategies"]["donchian"])
    pure_cfg = copy.deepcopy(donchian_cfg)
    pure_cfg["class"] = "donchian_pure"
    cfg["strategies"]["donchian_pure"] = pure_cfg

    tmp_settings = tmp_path / "settings_with_pure.yaml"
    with open(tmp_settings, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)

    legacy_result = run_backtest(
        strategy_id="donchian", days=90, interval="15m", source="stub",
        settings_path=str(tmp_settings),
    )
    pure_result = run_backtest(
        strategy_id="donchian_pure", days=90, interval="15m", source="stub",
        settings_path=str(tmp_settings),
    )

    assert legacy_result.metrics["n_trades"] == pure_result.metrics["n_trades"]
    assert legacy_result.metrics["total_return_pct"] == pytest.approx(
        pure_result.metrics["total_return_pct"], rel=1e-9
    )
    assert not legacy_result.strategy_errors
    assert not pure_result.strategy_errors
