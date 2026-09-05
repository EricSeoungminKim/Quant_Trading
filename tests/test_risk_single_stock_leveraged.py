"""단일 종목 레버리지 ETF 최소 자산 레일(`risk.single_stock_leveraged`,
2026-09-05, scratchpad/letf_spec.md) — Toss 제약: TSLL/NVDL 같은 단일 종목 3배
ETF는 계좌 자산이 `min_equity_krw`(기본 3천만원) 이상이어야 매매 가능하다.
지수/섹터 LETF(TQQQ/SQQQ 등, 설정 목록에 없는 심볼)는 무제한이다.

검증 항목: 문턱 미만 차단, 문턱 이상(경계 포함) 허용, 청산은 절대 막지 않음,
목록에 없는 심볼은 영향받지 않음.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Position, Quote, Signal, SignalAction
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
LEVERAGED_SYMBOL = "TSLL"   # 설정 목록에 있는 단일 종목 레버리지 ETF
UNLISTED_SYMBOL = "TQQQ"    # 설정 목록에 없는 지수 LETF — 이 레일과 무관
_DEFAULT_NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
_MIN_EQUITY_KRW = 30_000_000.0


def _risk_cfg(**overrides) -> dict:
    cfg = dict(
        max_position_pct=100,
        max_symbol_pct_total=0,
        daily_loss_limit_pct=100,
        max_orders_per_day=0,
        cooldown_bars_after_stop=0,
        max_order_notional_pct=0,
        single_stock_leveraged={
            "symbols": [LEVERAGED_SYMBOL, "NVDL"],
            "min_equity_krw": _MIN_EQUITY_KRW,
        },
    )
    cfg.update(overrides)
    return {"risk": cfg}


class _FakeBroker:
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


def _ctx(fake_clock_cls, price: float, cash: float, positions=None):
    from quant.core.ports import Context

    return Context(
        clock=fake_clock_cls(now=_DEFAULT_NOW), data=_FakeData(price),
        broker=_FakeBroker(cash, positions),
    )


def _entry(symbol: str, **kw) -> Signal:
    return Signal(
        strategy_id="letf_pair_qqq", symbol=symbol, action=SignalAction.ENTER_LONG,
        target_weight=1.0, **kw,
    )


def _scale_in(symbol: str, **kw) -> Signal:
    return Signal(
        strategy_id="letf_pair_qqq", symbol=symbol, action=SignalAction.SCALE_IN,
        target_weight=1.0, **kw,
    )


def _exit(symbol: str, **kw) -> Signal:
    return Signal(
        strategy_id="letf_pair_qqq", symbol=symbol, action=SignalAction.EXIT_LONG,
        target_weight=0.0, exit_fraction=1.0, **kw,
    )


def test_enter_blocked_below_threshold(fake_clock_cls):
    """계좌 자산(현금, 다른 포지션 없음)이 문턱 미만이면 목록에 있는 심볼의
    ENTER_LONG을 차단한다."""
    risk = RiskManagerImpl(
        _risk_cfg(), capital_fraction={"letf_pair_qqq": 1.0}, market_of={LEVERAGED_SYMBOL: "US"},
    )
    ctx = _ctx(fake_clock_cls, price=10.0, cash=_MIN_EQUITY_KRW - 1.0)

    order = risk.approve(_entry(LEVERAGED_SYMBOL), ctx)

    assert order is None
    assert "단일 종목 레버리지" in risk.last_block
    assert LEVERAGED_SYMBOL in risk.last_block


def test_scale_in_blocked_below_threshold(fake_clock_cls):
    """SCALE_IN도 ENTER_LONG과 동일하게 차단 대상이다."""
    risk = RiskManagerImpl(
        _risk_cfg(), capital_fraction={"letf_pair_qqq": 1.0}, market_of={LEVERAGED_SYMBOL: "US"},
    )
    ctx = _ctx(fake_clock_cls, price=10.0, cash=_MIN_EQUITY_KRW - 1.0)

    order = risk.approve(_scale_in(LEVERAGED_SYMBOL), ctx)

    assert order is None
    assert "단일 종목 레버리지" in risk.last_block


def test_enter_allowed_at_threshold(fake_clock_cls):
    """자산이 문턱과 정확히 같으면(경계) 허용된다 — `<` 비교이지 `<=`가 아니다."""
    risk = RiskManagerImpl(
        _risk_cfg(), capital_fraction={"letf_pair_qqq": 1.0}, market_of={LEVERAGED_SYMBOL: "US"},
    )
    ctx = _ctx(fake_clock_cls, price=10.0, cash=_MIN_EQUITY_KRW)

    order = risk.approve(_entry(LEVERAGED_SYMBOL), ctx)

    assert order is not None


def test_enter_allowed_above_threshold(fake_clock_cls):
    risk = RiskManagerImpl(
        _risk_cfg(), capital_fraction={"letf_pair_qqq": 1.0}, market_of={LEVERAGED_SYMBOL: "US"},
    )
    ctx = _ctx(fake_clock_cls, price=10.0, cash=_MIN_EQUITY_KRW * 2)

    order = risk.approve(_entry(LEVERAGED_SYMBOL), ctx)

    assert order is not None


def test_exit_always_allowed_even_below_threshold(fake_clock_cls):
    """청산은 자산 문턱과 무관하게 항상 허용된다 — 이 파일의 공통 원칙
    (청산을 막으면 손실 포지션을 가두는 꼴이다)."""
    risk = RiskManagerImpl(
        _risk_cfg(), capital_fraction={"letf_pair_qqq": 1.0}, market_of={LEVERAGED_SYMBOL: "US"},
    )
    position = Position(symbol=LEVERAGED_SYMBOL, qty=10.0, avg_cost=10.0)
    ctx = _ctx(
        fake_clock_cls, price=10.0, cash=_MIN_EQUITY_KRW - 1.0,
        positions={LEVERAGED_SYMBOL: position},
    )

    order = risk.approve(_exit(LEVERAGED_SYMBOL), ctx)

    assert order is not None


def test_unlisted_symbol_unaffected_below_threshold(fake_clock_cls):
    """설정 목록에 없는 심볼(지수/섹터 LETF 등)은 자산이 문턱 미만이어도
    이 레일에서 차단되지 않는다."""
    risk = RiskManagerImpl(
        _risk_cfg(), capital_fraction={"letf_pair_qqq": 1.0}, market_of={UNLISTED_SYMBOL: "US"},
    )
    ctx = _ctx(fake_clock_cls, price=10.0, cash=_MIN_EQUITY_KRW - 1.0)

    order = risk.approve(_entry(UNLISTED_SYMBOL), ctx)

    assert order is not None


def test_disabled_when_symbols_list_empty(fake_clock_cls):
    """`symbols`가 비어 있으면(설정 자체를 안 켠 것) 레일이 전혀 발동하지 않는다
    — 기존 동작 보존(하위호환)."""
    risk = RiskManagerImpl(
        {"risk": {
            "max_position_pct": 100, "max_symbol_pct_total": 0, "daily_loss_limit_pct": 100,
            "max_orders_per_day": 0, "cooldown_bars_after_stop": 0, "max_order_notional_pct": 0,
        }},
        capital_fraction={"letf_pair_qqq": 1.0}, market_of={LEVERAGED_SYMBOL: "US"},
    )
    # single_stock_leveraged 설정 자체가 없으므로 이 레일과는 무관 — 예산은
    # 다른 사이징 게이트를 통과할 만큼만 넉넉히 준다.
    ctx = _ctx(fake_clock_cls, price=10.0, cash=10_000_000.0)

    order = risk.approve(_entry(LEVERAGED_SYMBOL), ctx)

    assert order is not None
