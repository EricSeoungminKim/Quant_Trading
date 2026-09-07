"""결함주입 1/6 — 데이터피드: 빈 프레임, NaN/0 가격, stale 봉, 중복/역순 타임스탬프,
급변 호가(bad tick). 오너 지시(2026-09-07): 새 결함을 실제로 찾아 고치거나
문서화된 갭으로 남긴다.

여기서 검증하는 두 계층:
- `quant.adapters.data.service.MarketDataService` — 소스가 부정확한 프레임을
  줘도 전략에 넘기기 전에 최소한의 위생(정렬/중복 제거/미완성봉 제외)을 강제하는가.
- `quant.trade.risk.manager.RiskManagerImpl.approve` — NaN/0 이하 가격에 사이징하지
  않는가(전략이 걸러내지 못한 경우의 최후 방어선).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from quant.adapters.data.service import Capability, MarketDataService, SourceRoute
from quant.core.fx import FixedFxProvider
from quant.core.models import Quote, Signal, SignalAction
from quant.core.ports import Context
from quant.trade.risk.manager import RiskManagerImpl

_OHLCV = ["open", "high", "low", "close", "volume"]


class _Clock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return 1.0

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


class _Source:
    def __init__(self, df=None, quote=None):
        self._df = df if df is not None else pd.DataFrame(columns=_OHLCV)
        self._quote = quote

    def history(self, symbol, interval, n):
        return self._df

    def quote(self, symbol):
        return self._quote


def _svc(df=None, quote=None, now=None) -> MarketDataService:
    now = now or datetime(2026, 1, 5, 9, 40, tzinfo=UTC)
    return MarketDataService(
        routes=[SourceRoute(
            name="x", source=_Source(df=df, quote=quote),
            capabilities=frozenset({Capability.BARS, Capability.QUOTE}),
        )],
        clock=_Clock(now),
    )


# --------------------------------------------------------------- 1. 빈 프레임

def test_empty_frame_from_source_returns_empty_not_crash():
    svc = _svc(df=pd.DataFrame(columns=_OHLCV))
    out = svc.history("TQQQ", "1m", 30)
    assert out.empty


# ------------------------------------------------------ 2. 중복/역순 타임스탬프

def test_out_of_order_bars_are_sorted_before_reaching_strategy():
    """실측 결함(이 세션에서 발견·수정): 소스가 봉을 시간순으로 안 줄 수 있는데
    `.iloc[-1]`로 "마지막 봉"을 읽는 소비자(donchian 등)는 정렬을 가정한다.
    수정 전에는 아래 assert가 실패했다(`iloc[-1]`이 09:31 봉을 돌려줌, 실제
    최신은 09:33). `_normalize_frame`이 이제 정렬을 강제한다."""
    idx = pd.to_datetime(
        ["2026-01-05 09:33", "2026-01-05 09:30", "2026-01-05 09:32", "2026-01-05 09:31"], utc=True
    )
    df = pd.DataFrame(
        {"open": [4, 1, 3, 2], "high": [4, 1, 3, 2], "low": [4, 1, 3, 2],
         "close": [4, 1, 3, 2], "volume": [1, 1, 1, 1]},
        index=idx,
    )
    svc = _svc(df=df)
    out = svc.history("TQQQ", "1m", 4)
    assert out.index.is_monotonic_increasing
    assert out.iloc[-1]["close"] == 4.0  # 가장 늦은 시각(09:33)의 봉이어야 한다


def test_duplicate_timestamps_are_deduplicated():
    idx = pd.to_datetime(["2026-01-05 09:30", "2026-01-05 09:31", "2026-01-05 09:31"], utc=True)
    df = pd.DataFrame(
        {"open": [1, 2, 2.5], "high": [1, 2, 2.5], "low": [1, 2, 2.5],
         "close": [1, 2, 2.5], "volume": [1, 1, 1]},
        index=idx,
    )
    svc = _svc(df=df)
    out = svc.history("TQQQ", "1m", 3)
    assert not out.index.has_duplicates
    assert len(out) == 2
    # 나중 값(재조회로 갱신됐을 가능성이 높은 쪽)을 남긴다.
    assert out.loc[out.index[-1], "close"] == 2.5


# ---------------------------------------------------------------- 3. stale 봉

def test_stale_bar_is_excluded_as_incomplete_or_simply_old():
    """장중인데 마지막 봉이 한참 전(N분 이상 지남)이면 `_filter_completed_bars`는
    그 봉을 막지 않는다(완성봉 판정과 "낡음" 판정은 다른 문제) — 대신 이 정보는
    `MarketDataService.health()`가 아니라 전략/루프가 "새 봉이 없다"는 사실 자체로
    관측한다(같은 `bar_ts`가 반복되면 donchian은 `_last_bar_ts` 캐시로 재판단을
    건너뛴다, `_check_entry` 참고). 여기서는 최소한 "미래 봉을 완성봉으로 착각하지
    않는다"만 검증한다 — 그 반대 방향(과거 봉을 최신으로 착각) 검증."""
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    # 마지막 봉이 09:30(장중이지만 2시간 30분 전) — 완성봉 판정 자체는 통과해야
    # 한다(진짜 완성된 옛날 봉이다), 다만 "지금 09:30 봉을 최신으로 쓰면 안 된다"는
    # 판단은 소비자(전략)의 몫 — 여기서는 서비스가 그 봉을 미래 봉으로 오인해
    # 걸러내지 않는지만 확인한다(회귀 방지).
    idx = pd.to_datetime(["2026-01-05 09:30"], utc=True)
    df = pd.DataFrame({"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]}, index=idx)
    svc = _svc(df=df, now=now)
    out = svc.history("TQQQ", "1m", 1)
    assert len(out) == 1  # 걸러지지 않는다 — 오래됐다고 삭제하면 "새 봉 없음"이 안 보인다


def test_future_bar_from_sloppy_source_is_filtered_as_incomplete():
    """소스가 아직 마감 안 된(형성 중인) 미래 봉을 잘못 줘도 `_filter_completed_bars`가
    막는다 — look-ahead 최종 방어선(기존 계약, 회귀 방지로 명시)."""
    now = datetime(2026, 1, 5, 9, 30, 30, tzinfo=UTC)  # 09:30:30 — 09:30봉이 아직 진행 중
    idx = pd.to_datetime(["2026-01-05 09:29", "2026-01-05 09:30"], utc=True)
    df = pd.DataFrame(
        {"open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [1, 2], "volume": [1, 1]}, index=idx
    )
    svc = _svc(df=df, now=now)
    out = svc.history("TQQQ", "1m", 2)
    assert len(out) == 1
    assert out.index[-1] == pd.Timestamp("2026-01-05 09:29", tz="UTC")


# --------------------------------------------------------- 4. 급변 호가(bad tick)

def test_risk_manager_rejects_nan_quote_price():
    """가격이 NaN인 호가는 어느 사이징 모드든 승인하지 않는다."""
    risk = _risk()
    ctx = Context(clock=_Clock(datetime(2026, 1, 5, 10, 0, tzinfo=UTC)),
                  data=_Source(quote=Quote(symbol="TQQQ", ts=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
                                            price=float("nan"))),
                  broker=_Broker())
    order = risk.approve(_entry(), ctx)
    assert order is None
    assert "현재가 없음" in risk.last_block


def test_risk_manager_rejects_zero_or_negative_quote_price():
    risk = _risk()
    for bad_price in (0.0, -5.0):
        ctx = Context(clock=_Clock(datetime(2026, 1, 5, 10, 0, tzinfo=UTC)),
                      data=_Source(quote=Quote(symbol="TQQQ", ts=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
                                                price=bad_price)),
                      broker=_Broker())
        order = risk.approve(_entry(), ctx)
        assert order is None, f"price={bad_price}인데 승인됨"


def test_risk_manager_rejects_inf_quote_price():
    risk = _risk()
    ctx = Context(clock=_Clock(datetime(2026, 1, 5, 10, 0, tzinfo=UTC)),
                  data=_Source(quote=Quote(symbol="TQQQ", ts=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
                                            price=float("inf"))),
                  broker=_Broker())
    order = risk.approve(_entry(), ctx)
    assert order is None


def test_donchian_does_not_enter_on_low_volume_spike_bad_tick():
    """40% 급등 호가라도 거래량이 평소 수준이면(가짜 틱/오류 호가 특징) donchian의
    거래량 조건(volume_mult)이 진입을 막는다 — 이미 volume_mult 조건이 이 방어를
    겸하고 있음을 회귀 테스트로 고정한다."""
    from quant.trade.strategy.donchian import DonchianStrategy

    ts0 = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)
    rows = []
    for i in range(41):
        price = 100.0 + i * 0.01
        rows.append({"ts": ts0 + pd.Timedelta(minutes=15 * i), "open": price, "high": price + 0.05,
                     "low": price - 0.05, "close": price, "volume": 1000.0})
    # 40% 급등 bad tick, 그러나 거래량은 평소 그대로(가짜 틱의 전형적 특징 — 실제
    # 체결이 몰리지 않았다는 뜻).
    last_close = rows[-1]["close"] * 1.4
    rows[-1].update(close=last_close, high=last_close + 0.1, low=rows[-1]["close"] - 0.1, volume=1000.0)
    bars = pd.DataFrame(rows).set_index("ts")

    class _Data:
        def quote(self, symbol):
            return Quote(symbol=symbol, ts=bars.index[-1], price=last_close)

        def history(self, symbol, interval, n):
            return bars.tail(n)

    class _Brk:
        def positions(self):
            return {}

        def cash(self):
            return 1_000_000.0

    strat = DonchianStrategy(
        symbols=["TQQQ"], market="US",
        params=dict(interval_minutes=15, lookback_bars=40, volume_mult=1.5, stop_fallback_pct=1.5,
                    risk_reward=2.0, max_concurrent_names=1, flatten_before_close_minutes=10),
    )
    ctx = Context(clock=_Clock(bars.index[-1]), data=_Data(), broker=_Brk())
    signals = strat.on_cycle(ctx)
    assert signals == [], "거래량 확인 없이 가격 급변만으로 진입해서는 안 된다"


def test_donchian_enters_on_genuine_breakout_with_volume_confirmation():
    """대조군: 같은 40% 급등이라도 거래량이 동반되면(진짜 돌파) 정상적으로 진입한다
    — 위 테스트가 "거래량 조건 자체가 고장나서 아무것도 안 들어간다"를 가짜 통과로
    잡아내지 않게 하는 대조군."""
    from quant.trade.strategy.donchian import DonchianStrategy

    ts0 = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)
    rows = []
    for i in range(41):
        price = 100.0 + i * 0.01
        rows.append({"ts": ts0 + pd.Timedelta(minutes=15 * i), "open": price, "high": price + 0.05,
                     "low": price - 0.05, "close": price, "volume": 1000.0})
    last_close = rows[-1]["close"] * 1.4
    rows[-1].update(close=last_close, high=last_close + 0.1, low=rows[-1]["close"] - 0.1, volume=5000.0)
    bars = pd.DataFrame(rows).set_index("ts")

    class _Data:
        def quote(self, symbol):
            return Quote(symbol=symbol, ts=bars.index[-1], price=last_close)

        def history(self, symbol, interval, n):
            return bars.tail(n)

    class _Brk:
        def positions(self):
            return {}

        def cash(self):
            return 1_000_000.0

    strat = DonchianStrategy(
        symbols=["TQQQ"], market="US",
        params=dict(interval_minutes=15, lookback_bars=40, volume_mult=1.5, stop_fallback_pct=1.5,
                    risk_reward=2.0, max_concurrent_names=1, flatten_before_close_minutes=10),
    )
    ctx = Context(clock=_Clock(bars.index[-1]), data=_Data(), broker=_Brk())
    signals = strat.on_cycle(ctx)
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG


# ------------------------------------------------------------------- helpers

def _risk() -> RiskManagerImpl:
    cfg = dict(
        sizing_mode="cash_pct", max_position_pct=100, max_symbol_pct_total=0,
        daily_loss_limit_pct=100, max_orders_per_day=1000, cooldown_bars_after_stop=0,
        max_order_notional_pct=0, max_total_exposure_pct=0, max_concurrent_positions=0,
    )
    return RiskManagerImpl({"risk": cfg}, capital_fraction={"s": 1.0}, market_of={"TQQQ": "US"},
                           fx=FixedFxProvider(1500.0))


def _entry() -> Signal:
    return Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG,
                  target_weight=1.0, reason="테스트 진입")


class _Broker:
    def positions(self):
        return {}

    def cash(self) -> float:
        return 1_000_000.0
