"""NewsScalpStrategy(갈래 A) — 개장 진입·당일 청산 고정 단타 전략 테스트.

news_momentum과 달리 청산은 손절 + EoD 둘 뿐(부분익절·시간청산·목표가 없음).
핵심 시맨틱: EVENT_SCALP 태그 게이트, 개장 직후 진입창, 세션당 1회, 균등분할
사이징, 손절/EoD/세션롤 청산, 랏 소유권. test_news_momentum.py의 테스트
패턴을 따른다.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.ports import Context
from quant.core.models import Position, Quote, SignalAction
from quant.trade.strategy.news_scalp import NewsScalpStrategy

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DAY1 = date(2026, 1, 5)
DAY2 = date(2026, 1, 6)
US_OPEN = dtime(9, 30)


class FakeClock:
    def __init__(self, now: datetime, open_markets: set[str] = frozenset({"US"}),
                 flatten_markets: set[str] = frozenset()):
        self._now = now
        self._open = open_markets
        self._flatten = flatten_markets

    def now(self):
        return self._now

    def is_market_open(self, market):
        return market in self._open

    def minutes_to_close(self, market):
        return 300.0

    def cadence_minutes(self):
        return 5.0 / 60

    def should_flatten(self, market, flatten_minutes):
        return market in self._flatten


class FakeDataFeed:
    def __init__(self, quotes: dict[str, float], anchor_bars: dict[str, pd.DataFrame] | None = None):
        self._quotes = quotes
        # 시장 리스크오프 게이트 전용 — {앵커심볼: 1m bars}. 기본 비어 있음 →
        # anchor_drawdown이 None(게이트 부재, 기존 동작 보존).
        self._anchor_bars = anchor_bars or {}
        self.history_calls: list[str] = []

    def quote(self, symbol):
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(NY), price=self._quotes[symbol])

    def history(self, symbol, interval, n):
        self.history_calls.append(symbol)
        df = self._anchor_bars.get(symbol)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df.tail(n)


class FakeBroker:
    def __init__(self, positions=None):
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self):
        return 1_000_000.0


def _ctx(quotes, now, positions=None, open_markets=frozenset({"US"}), flatten_markets=frozenset(),
          anchor_bars=None):
    return Context(
        clock=FakeClock(now, open_markets, flatten_markets),
        data=FakeDataFeed(quotes, anchor_bars),
        broker=FakeBroker(positions),
    )


def _params(**over):
    p = dict(entry_window_seconds=120, max_entries_per_session=3,
              stop_loss_pct=3.0, flatten_before_close_minutes=1)
    p.update(over)
    return p


def _now_within_window(seconds_after_open: float = 30.0, day=DAY1) -> datetime:
    return datetime.combine(day, US_OPEN, tzinfo=NY) + timedelta(seconds=seconds_after_open)


# ============================================================ EVENT_SCALP 태그 게이트

def test_no_tags_of_means_no_entry_ever():
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of=None)
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window()))
    assert signals == []


def test_symbol_with_event_tag_but_not_event_scalp_does_not_enter():
    """news_momentum의 EVENT 태그만으로는 이 전략이 진입하지 않는다 — 별개 태그."""
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window()))
    assert signals == []


def test_event_scalp_tagged_symbol_enters_within_window():
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT_SCALP"]})
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window()))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG
    assert signals[0].symbol == "AAA"


# ============================================================ 진입창

def test_entry_after_window_is_blocked_but_within_is_fine():
    strat = NewsScalpStrategy(["AAA"], _params(entry_window_seconds=60), tags_of={"AAA": ["EVENT_SCALP"]})
    late = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(90.0)))
    assert late == []

    strat2 = NewsScalpStrategy(["AAA"], _params(entry_window_seconds=60), tags_of={"AAA": ["EVENT_SCALP"]})
    early = strat2.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(30.0)))
    assert len(early) == 1


def test_market_closed_means_no_entry_scan_at_all():
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT_SCALP"]})
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(), open_markets=frozenset()))
    assert signals == []


def test_missed_window_means_no_entry_for_the_rest_of_the_session():
    strat = NewsScalpStrategy(["AAA"], _params(entry_window_seconds=60), tags_of={"AAA": ["EVENT_SCALP"]})
    strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(120.0)))
    assert "AAA" not in strat._entered_today
    still_late = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(150.0)))
    assert still_late == []


# ============================================================ 세션당 1회 + 상한 + 균등분할

def test_one_entry_per_symbol_per_session_no_reentry():
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT_SCALP"]})
    now = _now_within_window()
    first = strat.on_cycle(_ctx({"AAA": 100.0}, now))
    assert len(first) == 1
    second = strat.on_cycle(_ctx({"AAA": 100.0}, now))
    assert second == []


def test_max_entries_per_session_caps_burst():
    strat = NewsScalpStrategy(
        ["AAA", "BBB", "CCC"], _params(max_entries_per_session=2),
        tags_of={"AAA": ["EVENT_SCALP"], "BBB": ["EVENT_SCALP"], "CCC": ["EVENT_SCALP"]},
    )
    signals = strat.on_cycle(_ctx({"AAA": 100.0, "BBB": 100.0, "CCC": 100.0}, _now_within_window()))
    assert len(signals) == 2


def test_equal_split_sizing_across_slots():
    """사이징: target_weight = 1/max_entries_per_session (균등 분할, 모듈 docstring 참고)."""
    strat = NewsScalpStrategy(["AAA"], _params(max_entries_per_session=4), tags_of={"AAA": ["EVENT_SCALP"]})
    [signal] = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window()))
    assert signal.target_weight == pytest.approx(0.25)
    assert signal.stop == pytest.approx(100.0 * 0.97)  # 기본 손절 3%


def test_already_holding_via_own_lot_blocks_reentry():
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT_SCALP"]})
    pos = Position(symbol="AAA", qty=10, avg_cost=100.0,
                   meta={"lots": {"news_scalp": {"qty": 10.0, "entry": 100.0,
                                                    "session": DAY1.isoformat()}}})
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(), positions={"AAA": pos}))
    entries = [s for s in signals if s.action == SignalAction.ENTER_LONG]
    assert entries == []


# ============================================================ 청산 — 손절 + EoD 둘 뿐

def _lot_position(entry=100.0, *, session=DAY1.isoformat()):
    return Position(symbol="AAA", qty=10, avg_cost=entry, meta={
        "lots": {"news_scalp": {"qty": 10.0, "entry": entry, "session": session}},
    })


def test_stop_loss_exits_full_position():
    strat = NewsScalpStrategy(["AAA"], _params(stop_loss_pct=3.0), tags_of=None)
    pos = _lot_position(entry=100.0)
    signals = strat.on_cycle(_ctx({"AAA": 96.9}, _now_within_window(5.0), positions={"AAA": pos}))  # -3.1%
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert signals[0].exit_fraction == 1.0
    assert "손절" in signals[0].reason


def test_price_exactly_at_stop_boundary_exits():
    strat = NewsScalpStrategy(["AAA"], _params(stop_loss_pct=3.0), tags_of=None)
    pos = _lot_position(entry=100.0)
    signals = strat.on_cycle(_ctx({"AAA": 97.0}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1 and signals[0].action == SignalAction.EXIT_LONG


def test_no_partial_take_profit_ladder_exists():
    """news_scalp에는 부분익절/목표가 사다리가 없다 — 손절/EoD/세션롤 전까지는 아무 신호도 없다."""
    strat = NewsScalpStrategy(["AAA"], _params(stop_loss_pct=3.0), tags_of=None)
    pos = _lot_position(entry=100.0)
    signals = strat.on_cycle(_ctx({"AAA": 130.0}, _now_within_window(5.0), positions={"AAA": pos}))
    assert signals == []


def test_eod_flatten_exits_position():
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of=None)
    pos = _lot_position(entry=100.0)
    signals = strat.on_cycle(_ctx({"AAA": 101.0}, _now_within_window(5.0), positions={"AAA": pos},
                                   flatten_markets=frozenset({"US"})))
    assert len(signals) == 1
    assert "EoD" in signals[0].reason


def test_session_roll_forces_exit_overnight():
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of=None)
    pos = _lot_position(entry=100.0, session=DAY1.isoformat())
    now = datetime.combine(DAY2, dtime(10, 30), tzinfo=NY)
    signals = strat.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos}))
    assert len(signals) == 1
    assert "오버나잇 금지" in signals[0].reason


# ============================================================ 재시작 복구

def test_restart_recovery_uses_avg_cost_and_keeps_managing():
    """랏 컨텍스트를 잃어도(순수 복구 케이스) avg_cost 기준으로 손절 판정을 이어간다."""
    strat = NewsScalpStrategy(["AAA"], _params(stop_loss_pct=3.0), tags_of=None)
    pos = Position(symbol="AAA", qty=10, avg_cost=100.0, meta={})
    signals = strat.on_cycle(_ctx({"AAA": 96.0}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert "손절" in signals[0].reason


# ============================================================ 랏 소유권

def test_does_not_manage_another_strategys_position():
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of=None)
    foreign = Position(symbol="AAA", qty=10, avg_cost=100.0,
                        meta={"strategy": "orb_scan", "stop": 90.0})
    assert strat._owns(foreign) is False
    mine = Position(symbol="AAA", qty=10, avg_cost=100.0,
                     meta={"strategy": "news_scalp", "stop": 90.0})
    assert strat._owns(mine) is True


# ============================================================ 생성자 검증

def test_invalid_stop_loss_pct_raises():
    with pytest.raises(ValueError):
        NewsScalpStrategy(["AAA"], _params(stop_loss_pct=0))


def test_invalid_entry_window_raises():
    with pytest.raises(ValueError):
        NewsScalpStrategy(["AAA"], _params(entry_window_seconds=0))


def test_invalid_max_entries_per_session_raises():
    with pytest.raises(ValueError):
        NewsScalpStrategy(["AAA"], _params(max_entries_per_session=0))


# ============================================================ 혼합 시장 (KR + US)

def _kr_now_within_window(seconds_after_open: float = 30.0, day=DAY1) -> datetime:
    return datetime.combine(day, dtime(9, 0), tzinfo=KST) + timedelta(seconds=seconds_after_open)


def test_kr_symbol_enters_in_kr_window_independent_of_us():
    strat = NewsScalpStrategy(["AAPL", "005930"], _params(), tags_of={"005930": ["EVENT_SCALP"]})
    ctx = Context(
        clock=FakeClock(_kr_now_within_window(), open_markets={"KR"}),
        data=FakeDataFeed({"005930": 70000.0}),
        broker=FakeBroker(),
    )
    signals = strat.on_cycle(ctx)
    assert [s.symbol for s in signals] == ["005930"]


# ============================================================ 시장 리스크오프 게이트
# quant/trade/indicators/breadth.py 배선 — 계산 자체는 tests/test_breadth.py가
# 고정한다. 여기서는 게이트가 진입 직전에 실제로 걸리는지 + 모드별 동작만 본다.

def _anchor_bars(pct: float, day=DAY1, tz=NY, start=US_OPEN) -> pd.DataFrame:
    idx = [datetime.combine(day, start, tzinfo=tz) + timedelta(minutes=i) for i in range(2)]
    last_close = 100.0 * (1 + pct / 100)
    rows = [
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000.0},
        {"open": 100.0, "high": max(100.0, last_close), "low": min(100.0, last_close),
         "close": last_close, "volume": 1000.0},
    ]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def test_market_risk_gate_defaults_to_shadow():
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT_SCALP"]})
    assert strat.market_risk_gate_mode == "shadow"


def test_market_risk_gate_shadow_tags_reason_without_blocking():
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT_SCALP"]})
    ctx = _ctx({"AAA": 100.0}, _now_within_window(), anchor_bars={"QQQ": _anchor_bars(-1.0)})
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert "[시장:리스크오프" in signals[0].reason


def test_market_risk_gate_block_mode_blocks_entry():
    strat = NewsScalpStrategy(
        ["AAA"], _params(market_risk_gate_mode="block"), tags_of={"AAA": ["EVENT_SCALP"]}
    )
    ctx = _ctx({"AAA": 100.0}, _now_within_window(), anchor_bars={"QQQ": _anchor_bars(-1.0)})
    signals = strat.on_cycle(ctx)
    assert signals == []
    assert "리스크오프" in strat.last_reject["AAA"]


def test_market_risk_gate_off_mode_skips_anchor_query():
    strat = NewsScalpStrategy(
        ["AAA"], _params(market_risk_gate_mode="off"), tags_of={"AAA": ["EVENT_SCALP"]}
    )
    ctx = _ctx({"AAA": 100.0}, _now_within_window(), anchor_bars={"QQQ": _anchor_bars(-5.0)})
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert "QQQ" not in ctx.data.history_calls


def test_market_risk_gate_missing_anchor_data_falls_back_to_pass():
    strat = NewsScalpStrategy(
        ["AAA"], _params(market_risk_gate_mode="block"), tags_of={"AAA": ["EVENT_SCALP"]}
    )
    ctx = _ctx({"AAA": 100.0}, _now_within_window())
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1


def test_market_risk_gate_does_not_affect_exit_management():
    strat = NewsScalpStrategy(["AAA"], _params(), tags_of={})
    pos = Position(symbol="AAA", qty=10, avg_cost=100.0,
                   meta={"lots": {strat.id: {"qty": 10.0, "entry": 100.0, "session": None}}})
    ctx = _ctx({"AAA": 96.0}, _now_within_window(), positions={"AAA": pos},
               anchor_bars={"QQQ": _anchor_bars(-3.0)})
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
