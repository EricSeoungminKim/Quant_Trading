"""데이터 소스 열화(degraded) 가시성 — 1차 소스가 죽어도 트레이딩 사이클은 계속
돌아가되, "누가 실제로 응답했는지"가 절대 조용히 묻히면 안 된다는 불변식.

WHY THIS EXISTS의 결함(a)와 직접 연관된다: 원래 시스템은 자격증명/소스가 실패하면
아무 신호 없이 합성 stub이나 기본값으로 조용히 대체됐다. tests/test_service.py는
MarketDataService 하나만 떼어놓고 폴백/health/provenance를 이미 촘촘히 검증하지만,
여기서는 그게 실제 전략이 데이터를 요청하고 주문이 체결되는 run_cycle 경로 전체를
관통해도 여전히 새어나가지 않는지 — 즉 "단위로는 맞는데 조립하면 새는" 부류의
결함이 없는지를 본다(full_cycle/assembly 테스트와 같은 관점).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.trade.loop import run_cycle
from quant.core.fx import FixedFxProvider
from quant.adapters.data.service import Capability, MarketDataService, SourceRoute
from quant.core.ports import Context
from quant.core.models import Quote, Side
from quant.adapters.execution.paper import PaperBroker
from quant.adapters.persistence.sink import MultiSink
from quant.core.portfolio.portfolio import Portfolio
from quant.trade.risk.manager import RiskManagerImpl
from quant.trade.strategy.donchian import DonchianStrategy

NY = ZoneInfo("America/New_York")
_SYMBOL = "TQQQ"
_MARKET_OF = {_SYMBOL: "US"}


class _CapturingSink:
    def __init__(self):
        self.fills = []

    def on_signal(self, signal) -> None:
        return None

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


class _FailingSource:
    """1차 소스 대역 — quote/history 모두 네트워크 단절을 흉내내며 예외를 던진다."""

    def __init__(self, error: Exception):
        self._error = error
        self.quote_calls = 0
        self.history_calls = 0

    def quote(self, symbol: str) -> Quote | None:
        self.quote_calls += 1
        raise self._error

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        self.history_calls += 1
        raise self._error


class _BarsSource:
    """정상 폴백 소스 대역 — 고정된 브레이크아웃 봉과 현재가를 제공한다."""

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


def test_primary_source_failure_stays_visible_through_a_real_trading_cycle(
    make_breakout_bars, donchian_params, tmp_path, caplog,
):
    bars = make_breakout_bars(41, breakout=True)
    entry_price = float(bars.iloc[-1]["close"])
    clock = _AlwaysOpenClock(datetime(2026, 1, 5, 20, 0, tzinfo=bars.index[-1].tzinfo))

    primary = _FailingSource(ConnectionError("실시세 연결 끊김"))
    fallback = _BarsSource(_SYMBOL, bars, entry_price)
    data = MarketDataService(
        routes=[
            SourceRoute(name="toss", source=primary, capabilities=frozenset({Capability.QUOTE, Capability.BARS})),
            SourceRoute(name="history", source=fallback, capabilities=frozenset({Capability.QUOTE, Capability.BARS})),
        ],
        clock=clock,
    )

    fx = FixedFxProvider(1000.0)
    portfolio = Portfolio(cash=10_000_000.0, state_path=tmp_path / "portfolio.json")
    broker = PaperBroker(data=data, portfolio=portfolio, fee_bps=7, market_of=_MARKET_OF, fx=fx)
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 50, "max_symbol_pct_total": 0, "daily_loss_limit_pct": 3}},
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF, fx=fx,
    )
    strategy = DonchianStrategy(symbols=[_SYMBOL], params=donchian_params(), market="US", id="donchian")
    ctx = Context(clock=clock, data=data, broker=broker)
    capture = _CapturingSink()

    with caplog.at_level("WARNING"):
        run_cycle([strategy], ctx, risk, MultiSink([capture]))

    # 1차 소스가 죽었어도 거래는 정상적으로 계속된다(가용성 — 폴백이 실제로 동작함).
    assert len(capture.fills) == 1
    assert capture.fills[0].side is Side.BUY
    assert primary.quote_calls > 0 or primary.history_calls > 0  # 조용히 건너뛴 게 아니라 실제로 1차부터 시도했다

    # 그러나 그 사실이 조용히 묻히면 안 된다 — health/provenance/로그 3중으로 확인한다.
    health = data.health()
    # degraded는 2026-08-12부터 "끝내 데이터를 못 받았다"는 뜻이다. 여기서는 폴백이
    # 성공해 체결까지 났으므로 False가 맞다 — 가시성은 아래 소스별 상태·provenance·
    # 경고 로그가 담당한다(이 테스트의 원래 의도는 '조용히 묻히지 않는가'이고,
    # 그 의도는 그대로 유지된다). 폴백 성공을 장애로 집계하던 옛 정의는 US 세션
    # 내내 오보를 냈다.
    assert health.degraded is False
    assert health.sources["toss"].healthy is False
    assert health.sources["history"].healthy is True
    assert data.provenance(_SYMBOL, Capability.QUOTE) == "history"
    assert data.provenance(_SYMBOL, Capability.BARS) == "history"
    assert any("toss" in rec.message for rec in caplog.records)


def test_when_primary_is_healthy_it_is_not_falsely_reported_degraded(make_breakout_bars):
    """대조군 — 1차 소스가 멀쩡하면 degraded가 거짓으로 찍히면 안 된다(위 테스트가
    "폴백만 있으면 항상 degraded"인 게 아니라 실제 실패를 정확히 반영함을 방증)."""
    bars = make_breakout_bars(41, breakout=True)
    entry_price = float(bars.iloc[-1]["close"])
    clock = _AlwaysOpenClock(datetime(2026, 1, 5, 20, 0, tzinfo=bars.index[-1].tzinfo))

    primary = _BarsSource(_SYMBOL, bars, entry_price)
    unused_fallback = _FailingSource(RuntimeError("정상 1차 소스가 있으면 호출되면 안 됨"))
    data = MarketDataService(
        routes=[
            SourceRoute(name="toss", source=primary, capabilities=frozenset({Capability.QUOTE, Capability.BARS})),
            SourceRoute(name="history", source=unused_fallback, capabilities=frozenset({Capability.QUOTE, Capability.BARS})),
        ],
        clock=clock,
    )

    data.quote(_SYMBOL)
    data.history(_SYMBOL, "15m", 41)

    health = data.health()
    assert health.degraded is False
    assert data.provenance(_SYMBOL, Capability.QUOTE) == "toss"
    assert data.provenance(_SYMBOL, Capability.BARS) == "toss"
    assert unused_fallback.quote_calls == 0
    assert unused_fallback.history_calls == 0
