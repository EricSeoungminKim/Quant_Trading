"""ConfluenceStrategy — 5축 합류 투표 전략 테스트.

지표 자체의 정확성(RSI Wilder 평활, MACD, 볼린저 등)은 test_indicators.py가
이미 검증한다. 여기서는 전략의 의사결정 로직만 본다:

- 합류 투표 집계/문턱값(`_confluence_votes`)은 지표 함수를 스텁으로 바꿔 각
  축을 독립적으로 켜고 끌 수 있게 한다 — 실제 가격으로 5축을 완전히 분리해
  재현하려면 축들이 서로 강하게 상관돼(추세 없이 돌파만 일어나는 경우가 거의
  없다) 비현실적으로 복잡한 합성 데이터가 필요해진다. 지표는 이미 검증됐으니
  전략 쪽에서는 "지표가 이런 값을 냈을 때 투표를 올바르게 세는가"만 본다.
- 진입 게이트(봉 슬롯/같은 봉 재판정 금지/사이징)는 `_confluence_votes`를
  monkeypatch로 고정해 지표 계산과 분리해서 검증한다.
- 청산(부분 익절/본전 이동/RSI·MACD 청산/EoD/오버나이트/손절)은 stop-hit·
  EoD·세션롤처럼 지표를 안 쓰는 경로는 실제 값으로, RSI/MACD를 쓰는 경로는
  마찬가지로 스텁으로 통제한다.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.ports import Context
from quant.core.models import Position, Quote, SignalAction
from quant.trade.strategy import confluence as conf_module
from quant.trade.strategy.confluence import ConfluenceStrategy

NY = ZoneInfo("America/New_York")
DAY1 = date(2026, 1, 5)


class FakeClock:
    def __init__(self, now, minutes_to_close=300.0):
        self._now = now
        self._minutes_to_close = minutes_to_close
        self._cadence = 5.0

    def now(self):
        return self._now

    def is_market_open(self, market):
        return True

    def minutes_to_close(self, market):
        return self._minutes_to_close

    def cadence_minutes(self):
        return self._cadence

    def should_flatten(self, market, flatten_minutes):
        return self._minutes_to_close - self._cadence < flatten_minutes


class FakeDataFeed:
    def __init__(self, bars, quotes):
        self._bars = bars
        self._quotes = quotes

    def quote(self, symbol):
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(NY), price=self._quotes[symbol])

    def history(self, symbol, interval, n):
        df = self._bars.get(symbol, {}).get(interval)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df.tail(n)


class FakeBroker:
    def __init__(self, positions=None):
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self):
        return 1_000_000.0


def _ctx(bars, quotes, now, positions=None, minutes_to_close=300.0):
    return Context(
        clock=FakeClock(now, minutes_to_close),
        data=FakeDataFeed(bars, quotes),
        broker=FakeBroker(positions),
    )


def _flat_bars(n, *, tz=NY, day=DAY1, open_time=dtime(9, 30), interval_minutes=5,
               base=100.0, wiggle=0.3):
    """진동 없는 평평한 봉 n개 — high/low만 조금 벌려 ATR>0을 보장한다.
    지표(추세/모멘텀/평균회귀/변동성/박스) 값은 실제로는 의미가 없어야 하는
    테스트(entry 게이트/청산 시각 로직)에서 쓴다 — 그런 테스트는 `_confluence_votes`
    나 `rsi`/`macd`를 스텁으로 바꿔 지표 계산과 분리한다."""
    idx = [datetime.combine(day, open_time, tzinfo=tz) + timedelta(minutes=interval_minutes * i)
           for i in range(n)]
    rows = [{"open": base, "high": base + wiggle, "low": base - wiggle, "close": base, "volume": 1000.0}
            for _ in range(n)]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=tz))


def _now_after(n, *, tz=NY, day=DAY1, open_time=dtime(9, 30), interval_minutes=5):
    """n개 봉이 방금 닫힌 직후 시각 — 봉 슬롯 게이트의 캐시 신선도 검사를
    통과하는 시점(intraday_scan과 동일한 산식: now = 세션시가 + n*간격)."""
    return datetime.combine(day, open_time, tzinfo=tz) + timedelta(minutes=interval_minutes * n)


def _neutral_series(index, value):
    return pd.Series([value] * len(index), index=index, dtype=float)


# ============================================================ 합류 투표 집계

def _patch_neutral_indicators(monkeypatch, bars):
    """5축을 전부 거짓으로 만드는 중립 스텁을 깔아 둔다 — 각 테스트는 원하는
    축만 덮어써서 그 축이 정말로 단독으로는 votes를 최소치까지 못 채우는지
    (또는 여러 축을 합쳐 채우는지) 확인한다."""
    idx = bars.index
    close_last = float(bars["close"].iloc[-1])

    state = {
        "sma": [_neutral_series(idx, close_last + 10), _neutral_series(idx, close_last + 20)],
        "macd": (_neutral_series(idx, 0.0), _neutral_series(idx, 0.0), _neutral_series(idx, 0.0)),
        "rsi": _neutral_series(idx, 50.0),
        "bollinger": (
            _neutral_series(idx, close_last), _neutral_series(idx, close_last + 1e6),
            _neutral_series(idx, close_last - 1e6), _neutral_series(idx, 0.0),
            _neutral_series(idx, 0.5),
        ),
        "squeeze": pd.Series([False] * len(idx), index=idx),
        "detect_box": (
            pd.Series([False] * len(idx), index=idx),
            _neutral_series(idx, float("nan")), _neutral_series(idx, float("nan")),
        ),
    }
    call_count = {"sma": 0}

    def fake_sma(_close, _period):
        out = state["sma"][call_count["sma"] % 2]
        call_count["sma"] += 1
        return out

    monkeypatch.setattr(conf_module, "sma", fake_sma)
    monkeypatch.setattr(conf_module, "macd", lambda *_a, **_k: state["macd"])
    monkeypatch.setattr(conf_module, "rsi", lambda *_a, **_k: state["rsi"])
    monkeypatch.setattr(conf_module, "bollinger", lambda *_a, **_k: state["bollinger"])
    monkeypatch.setattr(conf_module, "squeeze", lambda *_a, **_k: state["squeeze"])
    monkeypatch.setattr(conf_module, "detect_box", lambda *_a, **_k: state["detect_box"])
    return state


def test_votes_trend_axis_alone_is_insufficient(monkeypatch):
    bars = _flat_bars(10)
    s = ConfluenceStrategy(["TQQQ"], {})
    state = _patch_neutral_indicators(monkeypatch, bars)
    idx = bars.index
    close_last = float(bars["close"].iloc[-1])
    state["sma"] = [_neutral_series(idx, close_last - 1), _neutral_series(idx, close_last - 2)]

    votes, detail = s._confluence_votes(bars)
    assert votes == 1
    assert detail == "추세"
    assert votes < s.min_confluence


def test_votes_momentum_axis_alone_is_insufficient(monkeypatch):
    bars = _flat_bars(10)
    s = ConfluenceStrategy(["TQQQ"], {"cross_lookback": 3})
    state = _patch_neutral_indicators(monkeypatch, bars)
    idx = bars.index
    macd_line = _neutral_series(idx, -1.0)
    signal_line = _neutral_series(idx, 0.0)
    macd_line.iloc[-1] = 1.0  # 마지막 봉에서만 골든크로스
    state["macd"] = (macd_line, signal_line, macd_line - signal_line)

    votes, detail = s._confluence_votes(bars)
    assert votes == 1
    assert detail == "모멘텀"
    assert votes < s.min_confluence


def test_votes_mean_reversion_axis_alone_is_insufficient(monkeypatch):
    bars = _flat_bars(10)
    s = ConfluenceStrategy(["TQQQ"], {})
    state = _patch_neutral_indicators(monkeypatch, bars)
    idx = bars.index
    rsi_series = _neutral_series(idx, 50.0)
    rsi_series.iloc[-2] = 25.0
    rsi_series.iloc[-1] = 35.0  # 30 아래 -> 위 회복
    state["rsi"] = rsi_series

    votes, detail = s._confluence_votes(bars)
    assert votes == 1
    assert detail == "평균회귀반등"
    assert votes < s.min_confluence


def test_votes_volatility_axis_alone_is_insufficient(monkeypatch):
    bars = _flat_bars(10)
    s = ConfluenceStrategy(["TQQQ"], {})
    state = _patch_neutral_indicators(monkeypatch, bars)
    idx = bars.index
    close_last = float(bars["close"].iloc[-1])
    sq = pd.Series([False] * len(idx), index=idx)
    sq.iloc[-2] = True  # 직전 봉 스퀴즈
    state["squeeze"] = sq
    mid, _upper, lower, bw, pb = state["bollinger"]
    state["bollinger"] = (mid, _neutral_series(idx, close_last - 1), lower, bw, pb)  # 상단 돌파

    votes, detail = s._confluence_votes(bars)
    assert votes == 1
    assert detail == "변동성확장"
    assert votes < s.min_confluence


def test_votes_box_breakout_axis_alone_is_insufficient(monkeypatch):
    bars = _flat_bars(10).copy()
    bars.iloc[-1, bars.columns.get_loc("volume")] = 5000.0  # 거래량 게이트 통과
    s = ConfluenceStrategy(["TQQQ"], {"box_lookback": 5, "volume_mult": 1.5})
    state = _patch_neutral_indicators(monkeypatch, bars)
    idx = bars.index
    close_last = float(bars["close"].iloc[-1])
    is_box = pd.Series([False] * len(idx), index=idx)
    is_box.iloc[-2] = True
    box_high = _neutral_series(idx, float("nan"))
    box_high.iloc[-2] = close_last - 1
    state["detect_box"] = (is_box, box_high, state["detect_box"][2])

    votes, detail = s._confluence_votes(bars)
    assert votes == 1
    assert detail == "박스돌파"
    assert votes < s.min_confluence


def test_votes_three_axes_agree_reaches_default_threshold(monkeypatch):
    """추세 + 모멘텀 + 평균회귀반등 세 축이 동시에 참이면 기본 문턱(3)을 채운다."""
    bars = _flat_bars(10)
    s = ConfluenceStrategy(["TQQQ"], {})
    state = _patch_neutral_indicators(monkeypatch, bars)
    idx = bars.index
    close_last = float(bars["close"].iloc[-1])

    state["sma"] = [_neutral_series(idx, close_last - 1), _neutral_series(idx, close_last - 2)]
    rsi_series = _neutral_series(idx, 50.0)
    rsi_series.iloc[-2] = 25.0
    rsi_series.iloc[-1] = 35.0
    state["rsi"] = rsi_series
    macd_line = _neutral_series(idx, -1.0)
    signal_line = _neutral_series(idx, 0.0)
    macd_line.iloc[-1] = 1.0
    state["macd"] = (macd_line, signal_line, macd_line - signal_line)

    votes, detail = s._confluence_votes(bars)
    assert votes == 3
    assert votes >= s.min_confluence
    assert "추세" in detail and "모멘텀" in detail and "평균회귀반등" in detail


# ============================================================== 진입 게이트

def test_on_cycle_enters_when_votes_meet_threshold(monkeypatch):
    s = ConfluenceStrategy(["TQQQ"], {})
    n = s._lookback_bars
    bars = _flat_bars(n)
    now = _now_after(n)
    monkeypatch.setattr(s, "_confluence_votes", lambda _bars: (3, "추세+모멘텀+평균회귀반등"))

    ctx = _ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 102.5}, now)
    signals = s.on_cycle(ctx)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.ENTER_LONG
    assert sig.stop is not None and sig.stop < 102.5
    assert s._entries_today.get("TQQQ") == 1


def test_on_cycle_rejects_when_votes_below_threshold(monkeypatch):
    s = ConfluenceStrategy(["TQQQ"], {})
    n = s._lookback_bars
    bars = _flat_bars(n)
    now = _now_after(n)
    monkeypatch.setattr(s, "_confluence_votes", lambda _bars: (2, "추세+모멘텀"))

    ctx = _ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 102.5}, now)
    signals = s.on_cycle(ctx)
    assert signals == []
    assert "합류 부족" in s.last_reject["TQQQ"]


def test_same_bar_does_not_reenter_but_new_bar_can(monkeypatch):
    s = ConfluenceStrategy(["TQQQ"], {})
    n = s._lookback_bars
    monkeypatch.setattr(s, "_confluence_votes", lambda _bars: (5, "전체동의"))

    bars = _flat_bars(n)
    now = _now_after(n)
    assert len(s.on_cycle(_ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 102.5}, now))) == 1

    # 같은 봉 데이터로 재호출 — 재진입 없음
    assert s.on_cycle(_ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 102.5}, now)) == []

    # 새 봉 하나가 막 닫힌 시점 — 다시 진입 가능
    bars2 = _flat_bars(n + 1)
    now2 = _now_after(n + 1)
    assert len(s.on_cycle(_ctx({"TQQQ": {"5m": bars2}}, {"TQQQ": 102.5}, now2))) == 1


def test_stale_cache_does_not_consume_bar_slot(monkeypatch):
    s = ConfluenceStrategy(["TQQQ"], {})
    n = s._lookback_bars
    monkeypatch.setattr(s, "_confluence_votes", lambda _bars: (5, "전체동의"))
    bars = _flat_bars(n)
    # now가 n+1번째 봉을 기대하는 시점인데 데이터는 아직 n개까지만 — 슬롯 소비 없음
    now_expecting_more = _now_after(n + 1)
    assert s.on_cycle(_ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 102.5}, now_expecting_more)) == []
    assert "새 봉 데이터 대기" in s.last_reject["TQQQ"]


# ==================================================================== 소유권

def test_does_not_manage_a_position_opened_by_another_strategy():
    s = ConfluenceStrategy(symbols=["TQQQ"], params={})
    foreign = Position(symbol="TQQQ", qty=10, avg_cost=100.0,
                       meta={"strategy": "donchian", "stop": 90.0})
    assert s._owns(foreign) is False

    mine = Position(symbol="TQQQ", qty=10, avg_cost=100.0,
                    meta={"strategy": "confluence", "stop": 90.0})
    assert s._owns(mine) is True


def test_adopts_untagged_position_in_own_universe_only():
    s = ConfluenceStrategy(symbols=["TQQQ"], params={})
    assert s._owns(Position(symbol="TQQQ", qty=1, avg_cost=1.0, meta={})) is True
    assert s._owns(Position(symbol="SQQQ", qty=1, avg_cost=1.0, meta={})) is False


# ==================================================================== 관리/청산

def _lot_position(strategy_id, entry, stop, *, session=DAY1.isoformat(), scaled_out=False):
    pos = Position(symbol="TQQQ", qty=10, avg_cost=entry)
    pos.meta["lots"] = {
        strategy_id: {"entry": entry, "stop": stop, "r0": entry - stop,
                      "scaled_out": scaled_out, "session": session},
    }
    return pos


def test_stop_hit_exits_full():
    s = ConfluenceStrategy(["TQQQ"], {})
    n = s._lookback_bars
    bars = _flat_bars(n)
    now = _now_after(n)
    pos = _lot_position(s.id, entry=100.0, stop=98.0)
    ctx = _ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 97.5}, now, positions={"TQQQ": pos})

    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1 and exits[0].exit_fraction == 1.0
    assert "손절" in exits[0].reason


def test_eod_flatten_exits_full():
    s = ConfluenceStrategy(["TQQQ"], {"flatten_before_close_minutes": 10})
    n = s._lookback_bars
    bars = _flat_bars(n)
    now = _now_after(n)
    pos = _lot_position(s.id, entry=100.0, stop=98.0)
    ctx = _ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 101.0}, now, positions={"TQQQ": pos},
               minutes_to_close=8.0)  # flatten_minutes(10)보다 적게 남음

    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "EoD" in exits[0].reason


def test_session_rollover_forces_overnight_exit():
    s = ConfluenceStrategy(["TQQQ"], {})
    n = s._lookback_bars
    bars = _flat_bars(n)
    tomorrow = DAY1 + timedelta(days=1)
    now = _now_after(n, day=tomorrow)  # 진입은 DAY1인데 지금은 다음날
    pos = _lot_position(s.id, entry=100.0, stop=98.0, session=DAY1.isoformat())
    ctx = _ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 101.0}, now, positions={"TQQQ": pos})

    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1
    assert "오버나잇" in exits[0].reason


def test_scale_out_at_1_5r_moves_stop_to_breakeven(monkeypatch):
    s = ConfluenceStrategy(["TQQQ"], {"scale_out_at_r": 1.5, "scale_out_fraction": 0.3})
    n = s._lookback_bars
    bars = _flat_bars(n)
    now = _now_after(n)
    idx = bars.index
    # RSI/MACD를 중립으로 고정 — 그쪽 청산이 부분익절보다 먼저 발동하지 않게
    monkeypatch.setattr(conf_module, "rsi", lambda *_a, **_k: _neutral_series(idx, 50.0))
    monkeypatch.setattr(conf_module, "macd", lambda *_a, **_k: (
        _neutral_series(idx, 0.0), _neutral_series(idx, 0.0), _neutral_series(idx, 0.0)
    ))
    entry, stop = 100.0, 98.0  # r0 = 2.0 -> 1.5R = 103.0
    pos = _lot_position(s.id, entry, stop)
    ctx = _ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 103.0}, now, positions={"TQQQ": pos})

    signals = s.on_cycle(ctx)
    scale_outs = [x for x in signals if x.action == SignalAction.SCALE_OUT]
    assert len(scale_outs) == 1
    assert scale_outs[0].exit_fraction == pytest.approx(0.3)
    assert scale_outs[0].state_update == {"scaled_out": True, "stop": entry}

    # 발동분을 lot에 실제로 반영(run_cycle이 체결 후 하는 일을 흉내)한 뒤
    # 재호출하면 두 번째 부분익절은 나오지 않는다.
    pos.meta["lots"][s.id].update(scale_outs[0].state_update)
    ctx2 = _ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 103.0}, now, positions={"TQQQ": pos})
    signals2 = s.on_cycle(ctx2)
    assert [x for x in signals2 if x.action == SignalAction.SCALE_OUT] == []


def test_rsi_overbought_reversal_exits_full(monkeypatch):
    s = ConfluenceStrategy(["TQQQ"], {})
    n = s._lookback_bars
    bars = _flat_bars(n)
    now = _now_after(n)
    idx = bars.index
    rsi_series = _neutral_series(idx, 50.0)
    rsi_series.iloc[-2] = 75.0
    rsi_series.iloc[-1] = 65.0  # 70 위 -> 아래 이탈
    monkeypatch.setattr(conf_module, "rsi", lambda *_a, **_k: rsi_series)
    monkeypatch.setattr(conf_module, "macd", lambda *_a, **_k: (
        _neutral_series(idx, 0.0), _neutral_series(idx, 0.0), _neutral_series(idx, 0.0)
    ))
    pos = _lot_position(s.id, entry=100.0, stop=98.0)
    ctx = _ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 101.0}, now, positions={"TQQQ": pos})

    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1 and exits[0].exit_fraction == 1.0
    assert "RSI" in exits[0].reason


def test_macd_dead_cross_exits_full(monkeypatch):
    s = ConfluenceStrategy(["TQQQ"], {})
    n = s._lookback_bars
    bars = _flat_bars(n)
    now = _now_after(n)
    idx = bars.index
    monkeypatch.setattr(conf_module, "rsi", lambda *_a, **_k: _neutral_series(idx, 50.0))
    macd_line = _neutral_series(idx, 1.0)
    signal_line = _neutral_series(idx, 0.0)
    macd_line.iloc[-1] = -1.0  # 시그널선 하향 이탈
    monkeypatch.setattr(conf_module, "macd", lambda *_a, **_k: (
        macd_line, signal_line, macd_line - signal_line
    ))
    pos = _lot_position(s.id, entry=100.0, stop=98.0)
    ctx = _ctx({"TQQQ": {"5m": bars}}, {"TQQQ": 101.0}, now, positions={"TQQQ": pos})

    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1 and exits[0].exit_fraction == 1.0
    assert "MACD" in exits[0].reason
