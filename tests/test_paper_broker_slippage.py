"""PaperBroker 슬리피지 모델 테스트.

배경: PaperBroker는 슬리피지를 전혀 모델링하지 않고 완성봉 종가로 즉시 전량
체결했다. "슬리피지 0"은 "모름"이 아니라 "틀림"이다 — 실측: 편도 2.5bp를 얹으면
10년 백테스트 최종 배수가 0.493 -> 0.260으로 절반이 된다.

이 파일이 지키는 것: 부호 방향(매수는 불리하게 비싸게, 매도는 불리하게 싸게),
slippage_bps=0일 때 기존 동작과의 완전한 동일성, 슬리피지를 켠 상태에서도 회계
항등식(_reconcile)이 깨지지 않는다는 것.
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
SYM = "TQQQ"
TS0 = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
QUOTE_PRICE = 100.0


class _SpotFeed:
    def __init__(self, price: float = QUOTE_PRICE):
        self.price = price

    def quote(self, symbol: str) -> Quote | None:
        return Quote(symbol=symbol, ts=TS0, price=self.price)

    def history(self, symbol: str, interval: str, n: int):
        raise NotImplementedError


def _make_broker(*, slippage_bps=0.0, fee_bps: float = 0.0, price: float = QUOTE_PRICE):
    feed = _SpotFeed(price)
    portfolio = Portfolio(cash=1_000_000.0, state_path=None)
    broker = PaperBroker(
        data=feed, portfolio=portfolio, fee_bps=fee_bps,
        market_of={SYM: "US"}, fx=FixedFxProvider(), slippage_bps=slippage_bps,
    )
    return feed, portfolio, broker


# --------------------------------------------------------------- 부호 방향 회귀

def test_buy_fills_above_quote_price():
    """매수 체결가는 반드시 quote보다 높아야 한다 — 반대면 백테스트가 공짜 수익을 만든다."""
    _, _, broker = _make_broker(slippage_bps=10.0)
    fill = broker.place_order(Order(symbol=SYM, side=Side.BUY, qty=1.0, strategy_id="t")).fill

    assert fill is not None
    assert fill.price > QUOTE_PRICE
    assert fill.price == pytest.approx(QUOTE_PRICE * 1.001)


def test_sell_fills_below_quote_price():
    """매도 체결가는 반드시 quote보다 낮아야 한다."""
    _, _, broker = _make_broker(slippage_bps=10.0)
    broker.place_order(Order(symbol=SYM, side=Side.BUY, qty=1.0, strategy_id="t"))
    fill = broker.place_order(Order(symbol=SYM, side=Side.SELL, qty=1.0, strategy_id="t")).fill

    assert fill is not None
    assert fill.price < QUOTE_PRICE
    assert fill.price == pytest.approx(QUOTE_PRICE * 0.999)


# ------------------------------------------------------------- 기본값 동일성

def test_zero_slippage_matches_quote_price_exactly():
    """slippage_bps=0(기본값)이면 체결가는 quote가와 정확히 같아야 한다(기존 동작 보존)."""
    _, _, broker = _make_broker(slippage_bps=0.0)
    fill = broker.place_order(Order(symbol=SYM, side=Side.BUY, qty=1.0, strategy_id="t")).fill

    assert fill is not None
    assert fill.price == QUOTE_PRICE


def test_default_constructor_has_zero_slippage():
    feed = _SpotFeed()
    portfolio = Portfolio(cash=1_000_000.0, state_path=None)
    broker = PaperBroker(data=feed, portfolio=portfolio, market_of={SYM: "US"})
    fill = broker.place_order(Order(symbol=SYM, side=Side.SELL, qty=0.0, strategy_id="t")).fill
    assert fill is None  # qty<=0이라 체결 없음 — 생성자가 에러 없이 기본값을 받는지만 확인
    fill = broker.place_order(Order(symbol=SYM, side=Side.BUY, qty=1.0, strategy_id="t")).fill
    assert fill.price == QUOTE_PRICE


# ---------------------------------------------------------------- dict 지원

def test_dict_slippage_uses_symbol_specific_value():
    feed = _SpotFeed()
    portfolio = Portfolio(cash=1_000_000.0, state_path=None)
    broker = PaperBroker(
        data=feed, portfolio=portfolio, market_of={SYM: "US"},
        slippage_bps={SYM: 20.0, "SQQQ": 50.0},
    )
    fill = broker.place_order(Order(symbol=SYM, side=Side.BUY, qty=1.0, strategy_id="t")).fill
    assert fill.price == pytest.approx(QUOTE_PRICE * 1.002)


def test_dict_slippage_falls_back_to_max_for_unknown_symbol():
    """dict에 없는 종목은 0이 아니라 dict에 명시된 값 중 최댓값을 보수적으로 쓴다."""
    feed = _SpotFeed()
    portfolio = Portfolio(cash=1_000_000.0, state_path=None)
    broker = PaperBroker(
        data=feed, portfolio=portfolio, market_of={SYM: "US"},
        slippage_bps={"SQQQ": 5.0, "QQQ": 50.0},
    )
    fill = broker.place_order(Order(symbol=SYM, side=Side.BUY, qty=1.0, strategy_id="t")).fill
    assert fill.price == pytest.approx(QUOTE_PRICE * 1.005)  # 최댓값 50bp 적용


# ------------------------------------------------------------ 회계 항등식 유지

def test_backtest_with_slippage_still_reconciles():
    """실제 config/settings.yaml의 기본 slippage_bps(2.5)로 돌린 백테스트도
    _reconcile()을 통과해야 한다 — run_backtest이 통과를 강제하므로 예외가 없으면
    이미 성공이다."""
    result = run_backtest(strategy_id="donchian", days=20, interval="15m", source="stub")
    assert result.metrics["n_trades"] > 0, "거래가 0건이면 이 검산은 공허하다"
    rec = result.reconciliation
    assert abs(rec["residual"]) <= rec["tolerance"]
