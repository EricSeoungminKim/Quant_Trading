"""quant/trade/loop.py의 전략별 독립 명목계정(books) 배선 — 체결이 실제로 books에
반영되는지 확인하는 통합 테스트(실제 PaperBroker + RiskManagerImpl 사용).

고정하는 것:
- 체결이 확정되면 그 전략의 book이 갱신되고 디스크에 저장된다.
- 체결이 없으면(주문 거부) book이 전혀 바뀌지 않는다.
- 두 전략이 같은 종목을 사도 books가 독립적으로 갱신된다.
- books=None(shared 모드의 기본 배선)이면 관련 코드가 아예 실행되지 않는다
  (예외 없이 조용히 스킵).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.adapters.execution.paper import PaperBroker
from quant.core.fx import FixedFxProvider
from quant.core.models import Quote, Signal, SignalAction
from quant.core.ports import Context
from quant.core.portfolio.portfolio import Portfolio
from quant.trade.loop import _execute_signal
from quant.trade.risk.books import StrategyBooks
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
SYMBOL = "TQQQ"
MARKET_OF = {SYMBOL: "US"}
FX_RATE = 1500.0
PRICE = 100.0
INITIAL_KRW = 10_000_000.0


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=NOW, price=PRICE)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class _Sink:
    def on_signal(self, signal):
        pass

    def on_fill(self, fill):
        pass

    def on_order(self, state):
        pass


def _ctx(fake_clock_cls, broker) -> Context:
    return Context(clock=fake_clock_cls(now=NOW), data=_Data(), broker=broker)


def _broker(cash: float = 100_000_000.0) -> PaperBroker:
    fx = FixedFxProvider(FX_RATE)
    portfolio = Portfolio(cash=cash, state_path=None)  # 백테스트/테스트 원칙 — 영속화 없음
    return PaperBroker(data=_Data(), portfolio=portfolio, fee_bps=0.0, market_of=MARKET_OF, fx=fx)


def _risk(books, capital_mode="per_strategy") -> RiskManagerImpl:
    cfg = dict(
        capital_mode=capital_mode, sizing_mode="capital_fraction",
        max_position_pct=100, max_symbol_pct_total=0, daily_loss_limit_pct=100,
        max_orders_per_day=1000, cooldown_bars_after_stop=0, max_order_notional_pct=0,
        max_total_exposure_pct=0, max_concurrent_positions=0, min_order_notional_krw=0,
    )
    return RiskManagerImpl(
        {"risk": cfg}, capital_fraction={"a": 1.0, "b": 1.0}, market_of=MARKET_OF,
        fx=FixedFxProvider(FX_RATE), books=books,
    )


def _entry(strategy_id: str, target_weight: float = 0.5) -> Signal:
    return Signal(strategy_id=strategy_id, symbol=SYMBOL, action=SignalAction.ENTER_LONG,
                  target_weight=target_weight)


def _books(tmp_path) -> StrategyBooks:
    return StrategyBooks.load(tmp_path / "strategy_books.json", initial_krw=INITIAL_KRW)


# ------------------------------------------------------------- 기본 배선

def test_fill_updates_and_persists_the_strategy_book(fake_clock_cls, tmp_path):
    books = _books(tmp_path)
    risk = _risk(books)
    broker = _broker()
    ctx = _ctx(fake_clock_cls, broker)

    _execute_signal(_entry("a"), ctx, risk, _Sink(), notifier=None, books=books)

    assert "TQQQ" in books.books["a"]["positions"]
    assert books.books["a"]["positions"]["TQQQ"]["qty"] > 0
    assert books.path.exists()  # save()가 실제로 호출됐다


def test_rejected_order_does_not_touch_the_book(fake_clock_cls, tmp_path):
    books = _books(tmp_path)
    risk = _risk(books)
    broker = _broker()
    ctx = _ctx(fake_clock_cls, broker)

    # target_weight=0 → 예산 0 → risk.approve()가 None을 돌려준다(체결 없음).
    _execute_signal(_entry("a", target_weight=0.0), ctx, risk, _Sink(), notifier=None, books=books)

    # risk.approve()는 사이징 계산을 위해 equity_krw() 조회 과정에서 "a"의 book을
    # 조회는 하지만(_ensure가 initial_krw로 초기화), 체결이 아예 없었으므로 현금과
    # 포지션은 초기값 그대로다 — apply_fill()이 호출되지 않았다는 뜻이다.
    assert books.books["a"]["cash_krw"] == pytest.approx(INITIAL_KRW)
    assert books.books["a"]["positions"] == {}


def test_two_strategies_stay_independent_through_real_fills(fake_clock_cls, tmp_path):
    books = _books(tmp_path)
    risk = _risk(books)
    broker = _broker()  # 같은 브로커/계좌를 두 전략이 공유(실제 배선과 동일)
    ctx = _ctx(fake_clock_cls, broker)

    _execute_signal(_entry("a", target_weight=1.0), ctx, risk, _Sink(), notifier=None, books=books)
    _execute_signal(_entry("b", target_weight=0.1), ctx, risk, _Sink(), notifier=None, books=books)

    qty_a = books.books["a"]["positions"]["TQQQ"]["qty"]
    qty_b = books.books["b"]["positions"]["TQQQ"]["qty"]
    assert qty_a > 0 and qty_b > 0
    assert qty_a != qty_b  # 서로 다른 target_weight → 서로 다른 수량, 서로 간섭 없음


def test_books_none_is_a_complete_noop(fake_clock_cls, tmp_path):
    """books=None(기본, shared 모드 배선)이면 예외 없이 그냥 넘어간다."""
    risk = _risk(books=None, capital_mode="shared")
    broker = _broker()
    ctx = _ctx(fake_clock_cls, broker)

    # 예외가 나면 이 테스트 자체가 실패한다.
    _execute_signal(_entry("a"), ctx, risk, _Sink(), notifier=None, books=None)
