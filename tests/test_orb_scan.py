"""OrbScanStrategy — 다종목·롱 온리 ORB 스캐너 테스트.

원본 orb.py와 규칙이 같은 부분(도지/손절/사이징 산식)은 test_orb.py가 원본을
검증한다. 여기서는 **스캐너가 원본과 다른 지점**을 고정한다:
종목별 독립 판정, 음봉 스킵(인버스 없음), 유니버스에서 빠진 포지션의 계속 관리.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.ports import Context
from quant.core.models import Position, Quote, SignalAction
from quant.trade.strategy.orb_scan import OrbScanStrategy

NY = ZoneInfo("America/New_York")
DAY1 = date(2026, 1, 5)
DAY2 = date(2026, 1, 6)


class FakeClock:
    def __init__(self, now: datetime | None = None, minutes_to_close: float = 300.0):
        self._now = now or datetime.combine(DAY1, dtime(9, 35), tzinfo=NY)
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
    """symbol -> interval -> DataFrame. history 호출 횟수를 센다(진입 창 최적화 검증)."""

    def __init__(self, bars: dict[str, dict[str, pd.DataFrame]], quotes: dict[str, float]):
        self._bars = bars
        self._quotes = quotes
        self.history_calls = 0

    def quote(self, symbol):
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(NY), price=self._quotes[symbol])

    def history(self, symbol, interval, n):
        self.history_calls += 1
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

    def place_order(self, order):
        raise NotImplementedError


def _or_bar(day: date, o: float, h: float, l: float, c: float) -> pd.DataFrame:
    ts = datetime.combine(day, dtime(9, 30), tzinfo=NY)
    return pd.DataFrame(
        {"open": [o], "high": [h], "low": [l], "close": [c], "volume": [1000.0]},
        index=pd.DatetimeIndex([ts]),
    )


def _params(**over) -> dict:
    p = dict(
        bar_interval_minutes=5, opening_range_minutes=5,
        doji_threshold_pct=0.50, stop_mode="or_extreme",
        risk_budget_pct=1.0, max_leverage=4.0, flatten_before_close_minutes=1,
    )
    p.update(over)
    return p


def _ctx(bars, quotes, positions=None, now=None, minutes_to_close=300.0) -> Context:
    return Context(
        clock=FakeClock(now=now, minutes_to_close=minutes_to_close),
        data=FakeDataFeed(bars, quotes),
        broker=FakeBroker(positions),
    )


BULL = _or_bar(DAY1, 100.0, 101.2, 99.8, 101.0)   # +1.0% 양봉
BEAR = _or_bar(DAY1, 100.0, 100.2, 98.8, 99.0)    # -1.0% 음봉
FLAT = _or_bar(DAY1, 100.0, 100.3, 99.7, 100.2)   # +0.2% < 도지 임계 0.5%


def test_bullish_symbols_enter_bearish_and_doji_skip():
    """스캐너의 핵심 차이: 음봉은 인버스 매수가 아니라 **스킵**이다."""
    strat = OrbScanStrategy(["AAA", "BBB", "CCC"], _params(), market="US")
    bars = {"AAA": {"5m": BULL}, "BBB": {"5m": BEAR}, "CCC": {"5m": FLAT}}
    signals = strat.on_cycle(_ctx(bars, {"AAA": 101.0, "BBB": 99.0, "CCC": 100.2}))

    assert [s.symbol for s in signals] == ["AAA"]
    assert signals[0].action == SignalAction.ENTER_LONG
    assert "양봉 아님" in strat.last_reject["BBB"]
    assert "양봉 아님" in strat.last_reject["CCC"]


def test_multiple_bullish_symbols_signal_independently():
    strat = OrbScanStrategy(["AAA", "BBB"], _params(), market="US")
    bull_b = _or_bar(DAY1, 50.0, 50.8, 49.9, 50.5)  # +1.0% 양봉, 다른 가격대
    bars = {"AAA": {"5m": BULL}, "BBB": {"5m": bull_b}}
    signals = strat.on_cycle(_ctx(bars, {"AAA": 101.0, "BBB": 50.5}))

    assert sorted(s.symbol for s in signals) == ["AAA", "BBB"]
    # 손절은 각자 자기 OR 저가 기준 — 서로 섞이면 안 된다.
    by_symbol = {s.symbol: s for s in signals}
    assert by_symbol["AAA"].stop == pytest.approx(99.8)
    assert by_symbol["BBB"].stop == pytest.approx(50.5 * (1 - (50.5 - 49.9) / 50.5))


def test_position_removed_from_watchlist_is_still_managed():
    """유니버스는 신규 진입 대상 선정이지 청산 대상 선정이 아니다.

    관심종목에서 뺀 종목(=self.symbols에 없음)의 열린 포지션도 손절/EoD 관리가
    계속돼야 한다 — 여기가 끊기면 /unwatch 한 번에 포지션이 고아가 된다.
    """
    strat = OrbScanStrategy(["AAA"], _params(), market="US")  # ZZZ는 유니버스에 없다
    # meta의 "strategy"는 _ensure_state가 진입 시 항상 남긴다 — 실제 포지션의 모양.
    # 소유권 판정(_owns)이 유니버스가 아니라 이 태그를 보기 때문에, 관심종목에서
    # 빠져도 자기 포지션은 계속 관리된다.
    pos = Position(symbol="ZZZ", qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 98.0, "target": None,
                         "strategy": "orb_scan", "session": DAY1.isoformat()})
    bars = {"AAA": {"5m": FLAT}}
    ctx = _ctx(bars, {"AAA": 100.2, "ZZZ": 97.0}, positions={"ZZZ": pos})

    signals = strat.on_cycle(ctx)
    exits = [s for s in signals if s.symbol == "ZZZ"]
    assert len(exits) == 1 and exits[0].action == SignalAction.EXIT_LONG
    assert "손절" in exits[0].reason


def test_session_roll_forces_exit_for_any_open_position():
    strat = OrbScanStrategy(["AAA"], _params(), market="US")
    pos = Position(symbol="AAA", qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 90.0, "target": None,
                         "session": DAY1.isoformat()})
    bars = {"AAA": {"5m": _or_bar(DAY2, 100.0, 100.3, 99.7, 100.1)}}
    ctx = _ctx(bars, {"AAA": 100.1}, positions={"AAA": pos},
               now=datetime.combine(DAY2, dtime(10, 30), tzinfo=NY))

    [signal] = [s for s in strat.on_cycle(ctx) if s.action == SignalAction.EXIT_LONG]
    assert "오버나잇 금지" in signal.reason


def test_entry_window_gate_skips_history_entirely_outside_window():
    """관심종목 20개여도 창 밖 사이클은 history를 한 번도 안 부른다 — 5초 폴링
    x 종목 수만큼 조회가 늘면 사이클 지연이 유니버스 크기에 비례하게 된다."""
    strat = OrbScanStrategy([f"S{i}" for i in range(20)], _params(), market="US")
    ctx = _ctx({}, {}, now=datetime.combine(DAY1, dtime(11, 0), tzinfo=NY))

    assert strat.on_cycle(ctx) == []
    assert ctx.data.history_calls == 0


def test_one_entry_per_symbol_per_session():
    strat = OrbScanStrategy(["AAA"], _params(), market="US")
    bars = {"AAA": {"5m": BULL}}
    quotes = {"AAA": 101.0}
    assert len(strat.on_cycle(_ctx(bars, quotes))) == 1
    # 같은 세션, 같은 창에서 두 번째 사이클(라이브 5초 폴링) — 재진입 금지.
    assert strat.on_cycle(_ctx(bars, quotes)) == []


def test_holding_does_not_block_a_fresh_entry_signal():
    """2026-08-10 사용자 결정: 유효한 진입 신호를 '이미 보유'만을 이유로 막지 않는다.
    사이징은 리스크 레이어가 잔여룸(max_position_pct - 기보유분)으로 증분 계산한다."""
    strat = OrbScanStrategy(["AAA"], _params(), market="US")
    pos = Position(symbol="AAA", qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 90.0, "target": None,
                         "session": DAY1.isoformat()})
    bars = {"AAA": {"5m": BULL}}
    signals = strat.on_cycle(_ctx(bars, {"AAA": 101.0}, positions={"AAA": pos}))
    assert any(s.action == SignalAction.ENTER_LONG for s in signals)


def test_same_bar_does_not_fire_twice():
    """봉 가드 — 돌파 조건은 그 봉이 마지막인 동안 계속 참이라, 가드가 없으면
    5초 사이클마다 진입 신호가 쏟아진다."""
    strat = OrbScanStrategy(["AAA"], _params(), market="US")
    bars = {"AAA": {"5m": BULL}}
    first = strat.on_cycle(_ctx(bars, {"AAA": 101.0}))
    second = strat.on_cycle(_ctx(bars, {"AAA": 101.0}))
    assert len([s for s in first if s.action == SignalAction.ENTER_LONG]) == 1
    assert [s for s in second if s.action == SignalAction.ENTER_LONG] == []


def test_max_entries_per_day_caps_when_configured():
    """기본은 무제한이지만, 설정하면 하루 N회로 묶인다(되돌릴 수 있는 레버)."""
    params = _params()
    params["max_entries_per_day"] = 1
    strat = OrbScanStrategy(["AAA"], params, market="US")
    bars = {"AAA": {"5m": BULL}}
    assert len(strat.on_cycle(_ctx(bars, {"AAA": 101.0}))) == 1
    strat._last_entry_bar.clear()  # 봉 가드를 걷어내 '하루 상한'만 단독 검증
    signals = strat.on_cycle(_ctx(bars, {"AAA": 101.0}))
    assert [s for s in signals if s.action == SignalAction.ENTER_LONG] == []


def test_sizing_matches_risk_budget_over_risk_pct():
    strat = OrbScanStrategy(["AAA"], _params(), market="US")
    [signal] = strat.on_cycle(_ctx({"AAA": {"5m": BULL}}, {"AAA": 101.0}))
    risk_pct = (101.0 - 99.8) / 101.0
    assert signal.target_weight == pytest.approx(min(0.01 / risk_pct, 4.0))
    assert signal.target_weight * risk_pct == pytest.approx(0.01)


def test_atr_stop_requires_daily_bars_or_no_entry():
    strat = OrbScanStrategy(["AAA"], _params(stop_mode="atr_pct", atr_period=2), market="US")
    # 일봉 없음 → 진입 포기 (분봉으로 조용히 대체하지 않는다)
    assert strat.on_cycle(_ctx({"AAA": {"5m": BULL}}, {"AAA": 101.0})) == []
    assert "ATR 계산 불가" in strat.last_reject["AAA"]

    daily_rows = []
    for i, (h, l, c) in enumerate([(98.0, 94.0, 97.0), (101.0, 96.0, 100.0), (102.0, 99.0, 101.0)]):
        daily_rows.append({
            "ts": datetime(2026, 1, 2 + i, tzinfo=NY), "open": c - 1.0,
            "high": h, "low": l, "close": c, "volume": 1.0,
        })
    daily = pd.DataFrame(daily_rows).set_index("ts")
    strat2 = OrbScanStrategy(["AAA"], _params(stop_mode="atr_pct", atr_period=2), market="US")
    [signal] = strat2.on_cycle(_ctx({"AAA": {"5m": BULL, "1d": daily}}, {"AAA": 101.0}))
    assert signal.stop < 101.0


def test_invalid_intervals_raise():
    with pytest.raises(ValueError):
        OrbScanStrategy(["AAA"], _params(opening_range_minutes=7), market="US")
    with pytest.raises(ValueError):
        OrbScanStrategy(["AAA"], _params(stop_mode="midline"), market="US")


# ------------------------------------------------- 혼합 시장 (KR + US)

KST = ZoneInfo("Asia/Seoul")


class MarketAwareClock(FakeClock):
    """시장별로 개장 여부가 다른 시계 — 혼합 유니버스 테스트용."""

    def __init__(self, now, open_markets: set[str], flatten_markets: set[str] = frozenset()):
        super().__init__(now=now)
        self._open = open_markets
        self._flatten = flatten_markets

    def is_market_open(self, market):
        return market in self._open

    def should_flatten(self, market, flatten_minutes):
        return market in self._flatten


def _kr_or_bar(day: date, o, h, l, c) -> pd.DataFrame:
    ts = datetime.combine(day, dtime(9, 0), tzinfo=KST)
    return pd.DataFrame(
        {"open": [o], "high": [h], "low": [l], "close": [c], "volume": [1000.0]},
        index=pd.DatetimeIndex([ts]),
    )


def test_kr_symbol_enters_in_kr_window_while_us_market_closed():
    """혼합 관심종목의 존재 이유 — KR 종목은 KR 창(09:05 KST)에서 판정돼야 한다.

    예전 단일 market 구조에서는 첫 심볼의 시장이 전체를 지배해, US 인스턴스에
    들어간 KR 종목은 진입 창이 영원히 안 열려 조용히 죽었다.
    """
    strat = OrbScanStrategy(["AAPL", "005930"], _params(), market="US")
    kr_bull = _kr_or_bar(DAY1, 70000, 70900, 69900, 70800)  # +1.14% 양봉
    ctx = Context(
        clock=MarketAwareClock(datetime.combine(DAY1, dtime(9, 5), tzinfo=KST),
                               open_markets={"KR"}),
        data=FakeDataFeed({"005930": {"5m": kr_bull}}, {"005930": 70800.0}),
        broker=FakeBroker(),
    )
    signals = strat.on_cycle(ctx)
    assert [s.symbol for s in signals] == ["005930"]
    # US가 닫혀 있으니 AAPL은 조회조차 안 한다 (판정 시도 자체가 없어야 함)
    assert "AAPL" not in strat.last_reject


def test_us_position_is_not_session_rolled_by_kr_calendar():
    """KST 새벽(미국 장중)은 KR 달력으로는 '다음 날'이다 — US 포지션의 세션을
    KR 시계로 재면 멀쩡한 장중 포지션이 '오버나잇'으로 오판돼 조기 청산된다."""
    strat = OrbScanStrategy(["AAPL"], _params(), market="US")
    # 미국 월요일 세션에 진입한 포지션. 지금은 ET 월요일 20:05 = KST 화요일 10:05.
    et_monday = date(2026, 1, 5)
    pos = Position(symbol="AAPL", qty=10, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 90.0, "target": None,
                         "session": et_monday.isoformat()})
    now = datetime.combine(et_monday, dtime(20, 5), tzinfo=NY)  # = KST 화요일
    ctx = Context(
        clock=MarketAwareClock(now, open_markets=set()),  # 둘 다 장외
        data=FakeDataFeed({}, {"AAPL": 100.5}),
        broker=FakeBroker({"AAPL": pos}),
    )
    signals = strat.on_cycle(ctx)
    assert signals == [], f"ET 기준 같은 날인데 청산됨: {[s.reason for s in signals]}"


def test_kr_session_roll_does_not_reset_us_entry_record():
    """KR이 새 거래일이 됐다고 미국 종목의 '오늘 진입함' 기록을 지우면
    미국 장중(KST 자정 이후)에 같은 종목 재진입이 열린다 — 세션 롤은 시장별이다."""
    strat = OrbScanStrategy(["AAPL", "005930"], _params(), market="US")
    strat._entered_today = {"AAPL"}
    strat._session_date["KR"] = date(2026, 1, 4)  # KR은 어제 날짜로 세팅

    kr_flat = _kr_or_bar(DAY1, 70000, 70100, 69950, 70020)  # 도지 — 진입 없음
    ctx = Context(
        clock=MarketAwareClock(datetime.combine(DAY1, dtime(9, 5), tzinfo=KST),
                               open_markets={"KR"}),
        data=FakeDataFeed({"005930": {"5m": kr_flat}}, {"005930": 70020.0}),
        broker=FakeBroker(),
    )
    strat.on_cycle(ctx)
    assert "AAPL" in strat._entered_today, "KR 세션 롤이 US 진입 기록을 지웠다"
    assert strat._session_date["KR"] == DAY1


# ------------------------------------------------- 세션 롤과 _pending (감사 결함 2)

def test_session_roll_clears_pending_without_open_position():
    """신호는 냈지만 체결되지 않은 채(예: 노출 상한에 막힘) 세션이 넘어가면 그 종목의
    _pending은 사라져야 한다. 안 지우면 몇 주 뒤 사용자가 같은 종목을 손으로 사도
    이 낡은 stop/target이 그 포지션에 붙어 엉뚱한 EXIT_LONG을 낸다."""
    strat = OrbScanStrategy(["AAA"], _params(), market="US")
    bars = {"AAA": {"5m": BULL}}
    signals = strat.on_cycle(_ctx(bars, {"AAA": 101.0}))
    assert len(signals) == 1 and signals[0].action == SignalAction.ENTER_LONG
    assert "AAA" in strat._pending  # 신호는 냈지만 체결 확인 전(포지션 없음) -> pending 남음

    # 다음 날 세션 롤. AAA는 여전히 포지션이 없다(체결 안 됨) — 도지라 새 신호도 없다.
    day2_bars = {"AAA": {"5m": _or_bar(DAY2, 100.0, 100.3, 99.7, 100.2)}}
    ctx2 = _ctx(day2_bars, {"AAA": 100.2}, now=datetime.combine(DAY2, dtime(9, 35), tzinfo=NY))
    strat.on_cycle(ctx2)
    assert "AAA" not in strat._pending


def test_session_roll_keeps_pending_for_symbol_with_open_position():
    """세션 롤 시점에 이미 체결돼 포지션이 열려 있는 종목의 pending까지 지우면 안
    된다. 정상 흐름에서는 _ensure_state가 같은 사이클 안에서 먼저 pending을
    pos.meta로 옮기므로, 세션 롤 필터가 그 소비를 방해하지 않는지 방어적으로 확인한다."""
    strat = OrbScanStrategy(["AAA"], _params(), market="US")
    strat._pending["AAA"] = {"entry": 100.0, "stop": 98.0, "target": None, "session": DAY1.isoformat()}
    # 2026-08-11 랏(lot) 도입 — 체결 확인 기준이 pos.qty(합산)가 아니라 내 lot의
    # qty다. PaperBroker가 체결 시 lot["qty"]를 채우므로, "이미 체결됨"을 시뮬레이션
    # 하려면 lot에도 qty를 심어야 한다(meta={}만으로는 더 이상 체결로 인식되지 않음).
    pos = Position(symbol="AAA", qty=10, avg_cost=100.0,
                   meta={"lots": {"orb_scan": {"qty": 10.0, "avg_cost": 100.0}}})

    day2_bars = {"AAA": {"5m": _or_bar(DAY2, 100.0, 100.3, 99.7, 100.2)}}
    ctx2 = _ctx(day2_bars, {"AAA": 100.2}, positions={"AAA": pos},
                now=datetime.combine(DAY2, dtime(9, 35), tzinfo=NY))
    strat.on_cycle(ctx2)
    assert pos.meta["lots"]["orb_scan"].get("entry") == 100.0
    assert "AAA" not in strat._pending  # _ensure_state가 이미 소비 — 세션 롤 필터가 방해하지 않았다


# ================================================= 전략 간 포지션 소유권 (2026-08-11)

def test_orb_scan_does_not_manage_another_strategys_position():
    from quant.core.models import Position
    from quant.trade.strategy.orb_scan import OrbScanStrategy

    s = OrbScanStrategy(symbols=["005930"], params={}, market="KR")
    foreign = Position(symbol="005930", qty=10, avg_cost=1000.0,
                       meta={"strategy": "intraday_scan", "stop": 900.0})
    assert s._owns(foreign) is False
    mine = Position(symbol="005930", qty=10, avg_cost=1000.0,
                    meta={"strategy": "orb_scan", "stop": 900.0})
    assert s._owns(mine) is True


# --- 손절 거리 상한(stop_max_bps) ------------------------------------------
# intraday_scan 과 같은 근거·같은 관례(2026-08-21 원장 재생 실측). 두 전략은
# 손절/목표가 산식이 동일하므로 같은 파라미터로 같게 동작해야 한다.

def _bull_ctx():
    return _ctx({"AAA": {"5m": BULL}}, {"AAA": 101.0})


def test_stop_max_bps_caps_stop_distance():
    strat = OrbScanStrategy(["AAA"], _params(stop_max_bps=100), market="US")
    sig = strat.on_cycle(_bull_ctx())[0]
    assert sig.stop == pytest.approx(101.0 * 0.99, rel=1e-9)


def test_stop_max_bps_absent_keeps_today_behaviour():
    base = OrbScanStrategy(["AAA"], _params(), market="US").on_cycle(_bull_ctx())[0]
    off = OrbScanStrategy(["AAA"], _params(stop_max_bps=0), market="US").on_cycle(_bull_ctx())[0]
    assert off.stop == base.stop
    assert off.target_weight == base.target_weight


def test_stop_max_bps_does_not_change_position_size():
    base = OrbScanStrategy(["AAA"], _params(), market="US").on_cycle(_bull_ctx())[0]
    capped = OrbScanStrategy(["AAA"], _params(stop_max_bps=100), market="US").on_cycle(_bull_ctx())[0]
    assert capped.target_weight == base.target_weight


def test_target_is_derived_from_the_capped_stop():
    strat = OrbScanStrategy(["AAA"], _params(stop_max_bps=100, profit_target_r=1.0), market="US")
    sig = strat.on_cycle(_bull_ctx())[0]
    assert sig.target == pytest.approx(101.0 * 1.01, rel=1e-9)
