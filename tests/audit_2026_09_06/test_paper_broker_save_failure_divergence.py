"""2026-09-06 안정성 감사 — P2 (희귀 트리거, 발생 시 조용한 장부 오염).
`PaperBroker.place_order`(quant/adapters/execution/paper.py)는 포지션/현금을
**메모리에서 먼저 갱신한 뒤** `self.portfolio.save()`를 호출한다(파일 하단
`self.portfolio.save()` 호출, place_order 본문 끝부분). 그 저장이 실패하면
(디스크 풀 — `OSError: No space left on device`, 권한 문제 등) 예외가 `place_order`
밖으로 그대로 전파된다.

`quant/trade/loop.py`의 `_execute_signal`은 `ctx.broker.place_order(order)` 호출을
`try/except Exception`으로 감싸 "주문 실행 실패"로 로그만 남기고 넘어간다 — **이 경로는
`sinks.on_fill()`을 절대 호출하지 않으므로 이 체결은 거래 원장(trades.jsonl)에
전혀 기록되지 않는다.**

문제는 `place_order` 안에서 메모리 상의 `Portfolio.positions`/`cash`는 예외가 나기
**전에** 이미 갱신됐다는 것이다 — 저장 실패는 그 갱신을 되돌리지 않는다. 그 결과
프로세스는 "존재하지만 원장에는 없는" 포지션/현금 변화를 메모리에 안은 채 계속
돈다. 디스크 공간이 회복된 뒤 (같은 심볼이든 다른 심볼이든) 다음 주문이 성공하면,
그 성공한 주문의 `portfolio.save()`가 이 유령 변화까지 함께 디스크에 박아 넣는다 —
`/status`·`/balance`·리스크 사이징·하드레일은 이 유령 포지션을 실체로 보는데,
`trades.jsonl` 기반 스코어보드·PnL 귀속·라운드트립 집계는 이 거래를 영원히 모른다.

**2026-09-06 수정 완료**: `PaperBroker.place_order`는 이제 변경 전 스냅샷(현금 +
해당 심볼 Position의 deepcopy)을 떠 두고, `self.portfolio.save()`가 실패하면
그 스냅샷으로 정확히 이번 주문이 만든 변경만 되돌린 뒤 원래 예외(OSError 등)를
그대로 재전파한다 — 호출부(`quant/trade/loop.py`의 `_execute_signal`)는 기존과
동일하게 그 예외를 잡아 "주문 실행 실패" 알림을 낸다. `Portfolio.save()` 자체도
write+fsync+rename으로 원자화했다(`quant/core/portfolio/portfolio.py`). 아래
테스트가 그 계약을 고정한다(과거엔 xfail이었다 — 이제 통과한다). "오늘은
롤백되지 않는다"를 고정하던 특성화 테스트는 그 버그 자체가 없어졌으므로
제거했다."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from quant.adapters.execution.paper import PaperBroker
from quant.core.models import Order, Position, Side
from quant.core.portfolio.portfolio import Portfolio


class _FakeQuote:
    def __init__(self, price: float):
        self.price = price
        self.ts = datetime.now(timezone.utc)


class _FakeData:
    def quote(self, symbol: str):
        return _FakeQuote(100.0)


def _broker_with_failing_save(tmp_path):
    state_path = tmp_path / "portfolio.json"
    portfolio = Portfolio(cash=1_000_000.0, state_path=state_path)

    def _boom():
        raise OSError(28, "No space left on device")

    portfolio.save = _boom  # 디스크 풀 시뮬레이션
    broker = PaperBroker(portfolio=portfolio, data=_FakeData(), market_of={"TEST": "US"})
    return broker


def test_failed_save_rolls_back_new_position_and_cash(tmp_path):
    """바람직한 계약(2026-09-06 수정 완료): 저장 실패 시 이번 주문이 만든
    신규 포지션/현금 변경이 정확히 롤백돼야 한다 — 저장 실패면 매수도
    없었던 일이어야 한다."""
    broker = _broker_with_failing_save(tmp_path)
    order = Order(symbol="TEST", side=Side.BUY, qty=10, strategy_id="stratA", reason="entry")

    with pytest.raises(OSError):
        broker.place_order(order)

    pos = broker.positions().get("TEST")
    assert pos is None or pos.qty == 0.0
    assert broker.portfolio.cash == 1_000_000.0
    # 이 시점에 trades.jsonl에는 아무것도 안 남았다(loop.py의 _execute_signal이
    # place_order 예외를 잡아 sinks.on_fill을 절대 호출하지 않으므로) — 원장과
    # 포트폴리오 모두 "이 주문은 없었다"로 일치한다.


def test_failed_save_rolls_back_partial_sell_of_existing_position(tmp_path):
    """기존 포지션이 있는 상태에서 매도 저장이 실패해도 원래 수량/평단가로
    정확히 복원돼야 한다 — 신규 포지션 케이스뿐 아니라 기존 포지션을 깎는
    경우도 같은 롤백 계약을 지켜야 한다."""
    state_path = tmp_path / "portfolio.json"
    portfolio = Portfolio(cash=1_000_000.0, state_path=state_path)
    portfolio.positions["TEST"] = Position(symbol="TEST", qty=20.0, avg_cost=90.0)

    def _boom():
        raise OSError(28, "No space left on device")

    portfolio.save = _boom
    broker = PaperBroker(portfolio=portfolio, data=_FakeData(), market_of={"TEST": "US"})
    order = Order(symbol="TEST", side=Side.SELL, qty=5, strategy_id="stratA", reason="exit")

    with pytest.raises(OSError):
        broker.place_order(order)

    pos = broker.positions().get("TEST")
    assert pos is not None
    assert pos.qty == 20.0  # 매도 이전 수량으로 복원
    assert pos.avg_cost == 90.0
    assert broker.portfolio.cash == 1_000_000.0  # 매도 대금이 반영되지 않음


def test_successful_save_after_a_failed_one_is_not_polluted_by_a_ghost_change(tmp_path):
    """롤백이 정확하면, 실패 뒤 재시도한 성공 주문의 저장에는 이전 실패 주문의
    "유령" 변경이 전혀 섞이지 않는다 — 이게 이 수정이 막으려는 장부 발산이다."""
    state_path = tmp_path / "portfolio.json"
    portfolio = Portfolio(cash=1_000_000.0, state_path=state_path)

    real_save = Portfolio.save
    calls = {"n": 0}

    def _flaky_save(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        real_save(self)

    portfolio.save = lambda: _flaky_save(portfolio)
    broker = PaperBroker(portfolio=portfolio, data=_FakeData(), market_of={"TEST": "US"})

    with pytest.raises(OSError):
        broker.place_order(Order(symbol="TEST", side=Side.BUY, qty=10, strategy_id="stratA", reason="entry"))
    assert broker.positions().get("TEST") is None or broker.positions()["TEST"].qty == 0.0

    state = broker.place_order(
        Order(symbol="OTHER", side=Side.BUY, qty=3, strategy_id="stratA", reason="entry")
    )
    assert state is not None

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "TEST" not in saved["positions"]  # 실패했던 첫 주문의 흔적이 없다
    assert saved["positions"]["OTHER"]["qty"] == 3.0
