"""OpeningRangeBreakoutStrategy 단위 테스트.

검증 대상은 "합리적으로 보이는 동작"이 아니라 **논문에 적힌 규칙**이다
(Zarattini & Aziz, SSRN 4416622 Table 1 / Zarattini·Barbon·Aziz, SSRN 4729284 §4).
각 테스트는 어느 규칙을 지키는지 주석으로 명시한다.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.ports import Context
from quant.core.models import Position, Quote, SignalAction
from quant.trade.strategy.orb import OpeningRangeBreakoutStrategy

NY = ZoneInfo("America/New_York")

DAY1 = date(2026, 1, 5)
DAY2 = date(2026, 1, 6)


class FakeClock:
    """now()는 반드시 통제한다 — 전략이 "두 번째 봉" 진입 창을 시계로 판정하기 때문.
    기본값은 DAY1 09:35(5분 OR이 막 마감된 순간) = 논문의 진입 시점."""

    def __init__(self, minutes_to_close: float | None = 300.0, cadence_minutes: float = 5.0,
                 now: datetime | None = None):
        self._minutes_to_close = minutes_to_close
        self._cadence = cadence_minutes
        self._now = now or datetime.combine(DAY1, dtime(9, 35), tzinfo=NY)

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return self._minutes_to_close

    def cadence_minutes(self) -> float:
        return self._cadence

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        mtc = self.minutes_to_close(market)
        return mtc is not None and mtc - self._cadence < flatten_minutes


class FakeDataFeed:
    """interval별로 다른 봉을 돌려준다 — 전략이 분봉(방향)과 일봉(ATR)을 모두 읽기 때문."""

    def __init__(self, bars_by_interval: dict[str, pd.DataFrame], quotes: dict[str, float]):
        self._bars = bars_by_interval
        self._quotes = quotes

    def quote(self, symbol: str) -> Quote | None:
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(NY), price=self._quotes[symbol])

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        df = self._bars.get(interval)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df.tail(n)


class FakeBroker:
    def __init__(self, positions: dict[str, Position]):
        self._positions = positions

    def place_order(self, order):
        raise NotImplementedError

    def positions(self) -> dict[str, Position]:
        return self._positions

    def cash(self) -> float:
        return 1_000_000.0


def _intraday(
    day: date,
    rows: list[tuple[float, float, float, float]],
    interval_minutes: int = 5,
    volume: float = 1000.0,
) -> list[dict]:
    """rows: (open, high, low, close). 09:30부터 interval_minutes 간격으로 배치."""
    ts0 = datetime.combine(day, dtime(9, 30), tzinfo=NY)
    return [
        {
            "ts": ts0 + timedelta(minutes=interval_minutes * i),
            "open": o, "high": h, "low": l, "close": c, "volume": volume,
        }
        for i, (o, h, l, c) in enumerate(rows)
    ]


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows).set_index("ts")


def _params(**overrides) -> dict:
    p = dict(
        long_symbol="TQQQ", inverse_symbol="SQQQ", signal_symbol="TQQQ",
        bar_interval_minutes=5, opening_range_minutes=5,
        stop_mode="or_extreme", profit_target_r=None,
        risk_budget_pct=1.0, max_leverage=4.0, flatten_before_close_minutes=5,
    )
    p.update(overrides)
    return p


def _ctx(bars, quotes, positions=None, minutes_to_close=300.0, daily=None, now=None) -> Context:
    by_interval = {"5m": bars} if isinstance(bars, pd.DataFrame) else dict(bars)
    if daily is not None:
        by_interval["1d"] = daily
    return Context(
        clock=FakeClock(minutes_to_close, now=now),
        data=FakeDataFeed(by_interval, quotes),
        broker=FakeBroker(positions or {}),
    )


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, dtime(hour, minute), tzinfo=NY)


def _strat(**overrides) -> OpeningRangeBreakoutStrategy:
    return OpeningRangeBreakoutStrategy(
        symbols=["TQQQ", "SQQQ"], params=_params(**overrides), market="US"
    )


# --- 방향 규칙: OR 봉의 부호 (논문 4416622 Table 1 "Entry") ------------------


def test_bullish_or_candle_enters_long_symbol():
    # OR 봉이 양봉(100.0 -> 100.6) -> TQQQ 매수. 돌파를 기다리지 않는다.
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    signals = _strat().on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}))

    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG
    assert signals[0].symbol == "TQQQ"


def test_bearish_or_candle_enters_inverse_symbol():
    # OR 봉이 음봉(100.0 -> 99.4) -> 논문은 숏. 우리는 SQQQ 매수로 방향을 표현한다.
    bars = _df(_intraday(DAY1, [(100.0, 100.5, 99.2, 99.4)]))
    signals = _strat().on_cycle(_ctx(bars, {"TQQQ": 99.4, "SQQQ": 40.0}))

    assert len(signals) == 1
    assert signals[0].symbol == "SQQQ"


def test_doji_or_candle_produces_no_entry():
    # open ~ close -> 논문 명시: "No positions were opened ... doji".
    bars = _df(_intraday(DAY1, [(100.0, 100.5, 99.5, 100.005)]))
    strat = _strat()
    assert strat.on_cycle(_ctx(bars, {"TQQQ": 100.005, "SQQQ": 40.0})) == []
    assert "도지" in strat.last_reject


def test_entry_window_is_the_second_candle_only():
    """논문의 진입 시점은 "두 번째 봉의 시가" 단 한 번.

    진입 창은 두 번째 봉이 열려 있는 동안(09:35~09:40)만 유효하다. 창 안에서는
    폴링 타이밍이 봉 마감과 정확히 맞지 않아도 진입하고(라이브 10초 폴링 대비),
    창이 지나면 지나간 기회를 쫓지 않는다.
    """
    or_only = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    quotes = {"TQQQ": 100.6, "SQQQ": 40.0}

    # 창 안 — 봉 마감 직후, 그리고 3분 늦은 폴링 모두 진입한다.
    assert len(_strat().on_cycle(_ctx(or_only, quotes, now=_at(DAY1, 9, 35)))) == 1
    assert len(_strat().on_cycle(_ctx(or_only, quotes, now=_at(DAY1, 9, 38)))) == 1

    # 창 이전 — OR이 아직 마감되지 않았다.
    assert _strat().on_cycle(_ctx(or_only, quotes, now=_at(DAY1, 9, 34))) == []

    # 창 이후 — 세 번째 봉이 시작됐으므로 진입하지 않는다.
    later = _df(_intraday(DAY1, [
        (100.0, 100.8, 99.5, 100.6),
        (100.6, 101.5, 100.4, 101.2),
    ]))
    assert _strat().on_cycle(_ctx(later, quotes, now=_at(DAY1, 9, 42))) == []


def test_no_entry_before_opening_range_completes():
    # opening_range_minutes=10(5분봉 2개)인데 시계는 09:35 -> 아직 OR 미완성.
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    strat = _strat(opening_range_minutes=10)
    assert strat.on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0})) == []


def test_missing_bars_inside_entry_window_blocks_entry():
    # 진입 창(10분 OR -> 09:40)인데 완성봉이 1개뿐 = 데이터 결손. 진입하지 않는다.
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    strat = _strat(opening_range_minutes=10)
    assert strat.on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}, now=_at(DAY1, 9, 40))) == []
    assert "OR 봉 수 불일치" in strat.last_reject


def test_multi_bar_opening_range_aggregates_extremes():
    # OR = 5분봉 2개(=10분). 방향은 첫 봉 시가 대비 마지막 봉 종가, 고저는 두 봉 전체.
    bars = _df(_intraday(DAY1, [
        (100.0, 100.4, 99.8, 100.1),
        (100.1, 101.0, 99.0, 100.9),
    ]))
    strat = _strat(opening_range_minutes=10)
    [signal] = strat.on_cycle(
        _ctx(bars, {"TQQQ": 100.9, "SQQQ": 40.0}, now=_at(DAY1, 9, 40))
    )

    assert signal.symbol == "TQQQ"
    # 손절은 OR 전체 저가(99.0) 기준 — 첫 봉 저가(99.8)가 아니다.
    expected_risk_pct = (100.9 - 99.0) / 100.9
    assert signal.stop == pytest.approx(100.9 * (1 - expected_risk_pct))


# --- 손절 규칙 (논문 4416622 Table 1 "Stop loss") ----------------------------


def test_or_extreme_stop_long_uses_opening_range_low():
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    [signal] = _strat().on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}))

    # 롱: $R = 진입가 - 첫 봉 저가. 진입 종목이 TQQQ 자신이므로 손절가는 그대로 99.5.
    assert signal.stop == pytest.approx(99.5)


def test_or_extreme_stop_short_uses_high_converted_to_inverse_price():
    # 음봉 -> SQQQ 매수. 논문의 손절(첫 봉 고가)은 TQQQ 기준이므로 **비율**로 옮긴다.
    bars = _df(_intraday(DAY1, [(100.0, 100.5, 99.2, 99.4)]))
    [signal] = _strat().on_cycle(_ctx(bars, {"TQQQ": 99.4, "SQQQ": 40.0}))

    risk_pct = (100.5 - 99.4) / 99.4
    assert signal.stop == pytest.approx(40.0 * (1 - risk_pct))


def test_atr_pct_stop_uses_daily_bars_not_intraday():
    """개선판 규격: 손절 = 14일 **일봉** ATR의 5%.

    일봉을 주면 그 값으로 계산되고, 분봉만 있고 일봉이 없으면 진입하지 않는다 —
    조용히 분봉 ATR로 대체하면 손절 거리가 자릿수 단위로 달라진다.
    """
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    # 일봉 3개로 atr_period=2인 결정론적 ATR을 만든다.
    daily_rows = [
        {"ts": datetime(2026, 1, 2, tzinfo=NY), "open": 96.0, "high": 98.0, "low": 94.0,
         "close": 97.0, "volume": 1.0},
        {"ts": datetime(2026, 1, 3, tzinfo=NY), "open": 97.0, "high": 101.0, "low": 96.0,
         "close": 100.0, "volume": 1.0},
        {"ts": datetime(2026, 1, 4, tzinfo=NY), "open": 100.0, "high": 102.0, "low": 99.0,
         "close": 101.0, "volume": 1.0},
    ]
    daily = _df(daily_rows)

    strat = _strat(stop_mode="atr_pct", atr_period=2, atr_stop_mult=0.05)
    [signal] = strat.on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}, daily=daily))

    high, low, close = daily["high"], daily["low"], daily["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = float(tr.dropna().tail(2).mean())
    risk_pct = (0.05 * atr) / 100.6
    assert signal.stop == pytest.approx(100.6 * (1 - risk_pct))

    # 일봉이 없으면 진입 자체를 포기한다(분봉으로 대체하지 않는다).
    strat2 = _strat(stop_mode="atr_pct", atr_period=2, atr_stop_mult=0.05)
    assert strat2.on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0})) == []
    assert "ATR 계산 불가" in strat2.last_reject


# --- 사이징 규칙 (논문 4416622: Shares = int(min(A*0.01/$R, 4*A/P))) ---------


def test_position_size_risks_the_budget_percent_of_capital():
    # $R = 100.6-99.5 = 1.1 -> R_pct = 1.093%. 리스크 예산 1% -> 비중 0.915배.
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    [signal] = _strat().on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}))

    risk_pct = (100.6 - 99.5) / 100.6
    assert signal.target_weight == pytest.approx(0.01 / risk_pct)
    # 손절까지 가면 정확히 자본의 1%를 잃는 크기여야 한다.
    assert signal.target_weight * risk_pct == pytest.approx(0.01)


def test_position_size_capped_by_max_leverage():
    # 손절이 매우 타이트하면 공식상 비중이 커지지만 레버리지 상한에서 잘려야 한다.
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 100.5, 100.6)]))  # $R = 0.1 -> R_pct 0.099%
    [signal] = _strat(max_leverage=1.0).on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}))

    assert signal.target_weight == pytest.approx(1.0)


# --- 목표가 (논문 기본 10R / 개선판 없음) -----------------------------------


def test_profit_target_r_multiple():
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    [signal] = _strat(profit_target_r=10.0).on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}))

    assert signal.target == pytest.approx(100.6 + 10.0 * (100.6 - 99.5))


def test_no_profit_target_by_default():
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    [signal] = _strat().on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}))
    assert signal.target is None


# --- Relative Volume 필터 (논문 4729284 §4) ---------------------------------


def _sessions_with_or_volume(volumes: list[float]) -> tuple[pd.DataFrame, datetime]:
    """세션당 5분봉 1개씩, 지정한 OR 거래량을 갖는 연속 거래일들.
    반환값의 두 번째는 마지막 세션의 진입 창 시각(09:35)."""
    rows: list[dict] = []
    day = DAY1
    for vol in volumes:
        rows.extend(_intraday(day, [(100.0, 100.8, 99.5, 100.6)], volume=vol))
        day += timedelta(days=1)
    last_day = day - timedelta(days=1)
    return _df(rows), _at(last_day, 9, 35)


def test_relative_volume_below_threshold_blocks_entry():
    # 직전 14세션 평균 1000, 오늘 500 -> RV 0.5 < 1.0 -> 진입 차단.
    bars, now = _sessions_with_or_volume([1000.0] * 14 + [500.0])
    strat = _strat(relative_volume_min=1.0)
    assert strat.on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}, now=now)) == []
    assert "Relative Volume 0.50" in strat.last_reject


def test_relative_volume_above_threshold_allows_entry():
    bars, now = _sessions_with_or_volume([1000.0] * 14 + [2500.0])
    signals = _strat(relative_volume_min=1.0).on_cycle(
        _ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}, now=now)
    )
    assert len(signals) == 1


def test_relative_volume_blocks_when_history_insufficient():
    # 과거 세션이 14개에 못 미치면 RV를 알 수 없다 — 모르는 채로 진입하지 않는다.
    bars, now = _sessions_with_or_volume([1000.0] * 5 + [2500.0])
    strat = _strat(relative_volume_min=1.0)
    assert strat.on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}, now=now)) == []
    assert "계산 불가" in strat.last_reject


def test_relative_volume_filter_off_by_default():
    # 필터를 끄면 과거 세션이 없어도 진입한다(기본값 None).
    bars, now = _sessions_with_or_volume([500.0])
    assert len(_strat().on_cycle(_ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}, now=now))) == 1


# --- 포지션 관리 -------------------------------------------------------------


def test_eod_flatten_fires_near_close():
    strat = _strat()
    pos = Position(symbol="TQQQ", qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 98.0, "target": None})
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    ctx = _ctx(bars, {"TQQQ": 101.0, "SQQQ": 40.0}, positions={"TQQQ": pos}, minutes_to_close=5.0)

    [signal] = strat.on_cycle(ctx)
    assert signal.action == SignalAction.EXIT_LONG
    assert "EoD 청산" in signal.reason


def test_session_roll_forces_exit_even_when_flatten_never_fires():
    """오버나잇 금지는 시각 계산과 무관하게 성립해야 한다.

    장 마감 시각에는 is_market_open이 False → minutes_to_close=None →
    should_flatten이 무조건 False다. 이 경로 하나에만 기대면 flatten 창이 통째로
    사라져 포지션이 며칠씩 살아남는다(실측: 10년 백테스트에서 청산 17건).
    여기서는 should_flatten이 절대 True가 되지 않는 상황(장 초반)에서도
    진입 세션이 지나면 청산되는지 본다.
    """
    strat = _strat()
    pos = Position(symbol="TQQQ", qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 90.0, "target": None,
                         "session": DAY1.isoformat()})
    bars = _df(_intraday(DAY2, [(100.0, 100.8, 99.5, 100.6)]))
    # 장 초반(10:30, 마감까지 330분) — 손절가에도 안 닿았고 flatten 창도 아니다.
    ctx = _ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}, positions={"TQQQ": pos},
               minutes_to_close=330.0, now=_at(DAY2, 10, 30))

    [signal] = strat.on_cycle(ctx)
    assert signal.action == SignalAction.EXIT_LONG
    assert "오버나잇 금지" in signal.reason


def test_same_session_position_is_not_force_exited():
    strat = _strat()
    pos = Position(symbol="TQQQ", qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 90.0, "target": None,
                         "session": DAY1.isoformat()})
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    ctx = _ctx(bars, {"TQQQ": 100.6, "SQQQ": 40.0}, positions={"TQQQ": pos},
               minutes_to_close=330.0, now=_at(DAY1, 10, 30))

    assert strat.on_cycle(ctx) == []


def test_stop_loss_exit_fires():
    strat = _strat()
    pos = Position(symbol="TQQQ", qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 98.0, "target": None})
    bars = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    ctx = _ctx(bars, {"TQQQ": 97.5, "SQQQ": 40.0}, positions={"TQQQ": pos})

    [signal] = strat.on_cycle(ctx)
    assert signal.action == SignalAction.EXIT_LONG
    assert "손절" in signal.reason


def test_entry_state_resets_across_session_roll():
    strat = _strat()
    bars_day1 = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)]))
    assert len(strat.on_cycle(_ctx(bars_day1, {"TQQQ": 100.6, "SQQQ": 40.0}))) == 1

    # 새 세션 -> 다시 진입 가능해야 한다.
    bars_day2 = _df(_intraday(DAY1, [(100.0, 100.8, 99.5, 100.6)])
                    + _intraday(DAY2, [(50.0, 50.8, 49.5, 50.6)]))
    signals = strat.on_cycle(
        _ctx(bars_day2, {"TQQQ": 50.6, "SQQQ": 40.0}, now=_at(DAY2, 9, 35))
    )
    assert len(signals) == 1
    assert signals[0].stop == pytest.approx(49.5)


def test_legs_never_held_simultaneously():
    strat = _strat()
    pos = Position(symbol="TQQQ", qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 90.0, "target": None})
    # 음봉이라 SQQQ 진입 후보지만 TQQQ가 이미 열려 있다.
    bars = _df(_intraday(DAY1, [(100.0, 100.5, 99.2, 99.4)]))
    ctx = _ctx(bars, {"TQQQ": 99.4, "SQQQ": 40.0}, positions={"TQQQ": pos})

    signals = strat.on_cycle(ctx)
    assert all(s.action != SignalAction.ENTER_LONG for s in signals)


# --- 설정 검증 ---------------------------------------------------------------


def test_non_multiple_opening_range_minutes_raises():
    with pytest.raises(ValueError):
        OpeningRangeBreakoutStrategy(
            symbols=["TQQQ", "SQQQ"],
            params=_params(bar_interval_minutes=5, opening_range_minutes=7),
            market="US",
        )


def test_unknown_stop_mode_raises():
    with pytest.raises(ValueError):
        OpeningRangeBreakoutStrategy(
            symbols=["TQQQ", "SQQQ"], params=_params(stop_mode="or_midline"), market="US"
        )
