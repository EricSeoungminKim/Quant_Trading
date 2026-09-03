"""소수점 주(fractional) 분할매도 게이트 — `RiskManagerImpl.approve()` EXIT 분기.

2026-09-02 실사고. 브로커가 소수점 수량을 받는 조건은 딱 하나다:
**US `MARKET`+`SELL`, 그것도 정규장(09:30~16:00 ET) 중에만**
(`docs/api/toss/QUICKREF.md` — 그 밖은 422 `fractional-quantity-outside-regular-hours`).

그동안 리스크 레이어는 `market == "KR"`일 때만 내림+<1주 차단을 걸었다. 그래서
scalp_1m 이 US 프리마켓(`risk.extended_sessions.scalp_1m.US = 08:00-09:25`)에서
1주 포지션의 절반(`partial_fraction: 0.5`)을 익절하려 하면 qty=0.5 주문이 그대로
브로커까지 내려갔고, `TossBroker.place_order` 가 이를 0주로 내림해 **아무 말 없이
None 을 반환**했다. `state_update={"partial_taken": True}` 는 체결 시에만 적용되므로
같은 SCALE_OUT 이 5초마다 영원히 재발화하고 익절은 끝내 일어나지 않았다.

이 스위트가 고정하는 것:
- US 정규장 밖 1주 절반 익절 → 주문 없음 + orders 로그에 남는 명시적 차단 사유.
- US 정규장 중 1주 절반 익절 → 0.5주 그대로 통과(스펙상 유일한 소수점 허용 경로).
- KR 은 시각과 무관하게 이전과 동일하게 내림/차단(회귀 방지).
- 전량 청산(exit_fraction=1)은 어느 구간에서도 이 게이트를 타지 않는다 —
  손절이 여기 막히면 그게 진짜 사고다.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.fx import FixedFxProvider
from quant.core.models import Position, Quote, Signal, SignalAction
from quant.core.ports import Context
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

TQQQ = "TQQQ"
KR_SYM = "005930"
FX_RATE = 1500.0
PRICE = 100.0

# scalp_1m 의 실제 프리마켓 창(config/settings.yaml `risk.extended_sessions`) 안.
US_PREMARKET = datetime(2026, 1, 5, 9, 0, tzinfo=NY)
US_REGULAR = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
KR_REGULAR = datetime(2026, 1, 5, 10, 0, tzinfo=KST)


class _Broker:
    def __init__(self, positions):
        self._positions = positions

    def positions(self):
        return self._positions

    def cash(self) -> float:
        return 10_000_000.0


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=US_REGULAR, price=PRICE)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _risk() -> RiskManagerImpl:
    cfg = dict(
        sizing_mode="capital_fraction",
        max_position_pct=100,
        max_symbol_pct_total=0,
        daily_loss_limit_pct=100,
        max_orders_per_day=1000,
        cooldown_bars_after_stop=0,
        max_order_notional_pct=0,
        max_total_exposure_pct=0,
        max_concurrent_positions=0,
        extended_sessions={"scalp_1m": {"KR": ["08:00-08:50"], "US": ["08:00-09:25"]}},
    )
    return RiskManagerImpl(
        {"risk": cfg},
        capital_fraction={"scalp_1m": 1.0},
        market_of={TQQQ: "US", KR_SYM: "KR"},
        fx=FixedFxProvider(FX_RATE),
    )


def _held(symbol: str, qty: float = 1.0) -> dict[str, Position]:
    pos = Position(symbol=symbol, qty=qty, avg_cost=PRICE)
    pos.meta["lots"] = {"scalp_1m": {"qty": qty, "avg_cost": PRICE}}
    return {symbol: pos}


def _ctx(now, positions, fake_clock_cls, market_open: bool) -> Context:
    return Context(
        clock=fake_clock_cls(now=now, market_open=market_open),
        data=_Data(),
        broker=_Broker(positions),
    )


def _exit(symbol: str, fraction: float) -> Signal:
    action = SignalAction.SCALE_OUT if fraction < 1 else SignalAction.EXIT_LONG
    return Signal(
        strategy_id="scalp_1m",
        symbol=symbol,
        action=action,
        target_weight=0.0,
        exit_fraction=fraction,
        reason="절반 익절",
    )


# ------------------------------------------------------------------ US 정규장 밖


def test_us_premarket_half_exit_of_one_share_is_blocked(fake_clock_cls):
    """프리마켓에서 1주의 절반 → 0.5주. 브로커가 못 받는 수량이므로 주문을 만들지
    않고, 사유를 남긴다(예전에는 0.5주가 그대로 내려가 조용히 버려졌다)."""
    risk = _risk()
    ctx = _ctx(US_PREMARKET, _held(TQQQ), fake_clock_cls, market_open=False)

    order = risk.approve(_exit(TQQQ, 0.5), ctx)

    assert order is None
    assert "<1주" in risk.last_block
    assert "전량 청산 대기" in risk.last_block


def test_us_premarket_half_exit_of_three_shares_floors_to_one(fake_clock_cls):
    """3주의 절반 = 1.5주 → 정규장 밖이므로 1주로 내림해서 낸다(익절 자체는 살린다)."""
    risk = _risk()
    ctx = _ctx(US_PREMARKET, _held(TQQQ, qty=3.0), fake_clock_cls, market_open=False)

    order = risk.approve(_exit(TQQQ, 0.5), ctx)

    assert order is not None
    assert order.qty == 1


def test_us_premarket_full_exit_is_never_gated(fake_clock_cls):
    """전량 청산은 소수점이 생길 수 없으므로 이 게이트를 아예 타지 않는다."""
    risk = _risk()
    ctx = _ctx(US_PREMARKET, _held(TQQQ), fake_clock_cls, market_open=False)

    order = risk.approve(_exit(TQQQ, 1.0), ctx)

    assert order is not None
    assert order.qty == pytest.approx(1.0)


# ------------------------------------------------------------------ US 정규장


def test_us_regular_hours_half_exit_of_one_share_allows_fraction(fake_clock_cls):
    """정규장 중 US MARKET SELL 은 소수점이 스펙상 허용된다 — 0.5주가 나가야 한다."""
    risk = _risk()
    ctx = _ctx(US_REGULAR, _held(TQQQ), fake_clock_cls, market_open=True)

    order = risk.approve(_exit(TQQQ, 0.5), ctx)

    assert order is not None
    assert order.qty == pytest.approx(0.5)


# ------------------------------------------------------------------ KR 회귀


def test_kr_half_exit_of_one_share_still_blocked(fake_clock_cls):
    """KR 은 언제나 정수만 받는다 — 기존 `market == "KR"` 동작이 그대로 남는다."""
    risk = _risk()
    ctx = _ctx(KR_REGULAR, _held(KR_SYM), fake_clock_cls, market_open=True)

    order = risk.approve(_exit(KR_SYM, 0.5), ctx)

    assert order is None
    assert "<1주" in risk.last_block


def test_kr_half_exit_of_three_shares_floors(fake_clock_cls):
    risk = _risk()
    ctx = _ctx(KR_REGULAR, _held(KR_SYM, qty=3.0), fake_clock_cls, market_open=True)

    order = risk.approve(_exit(KR_SYM, 0.5), ctx)

    assert order is not None
    assert order.qty == 1
