"""시장별 수수료(execution.fee_bps).

`fee_bps`는 숫자 하나(전 시장 공통, 하위호환) 또는 `{US: 10, KR: 1.5}` dict를 받는다.
왜 시장별인가: 토스 미국주식 요율(10bp)과 국내 ETF 요율(1.5bp)은 자릿수가 다르다.
하나로 뭉뚱그리면 한쪽은 비용을 과대, 다른 쪽은 과소평가하고, 일중 전략에서 비용
과소평가는 그대로 가짜 엣지가 된다.

주의(코드 주석·settings.yaml에도 명시): KR 1.5bp는 **ETF 기준**이다. 국내 개별주는
매도 시 증권거래세 15~20bp가 별도로 붙는다 — 이 모델에는 들어 있지 않다.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quant.backtest.engine import run_backtest
from quant.core.fx import FixedFxProvider
from quant.core.models import Order, Quote, Side
from quant.adapters.execution.paper import PaperBroker
from quant.core.portfolio.portfolio import Portfolio

NY = ZoneInfo("America/New_York")
TS0 = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
US_SYM = "TQQQ"
KR_SYM = "069500"
PRICE = 100.0


class _Feed:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=TS0, price=PRICE)

    def history(self, symbol: str, interval: str, n: int):
        raise NotImplementedError


def _broker(fee_bps, market_of=None) -> PaperBroker:
    return PaperBroker(
        data=_Feed(), portfolio=Portfolio(cash=100_000_000.0, state_path=None),
        fee_bps=fee_bps, market_of=market_of or {US_SYM: "US", KR_SYM: "KR"},
        fx=FixedFxProvider(), slippage_bps=0.0,
    )


def _buy(broker: PaperBroker, symbol: str, qty: float = 10.0):
    return broker.place_order(Order(symbol=symbol, side=Side.BUY, qty=qty, strategy_id="t")).fill


def test_per_market_fee_applies_the_matching_rate():
    broker = _broker({"US": 10, "KR": 1.5})

    us_fill = _buy(broker, US_SYM)
    kr_fill = _buy(broker, KR_SYM)

    assert us_fill.fee == pytest.approx(10 * PRICE * 10 / 10_000)
    assert kr_fill.fee == pytest.approx(10 * PRICE * 1.5 / 10_000)


def test_scalar_fee_bps_still_applies_to_every_market():
    """하위호환: 숫자 하나면 예전처럼 전 시장 공통."""
    broker = _broker(7)

    assert _buy(broker, US_SYM).fee == pytest.approx(10 * PRICE * 7 / 10_000)
    assert _buy(broker, KR_SYM).fee == pytest.approx(10 * PRICE * 7 / 10_000)


def test_unknown_market_uses_the_highest_configured_rate_not_zero():
    """모르는 시장을 수수료 0으로 취급하면 비용 과소평가다 — 보수적으로 최댓값."""
    broker = _broker({"US": 10, "KR": 1.5}, market_of={"XYZ": "JP"})

    assert _buy(broker, "XYZ").fee == pytest.approx(10 * PRICE * 10 / 10_000)


def test_sell_side_uses_the_same_per_market_rate():
    broker = _broker({"US": 10, "KR": 1.5})
    _buy(broker, KR_SYM, qty=10.0)

    fill = broker.place_order(Order(symbol=KR_SYM, side=Side.SELL, qty=10.0, strategy_id="t")).fill

    assert fill.fee == pytest.approx(10 * PRICE * 1.5 / 10_000)


def test_backtest_accounting_identity_holds_with_per_market_fees():
    """config/settings.yaml의 dict fee_bps로 실제 백테스트가 돌고 회계 검산이 통과한다.
    (run_backtest는 잔차가 허용치를 넘으면 ReconciliationError를 던진다.)"""
    result = run_backtest(strategy_id="donchian", days=20, interval="15m", source="stub")

    rec = result.reconciliation
    assert abs(rec["residual"]) <= rec["tolerance"]
    assert rec["fees"] > 0  # 수수료가 실제로 잡혔다 — dict를 0으로 읽어버리지 않았다
