"""capital_mode: per_strategy(전략별 독립 명목계정) — RiskManagerImpl 통합 테스트.

고정하는 것(2026-08-19 사용자 지시 "각 전략마다 따로 노는 것"):
- capital_mode: shared(기본)에서는 books가 주입돼 있어도 기존 동작이 완전히
  보존된다(books를 아예 참조하지 않는다).
- 두 전략이 같은 종목을 사도 사이징이 서로의 book에 전혀 영향받지 않는다.
- 한 전략이 자기 몫 현금을 다 쓰면 그 전략만 거부되고 다른 전략은 계속 승인된다.
- per_strategy 모드는 capital_fraction 게이트(시장별 배분 0 차단)를 참조하지 않는다
  (설계 결정 — manager.py `_approve_entry_per_strategy` docstring 참고).
- 전략별 일일 손실 한도 / 일일 진입 상한이 분리된다.
"""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.fx import FixedFxProvider
from quant.core.models import Position, Quote, Side, Signal, SignalAction
from quant.core.ports import Context
from quant.trade.risk.books import StrategyBooks
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
SYMBOL = "TQQQ"
MARKET_OF = {SYMBOL: "US"}
FX_RATE = 1500.0
PRICE = 100.0
INITIAL_KRW = 10_000_000.0


class _Broker:
    """approve()가 항상 조회하는 계좌 전체 positions()/cash() — per_strategy 모드의
    entry 사이징에는 쓰이지 않지만(books가 대신한다), 함수 상단에서 무조건
    호출되므로 존재는 해야 한다. 넉넉하게 잡아 계좌 전체 쪽 레일에 걸리지
    않게 한다."""

    def __init__(self, cash: float = 1_000_000_000.0, positions=None):
        self._cash = cash
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self) -> float:
        return self._cash


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=NOW, price=PRICE)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _ctx(fake_clock_cls, cash: float = 1_000_000_000.0, positions=None) -> Context:
    return Context(clock=fake_clock_cls(now=NOW), data=_Data(), broker=_Broker(cash, positions))


def _books(tmp_path, initial_krw: float = INITIAL_KRW) -> StrategyBooks:
    return StrategyBooks.load(tmp_path / "strategy_books.json", initial_krw=initial_krw)


def _risk(books, capital_mode="per_strategy", capital_fraction=None, **overrides) -> RiskManagerImpl:
    cfg = dict(
        capital_mode=capital_mode,
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
        {"risk": cfg}, capital_fraction=capital_fraction or {},
        market_of=MARKET_OF, fx=FixedFxProvider(FX_RATE), books=books,
    )


def _entry(strategy_id: str, target_weight: float = 1.0, symbol: str = SYMBOL) -> Signal:
    return Signal(strategy_id=strategy_id, symbol=symbol, action=SignalAction.ENTER_LONG,
                  target_weight=target_weight)


def _entry_qty(strategy_id: str, target_qty: float, symbol: str = SYMBOL) -> Signal:
    """target_qty(고정 수량 지정, 2026-08-19 frgn_accumulate 고정액→고정수량 전환)
    신호. target_weight=0.0은 target_qty가 있을 때 참조되지 않는 자리표시자."""
    return Signal(strategy_id=strategy_id, symbol=symbol, action=SignalAction.ENTER_LONG,
                  target_weight=0.0, target_qty=target_qty)


def _expected_qty(krw_budget: float) -> float:
    return math.floor(krw_budget / FX_RATE / PRICE)


# ------------------------------------------------------------- shared 모드

def test_shared_mode_ignores_books_entirely(fake_clock_cls, tmp_path):
    """capital_mode: shared(기본)이면 books가 주입돼 있어도 무시하고 계좌 전체
    equity(여기서는 계좌 현금 100만원)를 기준으로 사이징한다."""
    books = _books(tmp_path)
    risk = _risk(books, capital_mode="shared", capital_fraction={"s": 1.0})

    order = risk.approve(_entry("s"), _ctx(fake_clock_cls, cash=1_000_000.0))

    assert order is not None
    assert order.qty == pytest.approx(_expected_qty(1_000_000.0))


def test_default_capital_mode_is_shared(fake_clock_cls, tmp_path):
    """capital_mode를 설정하지 않으면 shared다 — books를 넘겨도 무시된다."""
    books = _books(tmp_path)
    risk = RiskManagerImpl(
        {"risk": {"sizing_mode": "capital_fraction", "max_position_pct": 100,
                  "max_symbol_pct_total": 0, "max_order_notional_pct": 0,
                  "cooldown_bars_after_stop": 0}},
        capital_fraction={"s": 1.0}, market_of=MARKET_OF, fx=FixedFxProvider(FX_RATE),
        books=books,
    )
    assert risk.capital_mode == "shared"
    order = risk.approve(_entry("s"), _ctx(fake_clock_cls, cash=1_000_000.0))
    assert order.qty == pytest.approx(_expected_qty(1_000_000.0))


# --------------------------------------------------------- 전략 간 독립 사이징

def test_two_strategies_size_independently_from_own_books(fake_clock_cls, tmp_path):
    books = _books(tmp_path)
    # "a"는 이미 지출이 있어 현금이 100만원뿐, "b"는 새 전략(1,000만원 그대로).
    books.books["a"] = {
        "cash_krw": 1_000_000.0, "realized_pnl_krw": 0.0, "fees_krw": 0.0,
        "positions": {}, "updated": "2026-01-01T00:00:00Z",
    }
    risk = _risk(books, capital_fraction={"a": 1.0, "b": 1.0})

    order_a = risk.approve(_entry("a"), _ctx(fake_clock_cls))
    order_b = risk.approve(_entry("b"), _ctx(fake_clock_cls))

    assert order_a.qty == pytest.approx(_expected_qty(1_000_000.0))
    assert order_b.qty == pytest.approx(_expected_qty(INITIAL_KRW))
    assert order_a.qty != order_b.qty


def test_strategy_exhausting_cash_blocks_only_that_strategy(fake_clock_cls, tmp_path):
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    # "a"의 현금을 거의 다 쓴다(1주도 못 살 만큼).
    books.apply_fill("a", SYMBOL, Side.BUY, 66.0, PRICE, 0.0, "US", fx)
    risk = _risk(books, capital_fraction={"a": 1.0, "b": 1.0})

    order_a = risk.approve(_entry("a"), _ctx(fake_clock_cls))
    assert order_a is None
    assert "a" in risk.last_block  # last_block은 다음 approve() 호출에서 리셋되므로 즉시 확인

    order_b = risk.approve(_entry("b"), _ctx(fake_clock_cls))
    assert order_b is not None
    assert order_b.qty == pytest.approx(_expected_qty(INITIAL_KRW))


def test_explicit_cash_gate_blocks_when_room_exceeds_actual_cash(fake_clock_cls, tmp_path):
    """max_position_pct 등으로 계산된 room은 있어도(포지션 평가액이 커서 equity가
    높음) 실제 현금이 부족하면 전략별 현금 게이트가 막는다."""
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    # 현금을 거의 다 써서 큰 포지션을 만든다: equity는 크지만 가용현금은 작다.
    books.apply_fill("a", SYMBOL, Side.BUY, 66.0, PRICE, 0.0, "US", fx)
    risk = _risk(books, capital_fraction={"a": 1.0}, max_position_pct=100)

    order = risk.approve(_entry("a", target_weight=1.0), _ctx(fake_clock_cls))

    assert order is None
    assert "가용현금" in risk.last_block or "배분 자금 부족" in risk.last_block


# --------------------------------- capital_fraction: 값은 버리되 시장 허용은 유지
#
# 2026-08-19 배포 전 수정: capital_fraction은 (a) 사이징 비중 (b) "이 전략은 이
# 시장에서 아예 거래하지 않는다"는 차단, 두 가지를 겸해왔다. donchian처럼 심볼이
# 고정된 전략은 심볼 구성 자체가 (b)를 보장하지만, `universe: watchlist`로
# 관심종목에서 심볼을 받는 전략(news_momentum/news_scalp/frgn_accumulate)은
# 그렇지 않다 — 관심종목에 US 심볼이 섞여 있으면 KR 전용으로 설계된 전략이 US
# 신호를 낼 수 있다. per_strategy 모드는 (a)만 버리고 (b)는 그대로 유지한다.

KR_SYMBOL = "005930"


def test_zero_capital_fraction_blocks_that_market_in_per_strategy_mode(fake_clock_cls, tmp_path):
    """US: 0.0으로 선언된 전략은 per_strategy 모드에서도 US 진입이 차단된다 —
    watchlist에 US 심볼이 섞여 있어도 이 전략이 그걸 살 수 없게 막는 것이 이
    게이트의 존재 이유다."""
    books = _books(tmp_path)
    risk = _risk(books, capital_fraction={"news_momentum": {"KR": 0.1, "US": 0.0}})

    order = risk.approve(_entry("news_momentum"), _ctx(fake_clock_cls))  # SYMBOL=TQQQ(US)

    assert order is None
    assert "US 시장 배분이 0" in risk.last_block
    assert "news_momentum" in risk.last_block


def test_same_strategy_still_enters_kr_when_only_us_is_zero(fake_clock_cls, tmp_path):
    """같은 전략이라도 KR 배분이 0보다 크면 KR 심볼은 정상 진입한다."""
    books = _books(tmp_path)
    risk = _risk(books, capital_fraction={"news_momentum": {"KR": 0.1, "US": 0.0}})
    risk.market_of[KR_SYMBOL] = "KR"

    order = risk.approve(_entry("news_momentum", symbol=KR_SYMBOL), _ctx(fake_clock_cls))

    assert order is not None


def test_capital_fraction_magnitude_does_not_affect_sizing(fake_clock_cls, tmp_path):
    """KR 0.05로 선언된 전략과 KR 0.3으로 선언된 전략의 주문 크기가 동일해야
    한다 — per_strategy 모드는 값의 크기를 사이징에 쓰지 않는다(이중 분할 방지).
    둘 다 0보다 크기만 하면 통과하는 boolean 게이트만 남는다."""
    books = _books(tmp_path)
    risk = _risk(
        books,
        capital_fraction={"small": {"KR": 0.05, "US": 0.0}, "big": {"KR": 0.3, "US": 0.0}},
    )
    risk.market_of[KR_SYMBOL] = "KR"

    order_small = risk.approve(_entry("small", symbol=KR_SYMBOL), _ctx(fake_clock_cls))
    order_big = risk.approve(_entry("big", symbol=KR_SYMBOL), _ctx(fake_clock_cls))

    assert order_small is not None and order_big is not None
    assert order_small.qty == pytest.approx(order_big.qty)


def test_missing_capital_fraction_declaration_blocks_in_per_strategy_mode(fake_clock_cls, tmp_path):
    """capital_fraction 설정 자체가 없는 전략(dict에 아예 없음)은
    `_capital_fraction_for`가 0.0으로 취급하므로(기존 규약) per_strategy
    모드에서도 차단된다 — "모르면 안전한 쪽" 원칙이 여기서도 유지된다."""
    books = _books(tmp_path)
    risk = _risk(books, capital_fraction={})

    order = risk.approve(_entry("brand_new_strategy"), _ctx(fake_clock_cls))

    assert order is None
    assert "시장 배분이 0" in risk.last_block


def test_scalar_capital_fraction_allows_any_market_in_per_strategy_mode(fake_clock_cls, tmp_path):
    """스칼라로 선언된 전략(시장별 dict가 아님)은 기존 `_capital_fraction_for`
    규약대로 시장 무관 동일값 — 0보다 크면 어느 시장이든 허용된다."""
    books = _books(tmp_path)
    risk = _risk(books, capital_fraction={"donchian": 0.4})
    risk.market_of[KR_SYMBOL] = "KR"

    order_us = risk.approve(_entry("donchian"), _ctx(fake_clock_cls))  # TQQQ(US)
    order_kr = risk.approve(_entry("donchian", symbol=KR_SYMBOL), _ctx(fake_clock_cls))

    assert order_us is not None
    assert order_kr is not None


# --------------------------------------------------------- 전략별 일일 한도

def test_daily_loss_limit_is_per_strategy(fake_clock_cls, tmp_path):
    books = _books(tmp_path)
    risk = _risk(books, capital_fraction={"a": 1.0, "b": 1.0}, daily_loss_limit_pct=5)

    # 첫 호출(target_weight=0 → 예산 0으로 거부되지만, 그 과정에서 오늘의 시작
    # 자산이 "a"에 대해 스냅샷된다).
    first = risk.approve(_entry("a", target_weight=0.0), _ctx(fake_clock_cls))
    assert first is None  # 예산 부족으로 거부(딴 이유), 시작자산 스냅샷 목적

    # "a"만 20% 손실을 시뮬레이션(장부 현금을 직접 깎는다).
    books.books["a"]["cash_krw"] -= INITIAL_KRW * 0.20

    blocked = risk.approve(_entry("a", target_weight=1.0), _ctx(fake_clock_cls))
    assert blocked is None
    assert "일일 손실 한도" in risk.last_block
    assert "a" in risk.last_block

    still_ok = risk.approve(_entry("b", target_weight=1.0), _ctx(fake_clock_cls))
    assert still_ok is not None  # "b"는 손실이 없으므로 그대로 승인


def test_max_orders_per_day_is_per_strategy(fake_clock_cls, tmp_path):
    books = _books(tmp_path)
    risk = _risk(books, capital_fraction={"a": 1.0, "b": 1.0}, max_orders_per_day=1)

    first_a = risk.approve(_entry("a"), _ctx(fake_clock_cls))
    assert first_a is not None

    second_a = risk.approve(_entry("a"), _ctx(fake_clock_cls))
    assert second_a is None
    assert "일일 진입 상한" in risk.last_block

    first_b = risk.approve(_entry("b"), _ctx(fake_clock_cls))
    assert first_b is not None  # "b"의 예산은 "a"가 소진하지 않았다


# --------------------------------------------------------- target_qty(고정 수량 지정)
#
# 2026-08-19 frgn_accumulate 고정액→고정수량 전환의 핵심 회귀 테스트. 사고 실측:
# 001450 4주 @ 50,013 = 200,050원(의도 100,000원) — 원인은 target_weight =
# fixed_amount_krw / assumed_capital_krw 근사에서 assumed_capital_krw(500만)가
# 실제 전략 자본(1,000만, per_strategy 도입) 변경을 못 따라가 매수액이 2배가 된
# 것. target_qty는 전략 자본 규모를 아예 참조하지 않으므로 이 사고 자체가
# 구조적으로 재발 불가능해야 한다.

def test_target_qty_ignores_capital_size(fake_clock_cls, tmp_path):
    """같은 target_qty를 서로 다른 전략 자본(500만 vs 1,000만)에 요청해도 승인
    수량은 동일해야 한다 — 이번 사고(자본 규모 변경에 매수액이 좌우됨)의 직접
    회귀 테스트."""
    books_small = _books(tmp_path / "small", initial_krw=5_000_000.0)
    books_big = _books(tmp_path / "big", initial_krw=10_000_000.0)
    risk_small = _risk(books_small, capital_fraction={"frgn": 1.0})
    risk_big = _risk(books_big, capital_fraction={"frgn": 1.0})

    order_small = risk_small.approve(_entry_qty("frgn", target_qty=1), _ctx(fake_clock_cls))
    order_big = risk_big.approve(_entry_qty("frgn", target_qty=1), _ctx(fake_clock_cls))

    assert order_small is not None and order_big is not None
    assert order_small.qty == order_big.qty == 1


def test_target_qty_produces_exact_requested_quantity(fake_clock_cls, tmp_path):
    books = _books(tmp_path)
    risk = _risk(books, capital_fraction={"frgn": 1.0})

    order = risk.approve(_entry_qty("frgn", target_qty=1), _ctx(fake_clock_cls))

    assert order is not None
    assert order.qty == 1
    assert order.side == Side.BUY


def test_target_qty_still_capped_by_max_position_pct_room(fake_clock_cls, tmp_path):
    """하드레일 우회 금지 — target_qty를 아무리 크게 불러도 max_position_pct room을
    넘는 만큼은 잘린다(주문 자체가 거부되진 않되 수량이 줄어든다)."""
    books = _books(tmp_path, initial_krw=INITIAL_KRW)
    risk = _risk(books, capital_fraction={"frgn": 1.0}, max_position_pct=100)
    # room = 100% x 10,000,000 = 10,000,000원 → 최대 수량 = floor(10,000,000/1500/100) = 66
    max_affordable = math.floor(INITIAL_KRW / FX_RATE / PRICE)

    order = risk.approve(_entry_qty("frgn", target_qty=1000), _ctx(fake_clock_cls))

    assert order is not None
    assert order.qty == max_affordable
    assert order.qty < 1000


def test_target_qty_blocked_when_cash_insufficient(fake_clock_cls, tmp_path):
    """전략별 현금 게이트는 target_qty에도 그대로 적용된다 — 살 현금이 없으면
    수량이 아니라 주문 자체가 거부되고 사유가 남는다."""
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    books.apply_fill("frgn", SYMBOL, Side.BUY, 66.0, PRICE, 0.0, "US", fx)  # 현금 거의 소진
    risk = _risk(books, capital_fraction={"frgn": 1.0})

    order = risk.approve(_entry_qty("frgn", target_qty=1), _ctx(fake_clock_cls))

    assert order is None
    assert "frgn" in risk.last_block


def test_target_qty_in_shared_capital_mode_also_applies_hard_rails(fake_clock_cls, tmp_path):
    """capital_mode: shared에서도 target_qty가 room 하드레일에 그대로 걸린다 —
    per_strategy 전용 경로가 아니라 공통 계약이어야 한다(다른 전략이 나중에
    shared 모드에서 이 필드를 써도 안전하도록)."""
    books = _books(tmp_path)
    risk = _risk(books, capital_mode="shared", capital_fraction={"frgn": 1.0}, max_position_pct=100)
    max_affordable = math.floor(1_000_000.0 / FX_RATE / PRICE)

    order = risk.approve(
        _entry_qty("frgn", target_qty=1000), _ctx(fake_clock_cls, cash=1_000_000.0),
    )

    assert order is not None
    assert order.qty == max_affordable
    assert order.qty < 1000


def test_weight_based_signals_unaffected_by_target_qty_feature(fake_clock_cls, tmp_path):
    """target_qty=None(기본, 다른 9개 전략 전부)이면 기존 target_weight 사이징이
    100% 그대로다 — Signal.target_qty 필드 추가가 회귀를 만들지 않았음을 명시적으로
    증명."""
    books = _books(tmp_path)
    risk = _risk(books, capital_fraction={"donchian": 1.0})

    order = risk.approve(_entry("donchian", target_weight=1.0), _ctx(fake_clock_cls))

    assert order is not None
    assert order.qty == pytest.approx(_expected_qty(INITIAL_KRW))
