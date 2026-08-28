"""`NewsMomentumPureStrategy`(순수함수 계약, quant.core.strategy_api)가 기존
`NewsMomentumStrategy`와 **같은 신호**를 내는지 증명한다 — 엔진 분리 설계
Phase A, donchian/scalp_1m 에 이은 세 번째 이전 대상.

`tests/test_scalp_1m_pure.py`의 하니스를 그대로 따른다: 전 층위에서
`NewsMomentumPureShell.on_cycle(ctx)`(= `Strategy` Protocol 그대로)를 쓴다 —
`StrategySnapshot`을 손으로 조립하지 않고 `PureStrategyShell`이 실제로 `ctx`에서
스냅샷을 만드는 전체 경로(requirements() → snapshot 조립 → decide())를 태워
legacy `NewsMomentumStrategy.on_cycle(ctx)`와 나란히 비교한다(shell 배선 자체의
버그도 잡힌다).

1. 단일 사이클 동치 — EVENT 태그 게이트, 진입창 안/밖, 세션 상한, 후보 랭킹,
   시장 리스크오프 게이트(off/shadow/block), 개장 확인(off/bar/above_open),
   관리(손절·목표가·타임아웃·부분익절·EoD·오버나잇), 무포지션 무신호.
2. 다중 사이클 동치(state 왕복) — 진입 → 체결 시뮬레이션 → 다음 사이클 관리,
   재발동 방지(세션 1회·partial_taken), 개장확인 실패의 "오늘 재시도 없음".

`run_backtest`는 쓰지 않는다 — `test_scalp_1m_pure.py`와 같은 이유(이 전략은
관심종목 태그가 런타임에 채워지는 구조라 정적 백테스트 심볼이 없고, 저장소에
news_momentum용 `run_backtest` 선례 자체가 없다). 다중 세션 규모 동치는 합성
다중일 시퀀스로 대체한다.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Position, Quote, SignalAction
from quant.core.ports import Context
from quant.trade.strategy.news_momentum import NewsMomentumPureShell, NewsMomentumStrategy

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DAY1 = date(2026, 1, 5)  # 월요일
DAY2 = date(2026, 1, 6)
US_OPEN = dtime(9, 30)

LEGACY_ID = "news_momentum"
PURE_ID = "news_momentum_pure"


# ============================================================ 페이크 인프라
# test_news_momentum.py와 인터페이스는 같지만, should_flatten을 실제 공식
# (mtc - cadence < flatten_minutes, quant/core/clock.py)으로 구현한다 — legacy는
# ctx.clock.should_flatten을 직접 부르고, pure는 snap.minutes_to_close/
# cadence_minutes로 같은 공식을 재현하므로(NewsMomentumPureStrategy._should_flatten),
# 두 경로가 같은 공식을 공유해야 동치성이 의미가 있다(scalp_1m_pure 선례와 동일).

class FakeClock:
    def __init__(self, now, open_markets=frozenset({"US"}), minutes_to_close=300.0,
                 cadence_minutes=5.0 / 60):
        self._now = now
        self._open = open_markets
        self._mtc = minutes_to_close
        self._cadence = cadence_minutes

    def now(self):
        return self._now

    def is_market_open(self, market):
        return market in self._open

    def minutes_to_close(self, market):
        return self._mtc

    def cadence_minutes(self):
        return self._cadence

    def should_flatten(self, market, flatten_minutes):
        mtc = self.minutes_to_close(market)
        return mtc is not None and mtc > 0 and mtc - self._cadence < flatten_minutes


class FakeDataFeed:
    def __init__(self, quotes, bars=None):
        self._quotes = quotes
        self._bars = bars or {}
        self.history_calls: list[str] = []

    def quote(self, symbol):
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(NY), price=self._quotes[symbol])

    def history(self, symbol, interval, n):
        self.history_calls.append(symbol)
        df = self._bars.get(symbol)
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

    def place_order(self, order):
        raise NotImplementedError


def _ctx(quotes, now, bars=None, positions=None, open_markets=frozenset({"US"}),
         minutes_to_close=300.0):
    return Context(
        clock=FakeClock(now, open_markets, minutes_to_close),
        data=FakeDataFeed(quotes, bars),
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


def _strats(symbols, params, tags_of=None, market="US"):
    legacy = NewsMomentumStrategy(list(symbols), params, market=market, id=LEGACY_ID,
                                  tags_of=tags_of)
    pure = NewsMomentumPureShell(list(symbols), params, market=market, id=PURE_ID,
                                 tags_of=tags_of)
    return legacy, pure


def _sig_key(sig):
    """비교용 튜플 — strategy_id는 두 구현이 별도 이름으로 등록돼 의도적으로
    다르므로 제외(scalp_1m_pure의 관례와 동일). reason은 포함(엄격 비교)."""
    return (
        sig.symbol, sig.action, sig.target_weight,
        sig.exit_fraction, sig.reason, sig.stop, sig.target, sig.state_update,
    )


def _keys(signals):
    return [_sig_key(s) for s in signals]


def _now_within_window(seconds_after_open: float = 30.0, day=DAY1) -> datetime:
    return datetime.combine(day, US_OPEN, tzinfo=NY) + timedelta(seconds=seconds_after_open)


def _lot_position(strategy_id, *, symbol="AAA", qty=10.0, entry=100.0,
                  entered_at=None, session=DAY1.isoformat(), partial_taken=False,
                  bare=False):
    """`bare=True`면 랏에 수량만 넣는다 — legacy `_ensure_state`가 `_pending`을
    랏으로 승격시키는 경로(체결 확인)를 그대로 태우기 위한 것이다."""
    lot = {"qty": qty}
    if not bare:
        lot.update(entry=entry, entered_at=entered_at, partial_taken=partial_taken,
                   session=session)
    return Position(symbol=symbol, qty=qty, avg_cost=entry, meta={"lots": {strategy_id: lot}})


def _seed_open(pure, symbol, *, entry=100.0, entered_at=None, session=DAY1.isoformat(),
               partial_taken=False):
    """`NewsMomentumPureShell`의 내부 `_state["open"]`을 직접 채운다 — legacy가
    `Position.meta["lots"][id]`에서 읽는 것과 달리 pure는 그 정보를 **next_state로만**
    들고 있으므로(클래스 docstring), 관리 시나리오를 단일 사이클에서 재현하려면
    여기를 채워야 한다. 심볼에 대응하는 `Position`은 qty>0이기만 하면 된다
    (shell이 `snap.lots`에 심볼을 채우는 유일한 조건)."""
    pure._state = {**pure._state, "open": {
        **pure._state.get("open", {}),
        symbol: {"entry": entry, "entered_at": entered_at, "session": session,
                 "partial_taken": partial_taken},
    }}


def _anchor_bars(*, drawdown_pct: float, day=DAY1, n=3):
    """앵커(QQQ) 1분봉 — 당일 시가 100.0 대비 마지막 종가가 `drawdown_pct`%."""
    open_ts = datetime.combine(day, US_OPEN, tzinfo=NY)
    last = 100.0 * (1 + drawdown_pct / 100)
    rows = []
    for i in range(n):
        c = 100.0 if i < n - 1 else last
        rows.append({"open": 100.0, "high": max(100.0, c), "low": min(100.0, c),
                     "close": c, "volume": 1000.0})
    idx = [open_ts + timedelta(minutes=i) for i in range(n)]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=NY))


def _confirm_bars(*, up: bool, day=DAY1):
    """개장 확인용 세션 첫 1분봉 — 양봉(up=True)이면 확인 통과."""
    open_ts = datetime.combine(day, US_OPEN, tzinfo=NY)
    close = 100.5 if up else 99.5
    rows = [{"open": 100.0, "high": max(100.0, close) + 0.1,
             "low": min(100.0, close) - 0.1, "close": close, "volume": 1000.0}]
    return pd.DataFrame(rows, index=pd.DatetimeIndex([open_ts], tz=NY))


# ============================================================ 층위 1 — 단일 사이클 동치
# ---------------- EVENT 태그 게이트 / 진입 신호

def test_entry_signal_equivalence():
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(30.0)
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 100.0}, now))
    sig_pure = pure.on_cycle(_ctx({"AAA": 100.0}, now))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and sig_legacy[0].action == SignalAction.ENTER_LONG
    assert sig_legacy[0].stop == pytest.approx(98.0)


def test_no_tags_of_no_signal_equivalence():
    legacy, pure = _strats(["AAA"], _params(), tags_of=None)
    now = _now_within_window(30.0)
    assert legacy.on_cycle(_ctx({"AAA": 100.0}, now)) == []
    assert pure.on_cycle(_ctx({"AAA": 100.0}, now)) == []


def test_symbol_without_event_tag_equivalence():
    legacy, pure = _strats(["AAA", "BBB"], _params(), tags_of={"AAA": ["TREND"]})
    now = _now_within_window(30.0)
    assert legacy.on_cycle(_ctx({"AAA": 100.0, "BBB": 100.0}, now)) == []
    assert pure.on_cycle(_ctx({"AAA": 100.0, "BBB": 100.0}, now)) == []


def test_no_position_no_candidate_no_signal_equivalence():
    """포지션도 없고 후보도 없으면 양쪽 다 완전 침묵(관리 경로가 남의 포지션을
    입양하지 않는다)."""
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(30.0)
    # 다른 전략이 소유한 포지션(lots에 내 id가 없다) — 관리 대상이 아니다.
    other = Position(symbol="ZZZ", qty=5.0, avg_cost=50.0,
                     meta={"lots": {"someone_else": {"qty": 5.0, "entry": 50.0}}})
    sig_legacy = legacy.on_cycle(_ctx({"ZZZ": 40.0}, now, positions={"ZZZ": other}))
    sig_pure = pure.on_cycle(_ctx({"ZZZ": 40.0}, now, positions={"ZZZ": other}))
    assert sig_legacy == [] and sig_pure == []


def test_no_entry_outside_window_equivalence():
    legacy, pure = _strats(["AAA"], _params(entry_window_seconds=120),
                           tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(300.0)
    assert legacy.on_cycle(_ctx({"AAA": 100.0}, now)) == []
    assert pure.on_cycle(_ctx({"AAA": 100.0}, now)) == []


def test_no_entry_before_open_equivalence():
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(-30.0)
    assert legacy.on_cycle(_ctx({"AAA": 100.0}, now)) == []
    assert pure.on_cycle(_ctx({"AAA": 100.0}, now)) == []


def test_market_closed_no_entry_equivalence():
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(30.0)
    assert legacy.on_cycle(_ctx({"AAA": 100.0}, now, open_markets=frozenset())) == []
    assert pure.on_cycle(_ctx({"AAA": 100.0}, now, open_markets=frozenset())) == []


def test_no_quote_no_entry_equivalence():
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(30.0)
    assert legacy.on_cycle(_ctx({}, now)) == []
    assert pure.on_cycle(_ctx({}, now)) == []


def test_max_entries_per_session_cap_equivalence():
    """상한 2 — 후보 3개 중 2개만 진입, 심볼 순서까지 같아야 한다."""
    symbols = ["AAA", "BBB", "CCC"]
    tags = {s: ["EVENT"] for s in symbols}
    legacy, pure = _strats(symbols, _params(max_entries_per_session=2), tags_of=tags)
    now = _now_within_window(30.0)
    quotes = {"AAA": 100.0, "BBB": 200.0, "CCC": 300.0}
    sig_legacy = legacy.on_cycle(_ctx(quotes, now))
    sig_pure = pure.on_cycle(_ctx(quotes, now))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 2


def test_candidate_ranking_trend_first_equivalence():
    """TREND 동반 후보가 먼저 — `_rank_candidates` 재사용이 실제로 같은 순서를
    내는지 상한 1로 강제해 고정한다."""
    symbols = ["AAA", "BBB"]
    tags = {"AAA": ["EVENT"], "BBB": ["EVENT", "TREND"]}
    legacy, pure = _strats(symbols, _params(max_entries_per_session=1), tags_of=tags)
    now = _now_within_window(30.0)
    quotes = {"AAA": 100.0, "BBB": 200.0}
    sig_legacy = legacy.on_cycle(_ctx(quotes, now))
    sig_pure = pure.on_cycle(_ctx(quotes, now))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and sig_legacy[0].symbol == "BBB"


# ---------------- 시장 리스크오프 게이트 동치

def test_market_risk_gate_block_equivalence():
    legacy, pure = _strats(["AAA"], _params(market_risk_gate_mode="block",
                                            market_risk_max_drawdown_pct=0.5),
                           tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(30.0)
    bars = {"QQQ": _anchor_bars(drawdown_pct=-1.2)}
    assert legacy.on_cycle(_ctx({"AAA": 100.0}, now, bars=bars)) == []
    assert pure.on_cycle(_ctx({"AAA": 100.0}, now, bars=bars)) == []


def test_market_risk_gate_shadow_notes_but_enters_equivalence():
    legacy, pure = _strats(["AAA"], _params(market_risk_gate_mode="shadow",
                                            market_risk_max_drawdown_pct=0.5),
                           tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(30.0)
    bars = {"QQQ": _anchor_bars(drawdown_pct=-1.2)}
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 100.0}, now, bars=bars))
    sig_pure = pure.on_cycle(_ctx({"AAA": 100.0}, now, bars=bars))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1
    assert "시장:리스크오프" in sig_legacy[0].reason


def test_market_risk_gate_above_threshold_no_note_equivalence():
    legacy, pure = _strats(["AAA"], _params(market_risk_gate_mode="block",
                                            market_risk_max_drawdown_pct=0.5),
                           tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(30.0)
    bars = {"QQQ": _anchor_bars(drawdown_pct=-0.1)}
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 100.0}, now, bars=bars))
    sig_pure = pure.on_cycle(_ctx({"AAA": 100.0}, now, bars=bars))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "시장:" not in sig_legacy[0].reason


def test_market_risk_gate_off_equivalence():
    """off 모드는 앵커를 아예 안 본다 — pure 는 requirements()에서 선언조차 하지
    않는다(조회 0회)."""
    legacy, pure = _strats(["AAA"], _params(market_risk_gate_mode="off"),
                           tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(30.0)
    bars = {"QQQ": _anchor_bars(drawdown_pct=-5.0)}
    ctx_legacy = _ctx({"AAA": 100.0}, now, bars=bars)
    ctx_pure = _ctx({"AAA": 100.0}, now, bars=bars)
    sig_legacy = legacy.on_cycle(ctx_legacy)
    sig_pure = pure.on_cycle(ctx_pure)
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1
    assert ctx_legacy.data.history_calls == [] and ctx_pure.data.history_calls == []


def test_market_risk_gate_missing_anchor_data_is_absent_gate_equivalence():
    """앵커 데이터가 없으면 게이트 부재(기존 동작) — 양쪽 다 그냥 진입한다."""
    legacy, pure = _strats(["AAA"], _params(market_risk_gate_mode="block"),
                           tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(30.0)
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 100.0}, now))
    sig_pure = pure.on_cycle(_ctx({"AAA": 100.0}, now))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1


# ---------------- 개장 확인 동치

def test_open_confirm_bar_pass_equivalence():
    legacy, pure = _strats(["AAA"], _params(open_confirm_mode="bar"),
                           tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(70.0)
    bars = {"AAA": _confirm_bars(up=True)}
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 100.5}, now, bars=bars))
    sig_pure = pure.on_cycle(_ctx({"AAA": 100.5}, now, bars=bars))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "개장확인:bar" in sig_legacy[0].reason


def test_open_confirm_bar_fail_equivalence():
    legacy, pure = _strats(["AAA"], _params(open_confirm_mode="bar"),
                           tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(70.0)
    bars = {"AAA": _confirm_bars(up=False)}
    assert legacy.on_cycle(_ctx({"AAA": 99.5}, now, bars=bars)) == []
    assert pure.on_cycle(_ctx({"AAA": 99.5}, now, bars=bars)) == []


def test_open_confirm_wait_when_no_bars_equivalence():
    legacy, pure = _strats(["AAA"], _params(open_confirm_mode="bar"),
                           tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(30.0)
    assert legacy.on_cycle(_ctx({"AAA": 100.0}, now)) == []
    assert pure.on_cycle(_ctx({"AAA": 100.0}, now)) == []


def test_open_confirm_above_open_equivalence():
    """above_open — 확인 시간이 지나야 판정하므로 entry_window_seconds도 함께 넓힌다."""
    params = _params(open_confirm_mode="above_open", open_confirm_minutes=2,
                     entry_window_seconds=600)
    legacy, pure = _strats(["AAA"], params, tags_of={"AAA": ["EVENT"]})
    bars = {"AAA": _confirm_bars(up=True)}

    # 아직 2분이 안 지났다 — 양쪽 다 대기(신호 없음)
    early = _now_within_window(60.0)
    assert legacy.on_cycle(_ctx({"AAA": 101.0}, early, bars=bars)) == []
    assert pure.on_cycle(_ctx({"AAA": 101.0}, early, bars=bars)) == []

    # 2분 경과 + 시가 위 — 양쪽 다 진입
    late = _now_within_window(150.0)
    sig_legacy = legacy.on_cycle(_ctx({"AAA": 101.0}, late, bars=bars))
    sig_pure = pure.on_cycle(_ctx({"AAA": 101.0}, late, bars=bars))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "개장확인:above_open" in sig_legacy[0].reason


def test_open_confirm_above_open_fail_equivalence():
    params = _params(open_confirm_mode="above_open", open_confirm_minutes=2,
                     entry_window_seconds=600)
    legacy, pure = _strats(["AAA"], params, tags_of={"AAA": ["EVENT"]})
    bars = {"AAA": _confirm_bars(up=True)}
    late = _now_within_window(150.0)
    assert legacy.on_cycle(_ctx({"AAA": 99.0}, late, bars=bars)) == []
    assert pure.on_cycle(_ctx({"AAA": 99.0}, late, bars=bars)) == []


# ---------------- 관리(청산) 동치

def test_stop_loss_exit_equivalence():
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(300.0)
    entered_at = now.isoformat()
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, entered_at=entered_at)
    pos_pure = _lot_position(PURE_ID, entry=100.0, entered_at=entered_at)
    _seed_open(pure, "AAA", entry=100.0, entered_at=entered_at)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 97.9}, now, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 97.9}, now, positions={"AAA": pos_pure}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "손절" in sig_legacy[0].reason


def test_full_take_exit_equivalence():
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(300.0)
    entered_at = now.isoformat()
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, entered_at=entered_at)
    pos_pure = _lot_position(PURE_ID, entry=100.0, entered_at=entered_at)
    _seed_open(pure, "AAA", entry=100.0, entered_at=entered_at)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 110.5}, now, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 110.5}, now, positions={"AAA": pos_pure}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "목표가" in sig_legacy[0].reason


def test_max_hold_timeout_exit_equivalence():
    legacy, pure = _strats(["AAA"], _params(max_hold_minutes=30), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(300.0)
    entered_at = (now - timedelta(minutes=31)).isoformat()
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, entered_at=entered_at)
    pos_pure = _lot_position(PURE_ID, entry=100.0, entered_at=entered_at)
    _seed_open(pure, "AAA", entry=100.0, entered_at=entered_at)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos_pure}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "보유시간 초과" in sig_legacy[0].reason


def test_max_hold_disabled_no_timeout_equivalence():
    legacy, pure = _strats(["AAA"], _params(max_hold_minutes=0), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(300.0)
    entered_at = (now - timedelta(minutes=600)).isoformat()
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, entered_at=entered_at)
    pos_pure = _lot_position(PURE_ID, entry=100.0, entered_at=entered_at)
    _seed_open(pure, "AAA", entry=100.0, entered_at=entered_at)

    assert legacy.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos_legacy})) == []
    assert pure.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos_pure})) == []


def test_partial_take_profit_equivalence():
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(300.0)
    entered_at = now.isoformat()
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, entered_at=entered_at)
    pos_pure = _lot_position(PURE_ID, entry=100.0, entered_at=entered_at)
    _seed_open(pure, "AAA", entry=100.0, entered_at=entered_at)

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 105.5}, now, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 105.5}, now, positions={"AAA": pos_pure}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and sig_legacy[0].action == SignalAction.SCALE_OUT
    assert sig_legacy[0].state_update == {"partial_taken": True}


def test_partial_take_profit_does_not_refire_equivalence():
    """재발동 방지 플래그 동치 — partial_taken이 서 있으면 양쪽 다 침묵."""
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(300.0)
    entered_at = now.isoformat()
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, entered_at=entered_at,
                               partial_taken=True)
    pos_pure = _lot_position(PURE_ID, entry=100.0, entered_at=entered_at,
                             partial_taken=True)
    _seed_open(pure, "AAA", entry=100.0, entered_at=entered_at, partial_taken=True)

    assert legacy.on_cycle(_ctx({"AAA": 105.5}, now, positions={"AAA": pos_legacy})) == []
    assert pure.on_cycle(_ctx({"AAA": 105.5}, now, positions={"AAA": pos_pure})) == []


def test_eod_flatten_exit_equivalence():
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(300.0)
    entered_at = now.isoformat()
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, entered_at=entered_at)
    pos_pure = _lot_position(PURE_ID, entry=100.0, entered_at=entered_at)
    _seed_open(pure, "AAA", entry=100.0, entered_at=entered_at)

    # flatten_before_close_minutes=1, cadence=5/60 -> mtc=0.5면 0.5-0.083<1 True
    sig_legacy = legacy.on_cycle(
        _ctx({"AAA": 101.0}, now, positions={"AAA": pos_legacy}, minutes_to_close=0.5))
    sig_pure = pure.on_cycle(
        _ctx({"AAA": 101.0}, now, positions={"AAA": pos_pure}, minutes_to_close=0.5))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "EoD" in sig_legacy[0].reason


def test_session_roll_overnight_exit_equivalence():
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = datetime.combine(DAY2, dtime(10, 30), tzinfo=NY)
    entered_at = datetime.combine(DAY1, dtime(9, 31), tzinfo=NY).isoformat()
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, entered_at=entered_at,
                               session=DAY1.isoformat())
    pos_pure = _lot_position(PURE_ID, entry=100.0, entered_at=entered_at,
                             session=DAY1.isoformat())
    _seed_open(pure, "AAA", entry=100.0, entered_at=entered_at, session=DAY1.isoformat())

    sig_legacy = legacy.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos_legacy}))
    sig_pure = pure.on_cycle(_ctx({"AAA": 101.0}, now, positions={"AAA": pos_pure}))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and "오버나잇" in sig_legacy[0].reason


def test_no_quote_for_open_position_no_signal_equivalence():
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    now = _now_within_window(300.0)
    entered_at = now.isoformat()
    pos_legacy = _lot_position(LEGACY_ID, entry=100.0, entered_at=entered_at)
    pos_pure = _lot_position(PURE_ID, entry=100.0, entered_at=entered_at)
    _seed_open(pure, "AAA", entry=100.0, entered_at=entered_at)

    assert legacy.on_cycle(_ctx({}, now, positions={"AAA": pos_legacy})) == []
    assert pure.on_cycle(_ctx({}, now, positions={"AAA": pos_pure})) == []


# ============================================================ 층위 2 — state 왕복 동치
# legacy.on_cycle(ctx)와 pure.on_cycle(ctx)를 "체결 시뮬레이션"으로 연결한 여러
# 사이클에 걸쳐 나란히 구동한다.

def _fill(positions, strategy_id, symbol, entry, qty=10.0):
    """체결 시뮬레이션 — 랏에 수량만 채운다(진입 컨텍스트는 legacy가
    `_ensure_state`로 `_pending`에서, pure가 `pending`→`open`으로 각자 승격한다)."""
    positions[symbol] = _lot_position(strategy_id, symbol=symbol, qty=qty, entry=entry,
                                      bare=True)


def test_state_roundtrip_entry_then_manage_equivalence():
    """사이클1 진입 → 체결 → 사이클2 관리(손절). pending→open(state) 승격이
    legacy 의 `_pending`→`Position.meta` 승격과 같은 결과를 내야 한다."""
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    legacy_positions: dict[str, Position] = {}
    pure_positions: dict[str, Position] = {}

    now1 = _now_within_window(30.0)
    sig_legacy1 = legacy.on_cycle(_ctx({"AAA": 100.0}, now1, positions=legacy_positions))
    sig_pure1 = pure.on_cycle(_ctx({"AAA": 100.0}, now1, positions=pure_positions))
    assert _keys(sig_legacy1) == _keys(sig_pure1)
    assert len(sig_legacy1) == 1 and sig_legacy1[0].action == SignalAction.ENTER_LONG

    _fill(legacy_positions, LEGACY_ID, "AAA", 100.0)
    _fill(pure_positions, PURE_ID, "AAA", 100.0)

    # 사이클 2: 아직 진입창 안이지만 재진입은 없어야 하고(세션 1회), 가격이
    # 손절선 아래라 청산 신호가 나가야 한다.
    now2 = _now_within_window(60.0)
    sig_legacy2 = legacy.on_cycle(_ctx({"AAA": 97.5}, now2, positions=legacy_positions))
    sig_pure2 = pure.on_cycle(_ctx({"AAA": 97.5}, now2, positions=pure_positions))
    assert _keys(sig_legacy2) == _keys(sig_pure2)
    assert len(sig_legacy2) == 1 and "손절" in sig_legacy2[0].reason
    # 진입가는 체결 랏의 avg_cost 가 아니라 신호 시점 가격(pending 승격)이다.
    assert "entry=100.00" in sig_legacy2[0].reason
    assert "entry=100.00" in sig_pure2[0].reason


def test_state_roundtrip_no_reentry_same_session_equivalence():
    """세션당 1회 — 청산 후에도 같은 세션에는 재진입하지 않는다(`_entered_today`
    ↔ `entered_today`)."""
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    legacy_positions: dict[str, Position] = {}
    pure_positions: dict[str, Position] = {}

    now1 = _now_within_window(20.0)
    assert len(legacy.on_cycle(_ctx({"AAA": 100.0}, now1, positions=legacy_positions))) == 1
    assert len(pure.on_cycle(_ctx({"AAA": 100.0}, now1, positions=pure_positions))) == 1

    _fill(legacy_positions, LEGACY_ID, "AAA", 100.0)
    _fill(pure_positions, PURE_ID, "AAA", 100.0)
    # 청산 체결 — 포지션을 닫는다.
    now2 = _now_within_window(60.0)
    legacy.on_cycle(_ctx({"AAA": 97.5}, now2, positions=legacy_positions))
    pure.on_cycle(_ctx({"AAA": 97.5}, now2, positions=pure_positions))
    legacy_positions["AAA"].qty = 0.0
    legacy_positions["AAA"].meta["lots"].pop(LEGACY_ID, None)
    pure_positions["AAA"].qty = 0.0
    pure_positions["AAA"].meta["lots"].pop(PURE_ID, None)

    now3 = _now_within_window(90.0)
    sig_legacy3 = legacy.on_cycle(_ctx({"AAA": 100.0}, now3, positions=legacy_positions))
    sig_pure3 = pure.on_cycle(_ctx({"AAA": 100.0}, now3, positions=pure_positions))
    assert sig_legacy3 == [] and sig_pure3 == []


def test_state_roundtrip_partial_then_no_refire_equivalence():
    """부분익절 1회 → 다음 사이클 재발동 없음. legacy 는 `state_update`가
    `Position.meta` 랏에 적용되고, pure 는 `next_state["open"]`에 남는다."""
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    legacy_positions: dict[str, Position] = {}
    pure_positions: dict[str, Position] = {}

    now1 = _now_within_window(20.0)
    legacy.on_cycle(_ctx({"AAA": 100.0}, now1, positions=legacy_positions))
    pure.on_cycle(_ctx({"AAA": 100.0}, now1, positions=pure_positions))
    _fill(legacy_positions, LEGACY_ID, "AAA", 100.0)
    _fill(pure_positions, PURE_ID, "AAA", 100.0)

    now2 = _now_within_window(60.0)
    sig_legacy2 = legacy.on_cycle(_ctx({"AAA": 105.5}, now2, positions=legacy_positions))
    sig_pure2 = pure.on_cycle(_ctx({"AAA": 105.5}, now2, positions=pure_positions))
    assert _keys(sig_legacy2) == _keys(sig_pure2)
    assert len(sig_legacy2) == 1 and sig_legacy2[0].action == SignalAction.SCALE_OUT

    # 체결 반영(loop._execute_signal 동치) — legacy 랏에 state_update 적용.
    legacy_positions["AAA"].meta["lots"][LEGACY_ID].update(sig_legacy2[0].state_update)
    pure_positions["AAA"].meta["lots"][PURE_ID].update(sig_pure2[0].state_update)

    now3 = _now_within_window(90.0)
    assert legacy.on_cycle(_ctx({"AAA": 106.0}, now3, positions=legacy_positions)) == []
    assert pure.on_cycle(_ctx({"AAA": 106.0}, now3, positions=pure_positions)) == []


def test_state_roundtrip_open_confirm_fail_blocks_rest_of_day_equivalence():
    """개장확인 실패는 "오늘 이 종목 재시도 없음"으로 굳는다 — 다음 사이클에
    확인 조건이 충족돼도 양쪽 다 진입하지 않는다."""
    params = _params(open_confirm_mode="bar", entry_window_seconds=600)
    legacy, pure = _strats(["AAA"], params, tags_of={"AAA": ["EVENT"]})

    now1 = _now_within_window(70.0)
    bars_fail = {"AAA": _confirm_bars(up=False)}
    assert legacy.on_cycle(_ctx({"AAA": 99.5}, now1, bars=bars_fail)) == []
    assert pure.on_cycle(_ctx({"AAA": 99.5}, now1, bars=bars_fail)) == []

    now2 = _now_within_window(130.0)
    bars_ok = {"AAA": _confirm_bars(up=True)}
    assert legacy.on_cycle(_ctx({"AAA": 100.5}, now2, bars=bars_ok)) == []
    assert pure.on_cycle(_ctx({"AAA": 100.5}, now2, bars=bars_ok)) == []


def test_multi_session_entry_count_equivalence():
    """3거래일 — 매일 세션 롤로 재무장돼 총 진입 수가 같아야 한다."""
    legacy, pure = _strats(["AAA"], _params(), tags_of={"AAA": ["EVENT"]})
    legacy_positions: dict[str, Position] = {}
    pure_positions: dict[str, Position] = {}
    legacy_entries = pure_entries = 0

    for day in (DAY1, DAY2, date(2026, 1, 7)):
        now = _now_within_window(30.0, day=day)
        sig_legacy = legacy.on_cycle(_ctx({"AAA": 100.0}, now, positions=legacy_positions))
        sig_pure = pure.on_cycle(_ctx({"AAA": 100.0}, now, positions=pure_positions))
        assert _keys(sig_legacy) == _keys(sig_pure), f"day={day} 신호 불일치"
        legacy_entries += sum(1 for s in sig_legacy if s.action == SignalAction.ENTER_LONG)
        pure_entries += sum(1 for s in sig_pure if s.action == SignalAction.ENTER_LONG)

        # 체결 후 그날 안에 EoD 로 청산해 다음날이 깨끗하게 시작되게 한다.
        _fill(legacy_positions, LEGACY_ID, "AAA", 100.0)
        _fill(pure_positions, PURE_ID, "AAA", 100.0)
        now_eod = _now_within_window(90.0, day=day)
        sig_legacy_eod = legacy.on_cycle(
            _ctx({"AAA": 101.0}, now_eod, positions=legacy_positions, minutes_to_close=0.5))
        sig_pure_eod = pure.on_cycle(
            _ctx({"AAA": 101.0}, now_eod, positions=pure_positions, minutes_to_close=0.5))
        assert _keys(sig_legacy_eod) == _keys(sig_pure_eod)
        legacy_positions.pop("AAA")
        pure_positions.pop("AAA")

    assert legacy_entries == pure_entries == 3


def test_kr_us_markets_are_independent_equivalence():
    """시장별 세션 롤/카운터 분리 — KR 심볼과 US 심볼을 함께 들고 US만 열린
    사이클에서 두 구현이 같은 신호를 내야 한다."""
    symbols = ["AAA", "005930"]
    tags = {s: ["EVENT"] for s in symbols}
    legacy, pure = _strats(symbols, _params(), tags_of=tags)
    now = _now_within_window(30.0)
    quotes = {"AAA": 100.0, "005930": 80000.0}
    sig_legacy = legacy.on_cycle(_ctx(quotes, now, open_markets=frozenset({"US"})))
    sig_pure = pure.on_cycle(_ctx(quotes, now, open_markets=frozenset({"US"})))
    assert _keys(sig_legacy) == _keys(sig_pure)
    assert len(sig_legacy) == 1 and sig_legacy[0].symbol == "AAA"
