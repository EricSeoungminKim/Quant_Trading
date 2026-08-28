"""`FrgnAccumulatePureStrategy`(순수함수 계약, `quant.core.strategy_api`)가 기존
`FrgnAccumulateStrategy`와 **같은 신호**를 내는지 증명한다 — 엔진 분리 설계
Phase A, `donchian_pure`/`scalp_1m_pure` 다음 이전 대상.

`test_scalp_1m_pure.py`와 같은 원칙으로, 손으로 `StrategySnapshot`을 조립하지
않고 **`FrgnAccumulatePureShell.on_cycle(ctx)`**(= `Strategy` Protocol 그대로)를
쓴다 — `PureStrategyShell`이 실제로 `ctx`에서 스냅샷을 만드는 전체 경로
(`requirements()` → 스냅샷 조립 → `decide()`)를 그대로 태워 legacy
`FrgnAccumulateStrategy.on_cycle(ctx)`와 나란히 비교한다. 껍질 배선 자체의
버그도 이 방식이라야 잡힌다.

세 층위:

1. 단일 사이클 동치 — 무신호(태그 미배선/무태그/시장 닫힘/평가 전/현재가 없음),
   진입(FRGN), 청산 1일차(절반).
2. 다중 사이클 동치 — 일 1회 게이트, 다음 날 재평가, 이탈 2일 연속 전량,
   중립일이 연속을 끊는 경로.
3. 다중일 시퀀스 동치 — 여러 거래일에 걸친 태그 시나리오를 legacy/pure 양쪽에
   동일하게 흘려 신호 열 전체가 일치하는지 확인한다. `run_backtest`는 쓰지
   않는다: 이 전략은 `config/settings.yaml`에서 `symbols: []`(관심종목 유니버스가
   런타임에 채워진다) + `enabled: false`이고, 판단 입력이 가격이 아니라 **태그**라
   합성 가격 시계열로는 애초에 아무 것도 검증되지 않는다(같은 사유로
   `test_scalp_1m_pure.py`도 백테스트 대신 합성 시퀀스를 쓴다).

**수급 데이터 주입 경로**도 여기서 고정한다 — 이 전략의 외국인 수급 의존은
파일 I/O가 아니라 생성자 `tags_of` 주입이다(원장 `data/ledger/frgn_flow.jsonl`은
`quant/control/frgn_flow.py`·`quant/backtest/report_replay.py`가 다루고, 거래
평면은 그 결과물인 태그만 본다 — `FrgnAccumulatePureStrategy` 클래스 docstring
"외국인 수급 데이터 의존을 어떻게 다루는가" 절). 껍질을 통과해도 그 주입이
그대로 살아 있는지(`tags_of=None`이면 침묵, 주입하면 신호)를 확인한다.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Position, Quote, SignalAction
from quant.core.ports import Context
from quant.core.strategy_api import PureStrategy
from quant.trade.strategy.frgn_accumulate import (
    FrgnAccumulatePureShell,
    FrgnAccumulatePureStrategy,
    FrgnAccumulateStrategy,
)

KST = ZoneInfo("Asia/Seoul")
DAY1 = date(2026, 1, 5)  # 월요일
DAY2 = date(2026, 1, 6)
DAY3 = date(2026, 1, 7)
KR_OPEN = dtime(9, 0)
SYMBOL = "005930"

LEGACY_ID = "frgn_accumulate"
PURE_ID = "frgn_accumulate_pure"


# ============================================================ 페이크 인프라
# test_frgn_accumulate.py와 동일 인터페이스 — 두 구현이 같은 ctx를 받아야
# 비교가 의미를 갖는다.

class FakeClock:
    def __init__(self, now: datetime, open_markets=frozenset({"KR"})):
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


def _held(symbol=SYMBOL, qty=10.0, entry=70_000.0):
    """legacy/pure 둘 다 자기 lot을 찾을 수 있도록 **두 전략 id의 lot을 모두**
    심어 둔다 — 두 구현은 `id`가 다르고(레지스트리에 별도 이름으로 등록되는
    구조), lot은 전략별로 분리 보관되기 때문이다(`Position.meta["lots"]`).
    심볼 합산 `qty`는 두 lot의 합이 아니라 "이 심볼에 열려 있는 수량"이라는
    원래 의미 그대로 두 구현에 같은 값을 보여준다."""
    lot = {"qty": qty, "avg_cost": entry}
    return Position(
        symbol=symbol, qty=qty, avg_cost=entry,
        meta={"lots": {LEGACY_ID: dict(lot), PURE_ID: dict(lot)}},
    )


def _pair(symbols, params, tags_of):
    """같은 설정의 (legacy, pure shell) 쌍."""
    legacy = FrgnAccumulateStrategy(list(symbols), dict(params), id=LEGACY_ID, tags_of=tags_of)
    pure = FrgnAccumulatePureShell(list(symbols), dict(params), id=PURE_ID, tags_of=tags_of)
    return legacy, pure


def _key(sig):
    """strategy_id만 빼고 전부 비교한다 — id는 설계상 다르다(별도 등록)."""
    return (
        sig.symbol, sig.action, sig.target_weight, sig.target_qty,
        sig.exit_fraction, sig.reason, sig.stop,
    )


def _assert_same(legacy_signals, pure_signals, note=""):
    assert [_key(s) for s in legacy_signals] == [_key(s) for s in pure_signals], note


def _run_both(legacy, pure, cycles):
    """`cycles` = [(ctx_kwargs, tags_of|None)] 를 두 구현에 동일하게 흘리고
    사이클별 신호 열을 비교한 뒤, 전체 신호 열을 돌려준다."""
    out = []
    for kwargs, tags_of in cycles:
        if tags_of is not None:
            legacy.tags_of = tags_of
            pure.inner.tags_of = tags_of
        legacy_sigs = legacy.on_cycle(_ctx(**kwargs))
        pure_sigs = pure.on_cycle(_ctx(**kwargs))
        _assert_same(legacy_sigs, pure_sigs, f"사이클 불일치: {kwargs['now']} tags={tags_of}")
        out.append((legacy_sigs, pure_sigs))
    return out


# ============================================================ 계약 준수

def test_pure_strategy_satisfies_protocol():
    pure = FrgnAccumulatePureStrategy([SYMBOL], _params(), tags_of={SYMBOL: ["FRGN"]})
    assert isinstance(pure, PureStrategy)
    needs = pure.requirements()
    assert needs.quotes == (SYMBOL,)
    assert needs.needs_positions is True
    assert needs.bars == (), "이 전략은 가격 지표를 보지 않는다 — 봉 조회 불필요"


def test_constructor_validation_is_shared_with_legacy():
    """파라미터 검증을 레거시에 위임하므로 같은 입력에 같은 ValueError가 난다."""
    for bad in (dict(buy_qty=0), dict(buy_qty=1.5), dict(exit_fraction_first=0),
                dict(exit_fraction_first=1)):
        with pytest.raises(ValueError):
            FrgnAccumulateStrategy([SYMBOL], _params(**bad))
        with pytest.raises(ValueError):
            FrgnAccumulatePureStrategy([SYMBOL], _params(**bad))


# ============================================================ 1층: 단일 사이클 동치

def test_no_tags_of_means_silence_in_both():
    """수급 태그 미배선(= 수급 데이터 주입 없음) — 둘 다 아무 것도 하지 않는다."""
    legacy, pure = _pair([SYMBOL], _params(), tags_of=None)
    ctx_kwargs = dict(quotes={SYMBOL: 70_000.0}, now=_after_eval())
    _assert_same(legacy.on_cycle(_ctx(**ctx_kwargs)), pure.on_cycle(_ctx(**ctx_kwargs)))
    assert pure.on_cycle(_ctx(**ctx_kwargs)) == []


def test_injected_tags_reach_the_pure_strategy_through_the_shell():
    """수급 데이터 주입 경로 — 생성자 `tags_of`가 껍질을 지나 순수 구현까지
    살아 있어야 신호가 난다(같은 설정에서 tags_of=None이면 침묵)."""
    silent = FrgnAccumulatePureShell([SYMBOL], _params(), id=PURE_ID, tags_of=None)
    wired = FrgnAccumulatePureShell([SYMBOL], _params(), id=PURE_ID, tags_of={SYMBOL: ["FRGN"]})
    now = _after_eval()
    assert silent.on_cycle(_ctx({SYMBOL: 70_000.0}, now)) == []
    assert len(wired.on_cycle(_ctx({SYMBOL: 70_000.0}, now))) == 1


def test_untagged_symbol_is_silent_in_both():
    legacy, pure = _pair([SYMBOL], _params(), tags_of={SYMBOL: ["TREND"]})
    ctx_kwargs = dict(quotes={SYMBOL: 70_000.0}, now=_after_eval())
    _assert_same(legacy.on_cycle(_ctx(**ctx_kwargs)), pure.on_cycle(_ctx(**ctx_kwargs)))
    assert pure.on_cycle(_ctx(**ctx_kwargs)) == []


def test_entry_signal_is_identical():
    legacy, pure = _pair([SYMBOL], _params(buy_qty=3), tags_of={SYMBOL: ["FRGN"]})
    ctx_kwargs = dict(quotes={SYMBOL: 70_000.0}, now=_after_eval())
    legacy_sigs = legacy.on_cycle(_ctx(**ctx_kwargs))
    pure_sigs = pure.on_cycle(_ctx(**ctx_kwargs))
    _assert_same(legacy_sigs, pure_sigs)
    assert len(pure_sigs) == 1
    assert pure_sigs[0].action == SignalAction.ENTER_LONG
    assert pure_sigs[0].target_qty == 3


def test_entry_while_already_holding_is_identical():
    """적립 — 보유 중이어도 태그가 살아 있으면 계속 산다(양쪽 동일)."""
    legacy, pure = _pair([SYMBOL], _params(), tags_of={SYMBOL: ["FRGN"]})
    ctx_kwargs = dict(quotes={SYMBOL: 70_000.0}, now=_after_eval(),
                      positions={SYMBOL: _held()})
    legacy_sigs = legacy.on_cycle(_ctx(**ctx_kwargs))
    pure_sigs = pure.on_cycle(_ctx(**ctx_kwargs))
    _assert_same(legacy_sigs, pure_sigs)
    assert pure_sigs[0].action == SignalAction.ENTER_LONG


def test_before_eval_time_is_silent_in_both():
    legacy, pure = _pair([SYMBOL], _params(), tags_of={SYMBOL: ["FRGN"]})
    ctx_kwargs = dict(quotes={SYMBOL: 70_000.0}, now=_before_eval(30.0))
    _assert_same(legacy.on_cycle(_ctx(**ctx_kwargs)), pure.on_cycle(_ctx(**ctx_kwargs)))
    assert pure.on_cycle(_ctx(**ctx_kwargs)) == []


def test_market_closed_is_silent_in_both():
    legacy, pure = _pair([SYMBOL], _params(), tags_of={SYMBOL: ["FRGN"]})
    ctx_kwargs = dict(quotes={SYMBOL: 70_000.0}, now=_after_eval(), open_markets=frozenset())
    _assert_same(legacy.on_cycle(_ctx(**ctx_kwargs)), pure.on_cycle(_ctx(**ctx_kwargs)))
    assert pure.on_cycle(_ctx(**ctx_kwargs)) == []


def test_missing_quote_skips_in_both():
    legacy, pure = _pair([SYMBOL], _params(), tags_of={SYMBOL: ["FRGN"]})
    ctx_kwargs = dict(quotes={}, now=_after_eval())
    _assert_same(legacy.on_cycle(_ctx(**ctx_kwargs)), pure.on_cycle(_ctx(**ctx_kwargs)))
    assert pure.on_cycle(_ctx(**ctx_kwargs)) == []


def test_first_exit_tag_sells_half_identically():
    legacy, pure = _pair([SYMBOL], _params(exit_fraction_first=0.5),
                         tags_of={SYMBOL: ["FRGN_EXIT"]})
    ctx_kwargs = dict(quotes={SYMBOL: 70_000.0}, now=_after_eval(),
                      positions={SYMBOL: _held(qty=10.0)})
    legacy_sigs = legacy.on_cycle(_ctx(**ctx_kwargs))
    pure_sigs = pure.on_cycle(_ctx(**ctx_kwargs))
    _assert_same(legacy_sigs, pure_sigs)
    assert pure_sigs[0].action == SignalAction.SCALE_OUT
    assert pure_sigs[0].exit_fraction == pytest.approx(0.5)


def test_exit_tag_without_holding_is_silent_in_both():
    legacy, pure = _pair([SYMBOL], _params(), tags_of={SYMBOL: ["FRGN_EXIT"]})
    ctx_kwargs = dict(quotes={SYMBOL: 70_000.0}, now=_after_eval())
    _assert_same(legacy.on_cycle(_ctx(**ctx_kwargs)), pure.on_cycle(_ctx(**ctx_kwargs)))
    assert pure.on_cycle(_ctx(**ctx_kwargs)) == []


# ============================================================ 2층: 다중 사이클 동치

def test_once_per_day_gate_is_identical():
    legacy, pure = _pair([SYMBOL], _params(), tags_of={SYMBOL: ["FRGN"]})
    now = _after_eval()
    runs = _run_both(legacy, pure, [
        (dict(quotes={SYMBOL: 70_000.0}, now=now), None),
        (dict(quotes={SYMBOL: 70_000.0}, now=now + timedelta(minutes=5)), None),
        (dict(quotes={SYMBOL: 70_000.0}, now=now + timedelta(minutes=60)), None),
    ])
    assert len(runs[0][1]) == 1
    assert runs[1][1] == [] and runs[2][1] == [], "같은 날 두 번째 평가는 없다"


def test_next_day_reevaluates_identically():
    legacy, pure = _pair([SYMBOL], _params(), tags_of={SYMBOL: ["FRGN"]})
    runs = _run_both(legacy, pure, [
        (dict(quotes={SYMBOL: 70_000.0}, now=_after_eval(day=DAY1)), None),
        (dict(quotes={SYMBOL: 70_000.0}, now=_after_eval(day=DAY2)), None),
    ])
    assert len(runs[0][1]) == 1 and len(runs[1][1]) == 1


def test_two_consecutive_exit_days_liquidate_identically():
    legacy, pure = _pair([SYMBOL], _params(exit_fraction_first=0.5),
                         tags_of={SYMBOL: ["FRGN_EXIT"]})
    pos = {SYMBOL: _held(qty=10.0)}
    runs = _run_both(legacy, pure, [
        (dict(quotes={SYMBOL: 70_000.0}, now=_after_eval(day=DAY1), positions=pos), None),
        (dict(quotes={SYMBOL: 70_000.0}, now=_after_eval(day=DAY2), positions=pos), None),
    ])
    assert runs[0][1][0].action == SignalAction.SCALE_OUT
    assert runs[1][1][0].action == SignalAction.EXIT_LONG
    assert runs[1][1][0].exit_fraction == 1.0


def test_neutral_day_breaks_the_streak_identically():
    """이탈 → 중립(잔여 관망) → 이탈: 연속이 끊겨 다시 절반부터. 두 구현 동일."""
    legacy, pure = _pair([SYMBOL], _params(exit_fraction_first=0.5),
                         tags_of={SYMBOL: ["FRGN_EXIT"]})
    pos = {SYMBOL: _held(qty=10.0)}
    runs = _run_both(legacy, pure, [
        (dict(quotes={SYMBOL: 70_000.0}, now=_after_eval(day=DAY1), positions=pos), None),
        (dict(quotes={SYMBOL: 70_000.0}, now=_after_eval(day=DAY2), positions=pos),
         {SYMBOL: []}),
        (dict(quotes={SYMBOL: 70_000.0}, now=_after_eval(day=DAY3), positions=pos),
         {SYMBOL: ["FRGN_EXIT"]}),
    ])
    assert runs[0][1][0].exit_fraction == pytest.approx(0.5)
    assert runs[1][1] == [], "중립일에는 보유 유지"
    assert runs[2][1][0].exit_fraction == pytest.approx(0.5), "연속이 끊겼으니 다시 1일차"


# ============================================================ state 왕복 / 순수성

def test_state_round_trip_carries_the_daily_gate_and_streak():
    """`decide()`가 돌려준 `next_state`를 그대로 다시 넣으면 게이트가 이어진다 —
    껍질 없이 계약만으로도 상태가 온전히 왕복한다는 증명."""
    pure = FrgnAccumulatePureStrategy([SYMBOL], _params(exit_fraction_first=0.5),
                                      id=PURE_ID, tags_of={SYMBOL: ["FRGN_EXIT"]})
    shell = FrgnAccumulatePureShell([SYMBOL], _params(exit_fraction_first=0.5),
                                    id=PURE_ID, tags_of={SYMBOL: ["FRGN_EXIT"]})
    pos = {SYMBOL: _held(qty=10.0)}

    # 껍질을 통해 스냅샷을 만들고, 그 스냅샷으로 직접 decide()를 부른다.
    snap1 = shell._snapshot(_ctx({SYMBOL: 70_000.0}, _after_eval(day=DAY1), positions=pos))
    d1 = pure.decide(snap1, {})
    assert d1.signals[0].action == SignalAction.SCALE_OUT
    assert d1.next_state["evaluated_date"]["KR"] == DAY1
    assert d1.next_state["exit_streak"][SYMBOL] == 1

    # 같은 날 재호출 — 게이트가 살아 있어 무신호.
    assert pure.decide(snap1, d1.next_state).signals == ()

    snap2 = shell._snapshot(_ctx({SYMBOL: 70_000.0}, _after_eval(day=DAY2), positions=pos))
    d2 = pure.decide(snap2, d1.next_state)
    assert d2.signals[0].action == SignalAction.EXIT_LONG
    assert d2.next_state["exit_streak"][SYMBOL] == 0


def test_decide_does_not_mutate_the_state_it_was_given():
    """관측이 상태를 바꾸지 않는다 — 같은 (snap, state)로 여러 번 불러도 결과가
    같다. 레거시 `_check_exit_for`는 인스턴스 `_exit_streak`를 그 자리에서 올려
    같은 사이클 재호출이 스트릭을 진행시켰다(클래스 docstring "구조적으로
    없어지는 버그")."""
    pure = FrgnAccumulatePureStrategy([SYMBOL], _params(), id=PURE_ID,
                                      tags_of={SYMBOL: ["FRGN_EXIT"]})
    shell = FrgnAccumulatePureShell([SYMBOL], _params(), id=PURE_ID,
                                    tags_of={SYMBOL: ["FRGN_EXIT"]})
    snap = shell._snapshot(_ctx({SYMBOL: 70_000.0}, _after_eval(day=DAY1),
                                positions={SYMBOL: _held(qty=10.0)}))
    state = {}
    first = pure.decide(snap, state)
    second = pure.decide(snap, state)
    assert state == {}, "인자로 받은 state를 in-place로 고치지 않는다"
    assert [_key(s) for s in first.signals] == [_key(s) for s in second.signals]

    # 대조군: 레거시는 같은 사이클을 두 번 흘리면 하루 만에 전량청산까지 간다.
    legacy = FrgnAccumulateStrategy([SYMBOL], _params(), id=LEGACY_ID,
                                    tags_of={SYMBOL: ["FRGN_EXIT"]})
    ctx_kwargs = dict(quotes={SYMBOL: 70_000.0}, now=_after_eval(day=DAY1),
                      positions={SYMBOL: _held(qty=10.0)})
    assert legacy.on_cycle(_ctx(**ctx_kwargs))[0].action == SignalAction.SCALE_OUT
    legacy._evaluated_date.clear()  # 일 1회 게이트만 풀어 같은 하루를 재평가
    assert legacy.on_cycle(_ctx(**ctx_kwargs))[0].action == SignalAction.EXIT_LONG


# ============================================================ 3층: 다중일 시퀀스 동치

def test_multi_day_multi_symbol_sequence_is_identical():
    """여러 심볼·여러 거래일에 걸친 태그 시나리오 전체에서 신호 열이 일치한다.
    (적립 매수 → 이탈 1일차 → 중립 → 이탈 재시작 → 이탈 2일 연속 → 매수 재개)"""
    symbols = ["005930", "000660", "AAPL"]  # KR 2 + US 1 (시장 분리 경로도 태운다)
    params = _params(buy_qty=2, exit_fraction_first=0.4)
    legacy, pure = _pair(symbols, params, tags_of={})

    pos = {
        "005930": _held("005930", qty=10.0, entry=70_000.0),
        "000660": _held("000660", qty=5.0, entry=120_000.0),
    }
    days = [DAY1, DAY2, DAY3, date(2026, 1, 8), date(2026, 1, 9)]
    tag_plan = [
        {"005930": ["FRGN"], "000660": ["FRGN"], "AAPL": ["FRGN"]},
        {"005930": ["FRGN_EXIT"], "000660": ["FRGN"]},
        {"005930": [], "000660": ["FRGN_EXIT"]},
        {"005930": ["FRGN_EXIT"], "000660": ["FRGN_EXIT"]},
        {"005930": ["FRGN"], "000660": ["FRGN_EXIT"]},
    ]
    cycles = []
    for day, tags in zip(days, tag_plan):
        for minutes in (10.0, 90.0, 120.0):  # 평가 전 / 평가 / 같은 날 재호출
            cycles.append((
                dict(quotes={"005930": 70_000.0, "000660": 120_000.0, "AAPL": 200.0},
                     now=_after_eval(minutes, day=day), positions=pos),
                tags if minutes == 10.0 else None,
            ))

    runs = _run_both(legacy, pure, cycles)
    total_legacy = sum(len(l) for l, _ in runs)
    total_pure = sum(len(p) for _, p in runs)
    assert total_legacy == total_pure
    assert total_pure > 0, "시나리오가 실제로 신호를 냈어야 비교가 의미 있다"

    # 시장이 KR만 열려 있으므로 US 심볼(AAPL)은 양쪽 다 평가되지 않는다.
    assert all(s.symbol != "AAPL" for l, _ in runs for s in l)
