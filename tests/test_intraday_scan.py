"""IntradayScanStrategy — 세션 신고가 돌파 스캐너 테스트 (자체 설계, 백테스트 미검증)."""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pandas as pd

from quant.core.ports import Context
from quant.core.models import Position, Quote
from quant.core.models import SignalAction
from quant.trade.strategy.intraday_scan import IntradayScanStrategy

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DAY1 = date(2026, 1, 5)

PARAMS = {
    "bar_interval_minutes": 5,
    "entry_start_minutes_after_open": 30,
    "no_entry_minutes_before_close": 60,
    "min_session_bars": 6,
    "volume_mult": 2.0,
    "atr_period": 14,
    "atr_stop_mult": 0.35,
    "profit_target_r": None,
    "flatten_before_close_minutes": 1,
}


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
        # 호출 기록(symbol, interval) — 시장 리스크오프 게이트의 분 경계 캐시
        # 검증용(다른 기존 단언은 이 리스트를 보지 않는다).
        self.history_calls: list[tuple[str, str]] = []

    def quote(self, symbol):
        if symbol not in self._quotes:
            return None
        return Quote(symbol=symbol, ts=datetime.now(NY), price=self._quotes[symbol])

    def history(self, symbol, interval, n):
        self.history_calls.append((symbol, interval))
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


def _session_bars(tz, day, open_time, n, *, breakout_last=False, last_vol_mult=3.0):
    """세션 5분봉 n개. breakout_last=True면 마지막 봉이 세션 신고가 돌파 + 대량."""
    idx = [datetime.combine(day, open_time, tzinfo=tz) + timedelta(minutes=5 * i) for i in range(n)]
    base = 100.0
    rows = []
    for i in range(n):
        last = breakout_last and i == n - 1
        o = base + i * 0.01
        h = o + (2.0 if last else 0.3)
        c = h - 0.05 if last else o + 0.1
        rows.append({
            "open": o, "high": h, "low": o - 0.2, "close": c,
            "volume": 1000 * (last_vol_mult if last else 1.0),
        })
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=tz))


def _daily_bars(tz, n=25):
    idx = [datetime.combine(DAY1 - timedelta(days=n - i), dtime(0, 0), tzinfo=tz) for i in range(n)]
    return pd.DataFrame(
        {"open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 1e6},
        index=pd.DatetimeIndex(idx, tz=tz),
    )


def _ctx(strategy_symbols, bars, quotes, now, positions=None, minutes_to_close=300.0):
    return Context(
        clock=FakeClock(now, minutes_to_close),
        data=FakeDataFeed(bars, quotes),
        broker=FakeBroker(positions),
    )


def _anchor_bars_1m(pct: float, tz=NY, day=DAY1, start=dtime(9, 30)) -> pd.DataFrame:
    """시장 리스크오프 게이트 전용 앵커 1분봉 — 당일 시가 100.0에서 pct%만큼
    이동한 마지막 종가(quant/trade/indicators/breadth.py `anchor_drawdown`이
    보는 형태와 동일)."""
    idx = [datetime.combine(day, start, tzinfo=tz) + timedelta(minutes=i) for i in range(2)]
    last_close = 100.0 * (1 + pct / 100)
    rows = [
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000.0},
        {"open": 100.0, "high": max(100.0, last_close), "low": min(100.0, last_close),
         "close": last_close, "volume": 1000.0},
    ]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz=tz))


def _us_ctx(*, breakout=True, vol_mult=3.0, n_bars=8, now_time=dtime(10, 10), minutes_to_close=300.0,
            positions=None, anchor_pct: float | None = None):
    bars = {
        "TQQQ": {
            "5m": _session_bars(NY, DAY1, dtime(9, 30), n_bars, breakout_last=breakout, last_vol_mult=vol_mult),
            "1d": _daily_bars(NY),
        }
    }
    if anchor_pct is not None:
        bars["QQQ"] = {"1m": _anchor_bars_1m(anchor_pct)}
    now = datetime.combine(DAY1, now_time, tzinfo=NY)
    return _ctx(["TQQQ"], bars, {"TQQQ": 102.5}, now, positions, minutes_to_close)


def test_session_high_breakout_with_volume_enters():
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    signals = s.on_cycle(_us_ctx())
    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.ENTER_LONG
    assert sig.stop is not None and sig.stop < 102.5
    assert s._entries_today.get("TQQQ") == 1


def test_no_entry_before_start_window():
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    signals = s.on_cycle(_us_ctx(now_time=dtime(9, 50)))  # 개장 +20분 < 30분
    assert signals == []


def test_no_entry_near_close():
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    signals = s.on_cycle(_us_ctx(minutes_to_close=50.0))  # 60분 컷 안쪽
    assert signals == []


def test_volume_below_mult_rejected():
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    signals = s.on_cycle(_us_ctx(vol_mult=1.2))
    assert signals == []
    assert "거래량" in s.last_reject["TQQQ"]


def test_no_breakout_rejected():
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    signals = s.on_cycle(_us_ctx(breakout=False))
    assert signals == []
    assert "신고가" in s.last_reject["TQQQ"]


def test_same_bar_does_not_fire_twice_but_new_bar_can():
    """봉 가드: 같은 완성봉으로는 한 번만. 새 봉이 나오면 다시 진입 가능
    (2026-08-10 사용자 결정 — 신호가 나면 진입한다)."""
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    assert len(s.on_cycle(_us_ctx())) == 1
    assert s.on_cycle(_us_ctx(now_time=dtime(10, 40))) == [], "같은 봉 — 재진입 없음"

    # 봉이 하나 더 쌓이면(=새 완성봉) 다시 신호가 날 수 있다. now는 그 봉이 막
    # 닫힌 직후여야 한다 — 슬롯 게이트는 "기대한 새 봉이 실제로 도착했을 때"만
    # 판정을 소비한다(캐시 지연 시 다음 사이클 재시도).
    fresh = IntradayScanStrategy(["TQQQ"], PARAMS)
    assert len(fresh.on_cycle(_us_ctx(n_bars=8))) == 1
    assert len(fresh.on_cycle(_us_ctx(n_bars=9, now_time=dtime(10, 16)))) == 1


def test_stale_cache_does_not_consume_bar_slot():
    """봉이 닫혔는데 캐시가 이전 봉까지만 보여주면, 슬롯을 소비하지 않고 다음
    사이클에 재시도해야 한다 — 소비해버리면 그 봉의 신호를 통째로 놓친다."""
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    # now=10:20(슬롯 10)인데 데이터는 10:05 시작 봉(슬롯 7)까지만 → 낡은 캐시
    stale = _us_ctx(n_bars=8, now_time=dtime(10, 20))
    assert s.on_cycle(stale) == []
    assert "새 봉 데이터 대기" in s.last_reject["TQQQ"]
    # 캐시가 갱신돼 기대 봉(10:15 시작, 슬롯 9)이 도착하면 같은 슬롯에서 판정된다
    fresh = _us_ctx(n_bars=10, now_time=dtime(10, 21))
    assert len(s.on_cycle(fresh)) == 1


def test_kr_session_roll_does_not_reset_us_entry():
    s = IntradayScanStrategy(["TQQQ", "005930"], PARAMS)
    us_ctx = _us_ctx()
    assert len(s.on_cycle(us_ctx)) == 1
    # KR 세션 롤 발생 (KST 아침) — US의 오늘 진입 기록은 유지돼야 한다
    kr_bars = {
        "005930": {
            "5m": _session_bars(KST, DAY1 + timedelta(days=1), dtime(9, 0), 8),
            "1d": _daily_bars(KST),
        },
        "TQQQ": us_ctx.data._bars["TQQQ"],
    }
    kr_now = datetime.combine(DAY1 + timedelta(days=1), dtime(10, 0), tzinfo=KST)
    s.on_cycle(_ctx(["TQQQ", "005930"], kr_bars, {"005930": 100.0}, kr_now))
    assert s._entries_today.get("TQQQ") == 1, "KR 롤이 US 진입 기록을 지우면 안 된다"


def test_stop_hit_exits_full():
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    pos = Position(symbol="TQQQ", qty=10, avg_cost=102.5)
    pos.meta.update(entry=102.5, stop=101.0, target=None, session=DAY1.isoformat())
    ctx = _ctx(
        ["TQQQ"], {"TQQQ": {"5m": _session_bars(NY, DAY1, dtime(9, 30), 8), "1d": _daily_bars(NY)}},
        {"TQQQ": 100.5}, datetime.combine(DAY1, dtime(11, 0), tzinfo=NY), {"TQQQ": pos},
    )
    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1 and exits[0].exit_fraction == 1.0
    assert "손절" in exits[0].reason


# ================================================= 전략 간 포지션 소유권 (2026-08-11)

def test_does_not_manage_a_position_opened_by_another_strategy():
    """실운영 결함: orb_scan이 산 088350·229200을 intraday_scan이 청산해
    원장의 매수/매도가 다른 strategy_id에 기록됐고, 라운드트립이 종결되지 않아
    스코어보드에서 최대 수익·최대 손실이 통째로 누락됐다."""
    from quant.core.models import Position
    from quant.trade.strategy.intraday_scan import IntradayScanStrategy

    s = IntradayScanStrategy(symbols=["005930"], params={}, market="KR")
    foreign = Position(symbol="005930", qty=10, avg_cost=1000.0,
                       meta={"strategy": "orb_scan", "stop": 900.0})
    assert s._owns(foreign) is False, "남의 전략이 연 포지션은 관리하지 않는다"

    mine = Position(symbol="005930", qty=10, avg_cost=1000.0,
                    meta={"strategy": "intraday_scan", "stop": 900.0})
    assert s._owns(mine) is True


def test_adopts_untagged_position_in_own_universe_so_it_is_never_orphaned():
    """태그가 없으면(재시작으로 meta 유실 등) 내 유니버스 종목만 떠맡는다 —
    아무도 청산하지 않는 상태가 이중 청산보다 위험하다(3배 ETF 오버나이트)."""
    from quant.core.models import Position
    from quant.trade.strategy.intraday_scan import IntradayScanStrategy

    s = IntradayScanStrategy(symbols=["005930"], params={}, market="KR")
    assert s._owns(Position(symbol="005930", qty=1, avg_cost=1.0, meta={})) is True
    assert s._owns(Position(symbol="TQQQ", qty=1, avg_cost=1.0, meta={})) is False


# ========================================================= 선택적 추세 필터 (2026-08-12)

DAY0 = DAY1 - timedelta(days=1)


def _ctx_with_prior_history(prior_base: float):
    """DAY0에 평평한 20봉(prior_base)을 깔고 DAY1 오늘 세션(브레이크아웃 포함
    8봉)을 이어 붙인다 — 추세 필터(SMA20)는 오늘 세션 봉만으로는 워밍업이 안
    되므로 세션 경계를 넘는 이력이 필요하다. `_check_entry_for`의 신고가/거래량
    판정은 여전히 today_bars(DAY1 8봉)만 본다 — DAY0는 날짜가 달라 걸러진다."""
    prior_idx = [datetime.combine(DAY0, dtime(9, 30), tzinfo=NY) + timedelta(minutes=5 * i) for i in range(20)]
    prior = pd.DataFrame(
        {"open": prior_base, "high": prior_base + 0.1, "low": prior_base - 0.1,
         "close": prior_base, "volume": 1000.0},
        index=pd.DatetimeIndex(prior_idx, tz=NY),
    )
    today = _session_bars(NY, DAY1, dtime(9, 30), 8, breakout_last=True, last_vol_mult=3.0)
    combined = pd.concat([prior, today])
    bars = {"TQQQ": {"5m": combined, "1d": _daily_bars(NY)}}
    now = datetime.combine(DAY1, dtime(10, 10), tzinfo=NY)
    return _ctx(["TQQQ"], bars, {"TQQQ": 102.5}, now)


def test_trend_filter_default_off_does_not_change_existing_behavior():
    """require_trend_filter 미설정(기본 false) — 켜져 있다면 막힐 조건
    (SMA20이 오늘 종가보다 훨씬 위)에서도 꺼져 있으면 그대로 진입한다(회귀 0)."""
    s = IntradayScanStrategy(["TQQQ"], PARAMS)  # PARAMS에 require_trend_filter 없음
    assert s.require_trend_filter is False
    signals = s.on_cycle(_ctx_with_prior_history(prior_base=120.0))
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG


def test_trend_filter_blocks_entry_when_close_below_sma():
    params = {**PARAMS, "require_trend_filter": True, "trend_filter_period": 20}
    s = IntradayScanStrategy(["TQQQ"], params)
    signals = s.on_cycle(_ctx_with_prior_history(prior_base=120.0))  # SMA20이 종가보다 훨씬 위
    assert signals == []
    assert "추세 필터" in s.last_reject["TQQQ"]


# ============================================================ 시장 리스크오프 게이트
# quant/trade/indicators/breadth.py 배선 — 계산 자체는 tests/test_breadth.py가
# 고정한다. 여기서는 게이트가 세션 신고가 진입 직전에 실제로 걸리는지 + 모드별
# 동작 + 분 경계 캐시만 본다.

def test_market_risk_gate_defaults_to_shadow():
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    assert s.market_risk_gate_mode == "shadow"


def test_market_risk_gate_shadow_tags_reason_without_blocking():
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    signals = s.on_cycle(_us_ctx(anchor_pct=-1.0))
    assert len(signals) == 1
    assert "[시장:리스크오프" in signals[0].reason
    assert s.market_risk_verdict.get("US") is True


def test_market_risk_gate_block_mode_blocks_entry():
    params = {**PARAMS, "market_risk_gate_mode": "block"}
    s = IntradayScanStrategy(["TQQQ"], params)
    signals = s.on_cycle(_us_ctx(anchor_pct=-1.0))
    assert signals == []
    assert "리스크오프" in s.last_reject["TQQQ"]


def test_market_risk_gate_block_mode_allows_entry_when_within_threshold():
    params = {**PARAMS, "market_risk_gate_mode": "block", "market_risk_max_drawdown_pct": 0.5}
    s = IntradayScanStrategy(["TQQQ"], params)
    signals = s.on_cycle(_us_ctx(anchor_pct=-0.1))
    assert len(signals) == 1


def test_market_risk_gate_off_mode_skips_anchor_query():
    params = {**PARAMS, "market_risk_gate_mode": "off"}
    s = IntradayScanStrategy(["TQQQ"], params)
    ctx = _us_ctx(anchor_pct=-5.0)
    signals = s.on_cycle(ctx)
    assert len(signals) == 1
    assert "리스크오프" not in signals[0].reason
    assert not any(sym == "QQQ" for sym, _ in ctx.data.history_calls)


def test_market_risk_gate_missing_anchor_data_falls_back_to_pass():
    params = {**PARAMS, "market_risk_gate_mode": "block"}
    s = IntradayScanStrategy(["TQQQ"], params)
    signals = s.on_cycle(_us_ctx())  # anchor_pct 없음 -> QQQ 데이터 자체가 없음
    assert len(signals) == 1
    assert "리스크오프" not in signals[0].reason


def test_market_risk_gate_queries_anchor_once_per_minute():
    """같은 분 안의 반복 사이클은 앵커를 재조회하지 않는다(분 경계 캐시)."""
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    ctx = _us_ctx(anchor_pct=-1.0)
    s.on_cycle(ctx)
    s.on_cycle(ctx)  # 같은 now(=같은 분) — 재조회 없어야 함
    anchor_calls = [c for c in ctx.data.history_calls if c[0] == "QQQ"]
    assert len(anchor_calls) == 1


def test_market_risk_gate_does_not_affect_exit_management():
    """리스크오프 상태에서도 손절 청산은 정상 동작 — 게이트는 신규 진입에만 붙는다."""
    s = IntradayScanStrategy(["TQQQ"], PARAMS)
    pos = Position(symbol="TQQQ", qty=10, avg_cost=102.5)
    pos.meta.update(entry=102.5, stop=101.0, target=None, session=DAY1.isoformat())
    bars = {
        "TQQQ": {"5m": _session_bars(NY, DAY1, dtime(9, 30), 8), "1d": _daily_bars(NY)},
        "QQQ": {"1m": _anchor_bars_1m(-3.0)},
    }
    ctx = _ctx(
        ["TQQQ"], bars, {"TQQQ": 100.5}, datetime.combine(DAY1, dtime(11, 0), tzinfo=NY), {"TQQQ": pos},
    )
    signals = s.on_cycle(ctx)
    exits = [x for x in signals if x.action == SignalAction.EXIT_LONG]
    assert len(exits) == 1 and exits[0].exit_fraction == 1.0
    assert "손절" in exits[0].reason


def test_trend_filter_allows_entry_when_close_above_sma():
    params = {**PARAMS, "require_trend_filter": True, "trend_filter_period": 20}
    s = IntradayScanStrategy(["TQQQ"], params)
    signals = s.on_cycle(_ctx_with_prior_history(prior_base=90.0))  # SMA20이 종가보다 훨씬 아래
    assert len(signals) == 1
    assert signals[0].action == SignalAction.ENTER_LONG


# --- 손절 거리 상한(stop_max_bps) + 목표가 ---------------------------------
# 2026-08-21 실측 근거: 라이브 원장 종결 63건 중 1분봉 재생 가능한 27건에
# 사전 지정 청산 규칙 4종을 재생했다(사후 튜닝 없음). 현행은 중앙 -85.4bp /
# 평균 -77.3bp / 승률 19%, "익절 +100bp · 손절 -100bp"는 중앙 -7.6bp /
# 평균 -13.8bp / 승률 48%(합계 -2,087bp → -371bp). 진입 시점의 당일 레인지
# 위치는 승패를 가르지 못했다(rho=+0.00, 승 0.84 vs 패 0.89) — 즉 **고칠 곳은
# 진입이 아니라 청산**이다.
#
# ATR 손절(atr_stop_mult 0.35)은 실측 R=1.5~4.5%로 이 상한보다 훨씬 넓다.
# 상한을 걸되 **사이징은 원래의 넓은 ATR 리스크로 계산한 값을 그대로 둔다** —
# 좁아진 손절로 사이징을 다시 하면 target_weight 가 max_leverage 까지 부풀어
# 명목이 2배 이상 커진다(frgn_accumulate 가 겪은 사이징 사고와 같은 부류).
# 사이징을 그대로 두면 1회 손절 손실이 리스크 예산보다 **작아지므로** 보수적이다.

def _params_with(**over):
    p = dict(PARAMS)
    p.update(over)
    return p


def test_stop_max_bps_caps_stop_distance():
    s = IntradayScanStrategy(["TQQQ"], _params_with(stop_max_bps=100))
    sig = s.on_cycle(_us_ctx())[0]
    entry = 102.5
    assert sig.stop == pytest.approx(entry * (1 - 0.01), rel=1e-9)


def test_stop_max_bps_absent_keeps_today_behaviour():
    """상한 미설정(기본)이면 손절가가 지금과 100% 같아야 한다."""
    base = IntradayScanStrategy(["TQQQ"], PARAMS).on_cycle(_us_ctx())[0]
    off = IntradayScanStrategy(["TQQQ"], _params_with(stop_max_bps=0)).on_cycle(_us_ctx())[0]
    assert off.stop == base.stop
    assert off.target_weight == base.target_weight


def test_stop_max_bps_does_not_change_position_size():
    """손절을 좁혀도 명목이 커지면 안 된다 — 사이징은 원래 ATR 리스크 기준."""
    base = IntradayScanStrategy(["TQQQ"], PARAMS).on_cycle(_us_ctx())[0]
    capped = IntradayScanStrategy(["TQQQ"], _params_with(stop_max_bps=100)).on_cycle(_us_ctx())[0]
    assert capped.target_weight == base.target_weight


def test_target_is_derived_from_the_capped_stop():
    """profit_target_r=1.0 + stop_max_bps=100 이면 목표가는 진입가 +100bp."""
    s = IntradayScanStrategy(["TQQQ"], _params_with(stop_max_bps=100, profit_target_r=1.0))
    sig = s.on_cycle(_us_ctx())[0]
    entry = 102.5
    assert sig.target == pytest.approx(entry * 1.01, rel=1e-9)
