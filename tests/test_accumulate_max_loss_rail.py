"""적립(오버나이트 캐리) 전략 전용 포트폴리오 레벨 최대손실 레일
(2026-09-06 안정성 감사 P1-4) — `quant/trade/loop.py`의
`_accumulate_max_loss_check`.

배경: `frgn_accumulate`/`news_accumulate`는 가격 기반 손절이 전혀 없다(설계
의도 — 태그 소멸로만 청산, `tests/test_frgn_accumulate.py` 참고). 태그 파이프
라인이 조용히 멎으면 이 두 레인은 가격 기반 회로차단기가 전혀 없는 채로 방치된다
— 이 레일은 그 disclosure 갭을 메우는 옵트인 백스톱이다. `_intraday_hard_stop_check`
(오버나이트 전략을 *제외*)와 정반대로, 이 레일은 `risk.overnight_strategies`에
*속한* 전략만 보고 기준가는 entry가 아니라 lot의 avg_cost다."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.adapters.execution.paper import PaperBroker
from quant.core.models import Order, Position, Quote, Side, Signal
from quant.core.portfolio.portfolio import Portfolio
from quant.core.ports import Context
from quant.trade.loop import _accumulate_max_loss_check

NY = ZoneInfo("America/New_York")
SYMBOL = "TQQQ"
_NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)


class _FakeClock:
    def now(self) -> datetime:
        return _NOW

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return 15.0

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


class _Feed:
    def __init__(self, price: float):
        self._price = price

    def quote(self, symbol: str) -> Quote | None:
        return Quote(symbol=symbol, ts=_NOW, price=self._price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class _Sink:
    def __init__(self):
        self.signals: list[Signal] = []
        self.fills: list = []

    def on_signal(self, signal) -> None:
        self.signals.append(signal)

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


class _Settings:
    """loop.EngineSettings가 실제로 요구하는 것 중 이 레일이 쓰는 부분(.raw)만."""

    def __init__(self, raw: dict):
        self.raw = raw


def _settings(accumulate_max_loss_pct=15.0, overnight_strategies=("frgn_accumulate",)) -> _Settings:
    return _Settings({
        "risk": {
            "accumulate_max_loss_pct": accumulate_max_loss_pct,
            "overnight_strategies": list(overnight_strategies),
        }
    })


def _rig(price: float, strategy_id: str = "frgn_accumulate", avg_cost: float = 100.0):
    """PaperBroker + 그 전략의 lot(qty=10, avg_cost=`avg_cost`) 하나를 든 포지션 +
    RiskManagerImpl 없이도 동작하는 최소 리그 — 이 레일은 risk의 속성이 아니라
    settings.raw만 읽으므로 risk는 place_order/approve를 쓰지 않는 더미로 충분하다."""
    broker = PaperBroker(
        data=_Feed(price), portfolio=Portfolio(cash=10_000_000.0, positions={}, state_path=None),
        fee_bps=0.0, market_of={SYMBOL: "US"},
    )
    broker.portfolio.positions[SYMBOL] = Position(
        symbol=SYMBOL, qty=10.0, avg_cost=avg_cost,
        meta={"lots": {strategy_id: {"qty": 10.0, "avg_cost": avg_cost}}},
    )
    ctx = Context(clock=_FakeClock(), data=broker.data, broker=broker)
    return broker, ctx, _Sink()


class _NullRisk:
    """_hard_rail_exit -> _execute_signal 경로가 risk.approve()를 거치므로,
    이 레일이 만드는 EXIT_LONG을 항상 그대로 승인하는 최소 더미. 이 레일
    자체는 risk의 속성을 읽지 않는다(settings.raw만 읽는다)."""

    def approve(self, signal, ctx, risk_multiplier: float = 1.0, marks=None):
        return Order(
            symbol=signal.symbol, side=Side.SELL, qty=999_999, strategy_id=signal.strategy_id,
            reason=signal.reason,
        )


def test_forced_exit_when_loss_exceeds_threshold():
    broker, ctx, sink = _rig(price=80.0, avg_cost=100.0)  # -20% > 기본 15%

    _accumulate_max_loss_check(
        ctx, _NullRisk(), sink, notifier=None, books=None,
        marks={SYMBOL: 80.0}, settings=_settings(accumulate_max_loss_pct=15.0),
    )

    assert not broker.portfolio.positions[SYMBOL].is_open
    assert len(sink.fills) == 1
    assert sink.fills[0].reason == "적립 최대손실 -15%(리스크 레일)"


def test_no_exit_when_loss_below_threshold():
    broker, ctx, sink = _rig(price=90.0, avg_cost=100.0)  # -10% < 15%

    _accumulate_max_loss_check(
        ctx, _NullRisk(), sink, notifier=None, books=None,
        marks={SYMBOL: 90.0}, settings=_settings(accumulate_max_loss_pct=15.0),
    )

    assert broker.portfolio.positions[SYMBOL].is_open
    assert sink.fills == []


def test_disabled_by_default_zero_config():
    """accumulate_max_loss_pct가 0/미설정이면 완전히 비활성(기존 동작 그대로)."""
    broker, ctx, sink = _rig(price=10.0, avg_cost=100.0)  # -90%, 극단적 손실

    _accumulate_max_loss_check(
        ctx, _NullRisk(), sink, notifier=None, books=None,
        marks={SYMBOL: 10.0}, settings=_settings(accumulate_max_loss_pct=0.0),
    )

    assert broker.portfolio.positions[SYMBOL].is_open
    assert sink.fills == []


def test_only_applies_to_strategies_in_overnight_strategies_list():
    """오버나이트 목록에 없는 전략은 이 레일이 손대지 않는다 — 단타 전략의
    정상적인 손절은 이미 다른 레일(_intraday_hard_stop_check)이 다룬다."""
    broker, ctx, sink = _rig(price=50.0, strategy_id="scalp_1m", avg_cost=100.0)  # -50%

    _accumulate_max_loss_check(
        ctx, _NullRisk(), sink, notifier=None, books=None,
        marks={SYMBOL: 50.0},
        settings=_settings(accumulate_max_loss_pct=15.0, overnight_strategies=["frgn_accumulate"]),
    )

    assert broker.portfolio.positions[SYMBOL].is_open
    assert sink.fills == []


def test_skips_when_symbol_not_in_marks():
    broker, ctx, sink = _rig(price=10.0, avg_cost=100.0)

    _accumulate_max_loss_check(
        ctx, _NullRisk(), sink, notifier=None, books=None,
        marks={}, settings=_settings(accumulate_max_loss_pct=15.0),
    )

    assert broker.portfolio.positions[SYMBOL].is_open
    assert sink.fills == []


def test_uses_lot_avg_cost_not_position_level_avg_cost():
    """여러 전략이 같은 심볼을 나눠 들고 있을 때, 판정은 그 전략 lot의
    avg_cost를 써야 한다 — 심볼 합산 avg_cost가 아니라."""
    broker, ctx, sink = _rig(price=90.0, strategy_id="frgn_accumulate", avg_cost=100.0)
    # 심볼 합산 avg_cost(포지션 레벨)는 낮게(80) 만들어 두되, 이 전략의 lot만
    # 100으로 유지한다 — 판정이 lot 기준이면 -10%(15% 미만, 청산 없음), 포지션
    # 레벨 기준이면 +12.5%(이익, 역시 청산 없음)라 이 케이스만으론 구분이 안 되므로
    # 손실 방향으로 명확히 갈리게 값을 잡는다.
    broker.portfolio.positions[SYMBOL].avg_cost = 60.0  # 포지션 레벨: (60-90)/60 = -50%(레일 발동권)
    # lot avg_cost는 100 그대로: (100-90)/100 = -10%(레일 미발동)

    _accumulate_max_loss_check(
        ctx, _NullRisk(), sink, notifier=None, books=None,
        marks={SYMBOL: 90.0}, settings=_settings(accumulate_max_loss_pct=15.0),
    )

    # lot 기준(-10%)이면 청산 없음, 포지션 레벨 기준(-50%)이면 청산됨 — lot이
    # 옳으므로 청산이 없어야 한다.
    assert broker.portfolio.positions[SYMBOL].is_open
    assert sink.fills == []
