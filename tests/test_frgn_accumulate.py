"""FrgnAccumulateStrategy(갈래 B) — 외국인 수급 적립 전략 테스트.

핵심 시맨틱: FRGN/FRGN_EXIT 태그 게이트, 일 1회 평가 게이트(경과 시각),
고정 수량 매수(Signal.target_qty, 2026-08-19 고정액 근사에서 전환), 이탈 2일
연속 전량/1일차 절반, 중립 시 연속 카운터 리셋("잔여 관망"), 오버나이트/손절
레일이 없음. news_momentum.py의 테스트 패턴을 따르되 이 전략은 지속적 포지션
관리 루프가 없어(평가 시점에만 판단) 구조가 더 단순하다.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.ports import Context
from quant.core.models import Position, Quote, SignalAction
from quant.trade.strategy.frgn_accumulate import FrgnAccumulateStrategy

KST = ZoneInfo("Asia/Seoul")
DAY1 = date(2026, 1, 5)
DAY2 = date(2026, 1, 6)
KR_OPEN = dtime(9, 0)


class FakeClock:
    def __init__(self, now: datetime, open_markets: set[str] = frozenset({"KR"})):
        self._now = now
        self._open = open_markets

    def now(self):
        return self._now

    def is_market_open(self, market):
        return market in self._open

    def minutes_to_close(self, market):
        return 300.0

    def cadence_minutes(self):
        return 5.0 / 60

    def should_flatten(self, market, flatten_minutes):
        return False


class FakeDataFeed:
    def __init__(self, quotes: dict[str, float]):
        self._quotes = quotes

    def quote(self, symbol):
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(KST), price=self._quotes[symbol])

    def history(self, symbol, interval, n):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class FakeBroker:
    def __init__(self, positions=None):
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self):
        return 1_000_000.0


def _ctx(quotes, now, positions=None, open_markets=frozenset({"KR"})):
    return Context(
        clock=FakeClock(now, open_markets),
        data=FakeDataFeed(quotes),
        broker=FakeBroker(positions),
    )


def _params(**over):
    p = dict(buy_qty=1, eval_after_minutes_after_open=60, exit_fraction_first=0.5)
    p.update(over)
    return p


def _after_eval(minutes_after_open: float = 90.0, day=DAY1) -> datetime:
    return datetime.combine(day, KR_OPEN, tzinfo=KST) + timedelta(minutes=minutes_after_open)


def _before_eval(minutes_after_open: float = 10.0, day=DAY1) -> datetime:
    return datetime.combine(day, KR_OPEN, tzinfo=KST) + timedelta(minutes=minutes_after_open)


def _held(symbol="005930", qty=10.0, entry=70_000.0):
    return Position(symbol=symbol, qty=qty, avg_cost=entry,
                     meta={"lots": {"frgn_accumulate": {"qty": qty, "avg_cost": entry}}})


# ============================================================ 태그 게이트

def test_no_tags_of_means_nothing_happens():
    strat = FrgnAccumulateStrategy(["005930"], _params(), tags_of=None)
    signals = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval()))
    assert signals == []


def test_symbol_without_frgn_tag_does_nothing():
    strat = FrgnAccumulateStrategy(["005930"], _params(), tags_of={"005930": ["TREND"]})
    signals = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval()))
    assert signals == []


def test_frgn_tagged_symbol_buys_after_eval_time():
    strat = FrgnAccumulateStrategy(["005930"], _params(), tags_of={"005930": ["FRGN"]})
    signals = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval()))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG
    assert signals[0].symbol == "005930"


# ============================================================ 평가 시각 게이트 (일 1회)

def test_no_evaluation_before_eval_time():
    strat = FrgnAccumulateStrategy(["005930"], _params(eval_after_minutes_after_open=60),
                                    tags_of={"005930": ["FRGN"]})
    signals = strat.on_cycle(_ctx({"005930": 70_000.0}, _before_eval(30.0)))
    assert signals == []


def test_evaluates_only_once_per_day():
    strat = FrgnAccumulateStrategy(["005930"], _params(), tags_of={"005930": ["FRGN"]})
    now = _after_eval()
    first = strat.on_cycle(_ctx({"005930": 70_000.0}, now))
    assert len(first) == 1
    second = strat.on_cycle(_ctx({"005930": 70_000.0}, now + timedelta(minutes=5)))
    assert second == [], "같은 날 두 번째 평가는 없다 — 일 1회"


def test_evaluates_again_on_the_next_day():
    strat = FrgnAccumulateStrategy(["005930"], _params(), tags_of={"005930": ["FRGN"]})
    strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval(day=DAY1)))
    next_day = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval(day=DAY2)))
    assert len(next_day) == 1, "적립이므로 태그가 유지되면 다음 날도 다시 산다"


# ============================================================ 고정 수량 매수

def test_buys_even_while_already_holding():
    """적립의 정의 — 이미 보유 중이어도 태그가 유지되면 계속 산다."""
    strat = FrgnAccumulateStrategy(["005930"], _params(), tags_of={"005930": ["FRGN"]})
    pos = _held(qty=10.0, entry=70_000.0)
    signals = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval(), positions={"005930": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG


def test_buy_signal_carries_fixed_target_qty_regardless_of_price():
    """가격이 아무리 높아도(2026-08-19 이전이면 "1주도 못 삼"으로 건너뛰던 케이스)
    전략은 buy_qty를 그대로 target_qty로 싣는다 — 가격 대비 예산 판단은 risk
    레이어(하드레일/현금 게이트)의 몫으로 완전히 옮겨졌다."""
    strat = FrgnAccumulateStrategy(["005930"], _params(buy_qty=1), tags_of={"005930": ["FRGN"]})
    [signal] = strat.on_cycle(_ctx({"005930": 150_000.0}, _after_eval()))
    assert signal.target_qty == 1
    assert signal.target_weight == 0.0


def test_target_qty_matches_configured_buy_qty_not_capital_size():
    """buy_qty=1이면 항상 target_qty=1 — 전략은 자본 규모를 전혀 참조하지 않는다
    (assumed_capital_krw 제거로 이 클래스 자체가 자본 규모를 모른다).
    가격을 바꿔도 target_qty는 그대로여야 한다(과거 고정액 근사 사고의 회귀 테스트:
    실제 자본 규모 변화가 risk 레이어에서 어떻게 흡수되는지는
    tests/test_risk_per_strategy.py의 target_qty 테스트가 증명한다)."""
    strat = FrgnAccumulateStrategy(["005930"], _params(buy_qty=1), tags_of={"005930": ["FRGN"]})
    [low_price] = strat.on_cycle(_ctx({"005930": 10_000.0}, _after_eval(day=DAY1)))
    [high_price] = strat.on_cycle(_ctx({"005930": 500_000.0}, _after_eval(day=DAY2)))
    assert low_price.target_qty == high_price.target_qty == 1


def test_buy_qty_param_controls_target_qty():
    strat = FrgnAccumulateStrategy(["005930"], _params(buy_qty=3), tags_of={"005930": ["FRGN"]})
    [signal] = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval()))
    assert signal.target_qty == 3


def test_no_quote_skips_and_logs():
    strat = FrgnAccumulateStrategy(["005930"], _params(), tags_of={"005930": ["FRGN"]})
    signals = strat.on_cycle(_ctx({}, _after_eval()))
    assert signals == []
    assert "005930" in strat.last_reject


# ============================================================ 이탈 청산 (연속 2일)

def test_first_exit_tag_sells_half():
    strat = FrgnAccumulateStrategy(["005930"], _params(exit_fraction_first=0.5),
                                    tags_of={"005930": ["FRGN_EXIT"]})
    pos = _held(qty=10.0)
    signals = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval(), positions={"005930": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.SCALE_OUT
    assert signals[0].exit_fraction == pytest.approx(0.5)
    assert "1일차" in signals[0].reason


def test_consecutive_second_exit_tag_sells_all_remaining():
    strat = FrgnAccumulateStrategy(["005930"], _params(exit_fraction_first=0.5),
                                    tags_of={"005930": ["FRGN_EXIT"]})
    pos = _held(qty=10.0)
    strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval(day=DAY1), positions={"005930": pos}))
    signals = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval(day=DAY2), positions={"005930": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert signals[0].exit_fraction == 1.0
    assert "연속 2일차" in signals[0].reason


def test_neutral_day_breaks_the_streak_so_next_exit_is_half_again():
    """이탈 → 중립(잔여 관망, 보유 유지) → 이탈: 연속이 끊겼으니 다시 절반부터."""
    strat = FrgnAccumulateStrategy(["005930"], _params(exit_fraction_first=0.5), tags_of={"005930": ["FRGN_EXIT"]})
    pos = _held(qty=10.0)
    first = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval(day=DAY1), positions={"005930": pos}))
    assert first[0].exit_fraction == pytest.approx(0.5)

    strat.tags_of = {"005930": []}  # 중립 — 이탈 태그가 사라짐
    day2 = date(2026, 1, 6)
    neutral_signals = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval(day=day2), positions={"005930": pos}))
    assert neutral_signals == [], "중립일에는 보유 유지 — 청산 신호 없음"

    strat.tags_of = {"005930": ["FRGN_EXIT"]}
    day3 = date(2026, 1, 7)
    third = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval(day=day3), positions={"005930": pos}))
    assert len(third) == 1
    assert third[0].exit_fraction == pytest.approx(0.5), "연속이 끊겼으니 다시 1일차(절반)부터"


def test_exit_tag_with_nothing_held_does_nothing():
    strat = FrgnAccumulateStrategy(["005930"], _params(), tags_of={"005930": ["FRGN_EXIT"]})
    signals = strat.on_cycle(_ctx({"005930": 70_000.0}, _after_eval()))
    assert signals == []


# ============================================================ 오버나이트 / 손절 없음

def test_no_eod_flatten_or_stop_loss_management_loop():
    """이 전략은 지속 포지션 관리 루프가 없다 — 평가 시각이 아니면(태그 무관) 신호가 없다."""
    strat = FrgnAccumulateStrategy(["005930"], _params(), tags_of={"005930": ["FRGN"]})
    signals = strat.on_cycle(_ctx({"005930": 1.0}, _before_eval(5.0)))  # 폭락가여도 평가 전엔 무반응
    assert signals == []


# ============================================================ 생성자 검증

def test_invalid_buy_qty_raises():
    with pytest.raises(ValueError):
        FrgnAccumulateStrategy(["005930"], _params(buy_qty=0))
    with pytest.raises(ValueError):
        FrgnAccumulateStrategy(["005930"], _params(buy_qty=-1))
    with pytest.raises(ValueError):
        FrgnAccumulateStrategy(["005930"], _params(buy_qty=1.5))


def test_invalid_exit_fraction_first_raises():
    with pytest.raises(ValueError):
        FrgnAccumulateStrategy(["005930"], _params(exit_fraction_first=0))
    with pytest.raises(ValueError):
        FrgnAccumulateStrategy(["005930"], _params(exit_fraction_first=1))
