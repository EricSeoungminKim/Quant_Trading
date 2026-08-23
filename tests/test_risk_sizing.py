"""사이징 모드(risk.sizing_mode) 테스트.

`cash_pct`는 사용자가 같은 계좌에서 손으로도 매매하는 실계좌를 위해 있다: 예산 기준이
고정 시작자본이나 총자산이 아니라 **그날 실제 가용 현금**이어야, 사용자가 현금을 빼간
날에도 엔진이 매수 여력을 넘겨 주문하지 않는다. 이 스위트가 고정하는 것:

- cash_pct 예산이 가용 현금 변화를 그대로 따라간다
- 국면 배수(risk_multiplier)가 예산에 곱해진다 (기본 1.0 = 지금까지와 동일)
- 포트폴리오 상한/팻핑거 가드는 cash_pct에서도 그대로 걸린다
- 기본 모드(capital_fraction)의 결과는 이 변경으로 바뀌지 않는다
"""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.fx import FixedFxProvider
from quant.core.ports import Context
from quant.core.models import Position, Quote, Signal, SignalAction
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
SYMBOL = "TQQQ"
MARKET_OF = {SYMBOL: "US"}
FX_RATE = 1500.0
PRICE = 100.0  # USD


class _Broker:
    def __init__(self, cash: float, positions: dict[str, Position] | None = None):
        self._cash = cash
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self) -> float:
        return self._cash

    def place_order(self, order):
        raise AssertionError("이 스위트는 approve()만 검증한다")


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=NOW, price=PRICE)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _risk(capital_fraction: dict | None = None, **risk_overrides) -> RiskManagerImpl:
    cfg = dict(
        sizing_mode="cash_pct",
        max_position_pct=100,
        max_symbol_pct_total=0,
        daily_loss_limit_pct=100,
        max_orders_per_day=1000,
        cooldown_bars_after_stop=0,
        max_order_notional_pct=0,
        max_total_exposure_pct=0,
        max_concurrent_positions=0,
    )
    cfg.update(risk_overrides)
    return RiskManagerImpl(
        {"risk": cfg}, capital_fraction=capital_fraction or {"s": 1.0},
        market_of=MARKET_OF, fx=FixedFxProvider(FX_RATE),
    )


def _ctx(cash: float, positions: dict[str, Position] | None = None, fake_clock_cls=None) -> Context:
    return Context(clock=fake_clock_cls(now=NOW), data=_Data(), broker=_Broker(cash, positions))


def _entry(target_weight: float = 0.5) -> Signal:
    return Signal(strategy_id="s", symbol=SYMBOL, action=SignalAction.ENTER_LONG,
                  target_weight=target_weight)


def _expected_qty(cash_krw: float, weight: float, multiplier: float = 1.0) -> float:
    # 매수 수량은 전 시장 정수(QUICKREF:207) — RiskManagerImpl.approve()가
    # math.floor()로 내림하므로 기대값도 동일하게 내림해야 한다.
    return math.floor((cash_krw * weight * multiplier) / FX_RATE / PRICE)


# ------------------------------------------------- cash_pct가 가용 현금을 따라간다

def test_cash_pct_budget_tracks_available_cash(fake_clock_cls):
    risk = _risk()

    # 정수 수량 사이징(QUICKREF:207) 하에서 floor()가 "정확히 절반"이라는 비례관계를
    # 깰 수 있다 — 15,000,000/7,500,000을 쓰면 두 예산 모두 FX_RATE*PRICE(150,000)로
    # 나누어떨어져(각각 qty=50, 25) floor 내림 손실이 없다.
    rich = risk.approve(_entry(0.5), _ctx(15_000_000.0, fake_clock_cls=fake_clock_cls))
    # 사용자가 현금을 절반 빼갔다 — 엔진도 절반만 들어가야 한다.
    poor = risk.approve(_entry(0.5), _ctx(7_500_000.0, fake_clock_cls=fake_clock_cls))

    assert rich.qty == pytest.approx(_expected_qty(15_000_000.0, 0.5))
    assert poor.qty == pytest.approx(_expected_qty(7_500_000.0, 0.5))
    assert poor.qty == pytest.approx(rich.qty / 2)


def test_cash_pct_ignores_unrealized_position_value(fake_clock_cls):
    """총자산이 아니라 **현금**이 기준이다 — 평가액이 아무리 커도 현금이 없으면 못 산다."""
    risk = _risk()
    big_position = {"SPY": Position(symbol="SPY", qty=1000.0, avg_cost=500.0)}

    with_position = risk.approve(
        _entry(0.5), _ctx(1_000_000.0, big_position, fake_clock_cls=fake_clock_cls))
    without_position = risk.approve(
        _entry(0.5), _ctx(1_000_000.0, fake_clock_cls=fake_clock_cls))

    assert with_position.qty == pytest.approx(without_position.qty)


def test_cash_pct_blocks_when_cash_is_exhausted(fake_clock_cls):
    risk = _risk()
    positions = {"SPY": Position(symbol="SPY", qty=100.0, avg_cost=500.0)}

    order = risk.approve(_entry(0.5), _ctx(0.0, positions, fake_clock_cls=fake_clock_cls))

    assert order is None
    assert "배분 자금 부족" in risk.last_block


# ------------------------------------------------------------- 국면 배수 (regime)

def test_risk_multiplier_scales_the_cash_pct_budget(fake_clock_cls):
    risk = _risk()

    # cash=15,000,000 — 위 test_cash_pct_budget_tracks_available_cash와 같은 이유로
    # floor 내림 손실 없이 qty=50(중립) / 25(방어 x0.5)가 정확히 나온다.
    neutral = risk.approve(_entry(0.5), _ctx(15_000_000.0, fake_clock_cls=fake_clock_cls))
    defensive = risk.approve(
        _entry(0.5), _ctx(15_000_000.0, fake_clock_cls=fake_clock_cls), risk_multiplier=0.5)

    assert defensive.qty == pytest.approx(neutral.qty * 0.5)


def test_risk_multiplier_defaults_to_neutral(fake_clock_cls):
    risk = _risk()

    default = risk.approve(_entry(0.5), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    explicit = risk.approve(
        _entry(0.5), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls), risk_multiplier=1.0)

    assert default.qty == pytest.approx(explicit.qty)


@pytest.mark.parametrize("bad", [float("nan"), -1.0])
def test_invalid_risk_multiplier_falls_back_to_neutral(fake_clock_cls, bad):
    """국면 모듈이 이상값을 넘겨도 주문 크기가 폭주하거나 NaN이 되지 않는다."""
    risk = _risk()

    order = risk.approve(
        _entry(0.5), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls), risk_multiplier=bad)

    assert order is not None
    assert order.qty == pytest.approx(_expected_qty(10_000_000.0, 0.5))


# ------------------------------------------------------- 기존 상한은 그대로 걸린다

def test_max_order_notional_pct_still_applies_in_cash_pct_mode(fake_clock_cls):
    risk = _risk(max_order_notional_pct=10)

    order = risk.approve(_entry(1.0), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))

    assert order is None
    assert "단일 주문 규모 상한" in risk.last_block


def test_max_total_exposure_pct_still_applies_in_cash_pct_mode(fake_clock_cls):
    risk = _risk(max_total_exposure_pct=50)
    # 현금 1,000,000원 + 보유 평가 9,000,000원(= 6주 x $1000 x 1500) → 총자산 1,000만원.
    # 노출 900만원은 이미 50% 상한(500만원)을 넘었다.
    positions = {"SPY": Position(symbol="SPY", qty=6.0, avg_cost=1000.0)}
    risk.market_of["SPY"] = "US"

    order = risk.approve(_entry(0.5), _ctx(1_000_000.0, positions, fake_clock_cls=fake_clock_cls))

    assert order is None
    assert "총노출 상한" in risk.last_block


def test_max_concurrent_positions_still_applies_in_cash_pct_mode(fake_clock_cls):
    risk = _risk(max_concurrent_positions=1)
    positions = {"SPY": Position(symbol="SPY", qty=1.0, avg_cost=100.0)}
    risk.market_of["SPY"] = "US"

    order = risk.approve(_entry(0.5), _ctx(10_000_000.0, positions, fake_clock_cls=fake_clock_cls))

    assert order is None
    assert "동시 보유 종목 수 상한" in risk.last_block


# ---------------------------------------------------- 기본 모드는 바뀌지 않았다

def test_default_mode_is_capital_fraction_and_uses_equity_not_cash(fake_clock_cls):
    """설정에 sizing_mode가 없으면 기존 동작 그대로 — 총자산 기준으로 사이징한다."""
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 100, "max_symbol_pct_total": 0,
                  "max_order_notional_pct": 0, "cooldown_bars_after_stop": 0}},
        capital_fraction={"s": 1.0}, market_of={SYMBOL: "US", "SPY": "US"},
        fx=FixedFxProvider(FX_RATE),
    )
    assert risk.sizing_mode == "capital_fraction"
    # 현금 100만원 + 보유 평가 900만원 = 총자산 1,000만원. 현금 기준이면 100만원어치,
    # 총자산 기준이면 1,000만원어치를 배분한다 — 후자여야 한다.
    positions = {"SPY": Position(symbol="SPY", qty=6.0, avg_cost=1000.0)}

    order = risk.approve(_entry(1.0), _ctx(1_000_000.0, positions, fake_clock_cls=fake_clock_cls))

    assert order.qty == pytest.approx(_expected_qty(10_000_000.0, 1.0))


# ============================================== 시장별 capital_fraction (2026-08-12)
#
# donchian은 US 정규장 전용(TQQQ/SQQQ 고정)인데 capital_fraction이 전역 스칼라이던
# 시절엔 그 배분이 KR 세션 내내 노는 구조적 낭비가 있었다. capital_fraction이
# {market: 비중} dict를 받을 수 있게 하고, RiskManagerImpl이 신호 심볼의 시장으로
# 배분을 고르게 한다.

def test_scalar_capital_fraction_applies_to_any_market_unchanged(fake_clock_cls):
    """스칼라 선언(기존 동작)은 시장과 무관하게 동일한 비중을 쓴다 — 하위호환."""
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 100, "max_symbol_pct_total": 0,
                  "max_order_notional_pct": 0, "cooldown_bars_after_stop": 0}},
        capital_fraction={"s": 0.5}, market_of={SYMBOL: "US"},
        fx=FixedFxProvider(FX_RATE),
    )
    order = risk.approve(_entry(1.0), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    assert order is not None
    assert order.qty == pytest.approx(_expected_qty(10_000_000.0, 0.5))


def test_market_dict_capital_fraction_uses_signal_symbols_market(fake_clock_cls):
    """dict 선언은 신호 심볼의 시장 키를 골라 쓴다."""
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 100, "max_symbol_pct_total": 0,
                  "max_order_notional_pct": 0, "cooldown_bars_after_stop": 0}},
        capital_fraction={"s": {"KR": 0.0, "US": 0.4}}, market_of={SYMBOL: "US"},
        fx=FixedFxProvider(FX_RATE),
    )
    order = risk.approve(_entry(1.0), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    assert order is not None
    # 부동소수점으로 곱한 뒤 따로 floor한 값(_expected_qty(10e6,1.0)*0.4)은 실제
    # 계산 순서(budget = split*equity, qty = floor(budget/...))와 floor 지점이
    # 달라 어긋날 수 있다 — weight 자리에 split을 직접 넣어 같은 순서로 맞춘다.
    assert order.qty == pytest.approx(_expected_qty(10_000_000.0, 0.4))


def test_market_missing_from_capital_fraction_dict_blocks_entry(fake_clock_cls):
    """dict에 없는 시장은 0으로 취급되고, 0 배분은 조용히 수량 0이 아니라 명확한
    사유로 진입 자체를 차단한다 — "donchian은 KR 시장 배분이 0" 같은 사유."""
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 100, "max_symbol_pct_total": 0,
                  "max_order_notional_pct": 0, "cooldown_bars_after_stop": 0}},
        capital_fraction={"s": {"US": 0.4}},  # KR 미기재
        market_of={SYMBOL: "KR"},
        fx=FixedFxProvider(FX_RATE),
    )
    order = risk.approve(_entry(1.0), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    assert order is None
    assert "KR 시장 배분이 0" in risk.last_block
    assert "s" in risk.last_block


def test_explicit_zero_market_allocation_blocks_entry_with_clear_reason(fake_clock_cls):
    """donchian처럼 KR: 0.0을 명시한 경우도 같은 방식으로 차단돼야 한다."""
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 100, "max_symbol_pct_total": 0,
                  "max_order_notional_pct": 0, "cooldown_bars_after_stop": 0}},
        capital_fraction={"donchian": {"KR": 0.0, "US": 0.3}},
        market_of={"005930": "KR"},
        fx=FixedFxProvider(FX_RATE),
    )
    order = risk.approve(
        Signal(strategy_id="donchian", symbol="005930", action=SignalAction.ENTER_LONG,
               target_weight=1.0),
        _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls),
    )
    assert order is None
    assert risk.last_block == "donchian는 KR 시장 배분이 0 — 신규 진입 차단"


def test_zero_market_allocation_gate_also_applies_in_cash_pct_mode(fake_clock_cls):
    """cash_pct 모드는 budget 산식에 capital_fraction을 쓰지 않지만, "이 전략이
    이 시장에서 거래 가능한가"라는 게이트는 두 모드가 같은 답을 써야 한다.
    MARKET_OF는 SYMBOL(TQQQ)을 US로 매핑하므로 US: 0.0을 명시해 차단시킨다."""
    risk = _risk(capital_fraction={"s": {"KR": 0.5, "US": 0.0}})
    order = risk.approve(_entry(0.5), _ctx(10_000_000.0, fake_clock_cls=fake_clock_cls))
    assert order is None
    assert "US 시장 배분이 0" in risk.last_block
