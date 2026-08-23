"""NewsMomentumStrategy — 뉴스 모멘텀 개장매수 전략 테스트.

핵심 시맨틱만 고정한다: EVENT 태그 게이트(태그 없으면 절대 진입하지 않음),
개장 직후 진입창, 세션당 1회, 고정 % 청산 사다리(손절/부분익절/전량익절/타임아웃),
EoD·오버나잇 금지 레일, 랏 소유권. orb_scan.py/confluence.py의 테스트 패턴을 따른다.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.ports import Context
from quant.core.models import Position, Quote, SignalAction
from quant.trade.strategy.news_momentum import NewsMomentumStrategy

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DAY1 = date(2026, 1, 5)
DAY2 = date(2026, 1, 6)
US_OPEN = dtime(9, 30)


class FakeClock:
    """시장별 개장 여부/청산 여부를 직접 통제하는 시계 — 개장창 경계 테스트용."""

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
    p = dict(
        entry_window_seconds=120, max_entries_per_session=3,
        stop_loss_pct=2.0, partial_take_pct=5.0, partial_fraction=0.5,
        full_take_pct=10.0, max_hold_minutes=30,
        risk_budget_pct=1.0, max_leverage=4.0, flatten_before_close_minutes=1,
    )
    p.update(over)
    return p


def _now_within_window(seconds_after_open: float = 30.0, day=DAY1) -> datetime:
    return datetime.combine(day, US_OPEN, tzinfo=NY) + timedelta(seconds=seconds_after_open)


# ============================================================ EVENT 태그 게이트

def test_no_tags_of_means_no_entry_ever():
    """tags_of가 아예 없으면(테스트/백테스트/미배선) 뉴스 근거를 알 수 없다 — 진입 안 함."""
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of=None)
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window()))
    assert signals == []


def test_symbol_without_event_tag_does_not_enter():
    strat = NewsMomentumStrategy(["AAA", "BBB"], _params(), tags_of={"AAA": ["TREND"]})
    signals = strat.on_cycle(_ctx({"AAA": 100.0, "BBB": 100.0}, _now_within_window()))
    assert signals == []


def test_event_tagged_symbol_enters_within_window():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window()))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG
    assert signals[0].symbol == "AAA"


def test_symbol_with_event_among_multiple_tags_still_enters():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of={"AAA": ["TREND", "EVENT"]})
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window()))
    assert len(signals) == 1


# ============================================================ 진입창

def test_entry_before_window_seconds_elapsed_is_fine_but_after_is_not():
    strat = NewsMomentumStrategy(["AAA"], _params(entry_window_seconds=60), tags_of={"AAA": ["EVENT"]})
    late = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(90.0)))
    assert late == []

    strat2 = NewsMomentumStrategy(["AAA"], _params(entry_window_seconds=60), tags_of={"AAA": ["EVENT"]})
    early = strat2.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(30.0)))
    assert len(early) == 1


def test_market_closed_means_no_entry_scan_at_all():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(), open_markets=frozenset()))
    assert signals == []


def test_missed_window_means_no_entry_for_the_rest_of_the_session():
    """창을 놓치면 그날은 진입하지 않는다 — 몇 분 뒤 조건이 다시 맞아도 안 됨."""
    strat = NewsMomentumStrategy(["AAA"], _params(entry_window_seconds=60), tags_of={"AAA": ["EVENT"]})
    strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(120.0)))  # 창 밖
    assert "AAA" not in strat._entered_today
    # 다음 사이클도 여전히 같은 세션·같은 시장의 진입 판정 로직만 재실행 —
    # 창이 이미 지났으므로 계속 진입하지 않는다(별도 "놓침" 플래그 없이 창 검사
    # 자체가 매번 막는다).
    still_late = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(150.0)))
    assert still_late == []


# ============================================================ 세션당 1회 + 상한

def test_one_entry_per_symbol_per_session():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window()
    first = strat.on_cycle(_ctx({"AAA": 100.0}, now))
    assert len(first) == 1
    second = strat.on_cycle(_ctx({"AAA": 100.0}, now))
    assert second == []


def test_max_entries_per_session_caps_burst():
    strat = NewsMomentumStrategy(
        ["AAA", "BBB", "CCC"], _params(max_entries_per_session=2),
        tags_of={"AAA": ["EVENT"], "BBB": ["EVENT"], "CCC": ["EVENT"]},
    )
    signals = strat.on_cycle(_ctx({"AAA": 100.0, "BBB": 100.0, "CCC": 100.0}, _now_within_window()))
    assert len(signals) == 2
    assert {s.symbol for s in signals} <= {"AAA", "BBB", "CCC"}


def test_already_holding_via_own_lot_blocks_reentry():
    """랏(lot) 기준 — 재시작으로 인메모리 `_entered_today`가 사라져도 랏에 수량이
    남아 있으면 재진입하지 않는다."""
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    pos = Position(symbol="AAA", qty=10, avg_cost=100.0,
                   meta={"lots": {"news_momentum": {"qty": 10.0, "entry": 100.0,
                                                       "entered_at": _now_within_window().isoformat(),
                                                       "partial_taken": False,
                                                       "session": DAY1.isoformat()}}})
    signals = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window(), positions={"AAA": pos}))
    entries = [s for s in signals if s.action == SignalAction.ENTER_LONG]
    assert entries == []


# ============================================================ 사이징

def test_sizing_matches_risk_budget_over_stop_pct():
    strat = NewsMomentumStrategy(["AAA"], _params(stop_loss_pct=2.0, risk_budget_pct=1.0, max_leverage=4.0),
                                  tags_of={"AAA": ["EVENT"]})
    [signal] = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window()))
    risk_pct = 2.0 / 100
    assert signal.target_weight == pytest.approx(min(0.01 / risk_pct, 4.0))
    assert signal.stop == pytest.approx(100.0 * 0.98)


def test_max_leverage_caps_target_weight():
    strat = NewsMomentumStrategy(["AAA"], _params(stop_loss_pct=0.1, risk_budget_pct=1.0, max_leverage=2.0),
                                  tags_of={"AAA": ["EVENT"]})
    [signal] = strat.on_cycle(_ctx({"AAA": 100.0}, _now_within_window()))
    assert signal.target_weight == pytest.approx(2.0)


# ============================================================ 청산 사다리

def _lot_position(entry=100.0, *, session=DAY1.isoformat(), partial_taken=False,
                   entered_at=None):
    entered_at = entered_at or _now_within_window().isoformat()
    return Position(symbol="AAA", qty=10, avg_cost=entry, meta={
        "lots": {"news_momentum": {
            "qty": 10.0, "entry": entry, "entered_at": entered_at,
            "partial_taken": partial_taken, "session": session,
        }},
    })


def test_stop_loss_exits_full_position():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of=None)
    pos = _lot_position(entry=100.0)
    now = _now_within_window(5.0)
    signals = strat.on_cycle(_ctx({"AAA": 97.9}, now, positions={"AAA": pos}))  # -2.1%
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert signals[0].exit_fraction == 1.0
    assert "손절" in signals[0].reason


def test_price_exactly_at_stop_boundary_exits():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of=None)
    pos = _lot_position(entry=100.0)
    signals = strat.on_cycle(_ctx({"AAA": 98.0}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1 and signals[0].action == SignalAction.EXIT_LONG


def test_partial_take_profit_scales_out_once():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of=None)
    pos = _lot_position(entry=100.0)
    signals = strat.on_cycle(_ctx({"AAA": 105.5}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.SCALE_OUT
    assert sig.exit_fraction == pytest.approx(0.5)
    assert sig.state_update == {"partial_taken": True}

    # 체결 확인 후 loop.py가 하는 일을 시뮬레이션: state_update를 랏에 적용.
    pos.meta["lots"]["news_momentum"].update(sig.state_update)
    again = strat.on_cycle(_ctx({"AAA": 106.0}, _now_within_window(6.0), positions={"AAA": pos}))
    assert again == [], "부분 익절은 세션당 1회만 — 재발동 금지"


def test_full_take_profit_exits_everything_even_after_partial():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of=None)
    pos = _lot_position(entry=100.0, partial_taken=True)
    signals = strat.on_cycle(_ctx({"AAA": 111.0}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert signals[0].exit_fraction == 1.0
    assert "목표가" in signals[0].reason


def test_full_take_profit_wins_over_partial_when_price_gaps_past_both():
    """부분 익절 안 하고 바로 +10%를 넘긴 경우 — 부분 청산 없이 바로 전량."""
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of=None)
    pos = _lot_position(entry=100.0, partial_taken=False)
    signals = strat.on_cycle(_ctx({"AAA": 112.0}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG


def test_max_hold_timeout_exits_remainder():
    strat = NewsMomentumStrategy(["AAA"], _params(max_hold_minutes=30), tags_of=None)
    entered_at = (_now_within_window(5.0) - timedelta(minutes=31)).isoformat()
    pos = _lot_position(entry=100.0, entered_at=entered_at)
    signals = strat.on_cycle(_ctx({"AAA": 101.0}, _now_within_window(5.0), positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert "보유시간 초과" in signals[0].reason


def test_within_hold_window_and_no_target_hit_does_not_exit():
    strat = NewsMomentumStrategy(["AAA"], _params(max_hold_minutes=30), tags_of=None)
    entered_at = (_now_within_window(5.0) - timedelta(minutes=10)).isoformat()
    pos = _lot_position(entry=100.0, entered_at=entered_at)
    signals = strat.on_cycle(_ctx({"AAA": 101.0}, _now_within_window(5.0), positions={"AAA": pos}))
    assert signals == []


def test_eod_flatten_exits_position():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of=None)
    pos = _lot_position(entry=100.0)
    signals = strat.on_cycle(_ctx({"AAA": 101.0}, _now_within_window(5.0), positions={"AAA": pos},
                                   flatten_markets=frozenset({"US"})))
    assert len(signals) == 1
    assert "EoD" in signals[0].reason


def test_session_roll_forces_exit_overnight():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of=None)
    pos = _lot_position(entry=100.0, session=DAY1.isoformat())
    now = datetime.combine(DAY2, dtime(10, 30), tzinfo=NY)
    signals = strat.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos}))
    assert len(signals) == 1
    assert "오버나잇 금지" in signals[0].reason


# ============================================================ 재시작 복구

def test_restart_recovery_forces_prompt_timeout_exit():
    """재시작으로 랏 컨텍스트(entered_at/session)를 잃으면, 다음 관리 사이클에서
    곧바로 시간 손절이 걸리도록 entered_at을 과거로 잡는다(모듈 docstring 참고)."""
    strat = NewsMomentumStrategy(["AAA"], _params(max_hold_minutes=30), tags_of=None)
    pos = Position(symbol="AAA", qty=10, avg_cost=100.0, meta={})  # lots 없음 — 순수 복구 케이스
    now = _now_within_window(5.0)
    signals = strat.on_cycle(_ctx({"AAA": 100.5}, now, positions={"AAA": pos}))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG
    assert "보유시간 초과" in signals[0].reason


# ============================================================ 랏 소유권

def test_news_momentum_does_not_manage_another_strategys_position():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of=None)
    foreign = Position(symbol="AAA", qty=10, avg_cost=100.0,
                        meta={"strategy": "orb_scan", "stop": 90.0})
    assert strat._owns(foreign) is False
    mine = Position(symbol="AAA", qty=10, avg_cost=100.0,
                     meta={"strategy": "news_momentum", "stop": 90.0})
    assert strat._owns(mine) is True


def test_does_not_adopt_a_position_already_tracked_by_another_strategys_lot():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of=None)
    pos = Position(symbol="AAA", qty=10, avg_cost=100.0,
                    meta={"lots": {"orb_scan": {"qty": 10.0, "entry": 100.0}}})
    assert strat._owns(pos) is False


# ============================================================ 생성자 검증

def test_invalid_stop_loss_pct_raises():
    with pytest.raises(ValueError):
        NewsMomentumStrategy(["AAA"], _params(stop_loss_pct=0))


def test_invalid_partial_fraction_raises():
    with pytest.raises(ValueError):
        NewsMomentumStrategy(["AAA"], _params(partial_fraction=1.5))
    with pytest.raises(ValueError):
        NewsMomentumStrategy(["AAA"], _params(partial_fraction=0))


def test_invalid_entry_window_raises():
    with pytest.raises(ValueError):
        NewsMomentumStrategy(["AAA"], _params(entry_window_seconds=0))


# ============================================================ 혼합 시장 (KR + US)

def _kr_now_within_window(seconds_after_open: float = 30.0, day=DAY1) -> datetime:
    return datetime.combine(day, dtime(9, 0), tzinfo=KST) + timedelta(seconds=seconds_after_open)


def test_kr_symbol_enters_in_kr_window_independent_of_us():
    strat = NewsMomentumStrategy(["AAPL", "005930"], _params(), tags_of={"005930": ["EVENT"]})
    ctx = Context(
        clock=FakeClock(_kr_now_within_window(), open_markets={"KR"}),
        data=FakeDataFeed({"005930": 70000.0}),
        broker=FakeBroker(),
    )
    signals = strat.on_cycle(ctx)
    assert [s.symbol for s in signals] == ["005930"]


# ---------------------------------------------------------------------------
# 진입 우선순위 — 세션 상한에 걸릴 때 무엇을 남길 것인가
# 2026-08-13 실장: 후보 8개 / 상한 3개인데 관심종목 파일 순서대로 앞 3개를 집었다.
# 그 순서는 자동 편입 스크립트의 태그별 묶음(sort -u) 결과라, 알파벳 순
# "EVENT" < "TREND+EVENT" 가 그대로 매수 종목을 정해버렸다.
# ---------------------------------------------------------------------------

def test_trend_tagged_candidates_are_preferred_when_capped():
    """랭킹에도 오른 호재주(TREND+EVENT)가 뉴스만 있는 종목(EVENT)보다 먼저다."""
    tags = {
        "003540": ["EVENT"],            # 뉴스만 — 랭킹 없음
        "004170": ["EVENT"],
        "011070": ["EVENT"],
        "005930": ["TREND", "EVENT"],   # 뉴스 + 거래대금 랭킹 1위
        "000660": ["TREND", "EVENT"],
        "161890": ["TREND", "EVENT"],
    }
    # 파일 순서는 실제로 그날 등록된 순서 그대로(EVENT 묶음이 먼저 왔다)
    order = ["003540", "004170", "011070", "005930", "000660", "161890"]
    strat = NewsMomentumStrategy(order, _params(), tags_of=tags)
    ranked = strat._rank_candidates(order)
    assert ranked[:3] == ["000660", "005930", "161890"], (
        "상한 3개에 잘릴 때 TREND 를 함께 가진 종목이 남아야 한다"
    )


def test_ranking_is_deterministic_on_ties():
    """상한에 걸려 잘리는 종목이 매일 달라지면 성과를 해석할 수 없다."""
    tags = {s: ["EVENT"] for s in ("ZZZ", "AAA", "MMM")}
    strat = NewsMomentumStrategy(["ZZZ", "AAA", "MMM"], _params(), tags_of=tags)
    assert strat._rank_candidates(["ZZZ", "AAA", "MMM"]) == ["AAA", "MMM", "ZZZ"]
    assert strat._rank_candidates(["MMM", "ZZZ", "AAA"]) == ["AAA", "MMM", "ZZZ"]


def test_ranking_does_not_change_who_is_eligible():
    """우선순위는 '누가 먼저냐'만 정한다 — 진입 자격은 여전히 EVENT 뿐이다."""
    tags = {"AAA": ["TREND"], "BBB": ["EVENT"]}
    strat = NewsMomentumStrategy(["AAA", "BBB"], _params(), tags_of=tags)
    # _rank_candidates 는 넘어온 목록만 정렬한다 — EVENT 필터는 호출부에 있다
    assert strat._rank_candidates(["BBB"]) == ["BBB"]
    assert "AAA" not in strat._rank_candidates(["BBB"])


# ============================================================ 시장 리스크오프 게이트
# quant/trade/indicators/breadth.py 배선 — 계산 자체의 정확성/경계값은
# tests/test_breadth.py가 고정한다. 여기서는 "게이트가 신규 진입 직전에 실제로
# 걸리는지 + 모드별 동작 + 분 경계 캐시"만 본다.

def _anchor_bars(pct: float, day=DAY1, tz=NY, start=US_OPEN) -> pd.DataFrame:
    """앵커 1분봉 합성 — 당일 시가 100.0에서 pct%만큼 이동한 마지막 종가."""
    idx = [datetime.combine(day, start, tzinfo=tz) + timedelta(minutes=i) for i in range(2)]
    last_close = 100.0 * (1 + pct / 100)
    rows = [
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000.0},
        {"open": 100.0, "high": max(100.0, last_close), "low": min(100.0, last_close),
         "close": last_close, "volume": 1000.0},
    ]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def test_market_risk_gate_defaults_to_shadow():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    assert strat.market_risk_gate_mode == "shadow"


def test_market_risk_gate_shadow_tags_reason_without_blocking():
    """shadow(기본): 앵커가 임계 이상 빠져도 진입은 막지 않고 사유에만 표기한다."""
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    ctx = _ctx({"AAA": 100.0}, _now_within_window(), anchor_bars={"QQQ": _anchor_bars(-1.0)})
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1, "shadow는 진입을 막지 않는다"
    assert "[시장:리스크오프" in signals[0].reason
    assert strat.market_risk_verdict.get("US") is True


def test_market_risk_gate_block_mode_blocks_entry():
    strat = NewsMomentumStrategy(
        ["AAA"], _params(market_risk_gate_mode="block"), tags_of={"AAA": ["EVENT"]}
    )
    ctx = _ctx({"AAA": 100.0}, _now_within_window(), anchor_bars={"QQQ": _anchor_bars(-1.0)})
    signals = strat.on_cycle(ctx)
    assert signals == []
    assert "리스크오프" in strat.last_reject["AAA"]


def test_market_risk_gate_block_mode_allows_entry_when_anchor_within_threshold():
    strat = NewsMomentumStrategy(
        ["AAA"], _params(market_risk_gate_mode="block", market_risk_max_drawdown_pct=0.5),
        tags_of={"AAA": ["EVENT"]},
    )
    ctx = _ctx({"AAA": 100.0}, _now_within_window(), anchor_bars={"QQQ": _anchor_bars(-0.1)})
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1


def test_market_risk_gate_off_mode_skips_anchor_query_entirely():
    strat = NewsMomentumStrategy(
        ["AAA"], _params(market_risk_gate_mode="off"), tags_of={"AAA": ["EVENT"]}
    )
    ctx = _ctx({"AAA": 100.0}, _now_within_window(), anchor_bars={"QQQ": _anchor_bars(-5.0)})
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert "리스크오프" not in signals[0].reason
    assert "QQQ" not in ctx.data.history_calls, "off 모드는 앵커 조회 자체를 하지 않는다"


def test_market_risk_gate_missing_anchor_data_falls_back_to_pass():
    """앵커 데이터 없음(빈 결과) — block 모드여도 게이트 부재로 통과(기존 동작)."""
    strat = NewsMomentumStrategy(
        ["AAA"], _params(market_risk_gate_mode="block"), tags_of={"AAA": ["EVENT"]}
    )
    ctx = _ctx({"AAA": 100.0}, _now_within_window())  # anchor_bars 없음
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert "리스크오프" not in signals[0].reason


def test_market_risk_gate_queries_anchor_once_per_minute_across_symbols_and_cycles():
    """분 경계 캐시 — 후보 여러 종목 + 반복 사이클에도 앵커 조회는 분당 1회."""
    strat = NewsMomentumStrategy(
        ["AAA", "BBB"], _params(max_entries_per_session=5),
        tags_of={"AAA": ["EVENT"], "BBB": ["EVENT"]},
    )
    now = _now_within_window()
    ctx = _ctx({"AAA": 100.0, "BBB": 50.0}, now, anchor_bars={"QQQ": _anchor_bars(-1.0)})
    strat.on_cycle(ctx)
    strat.on_cycle(ctx)  # 같은 분 — 재조회 없어야 함
    assert ctx.data.history_calls.count("QQQ") == 1


def test_market_risk_gate_does_not_affect_exit_management():
    """리스크오프 상태에서도 청산(손절)은 정상 동작 — 게이트는 신규 진입에만 붙는다."""
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of={})
    pos = Position(symbol="AAA", qty=10, avg_cost=100.0,
                   meta={"lots": {strat.id: {"qty": 10.0, "entry": 100.0, "session": None}}})
    ctx = _ctx({"AAA": 97.0}, _now_within_window(), positions={"AAA": pos},
               anchor_bars={"QQQ": _anchor_bars(-3.0)})
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.EXIT_LONG


# ============================================================ 개장 확인(open confirmation)
# 2026-08-18 KR news_momentum 3건 동시 손절 관측 기반. `_ctx`의 `anchor_bars`는
# 사실 "요청 심볼별 history() 응답" 맵이라 앵커가 아닌 매매 대상 심볼("AAA")에도
# 그대로 재사용한다(FakeDataFeed.history는 심볼 무관하게 동작).

def _confirm_bars(open_price: float, close_price: float, day=DAY1, tz=NY, start=US_OPEN) -> pd.DataFrame:
    """개장 확인용 봉 합성 — 세션 오픈 시각에 걸리는 첫 1분봉 1개."""
    idx = [datetime.combine(day, start, tzinfo=tz)]
    rows = [{
        "open": open_price, "high": max(open_price, close_price),
        "low": min(open_price, close_price), "close": close_price, "volume": 1000.0,
    }]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def test_open_confirm_defaults_to_off():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    assert strat.open_confirm_mode == "off"


def test_open_confirm_off_is_unchanged_and_skips_history_lookup():
    strat = NewsMomentumStrategy(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    ctx = _ctx({"AAA": 100.0}, _now_within_window())
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG
    assert "개장확인" not in signals[0].reason
    assert "AAA" not in ctx.data.history_calls, "off 모드는 개장확인 조회 자체를 하지 않는다"


def test_open_confirm_bar_mode_enters_on_green_first_bar():
    strat = NewsMomentumStrategy(
        ["AAA"], _params(open_confirm_mode="bar"), tags_of={"AAA": ["EVENT"]},
    )
    bars = _confirm_bars(open_price=100.0, close_price=101.0)
    ctx = _ctx({"AAA": 101.5}, _now_within_window(seconds_after_open=90.0), anchor_bars={"AAA": bars})
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG
    assert "[개장확인:bar]" in signals[0].reason


def test_open_confirm_bar_mode_skips_on_red_first_bar_without_retry():
    strat = NewsMomentumStrategy(
        ["AAA"], _params(open_confirm_mode="bar"), tags_of={"AAA": ["EVENT"]},
    )
    red_bars = _confirm_bars(open_price=100.0, close_price=99.0)
    ctx = _ctx({"AAA": 99.0}, _now_within_window(seconds_after_open=90.0), anchor_bars={"AAA": red_bars})
    signals = strat.on_cycle(ctx)
    assert signals == []
    assert "개장확인 실패" in strat.last_reject["AAA"]

    # 실패는 그 세션 재시도 없음 — 다음 사이클에 데이터가 양봉으로 바뀌어도 진입하지 않는다.
    green_bars = _confirm_bars(open_price=100.0, close_price=101.0)
    ctx2 = _ctx({"AAA": 101.0}, _now_within_window(seconds_after_open=95.0), anchor_bars={"AAA": green_bars})
    assert strat.on_cycle(ctx2) == []


def test_open_confirm_bar_mode_waits_until_first_bar_completes():
    """첫 1분봉이 아직 없으면(개장 30초) 진입도 실패 확정도 아닌 대기 상태."""
    strat = NewsMomentumStrategy(
        ["AAA"], _params(open_confirm_mode="bar"), tags_of={"AAA": ["EVENT"]},
    )
    ctx = _ctx({"AAA": 100.0}, _now_within_window(seconds_after_open=30.0))  # bars 없음
    signals = strat.on_cycle(ctx)
    assert signals == []
    assert "AAA" not in strat.last_reject
    assert "AAA" not in strat._entered_today


def test_open_confirm_above_open_enters_when_price_above_open_after_n_minutes():
    strat = NewsMomentumStrategy(
        ["AAA"],
        _params(open_confirm_mode="above_open", open_confirm_minutes=5, entry_window_seconds=400),
        tags_of={"AAA": ["EVENT"]},
    )
    bars = _confirm_bars(open_price=100.0, close_price=100.0)
    ctx = _ctx({"AAA": 101.0}, _now_within_window(seconds_after_open=360.0), anchor_bars={"AAA": bars})
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert "[개장확인:above_open]" in signals[0].reason


def test_open_confirm_above_open_skips_when_price_at_or_below_open():
    strat = NewsMomentumStrategy(
        ["AAA"],
        _params(open_confirm_mode="above_open", open_confirm_minutes=5, entry_window_seconds=400),
        tags_of={"AAA": ["EVENT"]},
    )
    bars = _confirm_bars(open_price=100.0, close_price=100.0)
    ctx = _ctx({"AAA": 99.5}, _now_within_window(seconds_after_open=360.0), anchor_bars={"AAA": bars})
    signals = strat.on_cycle(ctx)
    assert signals == []
    assert "개장확인 실패" in strat.last_reject["AAA"]


def test_open_confirm_above_open_waits_before_n_minutes_elapsed():
    strat = NewsMomentumStrategy(
        ["AAA"],
        _params(open_confirm_mode="above_open", open_confirm_minutes=5, entry_window_seconds=400),
        tags_of={"AAA": ["EVENT"]},
    )
    bars = _confirm_bars(open_price=100.0, close_price=100.0)
    ctx = _ctx({"AAA": 105.0}, _now_within_window(seconds_after_open=120.0), anchor_bars={"AAA": bars})
    signals = strat.on_cycle(ctx)
    assert signals == []
    assert "AAA" not in strat.last_reject
    assert "AAA" not in strat._entered_today


def test_open_confirm_missed_entry_window_while_waiting_means_no_entry():
    """확인 대기 중(데이터 없음) 진입 창을 넘기면 그 세션은 끝 — 이후 데이터가
    도착해도 진입하지 않는다(재시도 창은 진입 창 안에서만)."""
    strat = NewsMomentumStrategy(
        ["AAA"], _params(open_confirm_mode="bar", entry_window_seconds=60),
        tags_of={"AAA": ["EVENT"]},
    )
    ctx_waiting = _ctx({"AAA": 100.0}, _now_within_window(seconds_after_open=30.0))  # bars 없음 → wait
    assert strat.on_cycle(ctx_waiting) == []

    bars = _confirm_bars(open_price=100.0, close_price=101.0)
    ctx_after_window = _ctx(
        {"AAA": 101.0}, _now_within_window(seconds_after_open=90.0), anchor_bars={"AAA": bars},
    )
    assert strat.on_cycle(ctx_after_window) == []


def test_open_confirm_bars_use_minute_boundary_cache():
    strat = NewsMomentumStrategy(
        ["AAA"], _params(open_confirm_mode="bar"), tags_of={"AAA": ["EVENT"]},
    )
    ctx = _ctx({"AAA": 100.0}, _now_within_window(seconds_after_open=30.0))  # bars 없음, wait만
    strat.on_cycle(ctx)
    strat.on_cycle(ctx)  # 같은 분 — 재조회 없어야 함
    assert ctx.data.history_calls.count("AAA") == 1


def test_invalid_open_confirm_mode_falls_back_to_off():
    strat = NewsMomentumStrategy(
        ["AAA"], _params(open_confirm_mode="bogus"), tags_of={"AAA": ["EVENT"]},
    )
    assert strat.open_confirm_mode == "off"


def test_invalid_open_confirm_minutes_raises():
    with pytest.raises(ValueError):
        NewsMomentumStrategy(["AAA"], _params(open_confirm_minutes=0))
