"""Cross-mode rail parity — `RiskManagerImpl.approve()` (2026-09-06 안정성 감사,
§1 "두 사이징 코드 경로" 항목).

`capital_mode: per_strategy`(`_approve_entry_per_strategy`)와 `capital_mode:
shared`(inline)는 `max_concurrent_positions`/`min_order_notional_krw`/
`max_total_exposure_pct`/`max_leveraged_exposure_pct` 네 레일을 각자 다른 장부
(전략별 books vs 계좌 전체)를 보고 독립적으로 재구현한다. 감사 당시엔 drift가
없었지만, "새 레일을 한쪽 분기에만 추가하고 잊는" 회귀를 잡는 테스트가 없다는
점이 지적됐다 — 이 파일이 그 gap을 메운다.

방법: 두 모드에 **동일한 경제적 조건**(같은 현금/자산, 같은 기존 노출)을
만들고, 같은 신호를 통과시켜 두 모드가 같은 판정(둘 다 승인 또는 둘 다 거부)을
내리는지 확인한다. `sizing_mode: cash_pct`를 써서 두 모드의 예산 산식을
`가용현금 × target_weight`로 맞춘다(shared는 `ctx.broker.cash()`, per_strategy는
`books.available_cash_krw_for_market`) — 두 산식의 모양이 이미 동일하므로,
입력값(현금/기존 포지션)만 맞추면 결과도 같아야 한다.

전부 KR 심볼만 쓴다 — market="KR"은 `to_krw`가 항등 변환이라 환율 반올림이
레일 판정 경계값에 끼어드는 것을 피한다."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.core.fx import FixedFxProvider
from quant.core.models import Position, Quote, Signal, SignalAction
from quant.core.ports import Context
from quant.trade.risk.books import StrategyBooks
from quant.trade.risk.manager import RiskManagerImpl

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=KST)
STRATEGY_ID = "parity_test"
SYMBOL = "000660"  # 신규 진입 대상
OTHER = "005930"  # 기존 보유(노출/동시보유 상한을 채우는 용도)
MARKET_OF = {SYMBOL: "KR", OTHER: "KR"}
PRICE = 10_000.0


class _Broker:
    def __init__(self, cash: float, positions: dict[str, Position] | None = None):
        self._cash = cash
        self._positions = positions or {}

    def positions(self) -> dict[str, Position]:
        return self._positions

    def cash(self) -> float:
        return self._cash


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=NOW, price=PRICE)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _base_cfg(capital_mode: str, **overrides) -> dict:
    cfg = dict(
        capital_mode=capital_mode,
        sizing_mode="cash_pct",
        max_position_pct=100,
        max_symbol_pct_total=0,
        daily_loss_limit_pct=100,
        max_orders_per_day=1000,
        cooldown_bars_after_stop=0,
        max_order_notional_pct=0,
        max_total_exposure_pct=0,
        max_concurrent_positions=0,
        min_order_notional_krw=0,
        max_leveraged_exposure_pct=0,
    )
    cfg.update(overrides)
    return cfg


def _entry(target_weight: float = 1.0) -> Signal:
    return Signal(
        strategy_id=STRATEGY_ID, symbol=SYMBOL, action=SignalAction.ENTER_LONG,
        target_weight=target_weight,
    )


def _shared(clock, cash: float, existing: dict[str, Position] | None = None, leverage_of=None, **overrides):
    risk = RiskManagerImpl(
        {"risk": _base_cfg("shared", **overrides)},
        capital_fraction={STRATEGY_ID: 1.0}, market_of=MARKET_OF,
        fx=FixedFxProvider(1500.0), leverage_of=leverage_of,
    )
    ctx = Context(clock=clock, data=_Data(), broker=_Broker(cash, existing))
    return risk, ctx


def _per_strategy(
    clock, tmp_path, cash: float, existing: dict | None = None, leverage_of=None, **overrides,
):
    books = StrategyBooks.load(tmp_path / "books.json", initial_krw=cash)
    books._ensure(STRATEGY_ID)  # cash=cash로 장부를 미리 만들어 둔다
    if existing:
        books.books[STRATEGY_ID]["positions"] = existing
    risk = RiskManagerImpl(
        {"risk": _base_cfg("per_strategy", **overrides)},
        capital_fraction={STRATEGY_ID: 1.0}, market_of=MARKET_OF,
        fx=FixedFxProvider(1500.0), books=books, leverage_of=leverage_of,
    )
    # per_strategy 사이징은 books만 본다 — ctx.broker는 approve() 상단의 공용
    # equity/포지션 조회에만 쓰이므로 넉넉한 더미면 충분하다(shared 레일과
    # 뒤섞이지 않게 빈 포지션으로 둔다).
    ctx = Context(clock=clock, data=_Data(), broker=_Broker(cash))
    return risk, ctx


# fake_clock_cls는 conftest.py의 전역 fixture(quote/clock 계열 테스트가 공용으로 씀).


def test_max_concurrent_positions_blocks_identically_in_both_modes(fake_clock_cls, tmp_path):
    clock = fake_clock_cls(now=NOW)
    existing_symbol_qty = 10.0
    shared_risk, shared_ctx = _shared(
        clock, cash=10_000_000.0,
        existing={OTHER: Position(symbol=OTHER, qty=existing_symbol_qty, avg_cost=PRICE)},
        max_concurrent_positions=1,
    )
    per_risk, per_ctx = _per_strategy(
        clock, tmp_path, cash=10_000_000.0,
        existing={OTHER: {"qty": existing_symbol_qty, "avg_cost": PRICE, "market": "KR"}},
        max_concurrent_positions=1,
    )

    assert shared_risk.approve(_entry(), shared_ctx) is None
    assert per_risk.approve(_entry(), per_ctx) is None


def test_min_order_notional_krw_blocks_identically_in_both_modes(fake_clock_cls, tmp_path):
    clock = fake_clock_cls(now=NOW)
    # budget = cash * target_weight = 10,000,000 * 0.01 = 100,000원 -> qty=10주
    # -> notional=100,000원 < 200,000원 문턱 -> 둘 다 거부.
    shared_risk, shared_ctx = _shared(clock, cash=10_000_000.0, min_order_notional_krw=200_000)
    per_risk, per_ctx = _per_strategy(clock, tmp_path, cash=10_000_000.0, min_order_notional_krw=200_000)

    assert shared_risk.approve(_entry(target_weight=0.01), shared_ctx) is None
    assert per_risk.approve(_entry(target_weight=0.01), per_ctx) is None

    # 문턱을 낮추면(같은 조건) 둘 다 승인돼야 한다 — "항상 거부"가 아니라 문턱이
    # 실제로 판정을 가른다는 것도 같이 확인한다.
    shared_risk2, shared_ctx2 = _shared(clock, cash=10_000_000.0, min_order_notional_krw=50_000)
    per_risk2, per_ctx2 = _per_strategy(clock, tmp_path / "b2", cash=10_000_000.0, min_order_notional_krw=50_000)

    assert shared_risk2.approve(_entry(target_weight=0.01), shared_ctx2) is not None
    assert per_risk2.approve(_entry(target_weight=0.01), per_ctx2) is not None


def test_max_total_exposure_pct_blocks_identically_in_both_modes(fake_clock_cls, tmp_path):
    clock = fake_clock_cls(now=NOW)
    # equity = cash(10M) + 기존노출(10M) = 20M. cap = 50%*20M = 10M = 기존노출 그대로
    # -> room_portfolio = 0 -> 둘 다 거부.
    existing_qty = 1000.0  # qty*avg_cost(10,000) = 10,000,000원
    shared_risk, shared_ctx = _shared(
        clock, cash=10_000_000.0,
        existing={OTHER: Position(symbol=OTHER, qty=existing_qty, avg_cost=PRICE)},
        max_total_exposure_pct=50,
    )
    per_risk, per_ctx = _per_strategy(
        clock, tmp_path, cash=10_000_000.0,
        existing={OTHER: {"qty": existing_qty, "avg_cost": PRICE, "market": "KR"}},
        max_total_exposure_pct=50,
    )

    assert shared_risk.approve(_entry(), shared_ctx) is None
    assert per_risk.approve(_entry(), per_ctx) is None


def test_max_leveraged_exposure_pct_blocks_identically_in_both_modes(fake_clock_cls, tmp_path):
    clock = fake_clock_cls(now=NOW)
    # cash=10M, target_weight=1.0 -> budget=10M -> qty=1000 -> notional=10M.
    # this_lev=3배 -> added_lev_exposure=30M. cap=10%*10M=1M. 30M > 1M -> 거부.
    leverage_of = {SYMBOL: 3.0}
    shared_risk, shared_ctx = _shared(
        clock, cash=10_000_000.0, max_leveraged_exposure_pct=10, leverage_of=leverage_of,
    )
    per_risk, per_ctx = _per_strategy(
        clock, tmp_path, cash=10_000_000.0, max_leveraged_exposure_pct=10, leverage_of=leverage_of,
    )

    assert shared_risk.approve(_entry(), shared_ctx) is None
    assert per_risk.approve(_entry(), per_ctx) is None

    # leverage_of가 아예 주입 안 되면(None) 두 모드 다 이 레일을 완전히 건너뛴다.
    shared_risk2, shared_ctx2 = _shared(clock, cash=10_000_000.0, max_leveraged_exposure_pct=10)
    per_risk2, per_ctx2 = _per_strategy(
        clock, tmp_path / "b2", cash=10_000_000.0, max_leveraged_exposure_pct=10,
    )

    assert shared_risk2.approve(_entry(), shared_ctx2) is not None
    assert per_risk2.approve(_entry(), per_ctx2) is not None
