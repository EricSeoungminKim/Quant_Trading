"""`capital_policy: fixed_dual`(2026-09-06) — RiskManagerImpl의 시장별 현금
게이트 통합 테스트.

`tests/test_risk_per_strategy.py`와 같은 harness 패턴을 쓰되, `books.
dual_currency = True`로 두고 한 전략이 KR/US 양쪽에 계좌를 가진 시나리오를
돈다. 고정하는 것:

- US 진입이 그 전략의 US(cash_usd) 지갑만 소모하고 KR(cash_krw) 지갑은
  전혀 건드리지 않는다 — "각 전략은 시장마다 완전히 분리된 계좌"(소유자
  결정 2026-09-06)가 사이징 레이어에서도 지켜진다.
- 한 시장의 지갑을 다 써도 같은 전략의 다른 시장 지갑은 여전히 정상 진입할
  수 있다(계좌가 진짜로 분리돼 있다는 증거 — 공유 풀이었다면 한쪽 소진이
  다른 쪽 예산도 깎았을 것이다).
- equity(사이징 목표 계산의 분모)는 두 지갑의 합이지만, 최종 현금 게이트는
  시장별로 분리된 값을 쓴다.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.fx import FixedFxProvider
from quant.core.models import Quote, Side, Signal, SignalAction
from quant.core.ports import Context
from quant.trade.risk.books import StrategyBooks
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
KR_SYMBOL = "005930"
US_SYMBOL = "TQQQ"
MARKET_OF = {KR_SYMBOL: "KR", US_SYMBOL: "US"}
FX_RATE = 1500.0
KR_PRICE = 70_000.0
US_PRICE = 70.0
INITIAL_KRW = 10_000_000.0
INITIAL_USD = 10_000.0


class _Broker:
    def __init__(self, cash: float = 1_000_000_000.0, positions=None):
        self._cash = cash
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self) -> float:
        return self._cash


class _Data:
    def quote(self, symbol: str) -> Quote:
        price = KR_PRICE if symbol == KR_SYMBOL else US_PRICE
        return Quote(symbol=symbol, ts=NOW, price=price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _ctx() -> Context:
    return Context(clock=_FakeClock(), data=_Data(), broker=_Broker())


class _FakeClock:
    def now(self):
        return NOW

    def is_market_open(self, market):
        return True

    def minutes_to_close(self, market):
        return 300.0

    def cadence_minutes(self):
        return 5.0 / 60

    def should_flatten(self, market, flatten_minutes):
        return False


def _dual_books(tmp_path, strategy_id: str = "s") -> StrategyBooks:
    books = StrategyBooks.load(tmp_path / "strategy_books.json", initial_krw=0.0)
    books.dual_currency = True
    books.initial_by_strategy = {strategy_id: INITIAL_KRW}
    books.initial_by_strategy_usd = {strategy_id: INITIAL_USD}
    return books


def _risk(books, **overrides) -> RiskManagerImpl:
    cfg = dict(
        capital_mode="per_strategy",
        sizing_mode="capital_fraction",
        max_position_pct=100,
        max_symbol_pct_total=0,
        daily_loss_limit_pct=100,
        max_orders_per_day=1000,
        cooldown_bars_after_stop=0,
        max_order_notional_pct=0,
        max_total_exposure_pct=0,
        max_concurrent_positions=0,
        min_order_notional_krw=0,
    )
    cfg.update(overrides)
    return RiskManagerImpl(
        {"risk": cfg}, capital_fraction={"s": {"KR": 1.0, "US": 1.0}},
        market_of=MARKET_OF, fx=FixedFxProvider(FX_RATE), books=books,
    )


def _entry(symbol: str, target_weight: float = 1.0) -> Signal:
    return Signal(strategy_id="s", symbol=symbol, action=SignalAction.ENTER_LONG,
                  target_weight=target_weight)


def test_us_entry_only_spends_the_usd_wallet(tmp_path):
    """approve()는 승인/수량만 정한다 — 실제 지갑 반영은 loop.py가 체결 후
    apply_fill()로 한다(quant/trade/loop.py `_execute_signal`과 같은 순서).
    그 순서를 그대로 흉내내 지갑 분리를 끝까지 확인한다."""
    books = _dual_books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    risk = _risk(books)

    order = risk.approve(_entry(US_SYMBOL, target_weight=0.5), _ctx())
    assert order is not None
    assert order.qty > 0
    books.apply_fill("s", US_SYMBOL, Side.BUY, order.qty, US_PRICE, 0.0, "US", fx)

    book = books.books["s"]
    assert book["cash_usd"] < INITIAL_USD, "US 진입인데 USD 지갑이 안 줄었다"
    assert book["cash_krw"] == pytest.approx(INITIAL_KRW), "US 진입이 KR 지갑을 건드렸다"


def test_kr_entry_only_spends_the_krw_wallet(tmp_path):
    books = _dual_books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    risk = _risk(books)

    order = risk.approve(_entry(KR_SYMBOL, target_weight=0.5), _ctx())
    assert order is not None
    assert order.qty > 0
    books.apply_fill("s", KR_SYMBOL, Side.BUY, order.qty, KR_PRICE, 0.0, "KR", fx)

    book = books.books["s"]
    assert book["cash_krw"] < INITIAL_KRW, "KR 진입인데 KRW 지갑이 안 줄었다"
    assert book["cash_usd"] == pytest.approx(INITIAL_USD), "KR 진입이 US 지갑을 건드렸다"


def test_exhausting_the_usd_wallet_does_not_block_kr_entries(tmp_path):
    """책은 서로 현금을 나누지 않는다 — 한 시장 지갑 소진이 다른 시장 진입을
    막으면 안 된다(공유 풀이라면 그렇게 됐을 것이다)."""
    books = _dual_books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    # US 지갑을 거의 다 쓴다(1주도 못 살 만큼).
    books.apply_fill("s", US_SYMBOL, Side.BUY, 142.0, US_PRICE, 20.0, "US", fx)
    risk = _risk(books)

    order_us = risk.approve(_entry(US_SYMBOL, target_weight=1.0), _ctx())
    assert order_us is None, "US 지갑이 바닥났으니 US 진입은 막혀야 한다"

    order_kr = risk.approve(_entry(KR_SYMBOL, target_weight=0.5), _ctx())
    assert order_kr is not None, "KR 지갑은 멀쩡한데 US 지갑 소진 때문에 막혔다"
    assert order_kr.qty > 0


def test_exhausting_the_krw_wallet_does_not_block_us_entries(tmp_path):
    books = _dual_books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    # KR 지갑을 거의 다 쓴다.
    books.apply_fill("s", KR_SYMBOL, Side.BUY, 142.0, KR_PRICE, 1000.0, "KR", fx)
    risk = _risk(books)

    order_kr = risk.approve(_entry(KR_SYMBOL, target_weight=1.0), _ctx())
    assert order_kr is None, "KR 지갑이 바닥났으니 KR 진입은 막혀야 한다"

    order_us = risk.approve(_entry(US_SYMBOL, target_weight=0.5), _ctx())
    assert order_us is not None, "US 지갑은 멀쩡한데 KR 지갑 소진 때문에 막혔다"
    assert order_us.qty > 0


def test_strategy_equity_is_the_sum_of_both_wallets(tmp_path):
    """사이징 목표(equity)는 두 지갑의 합 — 현금 게이트만 분리된다는 것과
    구분되는 지점이다(모듈 docstring 참고)."""
    books = _dual_books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    equity = books.equity_krw("s", {}, fx)
    assert equity == pytest.approx(INITIAL_KRW + INITIAL_USD * FX_RATE)
