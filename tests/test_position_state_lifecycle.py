"""포지션 상태 라이프사이클 방어 테스트 (adversarial audit로 발견된 defect A/B).

defect A: DonchianStrategy가 Signal 생성 시점에 Position.meta를 직접 mutate하던 버그.
risk 거부나 미체결 시 상태가 롤백되지 않고 영구 오염됐다. 지금은 Signal.state_update에
변경분만 담고, run_cycle이 확인된 Fill을 받은 뒤에만 적용한다.

defect B: TossBroker.positions()가 매 호출 holdings()로부터 완전히 새 Position을
만들어 meta가 항상 {}였던 버그 — 재시작 없이도 매 폴링마다 scaled_out=False로
리셋되어 잔여 물량을 계속 절반씩 파는 runaway liquidation을 유발했다. 지금은
TossBroker가 심볼별 meta를 로컬에 보존한다.

이 파일의 마지막 테스트는 두 수정이 함께 작동해 "같은 트리거 가격에서 반복 폴링해도
scale-out은 정확히 한 번만 발생한다"를 TossBroker + DonchianStrategy + run_cycle 조합으로
검증한다(runaway-liquidation 회귀 테스트).
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from quant.trade.loop import run_cycle
from quant.adapters.brokers.toss.broker import TossBroker
from quant.core.ports import Context
from quant.core.models import Fill, Order, Position, Quote, Side, SignalAction
from quant.core.portfolio.ownership import EngineOwnership
from quant.trade.strategy.donchian import DonchianStrategy

NY = ZoneInfo("America/New_York")


class _Clock:
    def __init__(self, minutes_to_close: float = 120.0, cadence: float = 15.0):
        self._mtc = minutes_to_close
        self._cadence = cadence

    def now(self) -> datetime:
        return datetime(2026, 1, 5, 20, 0, tzinfo=NY)

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return self._mtc

    def cadence_minutes(self) -> float:
        return self._cadence

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return self._mtc - self._cadence < flatten_minutes


class _DataFeed:
    def __init__(self, price: float):
        self._price = price

    def quote(self, symbol: str) -> Quote | None:
        return Quote(symbol=symbol, ts=datetime(2026, 1, 5, 20, 0, tzinfo=NY), price=self._price)

    def history(self, symbol: str, interval: str, n: int):
        raise NotImplementedError  # 이 테스트들은 이미 열린 포지션만 다룬다 (진입 로직 미사용)


class _Broker:
    def __init__(self, positions: dict[str, Position], fill: Fill | None = None):
        self._positions = positions
        self._fill = fill
        self.place_order_calls: list[Order] = []

    def place_order(self, order: Order) -> Fill | None:
        self.place_order_calls.append(order)
        return self._fill

    def positions(self) -> dict[str, Position]:
        return self._positions

    def cash(self) -> float:
        return 1_000_000.0


class _Risk:
    def __init__(self, order: Order | None):
        self._order = order
        self.last_block = "test-block"

    def approve(self, signal, ctx, risk_multiplier: float = 1.0, marks=None) -> Order | None:
        return self._order


class _AlwaysApproveRisk:
    """실제 사이징 없이 Signal을 그대로 Order로 통과시킨다 — 상태 라이프사이클만 검증."""

    def __init__(self):
        self.last_block = ""

    def approve(self, signal, ctx, risk_multiplier: float = 1.0, marks=None) -> Order:
        side = Side.SELL if signal.action in (SignalAction.EXIT_LONG, SignalAction.SCALE_OUT) else Side.BUY
        return Order(symbol=signal.symbol, side=side, qty=1, strategy_id=signal.strategy_id, reason=signal.reason)


class _Sink:
    def __init__(self):
        self.signals = []
        self.fills = []

    def on_signal(self, signal) -> None:
        self.signals.append(signal)

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


def _pm_params(**overrides) -> dict:
    p = dict(
        interval_minutes=15, lookback_bars=40, volume_mult=1.5, stop_fallback_pct=1.5,
        risk_reward=2.0, max_concurrent_names=1, flatten_before_close_minutes=10,
        position_mgmt={
            "initial_tranche_frac": 0.5, "max_scale_ins": 1, "scale_in_at_r": 5.0,
            "scale_out_at_r": 1.0, "scale_out_fraction": 0.5,
            "breakeven_after_scale_out": True, "no_add_minutes_before_close": 0,
        },
    )
    p.update(overrides)
    return p


def _open_position_at_scale_out_trigger() -> Position:
    # entry=100, r0=2 (stop=98) -> scale_out_at_r=1.0 트리거 가격 = 102. quote=103으로 호출.
    return Position(
        symbol="TQQQ", qty=10, avg_cost=100.0,
        meta={
            "entry": 100.0, "stop": 98.0, "target": 110.0, "r0": 2.0,
            "adds_done": 0, "scaled_out": False, "add_weight": 0.1,
        },
    )


def test_risk_rejection_leaves_meta_unchanged():
    pos = _open_position_at_scale_out_trigger()
    strat = DonchianStrategy(symbols=["TQQQ"], params=_pm_params(), market="US")
    broker = _Broker({"TQQQ": pos})
    ctx = Context(clock=_Clock(), data=_DataFeed(103.0), broker=broker)
    risk = _Risk(order=None)  # 항상 거부
    sink = _Sink()

    run_cycle([strat], ctx, risk, sink)

    assert broker.place_order_calls == []
    # 2026-08-11 랏(lot) 도입 — donchian도 평평한 meta 대신 meta["lots"]["donchian"]에
    # 상태를 저장한다(첫 접근 시 레거시 평평한 meta가 자동 이행된다).
    lot = pos.meta["lots"]["donchian"]
    assert lot["scaled_out"] is False
    assert lot["stop"] == 98.0
    assert lot["adds_done"] == 0


def test_unfilled_order_leaves_meta_unchanged():
    pos = _open_position_at_scale_out_trigger()
    strat = DonchianStrategy(symbols=["TQQQ"], params=_pm_params(), market="US")
    order = Order(symbol="TQQQ", side=Side.SELL, qty=5, strategy_id="donchian", reason="test")
    broker = _Broker({"TQQQ": pos}, fill=None)  # place_order가 승인은 됐지만 체결 실패
    ctx = Context(clock=_Clock(), data=_DataFeed(103.0), broker=broker)
    risk = _Risk(order=order)
    sink = _Sink()

    run_cycle([strat], ctx, risk, sink)

    assert len(broker.place_order_calls) == 1
    lot = pos.meta["lots"]["donchian"]
    assert lot["scaled_out"] is False
    assert lot["stop"] == 98.0


def test_confirmed_fill_applies_state_update_exactly_once():
    pos = _open_position_at_scale_out_trigger()
    strat = DonchianStrategy(symbols=["TQQQ"], params=_pm_params(), market="US")
    order = Order(symbol="TQQQ", side=Side.SELL, qty=5, strategy_id="donchian", reason="test")
    fill = Fill(
        symbol="TQQQ", side=Side.SELL, qty=5, price=103.0,
        ts=datetime(2026, 1, 5, 20, 0, tzinfo=NY), strategy_id="donchian",
    )
    broker = _Broker({"TQQQ": pos}, fill=fill)
    ctx = Context(clock=_Clock(), data=_DataFeed(103.0), broker=broker)
    risk = _Risk(order=order)
    sink = _Sink()

    run_cycle([strat], ctx, risk, sink)

    assert len(broker.place_order_calls) == 1
    assert len(sink.fills) == 1
    lot = pos.meta["lots"]["donchian"]
    assert lot["scaled_out"] is True
    assert lot["stop"] == 100.0  # breakeven_after_scale_out=True로 이동


def test_repeated_cycles_at_scale_out_trigger_fire_exactly_once(monkeypatch, tmp_path):
    """defect A + defect B 회귀 테스트: TossBroker + DonchianStrategy + run_cycle을 같은
    트리거 가격에서 반복 실행해도(실사이클 폴링 시뮬레이션) scale-out은 한 번만 나가야
    한다 — 매 폴링마다 meta가 리셋되던 runaway liquidation 재발 방지."""
    monkeypatch.setenv("MODE", "live")
    client = MagicMock()
    client.holdings.return_value = {
        "items": [{"symbol": "AAPL", "quantity": "10", "averagePurchasePrice": "100"}]
    }
    client.place_order.return_value = {"orderId": "o1", "clientOrderId": None}
    client.order.return_value = {
        "status": "FILLED",
        "execution": {
            "filledQuantity": "5", "averageFilledPrice": "103", "filledAmount": None,
            "commission": "0", "tax": "0", "filledAt": "2026-01-05T20:00:00Z",
            "settlementDate": "2026-01-06",
        },
    }
    # 엔진이 진입해서 보유 중인 10주라는 전제 — 엔진 소유 원장에 없으면 TossBroker가
    # "사용자 수동 보유"로 보고 매도 자체를 거부한다(그게 의도된 방어선이다).
    ownership = EngineOwnership(None)
    ownership.add("AAPL", 10.0)
    broker = TossBroker(
        client, poll_max_attempts=1, poll_interval=0,
        meta_state_path=tmp_path / "meta.json", ownership=ownership,
        intents_path=tmp_path / "intents.jsonl",
    )
    # risk_reward를 높여 target(=entry + risk_reward*r0)이 트리거 가격(103) 위로 오게 한다 —
    # 안 그러면 목표가 도달 청산이 scale-out보다 먼저 매 사이클 반복 발화한다.
    strat = DonchianStrategy(symbols=["AAPL"], params=_pm_params(risk_reward=5.0), market="US")
    ctx = Context(clock=_Clock(), data=_DataFeed(103.0), broker=broker)
    risk = _AlwaysApproveRisk()
    sink = _Sink()

    run_cycle([strat], ctx, risk, sink)
    run_cycle([strat], ctx, risk, sink)
    run_cycle([strat], ctx, risk, sink)

    assert client.place_order.call_count == 1  # 3번 폴링해도 매도 주문은 정확히 1건
