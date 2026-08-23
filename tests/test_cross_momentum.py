"""CrossMomentumStrategy — 횡단면 모멘텀 로테이션 테스트 (자체 설계, 백테스트 미검증)."""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quant.core.ports import Context
from quant.core.models import Position, Quote, SignalAction
from quant.trade.strategy.cross_momentum import CrossMomentumStrategy

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
MONDAY = date(2026, 1, 5)  # 월요일

PARAMS = {
    "lookback_sessions": 20,
    "top_n": 2,
    "rebalance_weekday": 0,  # 월요일
    "atr_period": 14,
    "atr_stop_mult": 2.0,
}

# 21세션(lookback_sessions+1) 종가 배열 — 수익률 순위: A(+30%) > C(+28%) > B(+5%) > D(-10%)
RETURNS = {
    "AAA": (100.0, 130.0),
    "BBB": (100.0, 105.0),
    "CCC": (100.0, 128.0),
    "DDD": (100.0, 90.0),
}

# 2026-08-17(월) 실제 사고 재현용 — KR 상위 2종목(009150/066570 근사), US 하위 2종목.
MIXED_RETURNS = {
    "009150": (100.0, 122.23),
    "066570": (100.0, 120.72),
    "AAA": (100.0, 105.0),
    "BBB": (100.0, 90.0),
}


class FakeClock:
    def __init__(self, now, open_markets=None):
        self._now = now
        # None → 두 시장 다 개장(기존 단일시장 테스트와 동일 동작 유지).
        # dict 로 주면 시장별로 개폐를 따로 통제할 수 있다(KR 휴장 재현용).
        self._open_markets = open_markets

    def now(self):
        return self._now

    def is_market_open(self, market):
        if self._open_markets is None:
            return True
        return self._open_markets.get(market, True)

    def minutes_to_close(self, market):
        return 300.0

    def cadence_minutes(self):
        return 5.0

    def should_flatten(self, market, flatten_minutes):
        return False


class FakeDataFeed:
    def __init__(self, bars, quotes):
        self._bars = bars
        self._quotes = quotes
        self.history_calls: dict[str, int] = {}

    def quote(self, symbol):
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(NY), price=self._quotes[symbol])

    def history(self, symbol, interval, n):
        self.history_calls[symbol] = self.history_calls.get(symbol, 0) + 1
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


def _daily_bars(start: float, end: float, n: int = 21, volume: float = 2_000_000.0) -> pd.DataFrame:
    closes = np.linspace(start, end, n)
    idx = pd.bdate_range(end=MONDAY - pd.Timedelta(days=1), periods=n, tz=NY)
    rows = [
        {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": volume}
        for c in closes
    ]
    return pd.DataFrame(rows, index=idx)


def _universe_bars() -> dict:
    return {sym: {"1d": _daily_bars(start, end)} for sym, (start, end) in RETURNS.items()}


def _quotes() -> dict:
    return {sym: end for sym, (_, end) in RETURNS.items()}


def _ctx(symbols, now, positions=None, bars=None, quotes=None, open_markets=None):
    data = FakeDataFeed(bars if bars is not None else _universe_bars(),
                        quotes if quotes is not None else _quotes())
    return Context(clock=FakeClock(now, open_markets), data=data, broker=FakeBroker(positions))


_MONDAY_OPEN = datetime.combine(MONDAY, dtime(9, 35), tzinfo=NY)


def test_rebalance_exits_dropped_and_enters_new_top_symbol():
    """A(+30%),C(+28%)가 top2 → B(보유중, 5%)는 청산, C(미보유)는 진입, A(보유중)는 유지."""
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    s = CrossMomentumStrategy(symbols, PARAMS)
    pos_a = Position(symbol="AAA", qty=5, avg_cost=100.0,
                      meta={"entry": 100.0, "stop": 50.0, "strategy": "cross_momentum"})
    pos_b = Position(symbol="BBB", qty=5, avg_cost=100.0,
                      meta={"entry": 100.0, "stop": 50.0, "strategy": "cross_momentum"})
    ctx = _ctx(symbols, _MONDAY_OPEN, positions={"AAA": pos_a, "BBB": pos_b})

    signals = s.on_cycle(ctx)
    by_symbol = {sig.symbol: sig for sig in signals}

    assert "AAA" not in by_symbol, "이미 보유 + 여전히 상위권 — 신호 없음"
    assert by_symbol["BBB"].action == SignalAction.EXIT_LONG
    assert "로테이션 이탈" in by_symbol["BBB"].reason
    assert by_symbol["CCC"].action == SignalAction.ENTER_LONG
    assert by_symbol["CCC"].target_weight == 0.5
    assert "DDD" not in by_symbol, "미보유 + 하위권 — 신호 없음"


def test_ranking_only_runs_once_per_week():
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    s = CrossMomentumStrategy(symbols, PARAMS)
    ctx = _ctx(symbols, _MONDAY_OPEN)
    s.on_cycle(ctx)
    calls_after_first = dict(ctx.data.history_calls)
    assert calls_after_first.get("AAA") == 1

    # 같은 날 반복 — 다시 랭킹하면 안 된다
    s.on_cycle(ctx)
    s.on_cycle(ctx)
    assert ctx.data.history_calls == calls_after_first

    # 같은 주의 화요일 — 재랭킹 요일이 아니므로 여전히 호출 없음
    tue = datetime.combine(MONDAY + timedelta(days=1), dtime(9, 35), tzinfo=NY)
    ctx_tue = _ctx(symbols, tue, bars=ctx.data._bars, quotes=ctx.data._quotes)
    ctx_tue.data.history_calls = ctx.data.history_calls
    s.on_cycle(ctx_tue)
    assert ctx_tue.data.history_calls == calls_after_first


def test_stop_hit_exits_full():
    symbols = ["AAA"]
    s = CrossMomentumStrategy(symbols, PARAMS)
    pos = Position(symbol="AAA", qty=5, avg_cost=100.0,
                   meta={"entry": 100.0, "stop": 95.0, "strategy": "cross_momentum"})
    ctx = _ctx(symbols, _MONDAY_OPEN, positions={"AAA": pos},
               bars={"AAA": {"1d": _daily_bars(100.0, 130.0)}}, quotes={"AAA": 90.0})
    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1 and exits[0].exit_fraction == 1.0
    assert "손절" in exits[0].reason


def test_does_not_manage_a_position_opened_by_another_strategy():
    s = CrossMomentumStrategy(symbols=["AAA"], params=PARAMS)
    foreign = Position(symbol="AAA", qty=10, avg_cost=100.0,
                        meta={"strategy": "orb_scan", "stop": 90.0})
    assert s._owns(foreign) is False

    mine = Position(symbol="AAA", qty=10, avg_cost=100.0,
                     meta={"strategy": "cross_momentum", "stop": 90.0})
    assert s._owns(mine) is True


# ------------------------------------------------------------ 2026-08-17 사고 회귀 테스트
#
# 실제 사고: 2026-08-17(월)은 KR 대체공휴일이라 KR이 열리지 않았고, US 트리거만
# (그 주에) 발화해 당시 시장 공유였던 _last_rebalance_week 게이트를 소비했다.
# 랭킹은 KR 상위 2종목(009150/066570)을 뽑았지만 KR 장이 닫혀 있어 두 진입
# 신호 모두 "장 마감 — 주문 불가"로 거부됐고, 게이트가 이미 소비돼 그 주
# 내내 재시도가 없었다(로그 원문은 CLAUDE.md 작업 지시 참고).


def _mixed_bars_and_quotes():
    bars = {sym: {"1d": _daily_bars(start, end)} for sym, (start, end) in MIXED_RETURNS.items()}
    quotes = {sym: end for sym, (_, end) in MIXED_RETURNS.items()}
    return bars, quotes


def test_kr_holiday_defers_rebalance_and_retries_when_kr_opens():
    """8/17 재현: KR 휴장 + US 개장 상태에서 리밸런스가 돌면 KR 상위 종목(009150/
    066570)에 진입 신호를 내면 안 되고(당시엔 신호가 나가서 주문이 거부됐다),
    KR 리밸런스는 미수행으로 남아 KR이 다음에 열릴 때(화요일) 재시도돼야 한다."""
    symbols = ["009150", "066570", "AAA", "BBB"]
    s = CrossMomentumStrategy(symbols, PARAMS)
    bars, quotes = _mixed_bars_and_quotes()

    mon_us_open = datetime.combine(MONDAY, dtime(9, 35), tzinfo=NY)
    ctx_mon = _ctx(symbols, mon_us_open, bars=bars, quotes=quotes,
                   open_markets={"US": True, "KR": False})
    signals_mon = s.on_cycle(ctx_mon)

    enter_mon = {sig.symbol for sig in signals_mon if sig.action == SignalAction.ENTER_LONG}
    assert enter_mon == set(), "KR 장 마감 중에는 KR 상위 종목이라도 진입 신호를 내면 안 된다"
    assert s._last_rebalance_week.get("US") is not None, "US는 이번 주 리밸런스를 마쳐야 한다"
    assert s._last_rebalance_week.get("KR") is None, "KR은 미수행 상태로 남아야 재시도된다"

    # 화요일 KR 개장 — 재시도돼야 한다.
    tue_kr_open = datetime.combine(MONDAY + timedelta(days=1), dtime(9, 5), tzinfo=KST)
    ctx_tue = _ctx(symbols, tue_kr_open, bars=bars, quotes=quotes,
                   open_markets={"US": False, "KR": True})
    signals_tue = s.on_cycle(ctx_tue)
    enter_tue = {sig.symbol for sig in signals_tue if sig.action == SignalAction.ENTER_LONG}
    assert enter_tue == {"009150", "066570"}, "KR 개장 후에는 재시도로 KR 상위 종목이 진입돼야 한다"
    assert s._last_rebalance_week.get("KR") is not None


def test_us_trigger_does_not_consume_kr_gate():
    """US 트리거가 (과거처럼) 시장 공유 게이트를 소비해 KR 몫까지 써버리면 안 된다 —
    시장별로 독립된 주간 게이트여야 한다."""
    symbols = ["009150", "066570", "AAA", "BBB"]
    s = CrossMomentumStrategy(symbols, PARAMS)
    bars, quotes = _mixed_bars_and_quotes()
    ctx = _ctx(symbols, _MONDAY_OPEN, bars=bars, quotes=quotes,
               open_markets={"US": True, "KR": False})
    s.on_cycle(ctx)
    assert s._last_rebalance_week.get("KR") is None
    assert s._last_rebalance_week.get("US") is not None


def test_normal_week_both_markets_rebalance_independently():
    """정상 케이스(양 시장 모두 개장) — 이번 주에 KR/US가 각자 트리거될 때 각
    시장의 상위 종목에 정상 진입해야 한다(기존 단일시장 동작이 유지됨을 확인).
    top2가 KR 1종목·US 1종목에 걸치도록 수익률을 구성해 두 시장 모두 실제로
    진입 신호가 나오는지 확인한다."""
    symbols = ["009150", "066570", "AAA", "BBB"]
    s = CrossMomentumStrategy(symbols, PARAMS)
    returns = {
        "009150": (100.0, 130.0),  # top1 (KR)
        "AAA": (100.0, 120.0),     # top2 (US)
        "066570": (100.0, 90.0),
        "BBB": (100.0, 80.0),
    }
    bars = {sym: {"1d": _daily_bars(start, end)} for sym, (start, end) in returns.items()}
    quotes = {sym: end for sym, (_, end) in returns.items()}

    kr_open = datetime.combine(MONDAY, dtime(9, 5), tzinfo=KST)
    ctx_kr = _ctx(symbols, kr_open, bars=bars, quotes=quotes,
                  open_markets={"KR": True, "US": False})
    signals_kr = s.on_cycle(ctx_kr)
    enter_kr = {sig.symbol for sig in signals_kr if sig.action == SignalAction.ENTER_LONG}
    assert enter_kr == {"009150"}, "KR 개장 중에는 top2 중 KR 종목만 즉시 진입해야 한다"

    us_open = datetime.combine(MONDAY, dtime(9, 35), tzinfo=NY)
    ctx_us = _ctx(symbols, us_open, bars=bars, quotes=quotes,
                  open_markets={"KR": False, "US": True})
    signals_us = s.on_cycle(ctx_us)
    enter_us = {sig.symbol for sig in signals_us if sig.action == SignalAction.ENTER_LONG}
    assert enter_us == {"AAA"}, "US 개장 중에는 top2 중 US 종목이 (KR은 이미 보유이므로) 진입해야 한다"


def test_rebalance_summary_log_counts_skip_reasons(caplog):
    """봉 부족 종목이 섞여 있어도 요약 로그 1줄에 랭킹 성공 수/skip 사유별 건수가
    집계돼야 한다 — 이 로그가 없어서(원래 이 파일엔 log 호출이 전혀 없었다) 8/17
    KR 리밸런스 소실이 8일간 아무도 몰랐다."""
    symbols = ["AAA", "BBB", "SHORT"]
    s = CrossMomentumStrategy(symbols, PARAMS)
    bars = {
        "AAA": {"1d": _daily_bars(100.0, 130.0)},
        "BBB": {"1d": _daily_bars(100.0, 105.0)},
        "SHORT": {"1d": _daily_bars(100.0, 150.0, n=5)},  # lookback_sessions+1(21) 미달 — 랭킹 skip
    }
    quotes = {"AAA": 130.0, "BBB": 105.0, "SHORT": 150.0}
    ctx = _ctx(symbols, _MONDAY_OPEN, bars=bars, quotes=quotes)

    with caplog.at_level(logging.INFO, logger="quant.trade.strategy.cross_momentum"):
        s.on_cycle(ctx)

    summary_lines = [r.getMessage() for r in caplog.records if "리밸런스" in r.getMessage()]
    assert len(summary_lines) == 1, "사이클당 요약 로그는 정확히 1줄이어야 한다"
    line = summary_lines[0]
    assert "2/3" in line, "랭킹 성공 2종목 / 전체 3종목이 로그에 드러나야 한다"
    assert "봉" in line, "봉 부족 skip 사유가 로그에 드러나야 한다(사유별 카운터)"


# ------------------------------------------------------------ 2차 수정: 시장 스코프 집행
#
# 1차 수정(시장별 독립 게이트)만으로는 한 주에 KR·US가 각각 트리거되면
# _rebalance()가 주 2회 실행됐고, 그때마다 청산 루프가 *전체* 보유 포지션을
# 훑어 "top_n 밖이면 청산"을 적용했다 — 즉 US 트리거가 도는 회차가 KR 자신의
# 주간 트리거를 기다리지 않고 KR 보유 포지션을 즉시 청산할 수 있었다(회전율
# 증가 우려). 이번 수정: 랭킹은 유니버스 전체로 유지하되, 각 트리거는 자기
# 시장 심볼의 청산·진입만 집행한다.


def test_same_market_not_rebalanced_twice_in_the_same_week():
    """US 트리거 회차가 (자기 시장이 아닌) KR 보유 포지션을 청산하면 안 된다 —
    설령 그 KR 포지션이 이미 상위권 밖으로 밀려났더라도, 청산은 KR 자신의
    주간 트리거를 기다려야 한다. 같은 주 화요일에 KR이 열리면 그때 비로소
    KR 트리거가 청산을 집행한다."""
    symbols = ["009150", "066570", "AAA", "BBB"]
    s = CrossMomentumStrategy(symbols, PARAMS)
    returns = {
        "AAA": (100.0, 130.0),      # top1 (US)
        "BBB": (100.0, 120.0),      # top2 (US)
        "009150": (100.0, 90.0),    # 하위권 — 이미 보유 중이라고 가정(과거 편입분)
        "066570": (100.0, 80.0),
    }
    bars = {sym: {"1d": _daily_bars(start, end)} for sym, (start, end) in returns.items()}
    quotes = {sym: end for sym, (_, end) in returns.items()}
    pos_kr = Position(symbol="009150", qty=1, avg_cost=100.0,
                       meta={"entry": 100.0, "stop": 50.0, "strategy": "cross_momentum"})

    # 월요일, US만 개장 — US 트리거가 돈다. 009150(KR)은 상위권 밖이지만 이번
    # 회차(US 트리거)에서 건드리면 안 된다.
    us_open = datetime.combine(MONDAY, dtime(9, 35), tzinfo=NY)
    ctx_us = _ctx(symbols, us_open, positions={"009150": pos_kr}, bars=bars, quotes=quotes,
                  open_markets={"KR": False, "US": True})
    signals_us = s.on_cycle(ctx_us)
    touched_us = {sig.symbol for sig in signals_us}
    assert "009150" not in touched_us, (
        "US 트리거가 KR 포지션(상위권 밖)을 청산하면 안 된다 — 그건 KR 자신의 트리거 몫이다"
    )
    assert touched_us == {"AAA", "BBB"}, "US 트리거는 US top2 두 종목 모두 신규 진입해야 한다"

    # 같은 주 화요일, KR 개장 — 이제 KR 자신의 트리거가 009150을 청산해야 한다.
    tue_kr_open = datetime.combine(MONDAY + timedelta(days=1), dtime(9, 5), tzinfo=KST)
    ctx_kr = _ctx(symbols, tue_kr_open, positions={"009150": pos_kr}, bars=bars, quotes=quotes,
                  open_markets={"KR": True, "US": False})
    signals_kr = s.on_cycle(ctx_kr)
    touched_kr = {sig.symbol for sig in signals_kr}
    assert touched_kr == {"009150"}, "KR 트리거 회차는 KR 심볼만 다뤄야 한다"
    exit_signal = next(sig for sig in signals_kr if sig.symbol == "009150")
    assert exit_signal.action == SignalAction.EXIT_LONG


def test_ranking_uses_full_universe_not_triggering_market_only():
    """랭킹(top_n 산출)은 항상 유니버스 전체로 계산돼야 한다 — 트리거한 시장의
    심볼만으로 로컬 랭킹을 만들면 안 된다. US 심볼이 상위를 독식하는 주에 KR이
    트리거돼도(KR 심볼끼리는 그중 누가 "낫든") 전체 유니버스 기준으로는 하위권이므로
    편입되면 안 된다."""
    symbols = ["009150", "066570", "AAA", "BBB"]
    s = CrossMomentumStrategy(symbols, PARAMS)
    returns = {
        "AAA": (100.0, 130.0),    # top1 (US)
        "BBB": (100.0, 120.0),    # top2 (US)
        "009150": (100.0, 90.0),  # KR, 하위 — KR 안에서는 1등이지만 전체론 3등
        "066570": (100.0, 80.0),  # KR, 하위
    }
    bars = {sym: {"1d": _daily_bars(start, end)} for sym, (start, end) in returns.items()}
    quotes = {sym: end for sym, (_, end) in returns.items()}

    kr_open = datetime.combine(MONDAY, dtime(9, 5), tzinfo=KST)
    ctx_kr = _ctx(symbols, kr_open, bars=bars, quotes=quotes,
                  open_markets={"KR": True, "US": False})
    signals_kr = s.on_cycle(ctx_kr)
    assert signals_kr == [], (
        "KR이 전체 유니버스 기준 top_n 밖이면 KR 트리거라도 진입 신호가 없어야 한다"
        "(랭킹이 KR만의 로컬 랭킹이 아니라는 증거)"
    )


def test_rebalance_weekday_semantics_first_open_day_on_or_after_target():
    """`today.weekday() < rebalance_weekday`(기본 0=월요일) 비교의 의미를 명시적으로
    고정한다. weekday()<0은 항상 False이므로 모든 거래일이 이 조건을 통과하고,
    실질적인 주 1회 제한은 ISO 주 키 게이트가 담당한다:
    - 월요일 개장 주: 월요일에 리밸런스, 같은 주 화/수엔 재랭킹 없음.
    - 월요일 휴장 주: 화요일에 캐치업 리밸런스, 캐치업 이후 같은 주 수요일엔
      재랭킹 없음.
    """
    symbols = ["AAA", "BBB", "CCC", "DDD"]

    # 1) 월요일 개장 주 — 월요일에만 랭킹, 화/수엔 재랭킹 없음
    s1 = CrossMomentumStrategy(symbols, PARAMS)
    ctx_mon = _ctx(symbols, _MONDAY_OPEN)
    s1.on_cycle(ctx_mon)
    calls_after_mon = dict(ctx_mon.data.history_calls)
    assert calls_after_mon.get("AAA") == 1, "월요일 개장이면 월요일에 랭킹해야 한다"

    tue = datetime.combine(MONDAY + timedelta(days=1), dtime(9, 35), tzinfo=NY)
    ctx_tue = _ctx(symbols, tue, bars=ctx_mon.data._bars, quotes=ctx_mon.data._quotes)
    ctx_tue.data.history_calls = ctx_mon.data.history_calls
    s1.on_cycle(ctx_tue)
    wed = datetime.combine(MONDAY + timedelta(days=2), dtime(9, 35), tzinfo=NY)
    ctx_wed = _ctx(symbols, wed, bars=ctx_mon.data._bars, quotes=ctx_mon.data._quotes)
    ctx_wed.data.history_calls = ctx_mon.data.history_calls
    s1.on_cycle(ctx_wed)
    assert ctx_wed.data.history_calls == calls_after_mon, "같은 주 화/수엔 재랭킹하면 안 된다"

    # 2) 월요일 휴장 주 — 화요일에 캐치업, 캐치업 이후 수요일엔 재실행 없음.
    # (별도 전략 인스턴스로 "월요일엔 아예 on_cycle이 안 돌았다"를 재현 —
    # 실제로 시장이 닫혀 있으면 봇이 그날 아무 상태도 기록하지 않는 것과 동일.)
    s2 = CrossMomentumStrategy(symbols, PARAMS)
    next_monday = MONDAY + timedelta(days=7)
    next_tue = datetime.combine(next_monday + timedelta(days=1), dtime(9, 35), tzinfo=NY)
    ctx_next_tue = _ctx(symbols, next_tue, bars=ctx_mon.data._bars, quotes=ctx_mon.data._quotes)
    s2.on_cycle(ctx_next_tue)
    calls_after_catchup = dict(ctx_next_tue.data.history_calls)
    assert calls_after_catchup.get("AAA") == 1, "월요일이 휴장이었다면 화요일에 캐치업 리밸런스가 돌아야 한다"

    next_wed = datetime.combine(next_monday + timedelta(days=2), dtime(9, 35), tzinfo=NY)
    ctx_next_wed = _ctx(symbols, next_wed, bars=ctx_mon.data._bars, quotes=ctx_mon.data._quotes)
    ctx_next_wed.data.history_calls = ctx_next_tue.data.history_calls
    s2.on_cycle(ctx_next_wed)
    assert ctx_next_wed.data.history_calls == calls_after_catchup, "캐치업 이후 같은 주엔 재랭킹하면 안 된다"
