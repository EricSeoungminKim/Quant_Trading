"""통화별 매수가능금액 분리 게이트(2026-08-31, 소유자 지시) 테스트.

"원화·달러 계좌가 분리돼 있고 자동 환전은 하지 않는다. 초기 실전은 넣은 금액
안에서만." 기존 사이징(capital_fraction/cash_pct)은 전부 **KRW 환산** 예산
기준이라, 계좌 전체로는 충분해 보여도 실제 USD 예수금이 모자라면 브로커가
insufficient-buying-power로 거부한다 — RiskManagerImpl.approve()가 US 진입에
한해 `ctx.broker.cash_usd()`(duck-typed, Broker Protocol에는 없다)로 한 번 더
min()을 씌운다.

이 스위트가 고정하는 것:
- KRW로는 충분한데 USD가 부족하면 수량이 축소되거나("자금 부족"까지는 아님) 0이면
  명확한 "자금 부족" 사유로 거부된다.
- cash_usd()가 없는 브로커(PaperBroker 등)는 게이트 자체가 조용히 건너뛰어진다 —
  기존 동작 100% 보존.
- KR 심볼은 이 게이트와 무관하다(시장이 다르면 애초에 안 걸린다).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.fx import FixedFxProvider
from quant.core.ports import Context
from quant.core.models import Quote, Signal, SignalAction
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
US_SYMBOL = "TQQQ"
KR_SYMBOL = "005930"
MARKET_OF = {US_SYMBOL: "US", KR_SYMBOL: "KR"}
FX_RATE = 1500.0
PRICE = 100.0  # USD (US 심볼) / KR 심볼도 같은 스텁 시세 사용


class _Broker:
    """cash_usd가 None이면 아예 속성이 없다 — getattr duck-typing이 조용히
    건너뛰는 PaperBroker류 브로커를 정확히 흉내낸다(hasattr(obj, 'cash_usd')가
    False가 되도록, 속성 자체를 만들지 않는다)."""

    def __init__(self, cash_krw: float, cash_usd: float | None = None):
        self._cash_krw = cash_krw
        if cash_usd is not None:
            self.cash_usd = lambda: cash_usd  # noqa: E731 — 테스트 전용 duck-typing

    def positions(self):
        return {}

    def cash(self) -> float:
        return self._cash_krw

    def place_order(self, order):
        raise AssertionError("이 스위트는 approve()만 검증한다")


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=NOW, price=PRICE)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _risk(**risk_overrides) -> RiskManagerImpl:
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
        {"risk": cfg}, capital_fraction={"s": 1.0}, market_of=MARKET_OF,
        fx=FixedFxProvider(FX_RATE),
    )


def _ctx(broker: _Broker, fake_clock_cls) -> Context:
    return Context(clock=fake_clock_cls(now=NOW), data=_Data(), broker=broker)


def _entry(symbol: str, target_weight: float = 1.0) -> Signal:
    return Signal(strategy_id="s", symbol=symbol, action=SignalAction.ENTER_LONG,
                  target_weight=target_weight)


# 충분한 KRW 예산(현금 100,000,000원)이 만드는 KRW 기준 요청 수량은 시세($100)에서
# floor(100,000,000 / 1500 / 100) = 666주 — 뒤 테스트의 usd_cash 값보다 훨씬 크다.
AMPLE_KRW = 100_000_000.0


def test_usd_shortfall_scales_down_the_entry_quantity(fake_clock_cls):
    """USD 예수금이 KRW 환산 예산보다 적으면 그 한도로 수량이 줄어든다(거부가 아니다)."""
    risk = _risk()
    broker = _Broker(cash_krw=AMPLE_KRW, cash_usd=1_000.0)  # $1,000 → 10주

    order = risk.approve(_entry(US_SYMBOL), _ctx(broker, fake_clock_cls))

    assert order is not None
    assert order.qty == 10


def test_usd_exhausted_blocks_with_a_funds_shortage_reason(fake_clock_cls):
    """USD 예수금이 1주 값보다도 적으면 거부되고, 사유에 risk 레이어의 예산 부족
    메시지와 통일된 "자금 부족" 마커가 남는다(CLI `orders --rejected-funds` 필터,
    quant/apps/cli.py cmd_orders 참고)."""
    risk = _risk()
    broker = _Broker(cash_krw=AMPLE_KRW, cash_usd=50.0)  # $100/주보다 적다

    order = risk.approve(_entry(US_SYMBOL), _ctx(broker, fake_clock_cls))

    assert order is None
    assert "자금 부족" in risk.last_block
    assert "USD" in risk.last_block


def test_krw_sufficient_and_usd_sufficient_uses_krw_budget_unchanged(fake_clock_cls):
    """USD가 넉넉하면(KRW 예산보다 크면) 기존 KRW 기준 사이징 그대로다."""
    risk = _risk()
    broker = _Broker(cash_krw=AMPLE_KRW, cash_usd=1_000_000.0)  # 충분히 크다

    order = risk.approve(_entry(US_SYMBOL), _ctx(broker, fake_clock_cls))

    assert order is not None
    assert order.qty == pytest.approx(666)  # KRW 예산이 여전히 상한


def test_broker_without_cash_usd_skips_the_gate_entirely(fake_clock_cls):
    """cash_usd가 없는 브로커(PaperBroker 등)는 이 게이트가 조용히 건너뛰어진다 —
    기존 동작이 100% 보존된다(paper는 가상 자본이라 통화 분리가 의미 없다)."""
    risk = _risk()
    broker = _Broker(cash_krw=AMPLE_KRW, cash_usd=None)
    assert not hasattr(broker, "cash_usd")

    order = risk.approve(_entry(US_SYMBOL), _ctx(broker, fake_clock_cls))

    assert order is not None
    assert order.qty == pytest.approx(666)


def test_kr_symbol_is_unaffected_by_the_usd_gate(fake_clock_cls):
    """KR 심볼은 market != "US"라 이 게이트와 아예 무관하다 — USD 예수금이
    0이어도 KR 진입은 막히지 않는다. KR은 원화 그대로라 환산이 없다
    (floor(100,000,000원 / 100원) = 1,000,000주)."""
    risk = _risk()
    broker = _Broker(cash_krw=AMPLE_KRW, cash_usd=0.0)

    order = risk.approve(_entry(KR_SYMBOL), _ctx(broker, fake_clock_cls))

    assert order is not None
    assert order.qty == pytest.approx(1_000_000)
