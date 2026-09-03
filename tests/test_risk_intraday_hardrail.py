"""인트라데이 하드레일(2026-09-03, 소유자 지시 6번) — "장중 매매는 최대 손실
−5%, 최대 목표 +10%를 유지한다"는 불변식이 실제로 강제되는지 검증한다.

감사 결과 이 불변식을 실제로 지키는 전략이 하나도 없었다: structure.py의
hard_cap_pct(3%)는 구조 모드 손절에만 적용되고(scalp_1m만 그 경로를 쓴다),
pullback_impulse는 손절 캡 자체가 없었다(그 모듈 docstring "아직 못 하는 것"
3번). 그래서 전략마다 심는 대신 리스크 평면에 두 레일을 둔다:

1. `risk/manager.py` `RiskManagerImpl.approve()`의 **진입 클램프** — 신규 진입
   시 stop이 −5%보다 넓으면 클램프, stop이 없으면 −5% 부착, target이 +10%보다
   넓으면 클램프. 클램프한 값은 Order.stop/target뿐 아니라 전략이 직접 관리하는
   lot 상태(Signal.state_update)에도 동기화된다. 오버나이트 전략은 제외.
2. `loop.py` `_intraday_hard_stop_check` — **사이클별 백스톱**. 전략의
   on_cycle 자체가 멎어도(ColdFetchBudgetExceeded, quote=None) 실제 시세로
   매 사이클 재확인해 −5%에서 시장가 청산한다. 시세가 없으면 건너뛴다. 익절
   강제는 옵트인(`risk.intraday_take_profit_cap_enabled`)이다.

설정 drift 가드: `config/settings.yaml`의 `risk.overnight_strategies`는
`quant/trade/loop.py`의 `_OVERNIGHT_STRATEGIES`를 수동으로 미러한다(risk/manager.py가
loop.py를 임포트할 수 없어 순환을 피하려는 것 — 두 모듈 docstring 참고). 이
테스트가 그 둘이 벌어지지 않았는지 잡는다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yaml

from quant.adapters.execution.paper import PaperBroker
from quant.core.models import Position, Quote, Signal, SignalAction
from quant.core.portfolio.portfolio import Portfolio
from quant.core.ports import Context
from quant.trade.loop import _OVERNIGHT_STRATEGIES, _intraday_hard_stop_check
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
SYMBOL = "TQQQ"
_DEFAULT_NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)


def _risk_cfg(**overrides) -> dict:
    cfg = dict(
        max_position_pct=100,
        max_symbol_pct_total=0,
        daily_loss_limit_pct=100,
        max_orders_per_day=0,
        cooldown_bars_after_stop=0,
        max_order_notional_pct=0,
        intraday_hard_stop_pct=5.0,
        intraday_max_target_pct=10.0,
        overnight_strategies=["frgn_accumulate"],
    )
    cfg.update(overrides)
    return {"risk": cfg}


# ============================================================= 레일 1: 진입 클램프 (risk/manager.py)


class _FakeBroker:
    """approve()만 검증 — place_order는 호출되지 않아야 한다(진입 클램프 테스트 전용)."""

    def __init__(self, cash: float, positions: dict[str, Position] | None = None):
        self._cash = cash
        self._positions = positions or {}

    def positions(self) -> dict[str, Position]:
        return self._positions

    def cash(self) -> float:
        return self._cash


class _FakeData:
    def __init__(self, price: float, now: datetime = _DEFAULT_NOW):
        self._price = price
        self._now = now

    def quote(self, symbol: str) -> Quote | None:
        return Quote(symbol=symbol, ts=self._now, price=self._price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _ctx(fake_clock_cls, price: float, cash: float) -> Context:
    return Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=_FakeData(price), broker=_FakeBroker(cash))


def _entry(strategy_id: str = "pullback_impulse", **kw) -> Signal:
    return Signal(
        strategy_id=strategy_id, symbol=SYMBOL, action=SignalAction.ENTER_LONG,
        target_weight=1.0, **kw,
    )


def test_entry_stop_wider_than_hard_cap_is_clamped(fake_clock_cls):
    risk = RiskManagerImpl(_risk_cfg(), capital_fraction={"pullback_impulse": 1.0}, market_of={SYMBOL: "US"})
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    signal = _entry(stop=90.0, state_update={"entry": 100.0, "stop": 90.0})  # -10%, 하드캡보다 넓음
    order = risk.approve(signal, ctx)

    assert order is not None
    assert order.stop == pytest.approx(95.0)  # -5%로 클램프
    # 전략이 자기 lot으로 스스로 청산을 판단하는 경우(pullback_impulse 등)를 위해
    # state_update도 같은 값을 봐야 한다 — Order.stop만 클램프하면 브로커 서버측
    # 조건주문은 안전해져도 lot["stop"]은 여전히 원래(더 넓은) 값을 가리킨다.
    assert signal.state_update["stop"] == pytest.approx(95.0)


def test_entry_target_wider_than_hard_cap_is_clamped(fake_clock_cls):
    risk = RiskManagerImpl(_risk_cfg(), capital_fraction={"pullback_impulse": 1.0}, market_of={SYMBOL: "US"})
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    signal = _entry(
        stop=97.0, target=120.0,  # +20%, 하드캡보다 넓음
        state_update={"entry": 100.0, "stop": 97.0, "target": 120.0},
    )
    order = risk.approve(signal, ctx)

    assert order is not None
    assert order.target == pytest.approx(110.0)  # +10%로 클램프
    assert signal.state_update["target"] == pytest.approx(110.0)


def test_entry_without_stop_gets_hard_cap_attached(fake_clock_cls):
    risk = RiskManagerImpl(_risk_cfg(), capital_fraction={"pullback_impulse": 1.0}, market_of={SYMBOL: "US"})
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    order = risk.approve(_entry(), ctx)  # stop=None — 레일이 부착해야 한다

    assert order is not None
    assert order.stop == pytest.approx(95.0)


def test_entry_stop_inside_hard_cap_is_left_alone(fake_clock_cls):
    """레일은 상한이지 강제 재설정이 아니다 — 이미 5% 안쪽인 손절은 그대로."""
    risk = RiskManagerImpl(_risk_cfg(), capital_fraction={"pullback_impulse": 1.0}, market_of={SYMBOL: "US"})
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    signal = _entry(stop=98.0, state_update={"entry": 100.0, "stop": 98.0})  # -2%, 하드캡 안쪽
    order = risk.approve(signal, ctx)

    assert order is not None
    assert order.stop == pytest.approx(98.0)
    assert signal.state_update["stop"] == pytest.approx(98.0)


def test_overnight_strategy_entry_is_not_clamped(fake_clock_cls):
    """오버나이트(캐리 설계) 전략은 이 레일에서 제외된다 — 밤을 넘기는 포지션에
    장중 −5%/+10% 규칙을 강제하면 전략 정의 자체가 무효화된다."""
    risk = RiskManagerImpl(
        _risk_cfg(overnight_strategies=["frgn_accumulate"]),
        capital_fraction={"frgn_accumulate": 1.0}, market_of={SYMBOL: "US"},
    )
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    signal = _entry(
        strategy_id="frgn_accumulate", stop=50.0, target=500.0,
        state_update={"entry": 100.0, "stop": 50.0, "target": 500.0},
    )
    order = risk.approve(signal, ctx)

    assert order is not None
    assert order.stop == pytest.approx(50.0)
    assert order.target == pytest.approx(500.0)
    assert signal.state_update["stop"] == pytest.approx(50.0)
    assert signal.state_update["target"] == pytest.approx(500.0)


# ============================================================= 레일 2: 사이클별 백스톱 (loop.py)


class _Feed:
    def __init__(self, price: float):
        self._price = price

    def quote(self, symbol: str) -> Quote | None:
        return Quote(symbol=symbol, ts=datetime.now(timezone.utc), price=self._price)

    def history(self, symbol, interval, n):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


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


def _rig(price: float, strategy_id: str = "pullback_impulse", entry: float = 100.0, **risk_overrides):
    """PaperBroker + 그 전략의 lot(qty=10, entry=`entry`) 하나를 든 포지션 + RiskManagerImpl.
    (broker, ctx, risk, sink) 튜플을 반환 — 각 테스트가 marks만 바꿔 호출한다."""
    broker = PaperBroker(
        data=_Feed(price), portfolio=Portfolio(cash=10_000_000.0, positions={}),
        fee_bps=0.0, market_of={SYMBOL: "US"},
    )
    broker.portfolio.positions[SYMBOL] = Position(
        symbol=SYMBOL, qty=10.0, avg_cost=entry,
        meta={"lots": {strategy_id: {"qty": 10.0, "avg_cost": entry, "entry": entry}}},
    )
    ctx = Context(clock=_FixedClock(), data=broker.data, broker=broker)
    risk = RiskManagerImpl(
        _risk_cfg(**risk_overrides), capital_fraction={strategy_id: 1.0}, market_of={SYMBOL: "US"},
    )
    return broker, ctx, risk, _Sink()


def test_percycle_hard_stop_fires_exactly_at_5pct_with_explicit_reason():
    broker, ctx, risk, sink = _rig(price=95.0, entry=100.0)  # 정확히 -5%

    _intraday_hard_stop_check(ctx, risk, sink, notifier=None, books=None, marks={SYMBOL: 95.0})

    assert not broker.portfolio.positions[SYMBOL].is_open
    assert len(sink.fills) == 1
    assert sink.fills[0].reason == "하드 손절 −5%(리스크 레일)"


def test_percycle_hard_stop_does_not_fire_at_49pct():
    broker, ctx, risk, sink = _rig(price=95.1, entry=100.0)  # -4.9%, 아직 상한 안쪽

    _intraday_hard_stop_check(ctx, risk, sink, notifier=None, books=None, marks={SYMBOL: 95.1})

    assert broker.portfolio.positions[SYMBOL].is_open
    assert sink.fills == []


def test_percycle_hard_stop_skips_overnight_strategy():
    assert "frgn_accumulate" in _OVERNIGHT_STRATEGIES  # 이 테스트 전제 — 목록이 바뀌면 여기서 드러난다
    broker, ctx, risk, sink = _rig(price=80.0, strategy_id="frgn_accumulate", entry=100.0)  # -20%

    _intraday_hard_stop_check(ctx, risk, sink, notifier=None, books=None, marks={SYMBOL: 80.0})

    assert broker.portfolio.positions[SYMBOL].is_open
    assert sink.fills == []


def test_percycle_hard_stop_skips_when_symbol_not_in_marks():
    """시세가 없으면(marks에 없음) 건너뛴다 — quote를 직접 조회하지 않는다."""
    broker, ctx, risk, sink = _rig(price=50.0, entry=100.0)  # 브로커의 데이터 소스는 -50%지만

    _intraday_hard_stop_check(ctx, risk, sink, notifier=None, books=None, marks={})  # marks가 비어 있다

    assert broker.portfolio.positions[SYMBOL].is_open
    assert sink.fills == []


def test_percycle_take_profit_fires_only_when_enabled():
    broker_off, ctx_off, risk_off, sink_off = _rig(
        price=111.0, entry=100.0, intraday_take_profit_cap_enabled=False,
    )
    _intraday_hard_stop_check(ctx_off, risk_off, sink_off, notifier=None, books=None, marks={SYMBOL: 111.0})
    assert broker_off.portfolio.positions[SYMBOL].is_open  # 기본 false — 강제 매도 안 됨
    assert sink_off.fills == []

    broker_on, ctx_on, risk_on, sink_on = _rig(
        price=111.0, entry=100.0, intraday_take_profit_cap_enabled=True,
    )
    _intraday_hard_stop_check(ctx_on, risk_on, sink_on, notifier=None, books=None, marks={SYMBOL: 111.0})
    assert not broker_on.portfolio.positions[SYMBOL].is_open
    assert sink_on.fills[0].reason == "하드 익절 +10%(리스크 레일)"


# ============================================================= 설정 drift 가드


def test_settings_overnight_strategies_mirrors_loop_module_constant():
    with open("config/settings.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    configured = set(cfg["risk"]["overnight_strategies"])
    assert configured == set(_OVERNIGHT_STRATEGIES), (
        "config/settings.yaml risk.overnight_strategies 가 "
        "quant/trade/loop.py _OVERNIGHT_STRATEGIES 와 벌어졌다 — 둘 다 갱신할 것"
    )
