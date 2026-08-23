"""오버나이트 금지 불변식 — 이 스위트가 생긴 직접적 계기(회귀 방지).

원래 Clock.should_flatten이 `minutes_to_close <= flatten_minutes`로 판정해서,
15분봉 리플레이의 마지막 판단 시점(15:45, 잔여 15분)에서는 조건이 절대 성립하지
않았다. 그 결과 오버나이트 금지가 백테스트에서는 무력화됐지만(판단 주기가 굵어서
창을 건너뜀), 10초마다 도는 라이브에서는 정상 동작해서 백테스트와 라이브가 다르게
행동했다. tests/test_clock.py가 Clock 단위로 이걸 이미 잡지만, 여기서는 실제
DonchianStrategy + run_cycle + PaperBroker + Portfolio를 다회 리플레이로 돌려서
"세션 마감 시점에 실제로 열린 포지션이 없다"를 시스템 레벨에서 확인한다 —
Clock의 산식이 맞아도 전략/브로커 배선이 어긋나면 여전히 오버나이트가 샐 수 있다.
"""
from __future__ import annotations

import pandas as pd
import pytest

from quant.trade.loop import run_cycle
from quant.core.clock import SimClock
from quant.adapters.data.resample import resample_1m
from quant.adapters.data.stub import StubDataFeed
from quant.core.ports import Context
from quant.adapters.execution.paper import PaperBroker
from quant.adapters.persistence.sink import MultiSink
from quant.core.portfolio.portfolio import Portfolio
from quant.trade.risk.manager import RiskManagerImpl
from quant.trade.strategy.donchian import DonchianStrategy

_BARS_PER_SESSION_1M = 390
_SYMBOL = "TQQQ"


def _replay_violations(donchian_params, interval_minutes: int, days: int, tmp_path) -> list:
    """run_backtest(quant/backtest/engine.py)와 동일한 조립을 재현하되,
    portfolio.positions를 직접 들여다볼 수 있도록 인라인으로 리플레이한다.
    각 거래일의 마지막 판단 시점(=세션 마감) 직후 열린 포지션이 있으면 위반으로 기록한다."""
    market_of = {_SYMBOL: "US"}
    data = StubDataFeed([_SYMBOL], days=days + 10)
    master_1m = data.bars_1m[_SYMBOL]
    bar_closes = resample_1m(master_1m, interval_minutes).index + pd.Timedelta(minutes=interval_minutes)
    bars_per_day = max(_BARS_PER_SESSION_1M // interval_minutes, 1)
    replay_closes = bar_closes[-(days * bars_per_day):]

    clock = SimClock(now=replay_closes[0], cadence_minutes=interval_minutes)
    portfolio = Portfolio(cash=5_000_000.0, state_path=tmp_path / "portfolio.json")
    broker = PaperBroker(data=data, portfolio=portfolio, fee_bps=7, market_of=market_of)
    risk = RiskManagerImpl({}, capital_fraction={"donchian": 1.0}, market_of=market_of)
    strategy = DonchianStrategy(
        symbols=[_SYMBOL], params=donchian_params(interval_minutes=interval_minutes), market="US",
    )
    ctx = Context(clock=clock, data=data, broker=broker)
    sinks = MultiSink([])

    ny_dates = replay_closes.tz_convert("America/New_York").normalize()
    violations = []
    n = len(replay_closes)
    for i, ts in enumerate(replay_closes):
        clock.set(ts)
        data.set_now(ts)
        run_cycle([strategy], ctx, risk, sinks, notifier=None)

        is_last_bar_of_day = (i == n - 1) or (ny_dates[i + 1] != ny_dates[i])
        if is_last_bar_of_day:
            open_positions = [s for s, p in portfolio.positions.items() if p.is_open]
            if open_positions:
                violations.append((ts, open_positions))
    return violations


@pytest.mark.parametrize("interval_minutes", [1, 5, 15])
def test_no_overnight_position_at_any_cadence(donchian_params, interval_minutes, tmp_path):
    violations = _replay_violations(donchian_params, interval_minutes, days=2, tmp_path=tmp_path)
    assert violations == [], (
        f"cadence={interval_minutes}분에서 세션 마감 시점에 열린 포지션이 남아있음: {violations}"
    )


def test_no_overnight_position_holds_across_multiple_days(donchian_params, tmp_path):
    violations = _replay_violations(donchian_params, interval_minutes=15, days=5, tmp_path=tmp_path)
    assert violations == [], f"다일 리플레이에서 오버나이트 포지션 발생: {violations}"
