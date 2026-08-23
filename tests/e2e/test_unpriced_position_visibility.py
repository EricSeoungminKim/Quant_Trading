"""보유 종목의 시세가 끊기면 손절 판정이 조용히 멈춘다 — 그 침묵을 잡는가.

전략의 `_manage_position`은 `quote is None`이면 `return None`한다. 즉 시세가
끊긴 동안에는 손절가를 넘어가도 아무 신호도 나오지 않고, 로그에도 안 남는다.
사람이 보고 있으면 알아채지만 이 시스템은 무인 운용이 전제다(2026-08-11 사용자:
"내가 프로그램을 보고 있지 않아도"). Toss 시세는 실측 3.3% 429가 나므로
가상의 위험이 아니다.
"""
from __future__ import annotations

from quant.trade.loop import _build_marks_and_unpriced
from quant.core.models import Position, Quote


class _Broker:
    def __init__(self, positions):
        self._positions = positions

    def positions(self):
        return self._positions

    def cash(self):
        return 1_000_000.0


class _Data:
    """priced에 있는 심볼만 시세를 준다 — 나머지는 조회 실패(None)."""

    def __init__(self, priced: dict[str, float]):
        self._priced = priced

    def quote(self, symbol):
        px = self._priced.get(symbol)
        return None if px is None else Quote(symbol=symbol, ts=None, price=px)


class _Clock:
    """market_open=True(기본)면 기존 테스트들이 가정하던 "항상 장중"을 그대로 재현한다."""

    def __init__(self, market_open: bool = True):
        self._market_open = market_open

    def is_market_open(self, market):
        return self._market_open


class _Ctx:
    def __init__(self, broker, data, clock=None):
        self.broker = broker
        self.data = data
        self.clock = clock or _Clock()


def _pos(symbol, qty=10.0):
    return Position(symbol=symbol, qty=qty, avg_cost=100.0, meta={"stop": 90.0})


def test_held_position_without_a_quote_is_reported_not_silently_skipped():
    ctx = _Ctx(
        _Broker({"AAA": _pos("AAA"), "BBB": _pos("BBB")}),
        _Data({"AAA": 101.0}),  # BBB 시세 실패
    )
    marks, unpriced = _build_marks_and_unpriced(ctx)
    assert marks == {"AAA": 101.0}
    assert unpriced == ["BBB"], "시세 못 받은 보유 종목이 드러나야 한다"


def test_closed_positions_are_not_reported_as_unpriced():
    """청산된 포지션은 시세가 없어도 정상 — 손절 판정 대상이 아니다."""
    closed = Position(symbol="CCC", qty=0.0, avg_cost=0.0, meta={})
    ctx = _Ctx(_Broker({"CCC": closed}), _Data({}))
    marks, unpriced = _build_marks_and_unpriced(ctx)
    assert marks == {}
    assert unpriced == []


def test_all_priced_means_nothing_to_report():
    ctx = _Ctx(_Broker({"AAA": _pos("AAA")}), _Data({"AAA": 101.0}))
    marks, unpriced = _build_marks_and_unpriced(ctx)
    assert marks == {"AAA": 101.0}
    assert unpriced == []


def test_invalid_price_counts_as_unpriced_not_as_a_valid_mark():
    """0/음수/NaN 시세를 유효한 마크로 받아들이면 MTM과 손절이 동시에 오염된다."""
    ctx = _Ctx(_Broker({"AAA": _pos("AAA")}), _Data({"AAA": 0.0}))
    marks, unpriced = _build_marks_and_unpriced(ctx)
    assert marks == {}
    assert unpriced == ["AAA"]


def test_closed_market_position_without_quote_is_not_reported_as_unpriced():
    """장이 닫힌 심볼은 시세가 없어도 정상 — quote()조차 부르지 않고, unpriced에도
    안 잡힌다. 장 마감은 "시세가 끊긴" 장애가 아니다."""
    data = _Data({})  # 아무 시세도 없음
    ctx = _Ctx(_Broker({"AAA": _pos("AAA")}), data, clock=_Clock(market_open=False))
    marks, unpriced = _build_marks_and_unpriced(ctx)
    assert marks == {}
    assert unpriced == []
