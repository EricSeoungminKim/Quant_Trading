"""`PureStrategyShell`의 거부 사유(last_reject) 로그 노출 — "필터의 정당한 침묵"과
"데이터 부족으로 전부 거부(고장)"를 운영자가 로그에서 구분할 수 있어야 한다.

`next_state["last_reject"]`는 순수 전략(`PureStrategy.decide`)이 매 사이클 반환하는
진단용 dict이지만, 원래는 `PureStrategyShell._state`에만 보관되고 어떤 로그에도
나오지 않았다. 이 테스트는 껍질이 사유가 바뀐 심볼만 INFO로 로그하고(스팸 방지),
시간당 요약을 남기며, `last_reject`를 안 쓰는 전략은 건드리지 않는지 확인한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Position, Quote
from quant.core.ports import Context
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.strategy.shell import PureStrategyShell

NY = ZoneInfo("America/New_York")


class FakeClock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    def is_market_open(self, market):
        return True

    def minutes_to_close(self, market):
        return 300.0

    def cadence_minutes(self):
        return 1.0


class FakeDataFeed:
    def quote(self, symbol):
        return None

    def history(self, symbol, interval, n):
        raise NotImplementedError


class FakeBroker:
    def positions(self):
        return {}

    def cash(self):
        return 0.0

    def place_order(self, order):
        raise NotImplementedError


def _ctx(now):
    return Context(clock=FakeClock(now), data=FakeDataFeed(), broker=FakeBroker())


class FakeStrategy:
    """`PureStrategy` Protocol을 만족하는 최소 더미. 매 사이클 미리 정해둔
    next_state를 그대로 반환한다 — 셸의 로그 배선만 검증하면 되므로 실제 판단
    로직은 필요 없다."""

    def __init__(self, id="fake", next_states=None):
        self.id = id
        self.symbols = ["AAA"]
        self._next_states = list(next_states or [])

    def requirements(self):
        return DataNeeds()

    def decide(self, snap, state):
        next_state = self._next_states.pop(0) if self._next_states else {}
        return Decision(signals=(), next_state=next_state)


def _now(minutes=0):
    return datetime(2026, 1, 5, 9, 30, tzinfo=NY) + timedelta(minutes=minutes)


def test_new_reject_reason_logs_once(caplog):
    strategy = FakeStrategy(next_states=[{"last_reject": {"AAA": "봉 없음"}}])
    shell = PureStrategyShell(strategy)

    with caplog.at_level("INFO"):
        shell.on_cycle(_ctx(_now()))

    messages = [r.message for r in caplog.records]
    assert any("AAA" in m and "봉 없음" in m and "진입 거부" in m for m in messages)
    assert sum(1 for m in messages if "진입 거부" in m) == 1


def test_same_reject_reason_repeated_does_not_log_again(caplog):
    strategy = FakeStrategy(next_states=[
        {"last_reject": {"AAA": "봉 없음"}},
        {"last_reject": {"AAA": "봉 없음"}},
        {"last_reject": {"AAA": "봉 없음"}},
    ])
    shell = PureStrategyShell(strategy)

    with caplog.at_level("INFO"):
        shell.on_cycle(_ctx(_now(0)))
        shell.on_cycle(_ctx(_now(1)))
        shell.on_cycle(_ctx(_now(2)))

    reject_logs = [r.message for r in caplog.records if "진입 거부" in r.message]
    assert len(reject_logs) == 1


def test_reject_reason_change_logs_again(caplog):
    strategy = FakeStrategy(next_states=[
        {"last_reject": {"AAA": "봉 없음"}},
        {"last_reject": {"AAA": "현재가 없음"}},
    ])
    shell = PureStrategyShell(strategy)

    with caplog.at_level("INFO"):
        shell.on_cycle(_ctx(_now(0)))
        shell.on_cycle(_ctx(_now(1)))

    reject_logs = [r.message for r in caplog.records if "진입 거부" in r.message]
    assert len(reject_logs) == 2
    assert "봉 없음" in reject_logs[0]
    assert "현재가 없음" in reject_logs[1]


def test_strategy_without_last_reject_is_noop(caplog):
    strategy = FakeStrategy(next_states=[{}, {"pending": {}}])
    shell = PureStrategyShell(strategy)

    with caplog.at_level("INFO"):
        shell.on_cycle(_ctx(_now(0)))
        shell.on_cycle(_ctx(_now(1)))

    assert not any("진입 거부" in r.message or "거부 요약" in r.message for r in caplog.records)


def test_hourly_summary_logs_top_reasons_and_resets(caplog):
    # 처음 3사이클(0/20/40분, 요약 윈도 시작 대비 40분 경과)은 아직 1시간 미만이라
    # 요약이 안 나온다. 4번째 사이클(70분, 3600초 초과)에서 사유가 바뀌며 요약이
    # 발화하고, "봉 없음"이 이 시점까지 3건으로 가장 많다.
    states = [
        {"last_reject": {"AAA": "봉 없음"}},
        {"last_reject": {"AAA": "봉 없음"}},
        {"last_reject": {"AAA": "봉 없음"}},
        {"last_reject": {"AAA": "현재가 없음"}},
    ]
    strategy = FakeStrategy(next_states=states)
    shell = PureStrategyShell(strategy)

    with caplog.at_level("INFO"):
        shell.on_cycle(_ctx(_now(0)))    # 요약 윈도 시작
        shell.on_cycle(_ctx(_now(20)))
        shell.on_cycle(_ctx(_now(40)))   # 40분 경과 — 아직 1시간 미만
        shell.on_cycle(_ctx(_now(70)))   # 70분 경과 — 요약 발화

    summary_logs = [r.message for r in caplog.records if "거부 요약" in r.message]
    assert len(summary_logs) == 1
    assert "봉 없음=3" in summary_logs[0]


def test_hourly_summary_does_not_log_when_no_rejects(caplog):
    strategy = FakeStrategy(next_states=[{"last_reject": {}}, {"last_reject": {}}])
    shell = PureStrategyShell(strategy)

    with caplog.at_level("INFO"):
        shell.on_cycle(_ctx(_now(0)))
        shell.on_cycle(_ctx(_now(120)))

    assert not any("거부 요약" in r.message for r in caplog.records)


# ---------------------------------------------------------------- 폐장 시장 조회 차단
# 2026-09-02: 껍질이 `market_open`을 fetch 뒤에 채우는 바람에 닫힌 시장 심볼의
# history/quote 를 매 사이클 전부 당겼다. 순수 전략은 닫히면 아무것도 하지 않으므로
# 전부 낭비였고, 그 낭비가 `cold_fetch_budget_per_cycle`을 먼저 소진해 **열린
# 시장에서 포지션을 든 전략의 on_cycle 이 통째로 스킵**되게 만들었다(손절 미판정).


class SplitMarketClock:
    """시장별로 개장 여부가 다른 시계 — KR 개장 / US 폐장(KST 장중)."""

    def __init__(self, now, open_markets):
        self._now = now
        self._open = set(open_markets)

    def now(self):
        return self._now

    def is_market_open(self, market):
        return market in self._open

    def minutes_to_close(self, market):
        return 300.0 if market in self._open else None

    def cadence_minutes(self):
        return 1.0


class CountingDataFeed:
    """호출된 심볼을 그대로 기록하는 가짜 피드."""

    def __init__(self):
        self.history_calls: list[tuple[str, str]] = []
        self.quote_calls: list[str] = []

    def quote(self, symbol):
        self.quote_calls.append(symbol)
        return Quote(symbol, _now(), 100.0)

    def history(self, symbol, interval, n):
        self.history_calls.append((symbol, interval))
        return pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
            index=pd.DatetimeIndex([_now()]),
        )


class SnapshotCapturingStrategy:
    """받은 스냅샷을 그대로 붙잡아 두는 더미 — 껍질이 무엇을 채웠는지 본다."""

    def __init__(self, symbols, needs):
        self.id = "capture"
        self.symbols = list(symbols)
        self._needs = needs
        self.last_snapshot = None

    def requirements(self):
        return self._needs

    def decide(self, snap, state):
        self.last_snapshot = snap
        return Decision(signals=(), next_state={})


def test_closed_market_symbols_are_not_fetched():
    """US 폐장 + KR 개장 시각: US 심볼은 history/quote 를 아예 부르지 않고,
    KR 심볼은 그대로 부른다."""
    symbols = ["TQQQ", "AAPL", "005930", "000660"]
    needs = DataNeeds(
        bars=tuple((s, "5m", 60) for s in symbols),
        quotes=tuple(symbols),
        needs_positions=False,
    )
    strategy = SnapshotCapturingStrategy(symbols, needs)
    shell = PureStrategyShell(strategy)
    data = CountingDataFeed()
    ctx = Context(
        clock=SplitMarketClock(_now(), {"KR"}), data=data, broker=FakeBroker()
    )

    shell.on_cycle(ctx)

    assert [s for s, _ in data.history_calls] == ["005930", "000660"]
    assert data.quote_calls == ["005930", "000660"]
    # 스냅샷도 열린 시장 것만 담는다 — 전략은 .get()으로 읽으므로 결측이 안전하다.
    assert set(strategy.last_snapshot.quotes) == {"005930", "000660"}
    assert ("TQQQ", "5m") not in strategy.last_snapshot.bars
    assert strategy.last_snapshot.market_open == {"KR": True, "US": False}


def test_open_market_symbols_are_still_fetched_when_all_open():
    """두 시장이 모두 열려 있으면 예전과 똑같이 전부 조회한다(회귀 방지)."""
    symbols = ["TQQQ", "005930"]
    needs = DataNeeds(
        bars=tuple((s, "5m", 60) for s in symbols),
        quotes=tuple(symbols),
        needs_positions=False,
    )
    shell = PureStrategyShell(SnapshotCapturingStrategy(symbols, needs))
    data = CountingDataFeed()
    ctx = Context(
        clock=SplitMarketClock(_now(), {"KR", "US"}), data=data, broker=FakeBroker()
    )

    shell.on_cycle(ctx)

    assert sorted(s for s, _ in data.history_calls) == ["005930", "TQQQ"]
    assert sorted(data.quote_calls) == ["005930", "TQQQ"]


def test_held_position_in_open_market_still_gets_its_quote():
    """개장 시장에서 포지션을 들고 있으면 현재가가 반드시 들어온다 —
    손절/목표가 판정의 입력이므로 이게 빠지면 그 사이클 방어선이 사라진다."""
    symbols = ["TQQQ", "005930"]
    needs = DataNeeds(bars=(), quotes=tuple(symbols), needs_positions=True)
    strategy = SnapshotCapturingStrategy(symbols, needs)
    shell = PureStrategyShell(strategy)

    held = Position(symbol="005930", qty=6, avg_cost=70000.0)
    held.meta["lots"] = {"capture": {"qty": 6, "entry": 70000.0, "stop": 68000.0}}

    class HoldingBroker(FakeBroker):
        def positions(self):
            return {"005930": held}

    data = CountingDataFeed()
    ctx = Context(
        clock=SplitMarketClock(_now(), {"KR"}), data=data, broker=HoldingBroker()
    )

    shell.on_cycle(ctx)

    snap = strategy.last_snapshot
    assert "005930" in snap.quotes           # 방어선 판정 입력이 살아 있다
    assert snap.lots["005930"]["stop"] == 68000.0
    assert "TQQQ" not in snap.quotes         # 폐장 US 는 여전히 안 부른다


def test_unknown_market_symbol_is_fetched_conservatively():
    """`requirements()`가 이 전략 `symbols` 밖 시장의 심볼을 요구하면 개장 여부를
    알 수 없다 — 그때는 조회한다(데이터가 빠지는 쪽보다 안전)."""
    needs = DataNeeds(bars=(("AAPL", "5m", 60),), quotes=("AAPL",))
    shell = PureStrategyShell(SnapshotCapturingStrategy(["005930"], needs))
    data = CountingDataFeed()
    ctx = Context(
        clock=SplitMarketClock(_now(), {"KR"}), data=data, broker=FakeBroker()
    )

    shell.on_cycle(ctx)

    assert data.history_calls == [("AAPL", "5m")]
    assert data.quote_calls == ["AAPL"]


def test_fetch_when_closed_opts_out_of_the_gate():
    """`fetch_when_closed=True`(프리마켓을 의도적으로 거래하는 scalp_1m)는
    폐장이어도 예전처럼 전부 조회한다 — 게이트가 그 전략을 굶기면 안 된다."""
    symbols = ["TQQQ", "005930"]
    needs = DataNeeds(
        bars=tuple((s, "1m", 60) for s in symbols),
        quotes=tuple(symbols),
        needs_positions=True,
        fetch_when_closed=True,
    )
    shell = PureStrategyShell(SnapshotCapturingStrategy(symbols, needs))
    data = CountingDataFeed()
    ctx = Context(
        clock=SplitMarketClock(_now(), {"KR"}), data=data, broker=FakeBroker()
    )

    shell.on_cycle(ctx)

    assert sorted(s for s, _ in data.history_calls) == ["005930", "TQQQ"]
    assert sorted(data.quote_calls) == ["005930", "TQQQ"]


def test_closed_market_held_symbol_keeps_its_quote():
    """폐장 시장이라도 **보유 중인 심볼**의 현재가는 남긴다 — 보유 관리를
    market_open 으로 감싸지 않는 전략(donchian)의 방어선 판정이 조용히 멈추면
    안 된다. 봉은 그래도 안 부른다(방어선 판정은 현재가만 쓴다)."""
    symbols = ["TQQQ", "SQQQ"]
    needs = DataNeeds(
        bars=tuple((s, "15m", 60) for s in symbols),
        quotes=tuple(symbols),
        needs_positions=True,
    )
    strategy = SnapshotCapturingStrategy(symbols, needs)
    shell = PureStrategyShell(strategy)

    held = Position(symbol="TQQQ", qty=2, avg_cost=90.0)
    held.meta["lots"] = {"capture": {"qty": 2, "stop": 85.0}}

    class HoldingBroker(FakeBroker):
        def positions(self):
            return {"TQQQ": held}

    data = CountingDataFeed()
    ctx = Context(
        clock=SplitMarketClock(_now(), {"KR"}), data=data, broker=HoldingBroker()
    )

    shell.on_cycle(ctx)

    assert data.quote_calls == ["TQQQ"]      # 보유분만 — SQQQ 는 안 부른다
    assert data.history_calls == []          # 폐장 봉은 어느 쪽도 안 부른다
    assert strategy.last_snapshot.quotes["TQQQ"].price == 100.0
