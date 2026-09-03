"""PaperBroker 통화별 현금 지갑 분리(2026-09-01 소유자 지시) 테스트.

"모의 포트폴리오를 완전히 초기화하고, 실제 토스 계좌 스냅샷을 이어받아
모의투자로 진행하라. 원화는 원화로, 달러는 달러로만(환전 금지)."

이 스위트가 고정하는 것:
- dual_currency=True면 KR 체결은 portfolio.cash(KRW)만, US 체결은
  portfolio.cash_usd(USD)만 움직인다 — 서로 절대 섞이지 않는다(환전 없음).
- dual_currency=False(기본값)는 기존 동작(단일 KRW 풀, US 체결도 환산해
  같은 풀을 씀) 100% 보존 — 백테스트가 이 인자를 넘기지 않으므로 결과가
  바뀌지 않는다.
- PaperBroker.cash_usd()는 dual_currency=False에서 None을 돌려줘, risk/manager.py
  의 duck-typed USD 게이트가 조용히 건너뛴다(백테스트 US 진입 사이징 불변).
  dual_currency=True에서는 실제 USD 풀 잔액을 돌려줘 게이트가 실제로 동작한다.
- Portfolio의 cash_usd 필드는 구버전 portfolio.json(필드 없음)도 0.0으로
  안전하게 읽는다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.adapters.execution.paper import PaperBroker
from quant.core.fx import FixedFxProvider
from quant.core.models import Order, Quote, Side, Signal, SignalAction
from quant.core.portfolio.portfolio import Portfolio
from quant.core.ports import Context
from quant.trade.risk.manager import RiskManagerImpl

FX_RATE = 1400.0


class _Feed:
    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    def quote(self, symbol):
        price = self._prices.get(symbol)
        if price is None:
            return None
        return Quote(symbol=symbol, ts=datetime.now(timezone.utc), price=price)

    def history(self, symbol, interval, n):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _broker(dual_currency: bool, cash: float = 1_000_000.0, cash_usd: float = 100.0) -> PaperBroker:
    portfolio = Portfolio(cash=cash, cash_usd=cash_usd, state_path=None)
    return PaperBroker(
        data=_Feed({"005930": 70_000.0, "TQQQ": 50.0}),
        portfolio=portfolio,
        fee_bps=0.0,
        market_of={"005930": "KR", "TQQQ": "US"},
        fx=FixedFxProvider(FX_RATE),
        dual_currency=dual_currency,
    )


# --- (a) 통화별 지갑 분리 -----------------------------------------------------

def test_kr_buy_debits_only_krw_pool():
    broker = _broker(dual_currency=True, cash=1_000_000.0, cash_usd=100.0)
    broker.place_order(Order(symbol="005930", side=Side.BUY, qty=1, strategy_id="t"))
    assert broker.portfolio.cash == pytest.approx(1_000_000.0 - 70_000.0)
    assert broker.portfolio.cash_usd == pytest.approx(100.0), "KR 체결이 USD 풀을 건드리면 안 된다"


def test_us_buy_debits_only_usd_pool():
    broker = _broker(dual_currency=True, cash=1_000_000.0, cash_usd=100.0)
    broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=1, strategy_id="t"))
    assert broker.portfolio.cash_usd == pytest.approx(100.0 - 50.0)
    assert broker.portfolio.cash == pytest.approx(1_000_000.0), "US 체결이 KRW 풀을 건드리면 안 된다"


def test_us_sell_credits_only_usd_pool():
    broker = _broker(dual_currency=True, cash=1_000_000.0, cash_usd=100.0)
    broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=1, strategy_id="t"))
    krw_after_buy = broker.portfolio.cash
    broker.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=1, strategy_id="t"))
    assert broker.portfolio.cash_usd == pytest.approx(100.0)  # 사고 판 왕복 — 수수료 0
    assert broker.portfolio.cash == pytest.approx(krw_after_buy), "매도도 KRW 풀을 건드리면 안 된다"


def test_no_conversion_code_path_krw_and_usd_pools_never_mix():
    """왕복(KR 매수+매도, US 매수+매도)을 섞어도 두 풀은 각자 시작값으로 돌아온다
    (환전이 있었다면 어느 한쪽이 소수점 이하로 어긋난다)."""
    broker = _broker(dual_currency=True, cash=1_000_000.0, cash_usd=100.0)
    broker.place_order(Order(symbol="005930", side=Side.BUY, qty=2, strategy_id="t"))
    broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=1, strategy_id="t"))
    broker.place_order(Order(symbol="005930", side=Side.SELL, qty=2, strategy_id="t"))
    broker.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=1, strategy_id="t"))
    assert broker.portfolio.cash == pytest.approx(1_000_000.0)
    assert broker.portfolio.cash_usd == pytest.approx(100.0)


# --- dual_currency=False 하위호환 ---------------------------------------------

def test_dual_currency_false_keeps_single_krw_pool_behaviour():
    """기본값(dual_currency=False)에서는 US 체결도 KRW 풀을 환산해서 쓴다 —
    기존 동작(백테스트가 지금까지 봐온 결과) 그대로."""
    broker = _broker(dual_currency=False, cash=1_000_000.0, cash_usd=0.0)
    broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=1, strategy_id="t"))
    assert broker.portfolio.cash == pytest.approx(1_000_000.0 - 50.0 * FX_RATE)
    assert broker.portfolio.cash_usd == pytest.approx(0.0), "꺼져 있으면 USD 풀은 손대지 않는다"


def test_dual_currency_false_cash_usd_returns_none():
    """기본값에서 cash_usd()는 None — risk/manager.py의 duck-typed 게이트가
    이를 '게이트 건너뛰기'로 취급해 백테스트 US 사이징이 바뀌지 않는다."""
    broker = _broker(dual_currency=False)
    assert broker.cash_usd() is None


def test_dual_currency_true_cash_usd_returns_real_balance():
    broker = _broker(dual_currency=True, cash_usd=123.45)
    assert broker.cash_usd() == pytest.approx(123.45)


# --- (b) USD 잔고 부족 시 US 진입이 실제 게이트를 경유해 clamp됨 -------------

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)


class _AlwaysOpenClock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    def is_market_open(self, market):
        return True


def test_real_paper_broker_usd_shortfall_clamps_entry_via_existing_gate():
    """새 게이트를 만들지 않는다 — risk/manager.py에 이미 있는 duck-typed
    cash_usd() 게이트가 실제 PaperBroker(dual_currency=True)와도 그대로
    맞물려 US 진입 수량을 실제 USD 잔고로 clamp해야 한다."""
    broker = _broker(dual_currency=True, cash=100_000_000.0, cash_usd=75.0)  # $75 → 1주(가격 $50)
    risk = RiskManagerImpl(
        {"risk": dict(
            sizing_mode="cash_pct", max_position_pct=100, max_symbol_pct_total=0,
            daily_loss_limit_pct=100, max_orders_per_day=0, cooldown_bars_after_stop=0,
            max_order_notional_pct=0, max_total_exposure_pct=0, max_concurrent_positions=0,
        )},
        capital_fraction={"t": 1.0}, market_of={"TQQQ": "US"}, fx=FixedFxProvider(FX_RATE),
    )
    ctx = Context(clock=_AlwaysOpenClock(NOW), data=broker.data, broker=broker)
    signal = Signal(strategy_id="t", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=1.0)

    order = risk.approve(signal, ctx)

    assert order is not None
    assert order.qty == 1  # floor(75/50) — KRW 예산은 훨씬 크므로 USD 잔고가 상한


def test_real_paper_broker_usd_exhausted_blocks_entry():
    broker = _broker(dual_currency=True, cash=100_000_000.0, cash_usd=10.0)  # $10 < 1주($50)
    risk = RiskManagerImpl(
        {"risk": dict(
            sizing_mode="cash_pct", max_position_pct=100, max_symbol_pct_total=0,
            daily_loss_limit_pct=100, max_orders_per_day=0, cooldown_bars_after_stop=0,
            max_order_notional_pct=0, max_total_exposure_pct=0, max_concurrent_positions=0,
        )},
        capital_fraction={"t": 1.0}, market_of={"TQQQ": "US"}, fx=FixedFxProvider(FX_RATE),
    )
    ctx = Context(clock=_AlwaysOpenClock(NOW), data=broker.data, broker=broker)
    signal = Signal(strategy_id="t", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=1.0)

    order = risk.approve(signal, ctx)

    assert order is None
    assert "자금 부족" in risk.last_block
    assert "USD" in risk.last_block


# --- Portfolio.cash_usd 영속화 + 구버전 스키마 하위호환 -----------------------

def test_portfolio_save_persists_cash_usd(tmp_path):
    p = Portfolio(cash=1000.0, cash_usd=50.0, state_path=tmp_path / "portfolio.json")
    p.save()
    import json
    data = json.loads((tmp_path / "portfolio.json").read_text())
    assert data["cash_usd"] == 50.0


def test_portfolio_load_or_init_reads_legacy_schema_without_cash_usd_field(tmp_path):
    """cash_usd 필드가 아예 없는(통화 분리 도입 이전) portfolio.json도 읽혀야 한다 —
    0.0으로 폴백."""
    import json
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({"cash": 12345.0, "positions": {}}), encoding="utf-8")

    restored = Portfolio.load_or_init(start_cash=0.0, state_path=path)

    assert restored.cash == 12345.0
    assert restored.cash_usd == 0.0


def test_portfolio_load_or_init_reads_cash_usd_when_present(tmp_path):
    import json
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({"cash": 1.0, "cash_usd": 99.0, "positions": {}}), encoding="utf-8")

    restored = Portfolio.load_or_init(start_cash=0.0, state_path=path)

    assert restored.cash_usd == 99.0


def test_equity_includes_usd_cash_converted_to_krw():
    p = Portfolio(cash=0.0, cash_usd=100.0, positions={}, state_path=None)
    assert p.equity({}, fx=FixedFxProvider(FX_RATE)) == pytest.approx(100.0 * FX_RATE)


def test_equal_split_includes_usd_pool_when_fx_given():
    """2026-09-01 실기동 결함 재현 방지 — KRW만 나누면 US 레인 명목예산이 $180
    수준이 되어 1주도 못 사는 침묵 무거래가 된다. fx가 주어지면 USD 풀을 KRW
    환산해 합산하고, cash_usd가 None(비활성)이면 기존 동작(KRW만)을 유지한다."""
    from quant.apps.assembly import equal_split_initial_krw

    class _FakeBroker:
        def __init__(self, krw, usd):
            self._krw, self._usd = krw, usd
        def cash(self):
            return self._krw
        def cash_usd(self):
            return self._usd

    class _FakeFx:
        def usd_krw(self):
            return 1000.0

    # USD 포함: (1_000_000 + 500 * 1000) / 3 = 500_000
    got = equal_split_initial_krw(_FakeBroker(1_000_000, 500.0), ["a", "b", "c"], fx=_FakeFx())
    assert got == 500_000.0
    # cash_usd 비활성(None) → KRW만 (구 동작 보존)
    got = equal_split_initial_krw(_FakeBroker(1_000_000, None), ["a", "b"], fx=_FakeFx())
    assert got == 500_000.0
    # fx 미주입 → KRW만 (구 호출부 하위호환)
    got = equal_split_initial_krw(_FakeBroker(1_000_000, 500.0), ["a", "b"])
    assert got == 500_000.0


# --- (d) cash_after_usd 기록 (2026-09-02) --------------------------------------

def test_fill_records_usd_cash_snapshot_under_dual_currency():
    """`cash_after`는 시장 무관 항상 KRW 풀이라 US 체결의 현금 변화를 전혀 남기지
    못했고, 세션 리포트가 "US … 계좌 현금 변화 +0원"이라는 거짓을 찍었다."""
    broker = _broker(dual_currency=True, cash=1_000_000.0, cash_usd=100.0)
    state = broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=1, strategy_id="t"))
    fill = state.fill
    assert fill.cash_after == pytest.approx(1_000_000.0)      # KRW 풀은 그대로
    assert fill.cash_after_usd == pytest.approx(50.0)         # USD 풀이 실제로 줄었다


def test_fill_usd_cash_snapshot_is_none_without_dual_currency():
    """dual_currency=False면 USD 풀이 개념적으로 없다 — 0.0을 적으면 "USD 현금이
    0이었다"는 사실 주장이 된다(cash_usd()가 None을 주는 것과 같은 계약)."""
    broker = _broker(dual_currency=False, cash=1_000_000.0, cash_usd=100.0)
    state = broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=1, strategy_id="t"))
    assert state.fill.cash_after_usd is None
