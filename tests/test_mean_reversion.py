"""MeanReversionStrategy — 과매도 반등 전략 테스트 (자체 설계, 백테스트 미검증)."""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.core.ports import Context
from quant.core.models import Position, Quote, SignalAction
from quant.trade.strategy.mean_reversion import MeanReversionStrategy

KST = ZoneInfo("Asia/Seoul")
DAY0 = date(2026, 1, 5)  # 월요일

PARAMS = {
    "entry_z": -2.0,
    "entry_rsi": 10,
    "zscore_window": 20,
    "rsi_period": 2,
    "atr_period": 14,
    "atr_stop_mult": 2.0,
    "exit_z": -0.5,
    "max_hold_sessions": 5,
    "turnover_window": 20,
    "min_turnover": 100_000_000,
}

# 시나리오별 종가 배열 (pandas로 사전 계산해 z/RSI 부호를 미리 확인한 값).
BOTH_MET = [100.0] * 19 + [95.0, 80.0]  # z=-4.12, RSI=0.0 — 둘 다 만족
Z_ONLY = [100.0] * 18 + [60.0, 61.0, 63.0]  # z=-2.20, RSI=100.0 — z만 만족
RSI_ONLY = [  # z=-1.32, RSI=0.0 — RSI만 만족
    100.0, 100.0, 98.59, 95.8, 97.0, 93.58, 93.87, 92.8, 89.26, 89.32,
    85.62, 85.09, 81.65, 78.37, 77.77, 80.39, 77.38, 75.16, 76.18, 75.18, 74.18,
]
FLAT = [100.0] * 21  # z=0.0 (진입도 청산 복귀 조건도 아님 — RSI 계산상 진입도 안 됨)


class FakeClock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    def is_market_open(self, market):
        return True

    def minutes_to_close(self, market):
        return 300.0

    def cadence_minutes(self):
        return 5.0

    def should_flatten(self, market, flatten_minutes):
        return False


class FakeDataFeed:
    def __init__(self, bars, quotes):
        self._bars = bars
        self._quotes = quotes
        self.history_calls: dict[str, int] = {}

    def quote(self, symbol):
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(KST), price=self._quotes[symbol])

    def history(self, symbol, interval, n):
        self.history_calls[symbol] = self.history_calls.get(symbol, 0) + 1
        df = self._bars.get(symbol, {}).get(interval)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df.tail(n)


class FakeBroker:
    def __init__(self, positions=None):
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self):
        return 1_000_000.0


def _daily_bars(closes: list[float], volume: float = 2_000_000.0) -> pd.DataFrame:
    idx = pd.bdate_range(end=DAY0 - pd.Timedelta(days=1), periods=len(closes), tz=KST)
    rows = [
        {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": volume}
        for c in closes
    ]
    return pd.DataFrame(rows, index=idx)


def _ctx(symbols, closes_by_symbol, quotes, now, positions=None):
    bars = {sym: {"1d": _daily_bars(closes)} for sym, closes in closes_by_symbol.items()}
    data = FakeDataFeed(bars, quotes)
    ctx = Context(clock=FakeClock(now), data=data, broker=FakeBroker(positions))
    return ctx


_NOW = datetime.combine(DAY0, dtime(9, 5), tzinfo=KST)


def test_entry_fires_when_z_and_rsi_both_met():
    s = MeanReversionStrategy(["069500"], PARAMS)
    ctx = _ctx(["069500"], {"069500": BOTH_MET}, {"069500": 80.0}, _NOW)
    signals = s.on_cycle(ctx)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.ENTER_LONG
    assert sig.stop is not None and sig.stop < 80.0
    assert sig.target_weight == 1.0


def test_no_entry_when_only_z_condition_met():
    s = MeanReversionStrategy(["069500"], PARAMS)
    ctx = _ctx(["069500"], {"069500": Z_ONLY}, {"069500": 63.0}, _NOW)
    assert s.on_cycle(ctx) == []


def test_no_entry_when_only_rsi_condition_met():
    s = MeanReversionStrategy(["069500"], PARAMS)
    ctx = _ctx(["069500"], {"069500": RSI_ONLY}, {"069500": 74.18}, _NOW)
    assert s.on_cycle(ctx) == []


def test_no_entry_when_flat():
    s = MeanReversionStrategy(["069500"], PARAMS)
    ctx = _ctx(["069500"], {"069500": FLAT}, {"069500": 100.0}, _NOW)
    assert s.on_cycle(ctx) == []


def test_stop_hit_exits_full():
    s = MeanReversionStrategy(["069500"], PARAMS)
    pos = Position(symbol="069500", qty=10, avg_cost=80.0)
    pos.meta.update(entry=80.0, stop=76.0, strategy="mean_reversion",
                     entered_session=DAY0.isoformat(), sessions_held=1)
    ctx = _ctx(["069500"], {"069500": FLAT}, {"069500": 75.0}, _NOW, {"069500": pos})
    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1 and exits[0].exit_fraction == 1.0
    assert "손절" in exits[0].reason


def test_zscore_revert_exits():
    """z-score가 exit_z(-0.5) 위로 복귀하면 청산 — FLAT(z=0.0)은 복귀 조건을 만족."""
    s = MeanReversionStrategy(["069500"], PARAMS)
    pos = Position(symbol="069500", qty=10, avg_cost=80.0)
    pos.meta.update(entry=80.0, stop=70.0, strategy="mean_reversion",
                     entered_session=DAY0.isoformat(), sessions_held=1)
    ctx = _ctx(["069500"], {"069500": FLAT}, {"069500": 100.0}, _NOW, {"069500": pos})
    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "평균회귀 복귀" in exits[0].reason


def test_time_exit_after_max_hold_sessions():
    """z가 여전히 낮아(복귀 아님) 손절도 아니어도 max_hold_sessions를 넘기면 시간 손절."""
    s = MeanReversionStrategy(["069500"], PARAMS)
    pos = Position(symbol="069500", qty=10, avg_cost=100.0)
    # sessions_held=4 → 이번 판정에서 5로 증가 → max_hold_sessions(5) 도달
    pos.meta.update(entry=100.0, stop=50.0, strategy="mean_reversion",
                     entered_session=DAY0.isoformat(), sessions_held=4)
    ctx = _ctx(["069500"], {"069500": BOTH_MET}, {"069500": 80.0}, _NOW, {"069500": pos})
    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "시간 손절" in exits[0].reason
    # 2026-08-11 랏(lot) 도입 — 전략별 상태는 이제 meta["lots"][strategy_id] 밑에
    # 있다(평평한 meta는 첫 접근 때 lot으로 이행되고 지워진다).
    assert pos.meta["lots"]["mean_reversion"]["sessions_held"] == 5


def test_evaluated_once_per_session_history_call_count_stable():
    """같은 세션(날짜) 안에서 사이클을 반복해도 history() 호출 횟수가 늘지 않는다."""
    s = MeanReversionStrategy(["069500"], PARAMS)
    ctx = _ctx(["069500"], {"069500": FLAT}, {"069500": 100.0}, _NOW)
    s.on_cycle(ctx)
    calls_after_first = ctx.data.history_calls.get("069500", 0)
    assert calls_after_first == 1
    s.on_cycle(ctx)
    s.on_cycle(ctx)
    assert ctx.data.history_calls.get("069500", 0) == calls_after_first, (
        "같은 세션 반복 판정은 history를 다시 부르면 안 된다"
    )

    # 다음 세션(다음 날)으로 넘어가면 다시 한 번 판정한다.
    next_day_now = datetime.combine(DAY0 + pd.Timedelta(days=1), dtime(9, 5), tzinfo=KST)
    ctx2 = _ctx(["069500"], {"069500": FLAT}, {"069500": 100.0}, next_day_now)
    ctx2.data.history_calls = ctx.data.history_calls  # 같은 인스턴스 이어쓰기 흉내
    s.on_cycle(ctx2)
    assert ctx2.data.history_calls.get("069500", 0) == calls_after_first + 1


# ---------------------------------------------------------------------------
# 레버리지 상품 금지 (leverage_of 생성자 인자) — 오버나이트 + 레버리지는 최악의
# 조합이라 진입 판정에서 차단한다. leverage_of가 주입되지 않으면(기본값 None)
# 이 게이트는 꺼진 채 기존 동작 그대로다.
# ---------------------------------------------------------------------------

def test_leverage_of_none_does_not_block_entry():
    """기본값(주입 없음) — 기존 동작 그대로 진입한다."""
    s = MeanReversionStrategy(["069500"], PARAMS, leverage_of=None)
    ctx = _ctx(["069500"], {"069500": BOTH_MET}, {"069500": 80.0}, _NOW)
    signals = s.on_cycle(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG


def test_leveraged_symbol_blocks_entry():
    s = MeanReversionStrategy(["069500"], PARAMS, leverage_of={"069500": 3.0})
    ctx = _ctx(["069500"], {"069500": BOTH_MET}, {"069500": 80.0}, _NOW)
    signals = s.on_cycle(ctx)
    assert signals == []
    assert "레버리지" in s.last_reject["069500"]


def test_inverse_leverage_also_blocks_entry():
    """부호와 무관하게 |배수|>1이면 차단한다."""
    s = MeanReversionStrategy(["069500"], PARAMS, leverage_of={"069500": -2.0})
    ctx = _ctx(["069500"], {"069500": BOTH_MET}, {"069500": 80.0}, _NOW)
    assert s.on_cycle(ctx) == []


def test_non_leveraged_symbol_still_enters_when_leverage_of_provided():
    s = MeanReversionStrategy(["069500"], PARAMS, leverage_of={"069500": 1.0})
    ctx = _ctx(["069500"], {"069500": BOTH_MET}, {"069500": 80.0}, _NOW)
    signals = s.on_cycle(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG


def test_unknown_leverage_blocks_entry_conservatively():
    """leverage_of가 주입됐지만 이 심볼이 dict에 없으면(레버리지 미상) — 모르는
    것을 비레버리지로 가정하지 않고 보수적으로 차단한다."""
    s = MeanReversionStrategy(["069500"], PARAMS, leverage_of={})
    ctx = _ctx(["069500"], {"069500": BOTH_MET}, {"069500": 80.0}, _NOW)
    signals = s.on_cycle(ctx)
    assert signals == []
    assert "미상" in s.last_reject["069500"]


def test_leverage_gate_does_not_block_exits():
    """레버리지 게이트는 진입 판정 전용 — 이미 보유 중인 포지션의 손절/청산은
    막지 않는다(레일 원칙: 청산은 절대 막지 않는다)."""
    s = MeanReversionStrategy(["069500"], PARAMS, leverage_of={"069500": 3.0})
    pos = Position(symbol="069500", qty=10, avg_cost=80.0)
    pos.meta.update(entry=80.0, stop=76.0, strategy="mean_reversion",
                     entered_session=DAY0.isoformat(), sessions_held=1)
    ctx = _ctx(["069500"], {"069500": FLAT}, {"069500": 75.0}, _NOW, {"069500": pos})
    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "손절" in exits[0].reason


def test_does_not_manage_a_position_opened_by_another_strategy():
    s = MeanReversionStrategy(symbols=["069500"], params=PARAMS)
    foreign = Position(symbol="069500", qty=10, avg_cost=100.0,
                        meta={"strategy": "orb_scan", "stop": 90.0})
    assert s._owns(foreign) is False

    mine = Position(symbol="069500", qty=10, avg_cost=100.0,
                     meta={"strategy": "mean_reversion", "stop": 90.0})
    assert s._owns(mine) is True
