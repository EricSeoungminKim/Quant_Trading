"""전략별 랏(lot) 분리 — 2026-08-11 사용자 지시.

"같은 전략이더라도 다른 전략으로 보고 들어간 거면, 해당 전략 선에서 규율을
지켜라. donchian으로 TQQQ 들어갔는데 다른 전략이 TQQQ 들어갔다고 한 전략의
법칙으로 그 종목 포지션 전체를 다루는 게 아니라, 전략마다 구매한 만큼을 그대로
지키면서 매도·청산해야 한다."

즉 여러 전략이 같은 심볼을 동시 보유할 수 있고, 각 전략은 자기 수량·자기 평단·
자기 손절/목표만 관리하며, 청산도 자기 몫만 판다. `Position.meta["lots"]`
(`quant/core/models.py`)가 전략별 부분 상태를 담고, 심볼 레벨
`pos.qty`/`pos.avg_cost`는 지금처럼 합산 진실로 유지된다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant.trade.loop import _flatten_all, run_cycle
from quant.control.ledger import round_trips
from quant.core.ports import Context
from quant.core.models import Order, Position, Quote, Side, Signal, SignalAction
from quant.adapters.execution.paper import PaperBroker
from quant.adapters.persistence.sink import MultiSink
from quant.core.portfolio.portfolio import Portfolio
from quant.trade.risk.manager import RiskManagerImpl

SYMBOL = "TQQQ"


class _Feed:
    def __init__(self, price: float):
        self._price = price

    def quote(self, symbol: str) -> Quote | None:
        return Quote(symbol=symbol, ts=datetime.now(timezone.utc), price=self._price)

    def history(self, symbol, interval, n):
        import pandas as pd
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _broker(price: float = 100.0, **kw) -> PaperBroker:
    return PaperBroker(
        data=_Feed(price),
        portfolio=Portfolio(cash=10_000_000.0, positions={}),
        fee_bps=0.0,
        market_of={SYMBOL: "US"},
        **kw,
    )


# ============================================================= PaperBroker: 랏 부기


def test_two_strategies_buy_same_symbol_creates_separate_lots():
    broker = _broker(price=100.0)
    broker.place_order(Order(symbol=SYMBOL, side=Side.BUY, qty=10, strategy_id="A"))
    broker.data._price = 110.0
    broker.place_order(Order(symbol=SYMBOL, side=Side.BUY, qty=5, strategy_id="B"))

    pos = broker.portfolio.positions[SYMBOL]
    assert pos.qty == pytest.approx(15)
    assert pos.avg_cost == pytest.approx((10 * 100.0 + 5 * 110.0) / 15)  # 심볼 합산은 그대로 블렌딩

    lots = pos.meta["lots"]
    assert lots["A"]["qty"] == pytest.approx(10)
    assert lots["A"]["avg_cost"] == pytest.approx(100.0)
    assert lots["B"]["qty"] == pytest.approx(5)
    assert lots["B"]["avg_cost"] == pytest.approx(110.0)


def test_exit_signal_for_one_strategy_only_sells_its_lot():
    """핵심 요구: A 전략 청산 신호가 B의 lot을 건드리면 안 된다."""
    broker = _broker(price=100.0)
    broker.place_order(Order(symbol=SYMBOL, side=Side.BUY, qty=10, strategy_id="A"))
    broker.place_order(Order(symbol=SYMBOL, side=Side.BUY, qty=5, strategy_id="B"))

    # A가 자기 몫보다 큰 수량(100)을 요청해도 자기 lot(10주)까지만 팔린다 —
    # B의 5주는 절대 건드리지 않는다.
    fill = broker.place_order(Order(symbol=SYMBOL, side=Side.SELL, qty=100, strategy_id="A")).fill

    assert fill is not None
    assert fill.qty == pytest.approx(10)
    pos = broker.portfolio.positions[SYMBOL]
    assert pos.qty == pytest.approx(5)  # B의 몫만 남음
    assert "A" not in pos.meta["lots"]
    assert pos.meta["lots"]["B"]["qty"] == pytest.approx(5)
    assert pos.meta["lots"]["B"]["avg_cost"] == pytest.approx(100.0)  # B는 손대지 않음


def test_lot_realized_pnl_uses_lot_avg_cost_not_symbol_average():
    """lot별 realized_pnl이 그 lot의 avg_cost 기준으로 계산돼야 한다 — 다른 전략이
    더 비싸게/싸게 산 몫이 내 실현손익에 섞이면 안 된다."""
    broker = _broker(price=100.0)
    broker.place_order(Order(symbol=SYMBOL, side=Side.BUY, qty=10, strategy_id="A"))  # A: 100
    broker.data._price = 200.0
    broker.place_order(Order(symbol=SYMBOL, side=Side.BUY, qty=10, strategy_id="B"))  # B: 200
    # 심볼 합산 avg_cost는 150 — 이게 A의 실현손익 계산에 쓰이면 틀린다.
    pos = broker.portfolio.positions[SYMBOL]
    assert pos.avg_cost == pytest.approx(150.0)

    broker.data._price = 120.0
    fill = broker.place_order(Order(symbol=SYMBOL, side=Side.SELL, qty=10, strategy_id="A")).fill

    assert fill.realized_pnl == pytest.approx((120.0 - 100.0) * 10)  # A의 원가(100) 기준
    assert fill.realized_pnl != pytest.approx((120.0 - 150.0) * 10)  # 합산 평균가 기준이면 이 값(오답)


def test_orphan_qty_outside_lots_is_not_managed_by_any_strategy_but_flatten_clears_it():
    """고아 잔량(예: 재시작 이행 과정의 잔여분/수동 매수분) — lot 밖 수량은 어느
    전략도 관리 대상으로 삼지 않지만(_owns가 막는다), kill switch(flatten)는 여전히
    전량을 청산한다."""
    from quant.trade.strategy.orb_scan import OrbScanStrategy

    pos = Position(
        symbol=SYMBOL, qty=15, avg_cost=100.0,
        meta={"lots": {"orb_scan": {"qty": 10.0, "avg_cost": 100.0, "entry": 100.0, "stop": 90.0}}},
    )
    strat = OrbScanStrategy([SYMBOL], {}, market="US")
    # lots 구조 자체가 있는데 orb_scan의 lot은 10주뿐 — 나머지 5주(고아분)는
    # orb_scan의 관리 대상이 아니다. _owns가 True/False로 이를 정확히 반영한다.
    assert strat._owns(pos) is True  # 내 lot(10주)은 있으니 그 몫은 관리한다
    assert pos.lot_qty("orb_scan") == pytest.approx(10.0)

    # 다른(가상의) 전략은 lots 구조가 이미 있고 자기 lot이 없으므로 입양하지 않는다.
    class _Other:
        id = "intraday_scan"
        symbols = [SYMBOL]
        _owns = OrbScanStrategy._owns
    assert _Other._owns(_Other(), pos) is False

    # flatten은 전략 소유권과 무관하게 전량(15주)을 청산해야 한다.
    broker = _broker(price=95.0)
    broker.portfolio.positions[SYMBOL] = pos
    ctx = Context(clock=_FixedClock(), data=broker.data, broker=broker)
    risk = RiskManagerImpl(
        {"risk": {"max_orders_per_day": 999, "cooldown_bars_after_stop": 0}},
        capital_fraction={"orb_scan": 1.0, "flatten": 1.0}, market_of={SYMBOL: "US"},
    )
    sink = _Sink()

    _flatten_all(ctx, risk, sink, notifier=None)

    assert not broker.portfolio.positions[SYMBOL].is_open
    assert broker.portfolio.positions[SYMBOL].qty == 0.0
    total_sold = sum(f.qty for f in sink.fills)
    assert total_sold == pytest.approx(15.0)


def test_flatten_day_scope_only_clears_intraday_lots_leaves_swing_and_orphan():
    """`/flatten day` — 단타(오버나이트 캐리 없음) 전략의 lot만 청산하고, 스윙
    (오버나이트 허용) 전략의 lot과 소유자 없는 잔여 수량은 손대지 않는다.

    orb_scan은 `_OVERNIGHT_STRATEGIES`에 없는 단타 전략, frgn_accumulate는 그
    집합에 있는 스윙(오버나이트) 전략 — loop.py의 EoD 강제청산 분류를 그대로
    재사용한다."""
    pos = Position(
        symbol=SYMBOL, qty=25, avg_cost=100.0,
        meta={"lots": {
            "orb_scan": {"qty": 10.0, "avg_cost": 100.0, "entry": 100.0, "stop": 90.0},
            "frgn_accumulate": {"qty": 10.0, "avg_cost": 100.0, "entry": 100.0, "stop": 90.0},
        }},
    )  # 25주 중 20주는 lot으로 추적, 5주는 고아분(소유자 없음)

    broker = _broker(price=95.0)
    broker.portfolio.positions[SYMBOL] = pos
    ctx = Context(clock=_FixedClock(), data=broker.data, broker=broker)
    risk = RiskManagerImpl(
        {"risk": {"max_orders_per_day": 999, "cooldown_bars_after_stop": 0}},
        capital_fraction={"orb_scan": 1.0, "frgn_accumulate": 1.0, "flatten": 1.0},
        market_of={SYMBOL: "US"},
    )
    sink = _Sink()

    _flatten_all(ctx, risk, sink, notifier=None, scope="day")

    remaining = broker.portfolio.positions[SYMBOL]
    assert remaining.is_open
    # orb_scan(단타) 10주만 팔렸다 — frgn_accumulate(스윙) 10주 + 고아분 5주는 그대로.
    assert remaining.qty == pytest.approx(15.0)
    assert "orb_scan" not in remaining.meta["lots"]
    assert remaining.meta["lots"]["frgn_accumulate"]["qty"] == pytest.approx(10.0)
    total_sold = sum(f.qty for f in sink.fills)
    assert total_sold == pytest.approx(10.0)


# ============================================================= Position.ensure_lot: 재시작 이행


def test_ensure_lot_migrates_flat_meta_with_matching_strategy_tag_preserving_stop():
    pos = Position(symbol=SYMBOL, qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 90.0, "target": 130.0, "strategy": "orb_scan"})

    lot = pos.ensure_lot("orb_scan")

    assert lot["stop"] == pytest.approx(90.0)
    assert lot["entry"] == pytest.approx(100.0)
    assert lot["target"] == pytest.approx(130.0)
    assert lot["qty"] == pytest.approx(10.0)
    assert lot["avg_cost"] == pytest.approx(100.0)
    # 평평한 키는 lots 밑으로 이행되고 원래 자리에는 남지 않는다.
    assert "entry" not in pos.meta
    assert "strategy" not in pos.meta
    assert pos.meta["lots"]["orb_scan"] is lot


def test_ensure_lot_migrates_untagged_flat_meta_donchian_style():
    """donchian은 meta에 'strategy' 태그를 남기지 않는다 — 태그가 아예 없으면
    미상(암묵적 단일 소유자)으로 보고 이행한다."""
    pos = Position(symbol=SYMBOL, qty=47, avg_cost=105.4,
                   meta={"entry": 105.4, "stop": 104.4, "target": 108.0})

    lot = pos.ensure_lot("donchian")

    assert lot["stop"] == pytest.approx(104.4)
    assert lot["qty"] == pytest.approx(47.0)


def test_ensure_lot_does_not_steal_another_strategys_flat_state():
    """flat meta가 이미 다른 전략 태그를 달고 있으면, 그 전략이 아닌 쪽이
    ensure_lot을 먼저 불러도 그 상태를 훔쳐가지 않는다 — 원 소유자는 나중에
    자기 몫을 온전히 이행받는다."""
    pos = Position(symbol=SYMBOL, qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 90.0, "strategy": "orb_scan"})

    other_lot = pos.ensure_lot("intraday_scan")
    assert other_lot == {}  # 남의 flat 상태를 가져오지 않음

    owner_lot = pos.ensure_lot("orb_scan")
    assert owner_lot["stop"] == pytest.approx(90.0)
    assert owner_lot["entry"] == pytest.approx(100.0)


def test_lot_qty_and_lot_are_pure_reads_without_migration():
    """`lot()`/`lot_qty()`는 소유권을 확정하지 않은 조회에서도 안전해야 한다 —
    호출만으로 레거시 상태를 훔쳐가면 안 된다(다른 전략의 `_owns` 스캔이 매 사이클
    모든 포지션을 이 메서드로 들여다본다)."""
    pos = Position(symbol=SYMBOL, qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 90.0})  # 태그 없음(donchian 스타일)

    assert pos.lot("intraday_scan") is None
    assert pos.lot_qty("intraday_scan") == 0.0
    # 조회만으로는 이행되지 않는다 — meta가 그대로다.
    assert "lots" not in pos.meta
    assert pos.meta.get("entry") == 100.0


# ============================================================= E2E: run_cycle 경유 두 전략 동시 보유


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return 15.0

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


class _Sink:
    def __init__(self):
        self.signals: list[Signal] = []
        self.fills: list = []

    def on_signal(self, signal) -> None:
        self.signals.append(signal)

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


class _ScriptedStrategy:
    """테스트 전용 최소 Strategy — 사이클 번호에 맞춰 미리 정해둔 신호를 그대로
    낸다. 진입 조건 판정 로직 자체는 이 테스트의 관심사가 아니다(각 전략 파일이
    이미 따로 검증한다) — 여기서는 두 전략이 같은 심볼을 동시 보유할 때 랏 분리와
    거래 원장 라운드트립이 올바른지만 본다."""

    def __init__(self, strategy_id: str, symbol: str, signals_by_cycle: list[list[Signal]]):
        self.id = strategy_id
        self.symbols = [symbol]
        self._signals_by_cycle = signals_by_cycle
        self._cycle = 0

    def on_cycle(self, ctx: Context) -> list[Signal]:
        signals = self._signals_by_cycle[self._cycle] if self._cycle < len(self._signals_by_cycle) else []
        self._cycle += 1
        return signals


def _enter(strategy_id: str, weight: float = 0.4) -> Signal:
    return Signal(strategy_id=strategy_id, symbol=SYMBOL, action=SignalAction.ENTER_LONG, target_weight=weight)


def _exit(strategy_id: str) -> Signal:
    return Signal(strategy_id=strategy_id, symbol=SYMBOL, action=SignalAction.EXIT_LONG,
                  target_weight=0.0, exit_fraction=1.0, reason="테스트 청산")


def test_e2e_two_strategies_hold_same_symbol_and_ledger_round_trips_close_independently():
    """run_cycle 경유로 두 전략(A/B)이 같은 심볼을 동시 보유 → A만 청산해도 B는
    그대로 → 마지막에 B도 청산 → 원장에서 (전략,심볼) 라운드트립이 각각 정상
    종결된다(2026-08-11 실사고 회귀 가드: 다른 전략이 청산해 라운드트립이 영원히
    안 닫히던 사고)."""
    portfolio = Portfolio(cash=10_000_000.0, positions={})
    data = _Feed(100.0)
    broker = PaperBroker(data=data, portfolio=portfolio, fee_bps=0.0, market_of={SYMBOL: "US"})
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 100, "max_symbol_pct_total": 0, "daily_loss_limit_pct": 100,
                  "max_orders_per_day": 999, "cooldown_bars_after_stop": 0}},
        capital_fraction={"A": 0.5, "B": 0.5}, market_of={SYMBOL: "US"},
    )
    ctx = Context(clock=_FixedClock(), data=data, broker=broker)

    strat_a = _ScriptedStrategy("A", SYMBOL, [[_enter("A")], [], [_exit("A")], []])
    strat_b = _ScriptedStrategy("B", SYMBOL, [[], [_enter("B")], [], [_exit("B")]])

    trades: list[dict] = []

    class _LedgerCapture:
        def on_signal(self, signal) -> None:
            return None

        def on_fill(self, fill) -> None:
            trades.append({
                "ts": fill.ts.isoformat(), "strategy_id": fill.strategy_id, "symbol": fill.symbol,
                "side": str(fill.side.value), "qty": fill.qty, "price": fill.price,
                "fee": fill.fee, "realized_pnl": fill.realized_pnl, "market": "US",
            })

    sink = MultiSink([_LedgerCapture()])

    # 사이클 1: A 진입
    run_cycle([strat_a, strat_b], ctx, risk, sink)
    pos = portfolio.positions[SYMBOL]
    assert pos.meta["lots"]["A"]["qty"] > 0
    assert "B" not in pos.meta.get("lots", {})

    # 사이클 2: B 진입 — A의 lot이 그대로인 채 B의 lot이 추가된다.
    a_qty_before = pos.meta["lots"]["A"]["qty"]
    run_cycle([strat_a, strat_b], ctx, risk, sink)
    assert pos.meta["lots"]["A"]["qty"] == pytest.approx(a_qty_before)
    assert pos.meta["lots"]["B"]["qty"] > 0
    b_qty = pos.meta["lots"]["B"]["qty"]

    # 사이클 3: A 청산 — B의 lot은 손대지 않는다(핵심 요구).
    run_cycle([strat_a, strat_b], ctx, risk, sink)
    assert "A" not in pos.meta.get("lots", {})
    assert pos.meta["lots"]["B"]["qty"] == pytest.approx(b_qty)
    assert pos.is_open  # B는 여전히 보유 중

    # 사이클 4: B도 청산 — 포지션 전량 종결.
    run_cycle([strat_a, strat_b], ctx, risk, sink)
    assert not portfolio.positions[SYMBOL].is_open

    trips = round_trips(trades)
    trips_by_key = {(t["strategy"], t["symbol"]): t for t in trips}
    assert ("A", SYMBOL) in trips_by_key
    assert ("B", SYMBOL) in trips_by_key
    assert trips_by_key[("A", SYMBOL)]["pnl_known"] is True
    assert trips_by_key[("B", SYMBOL)]["pnl_known"] is True
