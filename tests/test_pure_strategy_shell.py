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

import pytest

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
