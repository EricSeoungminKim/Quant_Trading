"""결함주입 6/6 — 리스크 사이징의 랜덤화 프로퍼티 테스트.

hypothesis는 이 저장소 의존성에 없으므로(pyproject.toml 미확인 항목 추가 회피)
고정 seed 랜덤 루프로 대신한다. 무작위 (equity, price, target_weight, fx) 조합에서:

- 수량은 항상 유한하고 0 이상(NaN/음수 없음)
- KR은 정수 수량
- 예산(전략자본×비중) 이내로 근사(슬리피지/체결 반영 전 사이징 단계라 정확히
  같지 않을 수 있어 상한 초과만 확인)
- 단일 종목 레버리지 ETF는 자산이 문턱 미만이면 절대 진입 주문이 나오지 않는다
"""
from __future__ import annotations

import math
import random
from datetime import UTC, datetime

from quant.core.fx import FixedFxProvider
from quant.core.models import Quote, Signal, SignalAction
from quant.core.ports import Context
from quant.trade.risk.manager import RiskManagerImpl

NOW = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
N_TRIALS = 500


class _Clock:
    def now(self):
        return NOW

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return 1.0

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


class _Data:
    def __init__(self, price: float):
        self._price = price

    def quote(self, symbol: str):
        return Quote(symbol=symbol, ts=NOW, price=self._price)

    def history(self, symbol: str, interval: str, n: int):
        """전일 상한가 레일(`_prev_limit_up_blocked`)이 일봉을 조회한다 — 빈
        프레임이면 그 레일은 "조회 실패는 차단 아님" 원칙대로 통과시킨다
        (risk/manager.py 모듈 docstring)."""
        import pandas as pd
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class _Broker:
    def __init__(self, cash: float):
        self._cash = cash

    def positions(self):
        return {}

    def cash(self) -> float:
        return self._cash


def _risk(market_of: dict[str, str], **risk_overrides) -> RiskManagerImpl:
    cfg = dict(
        sizing_mode="cash_pct", max_position_pct=100, max_symbol_pct_total=0,
        daily_loss_limit_pct=100, max_orders_per_day=0, cooldown_bars_after_stop=0,
        max_order_notional_pct=0, max_total_exposure_pct=0, max_concurrent_positions=0,
    )
    cfg.update(risk_overrides)
    return RiskManagerImpl({"risk": cfg}, capital_fraction={"s": 1.0}, market_of=market_of,
                           fx=FixedFxProvider(1500.0))


def test_random_kr_sizing_always_yields_finite_nonnegative_integer_qty():
    rng = random.Random(20260907)
    symbol = "005930"
    for _ in range(N_TRIALS):
        equity_krw = rng.uniform(1.0, 5_000_000_000.0)
        price = rng.uniform(1.0, 3_000_000.0)
        weight = rng.uniform(0.0, 3.0)  # >1도 시도 — 과도한 비중 요청
        risk = _risk({symbol: "KR"})
        ctx = Context(clock=_Clock(), data=_Data(price), broker=_Broker(equity_krw))
        signal = Signal(strategy_id="s", symbol=symbol, action=SignalAction.ENTER_LONG,
                        target_weight=weight, reason="랜덤")
        order = risk.approve(signal, ctx)
        if order is None:
            continue
        assert math.isfinite(order.qty)
        assert order.qty >= 0
        assert order.qty == int(order.qty), f"KR 수량은 정수여야 한다: {order.qty}"
        notional_krw = order.qty * price
        assert notional_krw <= equity_krw * 1.0001  # 자산을 초과하는 주문은 없어야 한다


def test_random_us_sizing_always_yields_finite_nonnegative_qty_within_budget():
    rng = random.Random(9070726)
    symbol = "TQQQ"
    for _ in range(N_TRIALS):
        cash_krw = rng.uniform(1.0, 5_000_000_000.0)
        price_usd = rng.uniform(0.01, 2000.0)
        weight = rng.uniform(0.0, 3.0)
        risk = _risk({symbol: "US"})
        ctx = Context(clock=_Clock(), data=_Data(price_usd), broker=_Broker(cash_krw))
        signal = Signal(strategy_id="s", symbol=symbol, action=SignalAction.ENTER_LONG,
                        target_weight=weight, reason="랜덤")
        order = risk.approve(signal, ctx)
        if order is None:
            continue
        assert math.isfinite(order.qty)
        assert order.qty >= 0
        notional_usd = order.qty * price_usd
        notional_krw = notional_usd * 1500.0
        assert notional_krw <= cash_krw * 1.0001


def test_random_fx_rate_never_produces_nan_or_negative_sizing():
    """환율 자체가 극단값(초고환율, 초저환율)이어도 사이징이 NaN/음수를 내지 않는다."""
    rng = random.Random(424242)
    symbol = "TQQQ"
    for _ in range(N_TRIALS):
        fx_rate = rng.uniform(0.01, 100_000.0)
        cash_krw = rng.uniform(1.0, 1_000_000_000.0)
        price_usd = rng.uniform(0.01, 1000.0)
        weight = rng.uniform(0.0, 2.0)
        cfg = dict(
            sizing_mode="cash_pct", max_position_pct=100, max_symbol_pct_total=0,
            daily_loss_limit_pct=100, max_orders_per_day=0, cooldown_bars_after_stop=0,
            max_order_notional_pct=0, max_total_exposure_pct=0, max_concurrent_positions=0,
        )
        risk = RiskManagerImpl({"risk": cfg}, capital_fraction={"s": 1.0}, market_of={symbol: "US"},
                               fx=FixedFxProvider(fx_rate))
        ctx = Context(clock=_Clock(), data=_Data(price_usd), broker=_Broker(cash_krw))
        signal = Signal(strategy_id="s", symbol=symbol, action=SignalAction.ENTER_LONG,
                        target_weight=weight, reason="랜덤")
        order = risk.approve(signal, ctx)
        if order is None:
            continue
        assert math.isfinite(order.qty)
        assert order.qty >= 0


def test_random_equity_never_bypasses_single_stock_leveraged_min_equity_rail():
    """자산이 얼마든(무작위) 문턱(3천만원) 미만이면 단일종목 레버리지 ETF
    진입은 단 한 번도 통과하지 않는다."""
    rng = random.Random(30000000)
    symbol = "TSLL"
    min_equity = 30_000_000.0
    for _ in range(N_TRIALS):
        equity_krw = rng.uniform(1.0, min_equity - 1.0)  # 항상 문턱 미만
        price_usd = rng.uniform(1.0, 100.0)
        risk = _risk(
            {symbol: "US"},
            single_stock_leveraged={"symbols": [symbol], "min_equity_krw": min_equity},
        )
        ctx = Context(clock=_Clock(), data=_Data(price_usd), broker=_Broker(equity_krw))
        signal = Signal(strategy_id="s", symbol=symbol, action=SignalAction.ENTER_LONG,
                        target_weight=1.0, reason="랜덤")
        order = risk.approve(signal, ctx)
        assert order is None, f"자산 {equity_krw:.0f}원(<{min_equity:.0f})인데 진입이 통과함"


def test_random_zero_or_negative_target_weight_never_produces_negative_qty():
    rng = random.Random(1)
    symbol = "TQQQ"
    for _ in range(N_TRIALS):
        weight = rng.uniform(-2.0, 0.0)
        risk = _risk({symbol: "US"})
        ctx = Context(clock=_Clock(), data=_Data(100.0), broker=_Broker(10_000_000.0))
        signal = Signal(strategy_id="s", symbol=symbol, action=SignalAction.ENTER_LONG,
                        target_weight=weight, reason="랜덤")
        order = risk.approve(signal, ctx)
        if order is not None:
            assert order.qty >= 0
