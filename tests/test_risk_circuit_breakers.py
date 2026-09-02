"""리스크 회로차단기(max_orders_per_day / cooldown_bars_after_stop / max_order_notional_pct /
NaN·inf·음수·0 수량 가드) 단위 테스트. 브로커가 포지션 메타를 잃어 10초 폴링마다 절반씩
매도하던 상태 폭주 사고(quant-expert SKILL.md §5) 이후 "코드 버그와 무관하게 걸리는 독립
레일"로 추가됐다 — 이 스위트가 지키는 불변식은 (1) 각 회로차단기가 경계값에서 정확히
걸리고/걸리지 않는지, (2) 어떤 회로차단기도 청산(EXIT_LONG/SCALE_OUT)은 절대 막지 않는지,
(3) 일자 롤오버가 주문 카운터를 정확히 리셋하는지, (4) 쿨다운이 봉 카운트 기반으로 정확히
만료되는지, (5) breaker_state()가 실제 상태를 정확히 반영하는지다.

주의(설계상 트레이드오프, 숨기지 않고 명시): "청산은 절대 막지 않는다"는 요구사항 때문에,
max_orders_per_day는 진입(ENTER_LONG/SCALE_IN) 폭주만 실제로 정지시킬 수 있다. 실제
사고는 청산(SCALE_OUT) 신호가 폭주한 경우였는데, 이 레일은 그 패턴 자체를 막지는 못한다
— 대신 breaker_state()로 폭주 자체(주문 카운트)는 드러나므로 운영자 개입(수동 킬스위치,
이 변경 범위 밖)의 근거가 된다. test_regression_runaway_repeated_signal_is_stopped_by_max_orders_per_day
에서 이 트레이드오프를 다시 설명한다.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.fx import FixedFxProvider
from quant.core.ports import ColdFetchBudgetExceeded, Context
from quant.core.models import Position, Quote, Side, Signal, SignalAction
from quant.core.portfolio.portfolio import to_krw
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
_SYMBOL = "TQQQ"
_MARKET_OF = {_SYMBOL: "US", "SQQQ": "US"}
_DEFAULT_NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)


class _FakeBroker:
    def __init__(self, cash: float, positions: dict[str, Position] | None = None):
        self._cash = cash
        self._positions = positions or {}

    def positions(self) -> dict[str, Position]:
        return self._positions

    def cash(self) -> float:
        return self._cash

    def place_order(self, order):
        raise NotImplementedError("이 스위트는 approve()만 검증 — place_order는 호출되지 않아야 한다")


class _FakeData:
    """price는 quote()가 매번 그대로 반환. bars[symbol]을 직접 채워 history()가 반환할
    완성봉 시퀀스를 테스트가 완전히 통제한다(쿨다운의 봉 카운트 검증용)."""

    def __init__(self, price: float, now: datetime = _DEFAULT_NOW, raise_on_history: Exception | None = None):
        self._price = price
        self._now = now
        self.bars: dict[str, pd.DataFrame] = {}
        # 콜드 페치 예산 초과 등 history() 실패를 흉내내기 위한 훅
        # (2026-09-02 결함 A 회귀 테스트: "청산은 절대 막지 않는다"가 실제로도
        # 그런지, history() 예외가 approve() 밖으로 새지 않는지 검증한다).
        self.raise_on_history = raise_on_history

    def quote(self, symbol: str) -> Quote | None:
        return Quote(symbol=symbol, ts=self._now, price=self._price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        if self.raise_on_history is not None:
            raise self.raise_on_history
        df = self.bars.get(symbol)
        if df is None or len(df) == 0:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df.tail(n)


def _bars(n: int, start: datetime = _DEFAULT_NOW, interval_minutes: int = 15) -> pd.DataFrame:
    rows = []
    for i in range(n):
        ts = start + timedelta(minutes=interval_minutes * i)
        rows.append({"ts": ts, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000.0})
    return pd.DataFrame(rows).set_index("ts")


def _risk_cfg(**overrides) -> dict:
    """관대한 기본값(다른 회로차단기가 우연히 끼어들지 않도록) — 각 테스트는 검증하려는
    항목만 override한다."""
    cfg = dict(
        max_position_pct=100,
        max_symbol_pct_total=0,
        daily_loss_limit_pct=100,
        max_orders_per_day=1000,
        cooldown_bars_after_stop=0,
        cooldown_bar_interval_minutes=15,
        max_order_notional_pct=0,  # 0 = 비활성(코드의 `if self.max_order_notional_pct` 단락평가)
    )
    cfg.update(overrides)
    return {"risk": cfg}


def _ctx(
    fake_clock_cls,
    price: float,
    cash: float,
    positions: dict[str, Position] | None = None,
    now: datetime = _DEFAULT_NOW,
    data: _FakeData | None = None,
) -> Context:
    d = data or _FakeData(price=price, now=now)
    return Context(clock=fake_clock_cls(now=now), data=d, broker=_FakeBroker(cash, positions))


def _entry(target_weight: float = 1.0, symbol: str = _SYMBOL) -> Signal:
    return Signal(strategy_id="donchian", symbol=symbol, action=SignalAction.ENTER_LONG, target_weight=target_weight)


def _exit(symbol: str = _SYMBOL, exit_fraction: float = 1.0, reason: str = "") -> Signal:
    return Signal(
        strategy_id="donchian", symbol=symbol, action=SignalAction.EXIT_LONG,
        target_weight=0.0, exit_fraction=exit_fraction, reason=reason,
    )


# ============================================================= max_orders_per_day

def test_max_orders_per_day_blocks_further_entries_at_boundary_but_not_before(fake_clock_cls):
    risk = RiskManagerImpl(_risk_cfg(max_orders_per_day=2), capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    # weight=0.01은 정수 수량 사이징(QUICKREF:207)에서 budget=100,000원 ->
    # floor(100,000/1500/100)=0주로 거부된다 — 0.02로 올려 매 승인마다 qty=1을 확보한다.
    assert risk.approve(_entry(0.02), ctx) is not None
    assert risk.approve(_entry(0.02), ctx) is not None  # 정확히 상한만큼은 승인돼야 한다

    blocked = risk.approve(_entry(0.01), ctx)
    assert blocked is None
    assert "진입 상한" in risk.last_block

    state = risk.breaker_state()["max_orders_per_day"]
    # 상한은 이제 **시장별 진입 예산**이다(2026-08-14). count/tripped 는 하위 호환 키.
    assert state["limit"] == 2 and state["tripped"] is True
    assert state["by_market"]["US"] == {"entries": 2, "orders": 2, "tripped": True}


def test_max_orders_per_day_never_blocks_exits(fake_clock_cls):
    risk = RiskManagerImpl(_risk_cfg(max_orders_per_day=1), capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF)
    ctx_entry = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)
    # weight=0.02: 정수 수량 사이징에서 qty=1을 확보하는 최소 배율(위 테스트 참고).
    assert risk.approve(_entry(0.02), ctx_entry) is not None
    assert risk.approve(_entry(0.02), ctx_entry) is None  # 상한 도달 확인

    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=10.0, avg_cost=100.0)}
    ctx_exit = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, positions=positions)
    exit_order = risk.approve(_exit(), ctx_exit)
    assert exit_order is not None
    assert exit_order.side is Side.SELL
    assert exit_order.qty == pytest.approx(10.0)


def test_day_rollover_resets_order_counter_but_daily_loss_baseline_semantics_unchanged(fake_clock_cls):
    """max_orders_per_day의 새 카운터는 daily_loss_limit_pct와 동일한 `ctx.clock.now().date()`
    기준으로 롤오버한다 — 기존 daily_loss_limit_pct의 day_start_equity 판정 로직/약점(세션
    시가가 아니라 해당 날짜 첫 approve 시점 기준)은 그대로 두었다. 새로 추가한 것은 카운터
    리셋 시점을 그 기존 롤오버 지점에 맞춘 것뿐이다."""
    risk = RiskManagerImpl(_risk_cfg(max_orders_per_day=1), capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF)
    # weight=0.02: 정수 수량 사이징에서 qty=1을 확보하는 최소 배율(위 테스트 참고).
    day1 = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
    ctx1 = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, now=day1)
    assert risk.approve(_entry(0.02), ctx1) is not None
    assert risk.approve(_entry(0.02), ctx1) is None
    assert risk.breaker_state()["max_orders_per_day"]["tripped"] is True

    day2 = datetime(2026, 1, 6, 10, 0, tzinfo=NY)
    ctx2 = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, now=day2)
    approved = risk.approve(_entry(0.02), ctx2)
    assert approved is not None
    st = risk.breaker_state()["max_orders_per_day"]
    assert st["count"] == 1 and st["limit"] == 1 and st["tripped"] is True
    assert st["by_market"]["US"]["entries"] == 1   # 롤 후 새로 센 1건


def test_regression_runaway_repeated_signal_is_stopped_by_max_orders_per_day(fake_clock_cls):
    """실제 사고(브로커가 포지션 메타를 잃어 10초 폴링마다 동일 신호가 재발동, 매번 잔여
    포지션의 절반을 매도)를 흉내낸다: 버그로 인해 같은 Signal이 매 사이클 그대로 재발동
    한다고 가정하고 approve()를 반복 호출한다.

    주의: 여기서는 진입(ENTER_LONG) 신호 폭주로 시뮬레이션한다. 청산(SCALE_OUT/EXIT_LONG)은
    이 리스크 매니저의 설계 원칙상 어떤 회로차단기도 절대 막지 않으므로(청산을 막으면 손실
    포지션을 가두는 게 더 위험하다), max_orders_per_day는 원 사고처럼 청산이 폭주하는
    패턴 자체는 막을 수 없다 — 오직 진입 폭주만 정지시킬 수 있다. 이건 이 구현의 알려진
    한계이지 은폐할 사항이 아니다: 청산 폭주는 breaker_state()의 주문 카운트로 여전히
    드러나고, 실제로 멈추려면 별도 레이어(수동 킬스위치/전량 청산 등, 이 변경 범위 밖)가
    필요하다.
    """
    cap = 5
    # 반복 진입 레일을 끄고 **일일 상한 자체**를 검증한다. 폭주는 이제 반복 레일이
    # 더 빨리(3건) 잡지만(아래 test_repeat_entry_rail_stops_the_runaway_signature),
    # 일일 상한을 켜 둔 구성에서도 여전히 멈춰야 한다.
    risk = RiskManagerImpl(_risk_cfg(max_orders_per_day=cap, max_repeat_entries_per_window=0),
                           capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)
    # weight=0.02: 정수 수량 사이징에서 qty=1을 확보하는 최소 배율(위 테스트들 참고).
    same_signal = _entry(0.02)  # 버그로 인해 매 폴링마다 동일하게 재발동하는 신호

    approved = [risk.approve(same_signal, ctx) for _ in range(cap)]
    assert all(o is not None for o in approved)

    # 버그가 100회 더 폴링해도(poll_seconds=10초 기준 약 16분) cap 이후로는 전부 거부.
    rejected = [risk.approve(same_signal, ctx) for _ in range(100)]
    assert all(o is None for o in rejected)
    assert "진입 상한" in risk.last_block   # 문구 변경(시장별 진입 예산), 보호는 동일
    assert risk.breaker_state()["max_orders_per_day"]["tripped"] is True


# ======================================================= cooldown_bars_after_stop

def test_cooldown_bars_after_stop_blocks_reentry_until_bar_count_elapses(fake_clock_cls):
    risk = RiskManagerImpl(
        _risk_cfg(cooldown_bars_after_stop=3, cooldown_bar_interval_minutes=15),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=50.0, avg_cost=100.0)}
    stop_data = _FakeData(price=97.5, now=_DEFAULT_NOW)
    stop_data.bars[_SYMBOL] = _bars(1, _DEFAULT_NOW)  # 손절이 발생한 봉 = 10:00
    stop_ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=stop_data, broker=_FakeBroker(10_000_000.0, positions))

    stop_signal = _exit(reason="손절: entry=100.00 stop=98.00 현재=97.50")
    stop_order = risk.approve(stop_signal, stop_ctx)
    assert stop_order is not None and stop_order.side is Side.SELL

    def _entry_ctx(n_bars: int) -> Context:
        d = _FakeData(price=100.0, now=_DEFAULT_NOW)
        d.bars[_SYMBOL] = _bars(n_bars, _DEFAULT_NOW)
        return Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=d, broker=_FakeBroker(10_000_000.0, {}))

    # elapsed=0 (아직 손절 봉과 동일) -> 차단
    # weight=0.02: 정수 수량 사이징(QUICKREF:207)에서 qty=1을 확보하는 최소 배율 —
    # 쿨다운이 풀리는 마지막 approve()가 실제로 승인되려면 필요하다(위 차단 케이스들은
    # 쿨다운이 사이징보다 먼저 걸리므로 영향 없음).
    blocked0 = risk.approve(_entry(0.02), _entry_ctx(1))
    assert blocked0 is None
    assert "쿨다운" in risk.last_block

    # elapsed=2 (<3, 10:15/10:30 두 봉 경과) -> 여전히 차단
    blocked2 = risk.approve(_entry(0.02), _entry_ctx(3))
    assert blocked2 is None
    assert "쿨다운" in risk.last_block

    # elapsed=3 (>=3, 10:15/10:30/10:45 세 봉 경과) -> 승인 + 쿨다운 상태 정리
    approved = risk.approve(_entry(0.02), _entry_ctx(4))
    assert approved is not None
    assert _SYMBOL not in risk.breaker_state()["cooldown_bars_after_stop"]["symbols_in_cooldown"]


def test_eod_cooldown_blocks_same_day_reentry_even_after_bars_elapse(fake_clock_cls):
    """2026-08-28 소유자 지시 실측(손절 후 재진입 승률 14% vs 이익 후 43%):
    cooldown_until_eod_strategies 대상 전략은 손절 후 그날 내내 재진입 차단 —
    N봉 쿨다운이 풀려도 유지된다."""
    risk = RiskManagerImpl(
        _risk_cfg(cooldown_bars_after_stop=3, cooldown_bar_interval_minutes=15,
                  cooldown_until_eod_strategies=["donchian"]),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=50.0, avg_cost=100.0)}
    stop_data = _FakeData(price=97.5, now=_DEFAULT_NOW)
    stop_data.bars[_SYMBOL] = _bars(1, _DEFAULT_NOW)
    stop_ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=stop_data,
                       broker=_FakeBroker(10_000_000.0, positions))
    assert risk.approve(_exit(reason="손절: entry=100.00 stop=98.00 현재=97.50"), stop_ctx) is not None

    # 봉 50개 경과 — 일반 쿨다운(3봉)은 진작 풀렸을 시간
    d = _FakeData(price=100.0, now=_DEFAULT_NOW)
    d.bars[_SYMBOL] = _bars(50, _DEFAULT_NOW)
    ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=d, broker=_FakeBroker(10_000_000.0, {}))
    assert risk.approve(_entry(0.02), ctx) is None
    assert "쿨다운(당일)" in risk.last_block


def test_eod_cooldown_ignores_profit_protection_exits(fake_clock_cls):
    """'이익보호 청산(본전/트레일)'은 손절이 아니다 — 기록되지 않아야 이익 후
    재진입(실측 최선 서브그룹)이 계속 열려 있다."""
    risk = RiskManagerImpl(
        _risk_cfg(cooldown_bars_after_stop=3, cooldown_until_eod_strategies=["donchian"]),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=50.0, avg_cost=100.0)}
    data = _FakeData(price=101.2, now=_DEFAULT_NOW)
    data.bars[_SYMBOL] = _bars(1, _DEFAULT_NOW)
    ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=data,
                  broker=_FakeBroker(10_000_000.0, positions))
    assert risk.approve(
        _exit(reason="이익보호 청산(본전/트레일): entry=100.00 stop=101.29 현재=101.20"), ctx,
    ) is not None

    entry_ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)
    assert risk.approve(_entry(0.02), entry_ctx) is not None


def test_eod_cooldown_scoped_to_strategy_and_symbol(fake_clock_cls):
    """차단은 (전략, 심볼) 단위다 — donchian 이 손절한 심볼이라도 목록에 없는
    다른 전략의 진입은 막지 않는다(측정된 근거가 scalp_1m 뿐이므로 과잉 차단 금지)."""
    risk = RiskManagerImpl(
        _risk_cfg(cooldown_bars_after_stop=0, cooldown_until_eod_strategies=["donchian"]),
        capital_fraction={"donchian": 1.0, "other": 1.0}, market_of=_MARKET_OF,
    )
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=50.0, avg_cost=100.0)}
    data = _FakeData(price=97.5, now=_DEFAULT_NOW)
    data.bars[_SYMBOL] = _bars(1, _DEFAULT_NOW)
    ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=data,
                  broker=_FakeBroker(10_000_000.0, positions))
    assert risk.approve(_exit(reason="손절: entry=100.00 stop=98.00 현재=97.50"), ctx) is not None

    entry_ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)
    assert risk.approve(_entry(0.02), entry_ctx) is None  # donchian 재진입 차단
    other = Signal(strategy_id="other", symbol=_SYMBOL,
                   action=SignalAction.ENTER_LONG, target_weight=0.02)
    assert risk.approve(other, entry_ctx) is not None  # 다른 전략은 통과


def test_eod_cooldown_clears_on_next_trading_day(fake_clock_cls):
    from datetime import timedelta
    risk = RiskManagerImpl(
        _risk_cfg(cooldown_bars_after_stop=0, cooldown_until_eod_strategies=["donchian"]),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=50.0, avg_cost=100.0)}
    data = _FakeData(price=97.5, now=_DEFAULT_NOW)
    data.bars[_SYMBOL] = _bars(1, _DEFAULT_NOW)
    ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=data,
                  broker=_FakeBroker(10_000_000.0, positions))
    assert risk.approve(_exit(reason="손절: entry=100.00 stop=98.00 현재=97.50"), ctx) is not None
    assert risk.approve(_entry(0.02), _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)) is None

    tomorrow = _DEFAULT_NOW + timedelta(days=1)
    next_ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, now=tomorrow)
    assert risk.approve(_entry(0.02), next_ctx) is not None


def test_cooldown_bars_after_stop_never_blocks_exits(fake_clock_cls):
    risk = RiskManagerImpl(
        _risk_cfg(cooldown_bars_after_stop=10, cooldown_bar_interval_minutes=15),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=50.0, avg_cost=100.0)}
    data = _FakeData(price=97.5, now=_DEFAULT_NOW)
    data.bars[_SYMBOL] = _bars(1, _DEFAULT_NOW)
    ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=data, broker=_FakeBroker(10_000_000.0, positions))

    stop1 = risk.approve(_exit(reason="손절: entry=100.00 stop=98.00 현재=97.50"), ctx)
    assert stop1 is not None
    # 손절 직후(elapsed=0, cooldown=10) 같은 봉에서 또 다른 청산(예: 잔여 포지션 정리)이
    # 와도 절대 막히지 않아야 한다.
    positions2 = {_SYMBOL: Position(symbol=_SYMBOL, qty=25.0, avg_cost=100.0)}
    ctx2 = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=data, broker=_FakeBroker(10_000_000.0, positions2))
    stop2 = risk.approve(_exit(reason="마감 전 청산"), ctx2)
    assert stop2 is not None


def test_stop_loss_exit_is_approved_even_when_cooldown_bar_fetch_raises_cold_fetch_budget_exceeded(fake_clock_cls):
    """2026-09-02 실사고(09:25~09:42, KR 078340) 회귀 테스트: 콜드 페치 예산 초과가
    approve() 안에서 손절 쿨다운 봉 기록(_bar_ts → ctx.data.history())을 실패시켜
    approve() 전체가 예외로 죽었고, 그 결과 하드레일 손절이 6회 연속(2분간) 막혔다.
    이 조회는 매도 자체에 필요한 데이터가 아니다(부기용) — 실패해도 매도는
    반드시 승인돼야 한다("청산은 절대 막지 않는다", 모듈 docstring)."""
    risk = RiskManagerImpl(
        _risk_cfg(cooldown_bars_after_stop=3, cooldown_bar_interval_minutes=15),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=50.0, avg_cost=100.0)}
    data = _FakeData(
        price=97.5, now=_DEFAULT_NOW,
        raise_on_history=ColdFetchBudgetExceeded(
            f"콜드 페치 예산 초과 (8/사이클, {_SYMBOL} 15m) — 다음 사이클"
        ),
    )
    ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=data, broker=_FakeBroker(10_000_000.0, positions))

    order = risk.approve(_exit(reason="손절: entry=100.00 stop=98.00 현재=97.50"), ctx)

    assert order is not None
    assert order.side is Side.SELL
    assert order.qty == pytest.approx(50.0)
    # 봉 조회가 실패했으니 쿨다운 상태는 기록되지 않는다 — 최악의 경우 이번 손절
    # 건은 재진입 쿨다운이 안 걸릴 뿐(허용 가능한 저하), 매도 자체는 나갔다.
    assert _SYMBOL not in risk.breaker_state()["cooldown_bars_after_stop"]["symbols_in_cooldown"]


def test_entry_reentry_check_still_propagates_cold_fetch_budget_exceeded(fake_clock_cls):
    """위 테스트와의 비대칭 확인 — 진입(재진입 쿨다운 판정)의 봉 조회는 이번
    수정 범위 밖이다: 예산 스로틀이 진입을 이번 사이클엔 미루는 것은 기존
    의도된 동작(loop.py의 ColdFetchBudgetExceeded 처리, 2026-08-31 수정)이고,
    청산과 달리 진입은 미뤄도 안전하다. 이 테스트는 그 비대칭이 실제로 지켜지는
    지 고정한다 — 진입 경로의 봉 조회는 여전히 예외를 그대로 올린다."""
    risk = RiskManagerImpl(
        _risk_cfg(cooldown_bars_after_stop=3, cooldown_bar_interval_minutes=15),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    # 먼저 손절을 하나 기록해 쿨다운 상태(_stop_bar_ts)를 만든다(정상 조회).
    stop_positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=50.0, avg_cost=100.0)}
    stop_data = _FakeData(price=97.5, now=_DEFAULT_NOW)
    stop_data.bars[_SYMBOL] = _bars(1, _DEFAULT_NOW)
    stop_ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=stop_data, broker=_FakeBroker(10_000_000.0, stop_positions))
    assert risk.approve(_exit(reason="손절: entry=100.00 stop=98.00 현재=97.50"), stop_ctx) is not None

    # 재진입 시도 — 쿨다운이 아직 살아있는 채로 이번엔 봉 조회 자체가 예산
    # 초과로 실패한다. 진입 경로(cooldown_bars_after_stop 재진입 판정)는 이
    # 수정 대상이 아니므로 예외가 그대로 전파돼야 한다.
    reentry_data = _FakeData(
        price=100.0, now=_DEFAULT_NOW,
        raise_on_history=ColdFetchBudgetExceeded(
            f"콜드 페치 예산 초과 (8/사이클, {_SYMBOL} 15m) — 다음 사이클"
        ),
    )
    reentry_ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=reentry_data, broker=_FakeBroker(10_000_000.0, {}))

    with pytest.raises(ColdFetchBudgetExceeded):
        risk.approve(_entry(0.02), reentry_ctx)


def test_cooldown_does_not_apply_without_a_prior_stop(fake_clock_cls):
    risk = RiskManagerImpl(
        _risk_cfg(cooldown_bars_after_stop=5), capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)
    # weight=0.02: 정수 수량 사이징에서 qty=1을 확보하는 최소 배율(위 테스트들 참고).
    order = risk.approve(_entry(0.02), ctx)
    assert order is not None


# ========================================================= max_order_notional_pct

def test_max_order_notional_pct_blocks_above_cap_but_not_at_boundary(fake_clock_cls):
    fx = FixedFxProvider(1000.0)
    risk = RiskManagerImpl(
        _risk_cfg(max_position_pct=100, max_symbol_pct_total=0, max_order_notional_pct=10),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF, fx=fx,
    )
    # price=$1.00(원래 $100)로 낮춘다: 정수 수량 사이징(QUICKREF:207)에서 1주=100,000원
    # 짜리 가격이면 floor()가 0.10/0.11 경계(10,000원 차이)를 통째로 삼켜버려 "경계
    # 초과"가 더 이상 거부되지 않는다(둘 다 floor로 qty=1). $1.00이면 1주=1,000원이라
    # floor 손실이 무시할 수준이 되고 원래 의도한 미세 경계 검증이 그대로 보인다.
    ctx = _ctx(fake_clock_cls, price=1.0, cash=1_000_000.0)

    # 경계: target_weight=0.10 -> budget = 0.10 * equity(1,000,000) = 100,000원 = 상한과 정확히 일치 -> 승인.
    at_boundary = risk.approve(_entry(0.10), ctx)
    assert at_boundary is not None
    assert to_krw(at_boundary.qty * 1.0, "US", fx) == pytest.approx(100_000.0)

    # 경계 초과: target_weight=0.11 -> budget=110,000원 > 상한(100,000원) -> 거부.
    over = risk.approve(_entry(0.11), ctx)
    assert over is None
    assert "규모 상한" in risk.last_block


def test_max_order_notional_pct_never_blocks_exits(fake_clock_cls):
    fx = FixedFxProvider(1000.0)
    risk = RiskManagerImpl(
        _risk_cfg(max_position_pct=100, max_symbol_pct_total=0, max_order_notional_pct=1),  # 매우 타이트한 캡
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF, fx=fx,
    )
    # 매도 명목가(qty*price)가 1% 캡을 훨씬 초과하는 대형 포지션이어도 청산은 승인돼야 한다.
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=1000.0, avg_cost=100.0)}
    ctx = _ctx(fake_clock_cls, price=100.0, cash=1_000_000.0, positions=positions)
    order = risk.approve(_exit(exit_fraction=1.0), ctx)
    assert order is not None
    assert order.qty == pytest.approx(1000.0)


# ================================= NaN/inf/음수/0 수량 — sanity 가드가 항상 이유를 남긴다

def test_nan_price_is_rejected_before_sizing(fake_clock_cls):
    risk = RiskManagerImpl(_risk_cfg(), capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF)
    ctx = _ctx(fake_clock_cls, price=float("nan"), cash=10_000_000.0)
    order = risk.approve(_entry(0.1), ctx)
    assert order is None
    assert risk.last_block  # 사유가 남아야 한다(무음 거부 금지)
    assert "현재가" in risk.last_block


def test_nan_equity_is_rejected_before_sizing(fake_clock_cls):
    risk = RiskManagerImpl(_risk_cfg(), capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=float("nan"))
    order = risk.approve(_entry(0.1), ctx)
    assert order is None
    assert risk.last_block
    assert "자산" in risk.last_block


def test_overflowed_quantity_is_rejected_by_final_sanity_guard(fake_clock_cls):
    """price가 극단적으로 작아 qty=budget/price 계산이 float64 범위를 넘어 inf가 되는
    경우 — 앞단의 개별 체크(`qty <= 0`)는 inf를 통과시키므로, 반드시 최종 sanity 가드가
    잡아야 한다. max_order_notional_pct=0으로 꺼서(inf 명목가가 그 체크에서 먼저 걸리는
    것을 피하고) 최종 가드 자체를 격리해서 검증한다."""
    risk = RiskManagerImpl(
        _risk_cfg(max_position_pct=100, max_symbol_pct_total=0, max_order_notional_pct=0),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    ctx = _ctx(fake_clock_cls, price=1e-320, cash=10_000_000.0)
    order = risk.approve(_entry(1.0), ctx)
    assert order is None
    assert "수량 계산 이상" in risk.last_block


def test_zero_budget_is_rejected_with_reason_logged(fake_clock_cls):
    risk = RiskManagerImpl(
        _risk_cfg(max_position_pct=50, max_symbol_pct_total=0),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    # 기존 포지션이 이미 max_position_pct 캡을 정확히 다 채운 상태 -> 남은 room=0 -> qty=0.
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=50_000.0, avg_cost=100.0)}
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, positions=positions)
    order = risk.approve(_entry(1.0), ctx)
    assert order is None
    assert risk.last_block


def test_negative_exit_fraction_produces_negative_qty_rejected_by_sanity_guard(fake_clock_cls):
    """전략이 오작동해 exit_fraction이 음수인 기형 Signal을 냈다고 가정한다. 이건
    '유효한 청산을 막는' 회로차단기가 아니라 애초에 말이 안 되는(음수) 수량을 브로커로
    보내지 않는 최종 방어선이다 — "청산은 절대 막지 않는다" 원칙과 충돌하지 않는다:
    막는 대상은 청산 자체가 아니라 청산 신호가 만들어낸 잘못된 계산 결과다."""
    risk = RiskManagerImpl(_risk_cfg(), capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF)
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=100.0, avg_cost=100.0)}
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, positions=positions)
    order = risk.approve(_exit(exit_fraction=-1.0), ctx)
    assert order is None
    assert "수량 계산 이상" in risk.last_block


# ======================================================================= breaker_state

def test_breaker_state_reports_accurate_counts_and_trip_flags(fake_clock_cls):
    risk = RiskManagerImpl(
        _risk_cfg(max_orders_per_day=3, daily_loss_limit_pct=3, cooldown_bars_after_stop=2),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    # weight=0.02: 정수 수량 사이징에서 qty=1을 확보하는 최소 배율(위 테스트들 참고).
    assert risk.approve(_entry(0.02), ctx) is not None
    state = risk.breaker_state()
    assert state["day"] == _DEFAULT_NOW.date().isoformat()
    assert state["max_orders_per_day"]["count"] == 1
    assert state["max_orders_per_day"]["limit"] == 3
    assert state["max_orders_per_day"]["tripped"] is False
    assert state["daily_loss_limit_pct"]["tripped"] is False
    assert state["daily_loss_limit_pct"]["day_pnl_pct"] == pytest.approx(0.0)
    assert state["cooldown_bars_after_stop"] == {"limit_bars": 2, "symbols_in_cooldown": []}

    # 같은 날 자산이 5% 감소(한도 3% 초과) -> 일일 손실 한도 트립. 이 주문은 거부되므로
    # max_orders_per_day 카운트는 늘지 않아야 한다(거부된 주문은 세지 않는다).
    ctx_loss = _ctx(fake_clock_cls, price=100.0, cash=9_500_000.0)
    blocked = risk.approve(_entry(0.02), ctx_loss)
    assert blocked is None
    state2 = risk.breaker_state()
    assert state2["daily_loss_limit_pct"]["tripped"] is True
    assert state2["max_orders_per_day"]["count"] == 1


def test_cooldown_does_not_carry_across_a_session_roll(fake_clock_cls):
    """쿨다운은 세션 안에서만 유효해야 한다.

    이 레일이 막으려는 것은 손절 직후의 휩소 재진입이고 그건 장중 현상이다. 야간에는
    봉이 생기지 않으므로 "N봉 경과"가 다음 날 아침까지 채워지지 않는다 — 세션을 넘겨
    유지하면 장 마감 직전 손절이 **다음 날 진입을 통째로 차단**한다(5분봉 실측:
    15:50 손절 -> 다음 날 09:35 진입이 "3/4봉 경과"로 거부). 하루 1회 진입 전략에서는
    그 자체로 전략을 반쯤 꺼버린다.
    """
    from datetime import timedelta

    risk = RiskManagerImpl(
        _risk_cfg(cooldown_bars_after_stop=4, cooldown_bar_interval_minutes=15),
        capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF,
    )
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=50.0, avg_cost=100.0)}
    stop_data = _FakeData(price=97.5, now=_DEFAULT_NOW)
    stop_data.bars[_SYMBOL] = _bars(1, _DEFAULT_NOW)
    stop_ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=stop_data,
                       broker=_FakeBroker(10_000_000.0, positions))
    assert risk.approve(_exit(reason="손절: entry=100.00 stop=98.00 현재=97.50"), stop_ctx) is not None

    # 같은 거래일: 봉이 거의 안 지났으므로 여전히 차단돼야 한다.
    # weight=0.02: 정수 수량 사이징에서 qty=1을 확보하는 최소 배율(위 테스트들 참고).
    same_day = _FakeData(price=100.0, now=_DEFAULT_NOW)
    same_day.bars[_SYMBOL] = _bars(1, _DEFAULT_NOW)
    blocked = risk.approve(
        _entry(0.02),
        Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=same_day,
                broker=_FakeBroker(10_000_000.0, {})),
    )
    assert blocked is None and "쿨다운" in risk.last_block

    # 다음 거래일: 경과 봉 수는 여전히 적지만 세션이 바뀌었으므로 풀려야 한다.
    next_day = _DEFAULT_NOW + timedelta(days=1)
    nd_data = _FakeData(price=100.0, now=next_day)
    nd_data.bars[_SYMBOL] = _bars(1, next_day)
    approved = risk.approve(
        _entry(0.02),
        Context(clock=fake_clock_cls(now=next_day), data=nd_data,
                broker=_FakeBroker(10_000_000.0, {})),
    )
    assert approved is not None, f"세션이 바뀌었는데 쿨다운이 남아 있다: {risk.last_block}"
    assert _SYMBOL not in risk.breaker_state()["cooldown_bars_after_stop"]["symbols_in_cooldown"]


# ================================================= 포트폴리오 레벨 상한 (다종목)

def _multi_cfg(**over):
    cfg = _risk_cfg(**over)
    # 종목별 상한은 넉넉히 — 포트폴리오 레벨 레일만 시험한다.
    cfg["risk"]["max_position_pct"] = 50
    cfg["risk"]["max_symbol_pct_total"] = 0
    cfg["risk"]["max_order_notional_pct"] = 0
    return cfg


def test_max_concurrent_positions_blocks_new_symbol_but_not_existing(fake_clock_cls):
    """상위 100종목 구성에서는 신호가 한 번에 수십 개 온다.

    max_position_pct는 **종목마다** 적용되므로 그것만으로는 총노출을 못 막는다
    (신호 20개 x 50% = 1,000%). 동시 보유 종목 수 상한은 그와 다른 축이다.
    """
    risk = RiskManagerImpl(
        _multi_cfg(max_concurrent_positions=2),
        capital_fraction={"donchian": 1.0},
        market_of={"TQQQ": "US", "SQQQ": "US", "SOXL": "US"},
    )
    held = {
        "TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=100.0),
        "SQQQ": Position(symbol="SQQQ", qty=10.0, avg_cost=100.0),
    }
    data = _FakeData(price=100.0, now=_DEFAULT_NOW)
    ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=data,
                  broker=_FakeBroker(10_000_000.0, held))

    third = risk.approve(_entry(0.01, symbol="SOXL"), ctx)
    assert third is None and "동시 보유 종목 수 상한" in risk.last_block

    # 이미 보유 중인 종목의 추가 매수는 새 종목이 아니므로 이 레일에 걸리지 않는다.
    # weight=0.05: 정수 수량 사이징(QUICKREF:207)에서 qty>=1을 확보하는 배율 —
    # room(=max_position_pct 잔여)이 5,000,000원인데 0.01이면 budget=130,000원 ->
    # floor(qty)=0으로 거부된다.
    assert risk.approve(_entry(0.05, symbol="TQQQ"), ctx) is not None


def test_max_total_exposure_caps_the_portfolio_not_just_each_symbol(fake_clock_cls):
    risk = RiskManagerImpl(
        _multi_cfg(max_total_exposure_pct=100),
        capital_fraction={"donchian": 1.0},
        market_of={"TQQQ": "US", "SQQQ": "US", "SOXL": "US"},
    )
    # 자산 1,000만 중 이미 900만 노출(현금 100만).
    held = {
        "TQQQ": Position(symbol="TQQQ", qty=6000.0, avg_cost=1.0),
        "SQQQ": Position(symbol="SQQQ", qty=3000.0, avg_cost=1.0),
    }
    data = _FakeData(price=1.0, now=_DEFAULT_NOW)
    ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=data,
                  broker=_FakeBroker(1_000_000.0, held))

    order = risk.approve(_entry(0.5, symbol="SOXL"), ctx)
    assert order is not None, f"잔여 노출룸이 있는데 막혔다: {risk.last_block}"
    equity = 1_000_000.0 + 9_000_000.0
    assert order.qty * 1.0 * 1500.0 <= equity - 9_000_000.0 + 1.0, "총노출 상한을 넘겨 배분했다"


def test_total_exposure_cap_never_blocks_exits(fake_clock_cls):
    """총노출이 상한을 넘어도 청산은 반드시 통과해야 한다."""
    risk = RiskManagerImpl(
        _multi_cfg(max_total_exposure_pct=10, max_concurrent_positions=1),
        capital_fraction={"donchian": 1.0}, market_of={"TQQQ": "US"},
    )
    held = {"TQQQ": Position(symbol="TQQQ", qty=100.0, avg_cost=100.0)}
    data = _FakeData(price=100.0, now=_DEFAULT_NOW)
    ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=data,
                  broker=_FakeBroker(100.0, held))

    exit_order = risk.approve(_exit(reason="손절: 현재=90.00"), ctx)
    assert exit_order is not None and exit_order.side is Side.SELL


def test_portfolio_caps_off_by_default(fake_clock_cls):
    """기본값 0 = 비활성. 1~2종목 구성의 기존 동작이 바뀌면 안 된다."""
    risk = RiskManagerImpl(
        _multi_cfg(), capital_fraction={"donchian": 1.0},
        market_of={"TQQQ": "US", "SQQQ": "US", "SOXL": "US"},
    )
    held = {
        "TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=100.0),
        "SQQQ": Position(symbol="SQQQ", qty=10.0, avg_cost=100.0),
    }
    data = _FakeData(price=100.0, now=_DEFAULT_NOW)
    ctx = Context(clock=fake_clock_cls(now=_DEFAULT_NOW), data=data,
                  broker=_FakeBroker(10_000_000.0, held))
    # weight=0.05: 정수 수량 사이징에서 qty>=1을 확보하는 배율(위 테스트 참고).
    assert risk.approve(_entry(0.05, symbol="SOXL"), ctx) is not None


# =========================== daily_loss_limit_pct: marks(MTM 평가금액) — 감사 결함 1

# 리스크 감사 재현: daily-loss 브레이커의 equity 계산이 "신호의 대상 종목"만 현재가로
# 평가하고 나머지 보유 종목은 전부 평균단가로 근사했다 — 계좌가 실제로 -25%(포지션
# 2개가 각각 반토막)여도 day_pnl_pct가 0.0으로 나와 신규 진입이 그대로 승인됐다.
# marks(dict[symbol, price])를 approve()에 넘기면 그 종목들의 실제 시세로 평가된다.
_MTM_MARKET_OF = {**_MARKET_OF, "AAA": "US", "BBB": "US", "CCC": "US"}


def _mtm_positions() -> dict[str, Position]:
    return {
        "AAA": Position(symbol="AAA", qty=50.0, avg_cost=100.0),
        "BBB": Position(symbol="BBB", qty=50.0, avg_cost=100.0),
    }


def test_daily_loss_limit_reflects_unrealized_pnl_of_all_held_symbols_via_marks(fake_clock_cls):
    positions = _mtm_positions()
    risk = RiskManagerImpl(
        _risk_cfg(daily_loss_limit_pct=10), capital_fraction={"donchian": 1.0},
        market_of=_MTM_MARKET_OF, fx=FixedFxProvider(1.0),  # KRW=USD 1:1 -> 산술 검증 단순화
    )
    # 하루 첫 approve — day_start_equity = 현금 10,000 + AAA 5,000 + BBB 5,000 = 20,000.
    # 신호는 보유하지 않은 CCC에 대한 진입(신호 종목 자체의 현재가만으로는 AAA/BBB의
    # 실제 손익이 절대 드러나지 않는 경로를 정확히 재현하기 위함).
    ctx1 = _ctx(fake_clock_cls, price=100.0, cash=10_000.0, positions=positions)
    risk.approve(_entry(0.001, symbol="CCC"), ctx1, marks={"AAA": 100.0, "BBB": 100.0})
    assert risk.breaker_state()["daily_loss_limit_pct"]["day_pnl_pct"] == pytest.approx(0.0)

    # 같은 날 나중 사이클 — AAA/BBB 둘 다 반토막(실현손익 없음, 포지션 그대로).
    # equity = 10,000 + 50*50 + 50*50 = 15,000 -> day_pnl = -25%.
    ctx2 = _ctx(fake_clock_cls, price=100.0, cash=10_000.0, positions=positions)
    blocked = risk.approve(_entry(0.001, symbol="CCC"), ctx2, marks={"AAA": 50.0, "BBB": 50.0})
    assert blocked is None
    assert "일일 손실 한도" in risk.last_block
    state = risk.breaker_state()["daily_loss_limit_pct"]
    assert state["day_pnl_pct"] == pytest.approx(-25.0)  # breaker_state는 %pt 단위(x100)
    assert state["tripped"] is True


def test_daily_loss_limit_without_marks_falls_back_to_avg_cost_old_behavior(fake_clock_cls):
    """marks를 아예 넘기지 않으면(백테스트 등 기존 호출부) 이전 동작 그대로 전부
    평균단가로 근사한다 — 위 테스트와의 대조군으로, 이 결함이 marks 부재 때문에
    생겼다는 것을 보여준다."""
    positions = _mtm_positions()
    risk = RiskManagerImpl(
        _risk_cfg(daily_loss_limit_pct=10), capital_fraction={"donchian": 1.0},
        market_of=_MTM_MARKET_OF, fx=FixedFxProvider(1.0),  # KRW=USD 1:1 -> 산술 검증 단순화
    )
    # weight=0.01(원래 0.001): 정수 수량 사이징(QUICKREF:207)에서 qty>=1을 확보하는
    # 배율 — equity=20,000, budget=weight*20,000이 100,000(1500x100... 여기선 fx=1.0
    # price=100이라 100/1주)을 넘어야 floor(qty)>=1이 된다.
    ctx1 = _ctx(fake_clock_cls, price=100.0, cash=10_000.0, positions=positions)
    risk.approve(_entry(0.01, symbol="CCC"), ctx1)  # marks=None

    ctx2 = _ctx(fake_clock_cls, price=100.0, cash=10_000.0, positions=positions)
    approved = risk.approve(_entry(0.01, symbol="CCC"), ctx2)  # marks=None -> AAA/BBB 평균단가 근사
    assert approved is not None
    assert risk.breaker_state()["daily_loss_limit_pct"]["day_pnl_pct"] == pytest.approx(0.0)


def test_daily_loss_limit_marks_degrade_per_symbol_when_a_mark_is_missing(fake_clock_cls):
    """marks에 없는 종목은 그 종목만 평균단가로 저하한다 — 하나가 빠졌다고 나머지
    시세까지 버리지 않는다(부분 실패가 전체를 무효화하지 않는다)."""
    positions = _mtm_positions()
    risk = RiskManagerImpl(
        _risk_cfg(daily_loss_limit_pct=10), capital_fraction={"donchian": 1.0},
        market_of=_MTM_MARKET_OF, fx=FixedFxProvider(1.0),  # KRW=USD 1:1 -> 산술 검증 단순화
    )
    ctx1 = _ctx(fake_clock_cls, price=100.0, cash=10_000.0, positions=positions)
    risk.approve(_entry(0.001, symbol="CCC"), ctx1, marks={"AAA": 100.0, "BBB": 100.0})

    # AAA만 시세 제공(반토막), BBB는 마크 누락 -> 평균단가(100)로 저하.
    # equity = 10,000 + 50*50(AAA) + 50*100(BBB avg_cost) = 17,500 -> day_pnl = -12.5%.
    ctx2 = _ctx(fake_clock_cls, price=100.0, cash=10_000.0, positions=positions)
    blocked = risk.approve(_entry(0.001, symbol="CCC"), ctx2, marks={"AAA": 50.0})
    assert blocked is None
    assert risk.breaker_state()["daily_loss_limit_pct"]["day_pnl_pct"] == pytest.approx(-12.5)  # %pt 단위


def test_daily_loss_limit_marks_never_block_exits(fake_clock_cls):
    """marks로 계산한 equity가 손실 한도를 트립시켜도 청산은 그대로 승인돼야 한다
    — daily_loss_limit_pct의 기존 원칙(청산은 절대 막지 않는다)은 marks 도입으로도
    바뀌지 않는다."""
    positions = _mtm_positions()
    risk = RiskManagerImpl(
        _risk_cfg(daily_loss_limit_pct=10), capital_fraction={"donchian": 1.0},
        market_of=_MTM_MARKET_OF, fx=FixedFxProvider(1.0),  # KRW=USD 1:1 -> 산술 검증 단순화
    )
    ctx1 = _ctx(fake_clock_cls, price=100.0, cash=10_000.0, positions=positions)
    risk.approve(_entry(0.001, symbol="CCC"), ctx1, marks={"AAA": 100.0, "BBB": 100.0})

    ctx2 = _ctx(fake_clock_cls, price=50.0, cash=10_000.0, positions=positions)
    order = risk.approve(_exit(symbol="AAA"), ctx2, marks={"AAA": 50.0, "BBB": 50.0})
    assert order is not None
    assert order.side is Side.SELL
    assert risk.breaker_state()["daily_loss_limit_pct"]["tripped"] is True


# ========================================================= 거래일 경계 (KST 08:00)

def test_daily_loss_limit_does_not_reset_at_calendar_midnight_during_us_session(fake_clock_cls):
    """US 세션은 KST 자정을 넘어 이어진다 — 달력 자정으로 한도를 리셋하면
    하루에 손실 한도를 두 번 쓰게 된다 (2026-08-11 사용자 정의: KR 개장~US 마감 = 하루).
    """
    KST = ZoneInfo("Asia/Seoul")
    rm = RiskManagerImpl(_risk_cfg(daily_loss_limit_pct=3), capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF)

    # 월요일 09:00 KST — 거래일 시작, 자산 10,000,000
    monday_open = datetime(2026, 8, 10, 9, 0, tzinfo=KST)
    assert rm.approve(_entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=10_000_000, now=monday_open)) is not None

    # 화요일 01:00 KST (= US 세션 한복판, 달력상 다음날) — 자산 3.5% 하락
    us_session = datetime(2026, 8, 11, 1, 0, tzinfo=KST)
    blocked = rm.approve(
        _entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=9_650_000, now=us_session)
    )
    assert blocked is None, "달력이 바뀌었어도 같은 거래일이므로 손실 한도가 살아 있어야 한다"

    # 화요일 09:00 KST — 새 거래일이므로 다시 진입 가능
    tuesday_open = datetime(2026, 8, 11, 9, 0, tzinfo=KST)
    assert rm.approve(
        _entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=9_650_000, now=tuesday_open)
    ) is not None, "새 거래일에는 한도가 리셋돼야 한다"


def test_max_orders_per_day_spans_the_us_session_past_midnight(fake_clock_cls):
    KST = ZoneInfo("Asia/Seoul")
    rm = RiskManagerImpl(_risk_cfg(max_orders_per_day=2), capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF)

    monday = datetime(2026, 8, 10, 10, 0, tzinfo=KST)
    for _ in range(2):
        assert rm.approve(_entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=10_000_000, now=monday)) is not None

    past_midnight = datetime(2026, 8, 11, 2, 0, tzinfo=KST)
    assert rm.approve(
        _entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=10_000_000, now=past_midnight)
    ) is None, "자정을 넘겨도 같은 거래일이면 주문 상한이 이어져야 한다"


# ========== 일일 리스크 상태 영속화 (2026-08-12 감사 A-3)

def test_daily_risk_state_survives_restart(fake_clock_cls, tmp_path):
    """재시작 한 번에 회로차단기가 전부 풀리던 문제. 재시작은 '나쁜 날'에 더 자주
    일어난다(자동 halt 후 재개, 배포) — 레일이 가장 필요한 순간에 풀렸다."""
    state = tmp_path / "risk_day.json"
    cfg = _risk_cfg(max_orders_per_day=2)

    rm = RiskManagerImpl(cfg, capital_fraction={"donchian": 1.0},
                         market_of=_MARKET_OF, state_path=state)
    for _ in range(2):
        assert rm.approve(_entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=10_000_000)) is not None
    assert state.exists(), "주문 승인 시 상태가 디스크에 남아야 한다"

    # 프로세스 재시작 시뮬레이션 — 같은 경로로 새 인스턴스
    rm2 = RiskManagerImpl(cfg, capital_fraction={"donchian": 1.0},
                          market_of=_MARKET_OF, state_path=state)
    assert rm2.approve(
        _entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=10_000_000)
    ) is None, "재시작해도 하루 주문 상한이 이어져야 한다"


def test_daily_loss_baseline_survives_restart(fake_clock_cls, tmp_path):
    """이미 -2.9% 난 날 재시작하면 손실 한도가 0%부터 다시 시작하던 문제."""
    state = tmp_path / "risk_day.json"
    cfg = _risk_cfg(daily_loss_limit_pct=3)

    rm = RiskManagerImpl(cfg, capital_fraction={"donchian": 1.0},
                         market_of=_MARKET_OF, state_path=state)
    assert rm.approve(_entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=10_000_000)) is not None

    # 재시작 후 자산이 3.5% 하락한 상태 — 기준자산이 복원됐다면 차단돼야 한다
    rm2 = RiskManagerImpl(cfg, capital_fraction={"donchian": 1.0},
                          market_of=_MARKET_OF, state_path=state)
    assert rm2.approve(
        _entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=9_650_000)
    ) is None, "재시작 전 그날 시작자산 기준으로 손실 한도가 이어져야 한다"


def test_new_trading_day_resets_persisted_state(fake_clock_cls, tmp_path):
    """영속화가 거래일 롤을 막으면 안 된다 — 새 거래일에는 정상 리셋."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
    state = tmp_path / "risk_day.json"
    cfg = _risk_cfg(max_orders_per_day=1)

    day1 = datetime(2026, 8, 12, 10, 0, tzinfo=KST)
    rm = RiskManagerImpl(cfg, capital_fraction={"donchian": 1.0},
                         market_of=_MARKET_OF, state_path=state)
    assert rm.approve(_entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=10_000_000, now=day1)) is not None
    assert rm.approve(_entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=10_000_000, now=day1)) is None

    day2 = datetime(2026, 8, 13, 10, 0, tzinfo=KST)
    rm2 = RiskManagerImpl(cfg, capital_fraction={"donchian": 1.0},
                          market_of=_MARKET_OF, state_path=state)
    assert rm2.approve(
        _entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=10_000_000, now=day2)
    ) is not None, "새 거래일에는 상한이 리셋돼야 한다"


def test_no_state_path_keeps_in_memory_behaviour(fake_clock_cls):
    """백테스트/단위테스트 경로(state_path=None)는 디스크를 건드리지 않는다."""
    rm = RiskManagerImpl(_risk_cfg(max_orders_per_day=1),
                         capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF)
    assert rm.state_path is None
    assert rm.approve(_entry(0.1), _ctx(fake_clock_cls, price=100.0, cash=10_000_000)) is not None


# ================================================ 시장별 일일 주문 상한 (2026-08-14)
#
# 실측 사고: KR 아침 세션을 시작하기도 전에 "오늘 주문 135건 / 상한 30건 — 한도 도달"
# 이 떠서 신규 진입이 통째로 막혀 있었다. 원인이 둘이었다.
#
#  (1) 카운터가 **시장 공용**이라 밤사이 US 세션이 쓴 주문이 KR 세션의 예산을 먹었다.
#      거래일 경계(KST 08:00)는 시장별로 이미 올바르다 — 문제는 경계가 아니라 지갑이
#      하나였다는 것이다.
#  (2) 상한은 **진입만** 막는데 카운터는 **청산도** 올렸다. 청산은 막히지 않으므로,
#      체결되지 않는 청산이 매 사이클 재승인되며 카운터를 무한히 밀어올린다(그날
#      9,047 사이클). 즉 상한이 스스로를 소진시켰다. 실측: 그날 체결 29건인데
#      카운터는 135건 — 약 106건이 승인됐지만 체결되지 않았다.
#
# (2)를 "청산을 세지 않는다"로 고치지 않는다 — 청산 카운트는 폭주 가시성으로 **의도된**
# 설계다(이 파일 상단 주석 참고). 두 숫자를 분리한다: 진입 예산은 진입만 세고,
# 총 주문 수는 계속 보이되 아무것도 막지 않는다.

_KR_SYMBOL = "005930"
_CROSS_MARKET_OF = {**_MARKET_OF, _KR_SYMBOL: "KR"}


def _cross_risk(**overrides):
    return RiskManagerImpl(_risk_cfg(**overrides), capital_fraction={"donchian": 1.0},
                           market_of=_CROSS_MARKET_OF)


def test_us_orders_do_not_consume_the_kr_daily_budget(fake_clock_cls):
    """**이 테스트가 이 변경의 이유다.** 밤사이 US 세션이 KR 아침 예산을 먹으면 안 된다."""
    risk = _cross_risk(max_orders_per_day=2)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    assert risk.approve(_entry(0.02), ctx) is not None          # US 1
    assert risk.approve(_entry(0.02), ctx) is not None          # US 2 — US 상한 도달
    assert risk.approve(_entry(0.02), ctx) is None

    # KR 은 아직 한 건도 안 썼다.
    assert risk.approve(_entry(0.02, symbol=_KR_SYMBOL), ctx) is not None
    assert risk.approve(_entry(0.02, symbol=_KR_SYMBOL), ctx) is not None
    assert risk.approve(_entry(0.02, symbol=_KR_SYMBOL), ctx) is None


def test_exits_do_not_consume_the_entry_budget(fake_clock_cls):
    """청산이 진입 예산을 먹으면 상한이 스스로를 소진한다 — 135건 사고의 메커니즘이다."""
    risk = _cross_risk(max_orders_per_day=2)
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=10.0, avg_cost=100.0)}
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, positions=positions)

    for _ in range(5):
        assert risk.approve(_exit(), ctx) is not None   # 청산은 언제나 통과한다

    # 청산 5건을 냈어도 진입 예산은 그대로 2건이다.
    assert risk.approve(_entry(0.02), ctx) is not None
    assert risk.approve(_entry(0.02), ctx) is not None
    assert risk.approve(_entry(0.02), ctx) is None


def test_breaker_state_reports_per_market_and_keeps_total_visible(fake_clock_cls):
    """총 주문 수는 계속 보여야 한다 — 폭주 가시성이 원래 설계 의도였다."""
    risk = _cross_risk(max_orders_per_day=5)
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=10.0, avg_cost=100.0)}
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, positions=positions)

    risk.approve(_entry(0.02), ctx)                      # US 진입 1
    risk.approve(_exit(), ctx)                           # US 청산 (예산 미소모)
    risk.approve(_entry(0.02, symbol=_KR_SYMBOL), ctx)   # KR 진입 1

    state = risk.breaker_state()["max_orders_per_day"]
    assert state["limit"] == 5
    assert state["by_market"]["US"]["entries"] == 1
    assert state["by_market"]["KR"]["entries"] == 1
    assert state["by_market"]["US"]["orders"] == 2      # 진입+청산 둘 다 보인다
    assert state["tripped"] is False


def test_one_market_tripping_does_not_trip_the_other(fake_clock_cls):
    risk = _cross_risk(max_orders_per_day=1)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    risk.approve(_entry(0.02), ctx)                       # US 소진
    assert risk.approve(_entry(0.02), ctx) is None

    state = risk.breaker_state()["max_orders_per_day"]
    assert state["by_market"]["US"]["tripped"] is True
    assert state["by_market"].get("KR", {}).get("tripped", False) is False


def test_per_market_counts_survive_restart(tmp_path, fake_clock_cls):
    """재시작 한 번에 레일이 풀리면 안 된다(2026-08-12 감사 A-3와 같은 이유)."""
    path = tmp_path / "risk_day.json"
    risk = RiskManagerImpl(_risk_cfg(max_orders_per_day=1), capital_fraction={"donchian": 1.0},
                           market_of=_CROSS_MARKET_OF, state_path=path)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)
    assert risk.approve(_entry(0.02), ctx) is not None

    revived = RiskManagerImpl(_risk_cfg(max_orders_per_day=1), capital_fraction={"donchian": 1.0},
                              market_of=_CROSS_MARKET_OF, state_path=path)
    assert revived.approve(_entry(0.02), ctx) is None                       # US 는 소진된 채 복원
    assert revived.approve(_entry(0.02, symbol=_KR_SYMBOL), ctx) is not None  # KR 은 그대로


def test_legacy_scalar_state_file_is_migrated_not_crashed(tmp_path, fake_clock_cls):
    """구버전 상태 파일(스칼라 카운트)을 만나도 기동이 막히면 안 된다.

    복원 실패가 거래를 막는 건 더 나쁜 실패다 — 기존 `_load_day_state` 계약과 같다.
    구 카운트는 어느 시장 것인지 알 수 없으므로 **버린다**(진입 예산을 과소가 아니라
    과다로 잡는 쪽이 위험하지만, 하루치 상한이라 다음 롤에서 정상화된다).
    """
    import json as _json

    path = tmp_path / "risk_day.json"
    path.write_text(_json.dumps({"day": "2026-08-13", "day_order_count": 135,
                                 "day_start_equity": 10_000_000.0}), encoding="utf-8")

    risk = RiskManagerImpl(_risk_cfg(max_orders_per_day=2), capital_fraction={"donchian": 1.0},
                           market_of=_CROSS_MARKET_OF, state_path=path)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    assert risk.approve(_entry(0.02), ctx) is not None


def test_stale_yesterday_counts_are_not_displayed_as_today(fake_clock_cls):
    """**어제 카운트를 "오늘"로 보여주지 않는다.**

    롤오버는 approve() 안에서만 일어난다. 오늘 아직 신호가 없으면 어제 숫자가 그대로
    남아 있는데, 하트비트가 그걸 "오늘 주문 135건 / 상한 30건 — 한도 도달"로 표시해
    사용자가 진입이 막힌 줄 알았다(2026-08-14 실측: 상태 파일 day=2026-08-13,
    실제로는 오늘 첫 approve 에서 리셋된다).

    표시만 바로잡는다 — approve() 의 롤오버 의미는 건드리지 않는다.
    """
    risk = _cross_risk(max_orders_per_day=1)
    day1 = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, now=day1)
    risk.approve(_entry(0.02), ctx)
    assert risk.breaker_state(day1)["max_orders_per_day"]["tripped"] is True

    # 거래일이 바뀌었지만 아직 approve 가 없다 — 표시는 새 날 기준이어야 한다.
    day2 = datetime(2026, 1, 6, 10, 0, tzinfo=NY)
    state = risk.breaker_state(day2)["max_orders_per_day"]

    assert state["tripped"] is False
    assert state["count"] == 0
    assert state["by_market"] == {}


def test_breaker_state_without_now_keeps_old_behaviour(fake_clock_cls):
    """인자를 안 주면 예전 그대로 — 마지막 approve 시점 스냅샷이다."""
    risk = _cross_risk(max_orders_per_day=1)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)
    risk.approve(_entry(0.02), ctx)

    assert risk.breaker_state()["max_orders_per_day"]["tripped"] is True


# ============================== 일일 상한 해제 + 반복 진입 레일 (2026-08-14)
#
# 사용자 판단: "시그널이 많이 잡히는 날은 많이 불려야 한다. 하루 30으로 묶으면 벌 때
# 적게 벌고 잃을 때 적게 잃는 — 서버비만 내는 프로그램이 된다."
#
# 실측이 그 판단을 지지한다(4거래일): 시장별 하루 진입 최대 **9건** / 상한 30 —
# 이 상한은 정상 진입을 막은 적이 없다. 사용자가 본 "135건 한도 도달"은 청산까지
# 세던 뭉개진 카운터였고 그건 앞 커밋에서 고쳤다.
#
# 다만 이 레일이 원래 막으려던 건 하루 총량이 아니라 **폭주**였다(2026-08 사고:
# 브로커가 포지션 메타를 잃어 같은 종목을 10초마다 반복 주문). 총량 상한은 "바쁜 날"과
# "버그 난 루프"를 구분하지 못한다 — 그래서 모양을 바꾼다: 같은 (종목, 전략)이 짧은
# 창 안에서 반복 진입하는 것만 막는다. 다양한 종목이 바쁜 날은 절대 걸리지 않는다.

def test_daily_entry_cap_can_be_disabled(fake_clock_cls):
    """`max_orders_per_day: 0` 이면 하루 진입 총량은 제한하지 않는다."""
    risk = _cross_risk(max_orders_per_day=0, max_repeat_entries_per_window=0)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    approved = [risk.approve(_entry(0.02, symbol=f"{i:06d}"), ctx) for i in range(50)]

    assert all(o is not None for o in approved)
    assert risk.breaker_state()["max_orders_per_day"]["tripped"] is False


def test_repeat_entry_rail_stops_the_runaway_signature(fake_clock_cls):
    """폭주의 실제 모양: **같은 종목·같은 전략**이 폴링마다 재발동한다."""
    risk = _cross_risk(max_orders_per_day=0, max_repeat_entries_per_window=3,
                       repeat_entry_window_minutes=5)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)
    same = _entry(0.02)

    approved = [risk.approve(same, ctx) for _ in range(3)]
    assert all(o is not None for o in approved)

    blocked = [risk.approve(same, ctx) for _ in range(50)]
    assert all(o is None for o in blocked)
    assert "반복 진입" in risk.last_block


def test_repeat_rail_does_not_bind_a_busy_day_across_symbols(fake_clock_cls):
    """**다양한 종목이 바쁜 날은 걸리지 않는다** — 그게 총량 상한과 다른 점이다."""
    risk = _cross_risk(max_orders_per_day=0, max_repeat_entries_per_window=3,
                       repeat_entry_window_minutes=5)
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0)

    approved = [risk.approve(_entry(0.02, symbol=f"{i:06d}"), ctx) for i in range(40)]

    assert all(o is not None for o in approved)


def test_repeat_rail_forgets_after_the_window(fake_clock_cls):
    """창이 지나면 다시 열린다 — 하루를 통째로 막는 레일이 아니다."""
    risk = _cross_risk(max_orders_per_day=0, max_repeat_entries_per_window=2,
                       repeat_entry_window_minutes=5)
    t0 = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
    ctx0 = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, now=t0)
    same = _entry(0.02)

    assert risk.approve(same, ctx0) is not None
    assert risk.approve(same, ctx0) is not None
    assert risk.approve(same, ctx0) is None

    later = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0,
                 now=t0 + timedelta(minutes=6))
    assert risk.approve(same, later) is not None


def test_repeat_rail_never_blocks_exits(fake_clock_cls):
    """어떤 회로차단기도 청산은 막지 않는다 — 이 저장소의 불변식."""
    risk = _cross_risk(max_orders_per_day=0, max_repeat_entries_per_window=1)
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=100.0, avg_cost=100.0)}
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, positions=positions)

    for _ in range(20):
        assert risk.approve(_exit(exit_fraction=0.01), ctx) is not None
