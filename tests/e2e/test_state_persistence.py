"""재시작 후 상태 영속화 — Portfolio.save()/load_or_init으로 프로세스가 완전히
새로 시작해도 열린 포지션과 그 stop/target 메타데이터가 살아남고, 재구성된
전략/브로커가 그 메타데이터로 여전히 올바르게 관리(손절)하는지 본다.

DonchianStrategy._ensure_state에는 "재시작 등으로 pending 정보가 없음" 복구 분기가
이미 있다 — 이건 결함이 아니라 설계된 안전망이다. 그런데 그 복구는 avg_cost 기반
근사치(stop_fallback_pct%만 적용)라서, 원래 진입 시 계산됐던(봉 저가 기반) stop과
값이 다를 수 있다. 즉 "메타데이터가 디스크에 실제로 반영된 뒤"의 재시작과
"아직 반영되기 전"의 재시작은 결과가 다르다 — 이 파일의 두 번째 테스트가 그 차이를
정확히 짚는다(실제 발견한 결함, REPORT 대상).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.trade.loop import run_cycle
from quant.core.fx import FixedFxProvider
from quant.core.ports import Context
from quant.core.models import Quote
from quant.adapters.execution.paper import PaperBroker
from quant.adapters.persistence.sink import MultiSink
from quant.core.portfolio.portfolio import Portfolio
from quant.trade.risk.manager import RiskManagerImpl
from quant.trade.strategy.donchian import DonchianStrategy

NY = ZoneInfo("America/New_York")
_SYMBOL = "TQQQ"
_MARKET_OF = {_SYMBOL: "US"}
_RISK_CFG = {"risk": {"max_position_pct": 50, "max_symbol_pct_total": 0, "daily_loss_limit_pct": 3}}


class _CapturingSink:
    def __init__(self):
        self.fills = []

    def on_signal(self, signal) -> None:
        return None

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


class _ScriptedFeed:
    """quote()는 스크립트된 현재가를, history()는 고정 봉을 반환하는 최소 DataFeed —
    tests/e2e/test_full_cycle_integration.py의 _ScriptedFeed와 동일한 계약(중복 정의:
    기존 e2e 파일은 수정하지 않는 범위 규칙이라 로컬로 복제)."""

    def __init__(self, symbol: str, bars: pd.DataFrame, price: float):
        self.symbol = symbol
        self._bars = bars
        self.price = price

    def quote(self, symbol: str) -> Quote | None:
        if symbol != self.symbol:
            return None
        return Quote(symbol=symbol, ts=self._bars.index[-1], price=self.price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        if symbol != self.symbol:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return self._bars.tail(n)


class _AlwaysOpenClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return 15.0

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


def _build(bars: pd.DataFrame, entry_price: float, portfolio: Portfolio, donchian_params, fx: FixedFxProvider):
    data = _ScriptedFeed(_SYMBOL, bars, entry_price)
    broker = PaperBroker(data=data, portfolio=portfolio, fee_bps=7, market_of=_MARKET_OF, fx=fx)
    risk = RiskManagerImpl(_RISK_CFG, capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF, fx=fx)
    strategy = DonchianStrategy(symbols=[_SYMBOL], params=donchian_params(), market="US", id="donchian")
    clock = _AlwaysOpenClock(datetime(2026, 1, 5, 20, 0, tzinfo=bars.index[-1].tzinfo))
    ctx = Context(clock=clock, data=data, broker=broker)
    return strategy, ctx, risk, data


def test_open_position_survives_restart_with_exact_stop_target_and_management_still_works(
    make_breakout_bars, donchian_params, tmp_path,
):
    bars = make_breakout_bars(41, breakout=True)
    entry_price = float(bars.iloc[-1]["close"])
    fx = FixedFxProvider(1000.0)
    state_path = tmp_path / "portfolio.json"
    portfolio = Portfolio(cash=10_000_000.0, state_path=state_path)
    strategy, ctx, risk, data = _build(bars, entry_price, portfolio, donchian_params, fx)
    capture = _CapturingSink()

    # 1) 진입 체결
    run_cycle([strategy], ctx, risk, MultiSink([capture]))
    assert len(capture.fills) == 1
    assert portfolio.positions[_SYMBOL].is_open

    # 2) 다음 사이클 — 전략이 pending 진입 상태(entry/stop/target)를 포지션 meta로
    #    옮긴다(메모리 상). 가격은 그대로라 stop/target 어느 쪽도 건드리지 않아 새
    #    체결은 없다.
    run_cycle([strategy], ctx, risk, MultiSink([capture]))
    assert len(capture.fills) == 1  # 추가 체결 없음
    live_meta = dict(portfolio.positions[_SYMBOL].meta)
    # 2026-08-11 랏(lot) 도입 — donchian도 meta["lots"]["donchian"]에 상태를 저장한다.
    live_lot = live_meta["lots"]["donchian"]
    assert live_lot.get("stop") is not None and live_lot.get("target") is not None
    original_stop = live_lot["stop"]

    # 3) "재시작" 직전 상태를 디스크에 반영 — Portfolio의 공개 API인 save()를 그대로 쓴다.
    portfolio.save()

    # 4) 재시작: 완전히 새로운 Portfolio/Strategy/Broker/RiskManager 인스턴스로 같은
    #    파일에서 복구한다(메모리 상태는 전혀 공유하지 않는다).
    restarted_portfolio = Portfolio.load_or_init(start_cash=10_000_000.0, state_path=state_path)
    restored_pos = restarted_portfolio.positions[_SYMBOL]
    assert restored_pos.qty == pytest.approx(portfolio.positions[_SYMBOL].qty)
    assert restored_pos.avg_cost == pytest.approx(portfolio.positions[_SYMBOL].avg_cost)
    restored_lot = restored_pos.meta["lots"]["donchian"]
    assert restored_lot["stop"] == pytest.approx(live_lot["stop"])
    assert restored_lot["target"] == pytest.approx(live_lot["target"])

    # 5) 재시작 후 관리(손절)가 "복구 근사치"가 아니라 원래 저장됐던 정확한 stop
    #    기준으로 동작하는지 확인한다. 봉 저가 기반 원본 stop과 avg_cost 기반 복구
    #    stop은 서로 다른 값이므로(donchian.py 참고), 그 사이의 가격을 스크립트해서
    #    "정확히 어느 stop이 쓰였는지"를 구분한다.
    fresh_strategy, fresh_ctx, fresh_risk, fresh_data = _build(
        bars, entry_price, restarted_portfolio, donchian_params, fx,
    )
    fresh_capture = _CapturingSink()
    fresh_data.price = original_stop - 0.2  # 원본 stop보다는 낮지만, 복구 근사 stop보다는 높다

    run_cycle([fresh_strategy], fresh_ctx, fresh_risk, MultiSink([fresh_capture]))

    assert len(fresh_capture.fills) == 1
    assert not restarted_portfolio.positions[_SYMBOL].is_open
    assert fresh_capture.fills[0].price == pytest.approx(original_stop - 0.2)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "실제 프로덕션 결함(REPORT 대상, 수정하지 않음): Position.meta(stop/target)는 "
        "PaperBroker.place_order 안에서 체결이 발생할 때만 portfolio.save()로 디스크에 "
        "반영된다(quant/adapters/execution/paper.py:69). 반면 DonchianStrategy._ensure_state는 "
        "체결 '다음' 사이클에서야 pending 상태를 Position.meta로 옮긴다(메모리 상만) — "
        "그 사이 새 체결이 없으면 meta는 여전히 디스크에 반영되지 않는다. 따라서 진입 "
        "체결 직후 ~ 다음 체결 이벤트 사이에 프로세스가 재시작되면, 디스크의 meta는 "
        "빈 dict({})이고 재시작된 전략은 avg_cost 기반 근사 stop(더 헐거움)으로 대체 "
        "복구한다(quant/trade/strategy/donchian.py의 '재시작 등으로 pending 정보가 "
        "없음' 분기). 그 결과 원래 계산됐던(봉 저가 기반) 더 타이트한 stop 밑으로 가격이 "
        "떨어져도 청산되지 않는다 — 재시작 타이밍에 따라 실제 감내 손실폭이 달라진다."
    ),
)
def test_restart_immediately_after_entry_loses_exact_stop_before_next_save(
    make_breakout_bars, donchian_params, tmp_path,
):
    bars = make_breakout_bars(41, breakout=True)
    entry_price = float(bars.iloc[-1]["close"])
    fx = FixedFxProvider(1000.0)
    state_path = tmp_path / "portfolio.json"
    portfolio = Portfolio(cash=10_000_000.0, state_path=state_path)
    strategy, ctx, risk, data = _build(bars, entry_price, portfolio, donchian_params, fx)
    capture = _CapturingSink()

    run_cycle([strategy], ctx, risk, MultiSink([capture]))  # 진입 체결 -> 이 시점 저장된 meta는 {}
    assert len(capture.fills) == 1

    # "재시작": 다음 체결(및 그로 인한 save)이 있기 전에 프로세스가 죽었다고 가정한다.
    restarted_portfolio = Portfolio.load_or_init(start_cash=10_000_000.0, state_path=state_path)
    fresh_strategy, fresh_ctx, fresh_risk, fresh_data = _build(
        bars, entry_price, restarted_portfolio, donchian_params, fx,
    )
    fresh_capture = _CapturingSink()

    # 원본(봉 저가 기반) stop 바로 아래로 가격을 스크립트 — "원래 저장됐어야 할" stop
    # 기준이면 여기서 손절 체결이 나야 한다.
    original_stop = max(
        float(bars.iloc[-1]["low"]),
        entry_price * (1 - donchian_params()["stop_fallback_pct"] / 100),
    )
    fresh_data.price = original_stop - 0.05

    run_cycle([fresh_strategy], fresh_ctx, fresh_risk, MultiSink([fresh_capture]))

    assert len(fresh_capture.fills) == 1
