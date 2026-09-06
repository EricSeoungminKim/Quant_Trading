"""PaperBroker의 engine_owned_qty/engine_owned_symbols/engine_owned_cash
(2026-09-06 라이브 준비 D1).

배경: `quant.trade.reconcile.Reconciler`는 이 세 메서드가 있는 브로커에서만
동작한다(`supported` 판정). PaperBroker는 지금까지 이걸 노출하지 않아
`build_reconciler`가 항상 None을 돌려줬다 — 대사 코드 경로가 paper에서 단
한 번도 실행된 적이 없었다(감사 발견). 이 파일이 지키는 것: 세 메서드가
positions()/cash()와 정확히 같은 값(항등)을 돌려준다는 것 — paper에는
"사용자 수동 보유" 개념이 없으므로 그래야 맞고, 그래야 이 메서드들을 추가해도
기존 risk/manager.py·loop.py의 duck-typed 사용처(매도가능수량 클램프, flatten
필터)가 회귀하지 않는다.
"""
from __future__ import annotations

from datetime import UTC, datetime

from quant.adapters.execution.paper import PaperBroker
from quant.core.fx import FixedFxProvider
from quant.core.models import Position, Quote
from quant.core.portfolio.portfolio import Portfolio


class _SpotFeed:
    def quote(self, symbol: str):
        return Quote(symbol=symbol, ts=datetime.now(UTC), price=100.0)

    def history(self, symbol, interval, n):
        raise NotImplementedError


def _make_broker(cash: float = 1_000_000.0, positions: dict[str, Position] | None = None):
    portfolio = Portfolio(cash=cash, positions=positions or {}, state_path=None)
    broker = PaperBroker(data=_SpotFeed(), portfolio=portfolio, fx=FixedFxProvider())
    return broker, portfolio


def test_engine_owned_qty_matches_position_qty_exactly():
    positions = {"TQQQ": Position(symbol="TQQQ", qty=12.5, avg_cost=50.0)}
    broker, _ = _make_broker(positions=positions)

    assert broker.engine_owned_qty("TQQQ") == 12.5
    assert broker.engine_owned_qty("TQQQ") == broker.positions()["TQQQ"].qty


def test_engine_owned_qty_is_zero_for_unknown_symbol():
    broker, _ = _make_broker()

    assert broker.engine_owned_qty("AAPL") == 0.0


def test_engine_owned_symbols_lists_only_open_positions():
    positions = {
        "TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=50.0),
        "SQQQ": Position(symbol="SQQQ", qty=0.0, avg_cost=0.0),  # 청산된 포지션
    }
    broker, _ = _make_broker(positions=positions)

    assert broker.engine_owned_symbols() == {"TQQQ"}


def test_engine_owned_cash_matches_cash_exactly():
    broker, _ = _make_broker(cash=4_200_000.0)

    assert broker.engine_owned_cash() == 4_200_000.0
    assert broker.engine_owned_cash() == broker.cash()


def test_reconciler_finds_paper_broker_supported_and_never_mismatched(tmp_path):
    """이게 이 변경 전체의 핵심 불변식이다: PaperBroker는 이제 대사 대상이지만,
    engine_owned_*가 positions()/cash()와 같은 객체를 읽으므로 **불일치가 날 수
    없다** — 새 위험이 아니라 코드 경로가 매 사이클 실제로 도는지 확인하는
    배선이다."""
    from quant.trade.control import TradingControl
    from quant.trade.reconcile import Reconciler

    positions = {"TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=50.0)}
    broker, _ = _make_broker(cash=1_000_000.0, positions=positions)
    control = TradingControl(state_path=tmp_path / "control.json")

    reconciler = Reconciler(broker, control)

    assert reconciler.supported is True
    report = reconciler.check(force=True)
    assert report.ok
    assert report.mismatches == []
    assert not control.is_halted()
