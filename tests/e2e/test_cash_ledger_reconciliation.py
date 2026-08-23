"""원장(trades.jsonl) <-> 실제 현금 대조 E2E — cash_audit.audit_cash의 계약을 고정한다.

핵심 계약: PaperBroker로 실제 체결을 발생시키고, 그 체결들을 (프로덕션과 동일한)
TradeLedgerSink + load_trades()로 원장 형식 dict로 만들어 audit_cash에 넣으면 차액이
0에 수렴해야 한다. 이게 깨지면 "원장이 현금을 설명한다"는 계약 자체가 무너진 것이다.

PaperBroker의 현금 갱신식(quant/adapters/execution/paper.py):
  매수: cash -= to_krw(qty*price + fee, market)
  매도: cash += to_krw(qty*price - fee, market)
cash_audit.audit_cash가 재구성하는 현금흐름과 형태가 동일하므로, 같은 fx 상수를
주입하면 부동소수 오차 수준(<1e-6)까지 정확히 일치해야 한다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from quant.trade.cash_audit import audit_cash
from quant.control.ledger import TradeLedgerSink, load_trades
from quant.core.fx import FixedFxProvider
from quant.core.models import Order, Quote, Side
from quant.adapters.execution.paper import PaperBroker
from quant.core.portfolio.portfolio import Portfolio

FX_RATE = 1_400.0
START_CASH_KRW = 5_000_000.0
TOL = 1e-6

TS0 = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)

_EMPTY_BARS = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class SpotFeed:
    """가격을 손으로 움직일 수 있는 최소 DataFeed 페이크 (quote만 있으면 충분)."""

    def __init__(self, prices: dict[str, float], ts: datetime = TS0):
        self.prices = dict(prices)
        self.ts = ts

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def quote(self, symbol: str) -> Quote | None:
        price = self.prices.get(symbol)
        if price is None:
            return None
        return Quote(symbol=symbol, ts=self.ts, price=price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return _EMPTY_BARS


class _InnerSink:
    """TradeLedgerSink가 감싸는 내부 sink — 아무 것도 하지 않고 통과만 시킨다."""

    def on_signal(self, signal) -> None:
        pass

    def on_fill(self, fill) -> None:
        pass


def _make_broker(
    tmp_path,
    *,
    fee_bps: float = 5.0,
    kr_stock_sell_tax_bps: float = 0.0,
    kr_etf_symbols: frozenset = frozenset(),
    prices: dict[str, float] | None = None,
    start_cash: float = START_CASH_KRW,
):
    """PaperBroker + 체결을 원장(JSONL)에 기록하는 TradeLedgerSink를 함께 구성한다."""
    feed = SpotFeed(prices or {})
    portfolio = Portfolio(cash=start_cash, state_path=None)  # 디스크 상태 안 건드림
    broker = PaperBroker(
        data=feed,
        portfolio=portfolio,
        fee_bps=fee_bps,
        kr_stock_sell_tax_bps=kr_stock_sell_tax_bps,
        kr_etf_symbols=kr_etf_symbols,
        fx=FixedFxProvider(FX_RATE),
    )
    ledger_path = tmp_path / "trades.jsonl"
    sink = TradeLedgerSink(_InnerSink(), path=ledger_path)
    return feed, portfolio, broker, sink, ledger_path


def _fill(broker, sink, symbol: str, side: Side, qty: float, strategy: str = "test"):
    order = Order(symbol=symbol, side=side, qty=qty, strategy_id=strategy, reason="test")
    fill = broker.place_order(order).fill
    assert fill is not None, f"체결 실패: {symbol} {side} {qty}"
    sink.on_fill(fill)
    return fill


# ---------------------------------------------------------------------------
# 1. 가장 중요한 계약: KR + US 혼합 왕복이 차액 0에 수렴한다
# ---------------------------------------------------------------------------

def test_kr_and_us_mixed_round_trip_reconciles_to_zero(tmp_path):
    kr_symbol = "005930"
    us_symbol = "AAPL"
    feed, portfolio, broker, sink, ledger_path = _make_broker(
        tmp_path, prices={kr_symbol: 70_000.0, us_symbol: 150.0}
    )

    _fill(broker, sink, kr_symbol, Side.BUY, 10)
    _fill(broker, sink, us_symbol, Side.BUY, 5)

    feed.set_price(kr_symbol, 72_000.0)
    feed.set_price(us_symbol, 155.0)
    _fill(broker, sink, kr_symbol, Side.SELL, 10)
    _fill(broker, sink, us_symbol, Side.SELL, 5)

    trades = load_trades(ledger_path)
    assert len(trades) == 4
    assert {t["market"] for t in trades} == {"KR", "US"}

    result = audit_cash(
        trades, start_capital_krw=START_CASH_KRW, actual_cash_krw=portfolio.cash, usd_krw=FX_RATE
    )
    assert result.reconcilable
    assert abs(result.diff_krw) < TOL, result.summary
    assert not result.is_mismatch, result.summary


# ---------------------------------------------------------------------------
# 2. KR 개별주 매도세(15bp)가 붙어도 대조가 맞는다
# ---------------------------------------------------------------------------

def test_kr_stock_sell_tax_reconciles(tmp_path):
    kr_symbol = "035420"  # ETF 목록에 없으므로 매도세 대상
    feed, portfolio, broker, sink, ledger_path = _make_broker(
        tmp_path,
        kr_stock_sell_tax_bps=15.0,
        kr_etf_symbols=frozenset(),
        prices={kr_symbol: 50_000.0},
    )

    _fill(broker, sink, kr_symbol, Side.BUY, 20)
    feed.set_price(kr_symbol, 51_000.0)
    sell = _fill(broker, sink, kr_symbol, Side.SELL, 20)

    # 매도세가 실제로 fee에 반영됐는지 먼저 확인 — 안 그러면 이 테스트가 의미 없다.
    expected_sell_fee = 20 * 51_000.0 * (5.0 + 15.0) / 10_000
    assert sell.fee == pytest.approx(expected_sell_fee)

    trades = load_trades(ledger_path)
    result = audit_cash(
        trades, start_capital_krw=START_CASH_KRW, actual_cash_krw=portfolio.cash, usd_krw=FX_RATE
    )
    assert result.reconcilable
    assert abs(result.diff_krw) < TOL, result.summary
    assert not result.is_mismatch, result.summary


# ---------------------------------------------------------------------------
# 3. 부분 매도(스케일아웃)에서도 맞는다
# ---------------------------------------------------------------------------

def test_partial_exit_reconciles(tmp_path):
    kr_symbol = "005930"
    feed, portfolio, broker, sink, ledger_path = _make_broker(
        tmp_path, prices={kr_symbol: 10_000.0}
    )

    _fill(broker, sink, kr_symbol, Side.BUY, 100)
    feed.set_price(kr_symbol, 10_500.0)
    _fill(broker, sink, kr_symbol, Side.SELL, 40)  # 부분 청산 — 60주 잔존

    assert broker.portfolio.positions[kr_symbol].qty == pytest.approx(60.0)

    trades = load_trades(ledger_path)
    assert len(trades) == 2
    result = audit_cash(
        trades, start_capital_krw=START_CASH_KRW, actual_cash_krw=portfolio.cash, usd_krw=FX_RATE
    )
    assert result.reconcilable
    assert abs(result.diff_krw) < TOL, result.summary
    assert not result.is_mismatch, result.summary


# ---------------------------------------------------------------------------
# 4. 음성 검사 — 체결이 하나라도 누락되면 대조는 반드시 실패해야 한다
# ---------------------------------------------------------------------------

def test_missing_fill_causes_mismatch(tmp_path):
    kr_symbol = "005930"
    feed, portfolio, broker, sink, ledger_path = _make_broker(
        tmp_path, prices={kr_symbol: 100_000.0}
    )

    _fill(broker, sink, kr_symbol, Side.BUY, 50)
    feed.set_price(kr_symbol, 105_000.0)
    _fill(broker, sink, kr_symbol, Side.SELL, 50)

    trades = load_trades(ledger_path)
    assert len(trades) == 2
    # 매도 체결을 원장에서 누락시킨다(예: 기록 유실을 흉내) — 실제 현금은 두 체결이
    # 모두 반영된 값 그대로 둔다.
    trades_with_gap = [trades[0]]

    result = audit_cash(
        trades_with_gap,
        start_capital_krw=START_CASH_KRW,
        actual_cash_krw=portfolio.cash,
        usd_krw=FX_RATE,
    )
    assert result.reconcilable
    assert result.is_mismatch, (
        "체결 누락에도 불일치로 잡히지 않았다 — 대조 함수가 무의미하다: " + result.summary
    )
    # 누락된 매도 체결의 순현금흐름만큼 차액이 나야 한다.
    assert abs(result.diff_krw) > result.threshold_krw


# ---------------------------------------------------------------------------
# 5. 원장이 비었으면 "대조 불가"를 명시적으로 반환한다
# ---------------------------------------------------------------------------

def test_empty_ledger_is_explicitly_unreconcilable():
    result = audit_cash([], start_capital_krw=START_CASH_KRW, actual_cash_krw=START_CASH_KRW, usd_krw=FX_RATE)
    assert result.reconcilable is False
    assert result.expected_cash_krw is None
    assert result.diff_krw is None
    assert result.is_mismatch is None
    assert "대조 불가" in result.summary
