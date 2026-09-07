"""결함주입 2/6 — 브로커: 거부, 부분체결, 중복 체결 콜백, 체결가 괴리(슬리피지),
유니버스 밖 종목 체결, 보유 초과 매도(공매도 생성 금지). `quant/adapters/execution/
paper.py`(PaperBroker)만 대상 — 실브로커(Toss/Kiwoom)는 이 작업 범위 밖."""
from __future__ import annotations

from datetime import UTC, datetime

from quant.adapters.execution.paper import PaperBroker
from quant.core.models import Order, OrderStatus, Side
from quant.core.portfolio.portfolio import Portfolio


def _feed(price: float, ts=None):
    class _F:
        def quote(self, symbol):
            return _Quote(symbol, price, ts or datetime(2026, 1, 5, 10, 0, tzinfo=UTC))

    return _F()


class _Quote:
    def __init__(self, symbol, price, ts):
        self.symbol = symbol
        self.price = price
        self.ts = ts


def _broker(price=100.0, cash=1_000_000.0, **kwargs) -> PaperBroker:
    pf = Portfolio(cash=cash)
    return PaperBroker(data=_feed(price), portfolio=pf, market_of={"TQQQ": "US"}, **kwargs)


# --------------------------------------------------------------- 1. 거부 주문

def test_rejected_buy_zero_qty_not_submitted():
    b = _broker()
    state = b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=0, strategy_id="s"))
    assert state.status == OrderStatus.REJECTED
    assert state.broker_order_id is None
    assert b.positions() == {}


def test_rejected_sell_with_no_position():
    b = _broker()
    state = b.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=10, strategy_id="s"))
    assert state.status == OrderStatus.REJECTED
    assert "보유 수량 없음" in state.reason


def test_quote_missing_rejects_order_not_crash():
    class _NoQuote:
        def quote(self, symbol):
            return None

    pf = Portfolio(cash=1_000_000.0)
    b = PaperBroker(data=_NoQuote(), portfolio=pf, market_of={"TQQQ": "US"})
    state = b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=10, strategy_id="s"))
    assert state.status == OrderStatus.REJECTED
    assert state.broker_order_id is None


# ---------------------------------------------------------- 2. 보유 초과 매도

def test_sell_more_than_held_clamps_to_sellable_never_negative():
    """공매도 생성 불가 — 매도 요청이 보유량을 넘으면 보유량으로 잘라 체결하고,
    포지션은 절대 음수가 되지 않는다(롱 온리 불변식)."""
    b = _broker()
    b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=10, strategy_id="s"))
    state = b.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=1_000_000, strategy_id="s"))
    assert state.fill is not None
    assert state.fill.qty == 10  # 보유량으로 클램프
    pos = b.positions()["TQQQ"]
    assert pos.qty == 0.0
    assert pos.qty >= 0.0


def test_sell_more_than_held_across_repeated_attempts_never_goes_negative():
    """반복적으로 과다 매도를 시도해도(전략 버그로 같은 신호가 여러 번 발화하는
    상황을 흉내) 포지션 수량이 음수로 내려가지 않는다."""
    b = _broker()
    b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=5, strategy_id="s"))
    for _ in range(5):
        b.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=100, strategy_id="s"))
    pos = b.positions().get("TQQQ")
    qty = pos.qty if pos is not None else 0.0
    assert qty >= 0.0


def test_partial_fill_when_other_strategy_owns_part_of_the_lot():
    """다른 전략의 lot은 건드리지 않는다 — 이 전략 몫만 매도되는 부분체결."""
    b = _broker()
    b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=10, strategy_id="s1"))
    b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=5, strategy_id="s2"))
    state = b.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=999, strategy_id="s1"))
    assert state.fill.qty == 10  # s1 몫만
    pos = b.positions()["TQQQ"]
    assert pos.qty == 5.0  # s2 몫은 그대로


# --------------------------------------------------------- 3. 체결가 괴리(슬리피지)

def test_slippage_always_moves_price_against_the_trader():
    """슬리피지는 항상 불리한 방향 — 매수는 더 비싸게, 매도는 더 싸게."""
    b = _broker(price=100.0, slippage_bps=50.0)  # 0.5%
    buy = b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=1, strategy_id="s"))
    assert buy.fill.price > 100.0
    assert abs(buy.fill.price - 100.5) < 1e-6

    sell = b.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=1, strategy_id="s"))
    assert sell.fill.price < 100.0


def test_unknown_symbol_slippage_uses_max_of_dict_not_zero():
    """슬리피지 dict에 없는 종목은 0이 아니라 dict 최댓값을 쓴다 — 비용 과소평가 방지
    (모듈 docstring 명시 원칙의 회귀 테스트)."""
    b = _broker(price=100.0, slippage_bps={"005930": 10.0, "TQQQ": 20.0})
    # market_of에 없는 새 심볼 — 슬리피지 dict에도 없음
    pf = b.portfolio
    b2 = PaperBroker(data=_feed(100.0), portfolio=pf, slippage_bps={"005930": 10.0, "TQQQ": 20.0})
    state = b2.place_order(Order(symbol="UNKNOWN", side=Side.BUY, qty=1, strategy_id="s"))
    assert state.fill is not None
    # 20bp(최댓값) 적용 — 100 * 1.002 = 100.2
    assert abs(state.fill.price - 100.2) < 1e-6


# --------------------------------------------------- 4. 유니버스 밖 종목 체결

def test_fill_for_symbol_not_in_market_of_dict_does_not_crash():
    """market_of 힌트 dict에 없는 심볼(예: 장중 새로 편입됐거나 부팅 스냅샷 이후
    등록된 종목)도 market_of_symbol()로 시장을 추론해 정상 체결된다 — 크래시도,
    잘못된 시장으로의 오인도 없어야 한다."""
    pf = Portfolio(cash=10_000_000.0)
    b = PaperBroker(data=_feed(50_000.0), portfolio=pf, market_of={})  # 빈 힌트
    state = b.place_order(Order(symbol="005930", side=Side.BUY, qty=1, strategy_id="s"))
    assert state.fill is not None
    assert "005930" in b.positions()


# ------------------------------------------------- 5. 중복 체결 콜백(설계 한계 문서화)

def test_duplicate_place_order_call_double_fills_paper_broker():
    """PaperBroker는 즉시체결 모델이라 브로커 차원의 "중복 콜백"이 존재하지 않는다
    (open_orders()는 항상 빈 리스트, cancel_order()는 항상 False — 모듈 계약).
    대신 위험은 **호출부**(전략/루프)가 같은 신호를 두 번 실행하는 경우로 옮겨간다.
    이 테스트는 그 경우 PaperBroker가 스스로 중복을 막지 않음을 문서화한다 —
    실제 방어선은 quant/trade/loop.py의 승인 게이트 중복 억제
    (`_duplicate_of_recent`)와 리스크 매니저의 쿨다운/주문상한이다. PaperBroker
    수준에서 멱등성을 요구하는 것은 이 계층의 책임이 아니다(설계 한계, 버그 아님)."""
    b = _broker(cash=1_000_000.0)
    order = Order(symbol="TQQQ", side=Side.BUY, qty=10, strategy_id="s")
    b.place_order(order)
    b.place_order(order)  # 같은 Order 객체를 실수로 두 번 제출
    pos = b.positions()["TQQQ"]
    assert pos.qty == 20.0  # 방어되지 않는다 — 상위 계층(loop/risk)의 책임임을 확인
