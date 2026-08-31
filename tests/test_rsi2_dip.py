"""`Rsi2DipStrategy`(RSI(2) 눌림매수) 계약 고정.

신규 전략이라 동치를 비교할 레거시 쌍둥이가 없다 — `test_overnight_drift.py`와
같은 방식으로, 규칙 하나하나를 손으로 조립한 `StrategySnapshot`으로 직접
고정한다(`decide()`가 곧 이 전략의 계약이다).

RSI(2) 계산 자체의 정확성은 손으로 계산한 수열(①)로 별도 고정한다 — 이후 모든
진입/청산 테스트는 그 검증된 계산 위에서 시나리오만 다룬다. ①은 이제
`quant.trade.strategy.rsi2_dip._wilder_rsi`가 아니라 `quant.trade.indicators.rsi`를
직접 검증한다 — 커널 추출 수술 이후 이 전략이 그 함수를 `period=2`로 그대로
가져다 쓰기 때문에("이 파일 안에서 완결적으로 검증돼야 한다"는 옛 자기완결
요구는 공용 커널 도입으로 소멸했다), ①이 고정하는 대상도 실제 호출 지점을
따라간다.

시각은 전부 America/New_York로 조립한다(기본 `market="US"`). 날짜는 전부 실제
평일(월~금)로 골랐다 — `in_continuous_session`은 주말만 걸러내고 공휴일은 모른다.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.indicators import rsi as _wilder_rsi
from quant.trade.strategy.rsi2_dip import Rsi2DipShell, Rsi2DipStrategy

NY = ZoneInfo("America/New_York")
SYM = "RSIU"


def _daily(closes: list[float], end: date) -> pd.DataFrame:
    """`end`로 끝나는 평일 일봉 시리즈(종가만 의미 있음, 나머지는 종가로 채운다)."""
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes)).tz_localize(NY)
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1e6] * len(closes)},
        index=idx,
    )


def _snap(*, now: datetime, market_open: bool = True,
          minutes_to_close: float | None = 10.0, price: float = 100.0,
          lots: dict | None = None, bars: dict | None = None) -> StrategySnapshot:
    return StrategySnapshot(
        now=now,
        market_open={"US": market_open},
        minutes_to_close={"US": minutes_to_close},
        cadence_minutes=1.0,
        bars=bars if bars is not None else {},
        quotes={SYM: Quote(symbol=SYM, ts=now, price=price)},
        lots=lots if lots is not None else {},
    )


def _strategy(**params) -> Rsi2DipStrategy:
    return Rsi2DipStrategy([SYM], params)


def _entry_signal(strat: Rsi2DipStrategy, snap: StrategySnapshot, state: dict | None = None):
    decision = strat.decide(snap, state or {})
    entries = [s for s in decision.signals if s.action is SignalAction.ENTER_LONG]
    return (entries[0] if entries else None), decision


def _lot_from(signal) -> dict:
    """루프가 체결 확인 후 `Position.meta["lots"][id]`에 쓰는 것과 같은 내용."""
    return dict(signal.state_update)


# ============================================================ ① RSI(2) 계산 정확성
# (이 전략이 period=2로 그대로 쓰는 quant.trade.indicators.rsi 자체를 검증한다)

def test_wilder_rsi_matches_hand_computed_sequence():
    """손으로 계산한 Wilder RSI(2) 수열 — 시드 후 세 스텝을 정확한 분수로 검증.

    closes = [10, 12, 11, 13, 9, 15], period=2.
    delta = [nan, 2, -1, 2, -4, 6] -> gain=[nan,2,0,2,0,6] loss=[nan,0,1,0,4,0]
    시드@idx2: avg_gain=(2+0)/2=1.0, avg_loss=(0+1)/2=0.5 -> RSI=100-100/3=66.667
    idx3: avg_gain=(1.0+2)/2=1.5, avg_loss=(0.5+0)/2=0.25 -> RSI=100-100/7=85.714
    idx4: avg_gain=(1.5+0)/2=0.75, avg_loss=(0.25+4)/2=2.125 -> RSI=26.087
    idx5: avg_gain=(0.75+6)/2=3.375, avg_loss=(2.125+0)/2=1.0625 -> RSI=76.056
    """
    closes = pd.Series([10.0, 12.0, 11.0, 13.0, 9.0, 15.0])
    result = _wilder_rsi(closes, 2)

    assert pd.isna(result.iloc[0])
    assert pd.isna(result.iloc[1])
    assert result.iloc[2] == pytest.approx(200 / 3, rel=1e-9)
    assert result.iloc[3] == pytest.approx(600 / 7, rel=1e-9)
    assert result.iloc[4] == pytest.approx(26.086956521739133, rel=1e-9)
    assert result.iloc[5] == pytest.approx(76.05633802816901, rel=1e-9)


def test_wilder_rsi_flat_prices_is_neutral_fifty():
    """가격 불변(상승분/하락분 모두 0)이면 50 — 극단값(0/100)으로 오판하지 않는다."""
    closes = pd.Series([100.0] * 6)
    result = _wilder_rsi(closes, 2)
    assert result.iloc[2:].tolist() == [50.0, 50.0, 50.0, 50.0]


def test_wilder_rsi_only_gains_is_hundred():
    """하락분이 전혀 없으면(avg_loss=0) 100 — 0으로 나누기가 아니라 명시적 상한."""
    closes = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
    result = _wilder_rsi(closes, 2)
    assert result.iloc[2:].tolist() == [100.0, 100.0, 100.0]


# ============================================================ ② 진입 — 과매도 + 추세 위

# 추세 필터용 20일 SMA 시나리오: 10일 완만한 상승(100~109) + 10일 고점권 횡보
# (128~131) 뒤 오늘 현재가가 하루 급락. `trend_sma_days=20`으로 테스트 규모를
# 줄인다(기본 200은 ⑦번 테스트가 별도로 확인).
_TREND_HIST = [100.0 + i for i in range(10)] + [
    128.0, 130.0, 129.0, 131.0, 130.0, 129.0, 131.0, 130.0, 129.0, 131.0,
]
_TREND_END = date(2026, 2, 27)  # 진입일(2026-03-02, 월) 직전 마지막 평일
ENTRY_DAY = date(2026, 3, 2)
ENTRY_NOW = datetime.combine(ENTRY_DAY, dtime(15, 55), tzinfo=NY)  # 마감 5분 전


def _trend_bars() -> dict:
    return {(SYM, "1d"): _daily(_TREND_HIST, _TREND_END)}


def test_enters_on_oversold_dip_above_trend_before_close():
    """RSI(2)=9.25 (<10) 이고 현재가(120) > SMA20(118.15) — 진입.

    (직접 계산 확인: extended=[...129,131,120], RSI(2)=9.246, SMA20=118.15)
    """
    strat = _strategy(trend_sma_days=20)
    snap = _snap(now=ENTRY_NOW, minutes_to_close=5.0, price=120.0, bars=_trend_bars())
    entry, _ = _entry_signal(strat, snap)
    assert entry is not None
    assert entry.symbol == SYM
    assert entry.action is SignalAction.ENTER_LONG
    assert entry.target_weight == pytest.approx(strat.target_weight)
    assert entry.stop == pytest.approx(120.0 * 0.95)
    assert "RSI(2)=9.2" in entry.reason


def test_no_entry_when_price_below_trend_sma_even_if_oversold():
    """RSI(2)=7.98 (<10, 과매도 조건은 성립)인데 현재가(118) <= SMA20(118.05) —
    추세 필터가 진입을 막는다. RSI만으로는 사지 않는다는 것을 격리해서 확인."""
    strat = _strategy(trend_sma_days=20)
    snap = _snap(now=ENTRY_NOW, minutes_to_close=5.0, price=118.0, bars=_trend_bars())
    entry, _ = _entry_signal(strat, snap)
    assert entry is None


def test_no_entry_when_rsi_not_oversold():
    """추세는 위인데(평탄한 최근 종가) RSI(2)가 10 미만이 아니면 진입하지 않는다."""
    strat = _strategy(trend_sma_days=20)
    # 급락이 없는 완만한 흐름 — RSI가 과매도 영역에 들지 않는다.
    hist = [100.0 + i * 0.1 for i in range(20)]
    bars = {(SYM, "1d"): _daily(hist, _TREND_END)}
    snap = _snap(now=ENTRY_NOW, minutes_to_close=5.0, price=102.0, bars=bars)
    entry, _ = _entry_signal(strat, snap)
    assert entry is None


def test_no_entry_outside_entry_window_or_market_closed():
    strat = _strategy(trend_sma_days=20)
    bars = _trend_bars()
    # 창 밖(마감 20분 전)
    outside, _ = _entry_signal(strat, _snap(
        now=ENTRY_NOW, minutes_to_close=20.0, price=120.0, bars=bars))
    assert outside is None
    # 시장이 닫혀 있다
    closed, _ = _entry_signal(strat, _snap(
        now=ENTRY_NOW, market_open=False, minutes_to_close=5.0, price=120.0, bars=bars))
    assert closed is None


def test_no_second_position_while_holding():
    strat = _strategy(trend_sma_days=20)
    lot = {"entry": 100.0, "stop": 95.0, "entered_date": "2026-02-20"}
    snap = _snap(now=ENTRY_NOW, minutes_to_close=5.0, price=120.0,
                bars=_trend_bars(), lots={SYM: lot})
    decision = strat.decide(snap, {})
    assert [s for s in decision.signals if s.action is SignalAction.ENTER_LONG] == []


def test_no_entry_when_daily_bars_insufficient():
    """확인 불가는 통과가 아니라 거부다 — 추세 필터 계산에 필요한 만큼 일봉이
    없으면(SMA가 NaN) 진입하지 않는다."""
    strat = _strategy(trend_sma_days=20)
    short_bars = {(SYM, "1d"): _daily([100.0, 101.0], date(2026, 2, 27))}
    entry, _ = _entry_signal(strat, _snap(
        now=ENTRY_NOW, minutes_to_close=5.0, price=90.0, bars=short_bars))
    assert entry is None
    # 일봉이 아예 없다
    entry2, _ = _entry_signal(strat, _snap(
        now=ENTRY_NOW, minutes_to_close=5.0, price=90.0, bars={}))
    assert entry2 is None


# ============================================================ ③ 청산 — RSI 회복

def test_exit_when_rsi_recovers_above_exit_threshold():
    """RSI(2)=85.7 (>60) — 다음 판단 시점에 청산.

    daily=[100,95,90,85,95](2026-02-27~03-05), 진입 2026-03-02, 오늘(현재가 105,
    2026-03-06) extended RSI(2)=85.714 (직접 계산 확인).
    """
    strat = _strategy()
    daily = _daily([100.0, 95.0, 90.0, 85.0, 95.0], date(2026, 3, 5))
    lot = {"entry": 100.0, "stop": 95.0, "entered_date": "2026-03-02"}
    now = datetime.combine(date(2026, 3, 6), dtime(11, 0), tzinfo=NY)  # 금요일 장중
    snap = _snap(now=now, price=105.0, bars={(SYM, "1d"): daily}, lots={SYM: lot})
    decision = strat.decide(snap, {})
    [exit_sig] = [s for s in decision.signals if s.action is SignalAction.EXIT_LONG]
    assert "RSI(2)=85.7" in exit_sig.reason
    assert exit_sig.exit_fraction == 1.0
    assert exit_sig.target_weight == 0.0


def test_no_rsi_exit_when_still_below_threshold():
    strat = _strategy()
    daily = _daily([100.0] * 5, date(2026, 3, 5))  # 평탄 — RSI 항상 50
    lot = {"entry": 100.0, "stop": 95.0, "entered_date": "2026-03-02"}
    now = datetime.combine(date(2026, 3, 6), dtime(11, 0), tzinfo=NY)
    snap = _snap(now=now, price=100.0, bars={(SYM, "1d"): daily}, lots={SYM: lot})
    decision = strat.decide(snap, {})
    assert [s for s in decision.signals if s.action is SignalAction.EXIT_LONG] == []


# ============================================================ ④ 청산 — 보유기간 초과

def test_exit_after_max_hold_days_exceeded():
    """보유 5거래일(기본) 초과 — 진입 2026-03-02, 완성 일봉이 03-03~03-06까지
    4개(entered 이후) 쌓이면 경과 6거래일(4+2) > 5 → 청산."""
    strat = _strategy()
    daily = _daily([100.0] * 4, date(2026, 3, 6))  # 03-03,04,05,06
    lot = {"entry": 100.0, "stop": 95.0, "entered_date": "2026-03-02"}
    now = datetime.combine(date(2026, 3, 9), dtime(11, 0), tzinfo=NY)  # 다음 월요일
    snap = _snap(now=now, price=100.0, bars={(SYM, "1d"): daily}, lots={SYM: lot})
    decision = strat.decide(snap, {})
    [exit_sig] = [s for s in decision.signals if s.action is SignalAction.EXIT_LONG]
    assert "보유기간 청산" in exit_sig.reason
    assert "6거래일" in exit_sig.reason


def test_no_time_exit_at_exact_hold_boundary():
    """경과 5거래일(=max_hold_days) 정확히면 아직 "초과"가 아니다 — 청산하지 않는다."""
    strat = _strategy()
    daily = _daily([100.0] * 5, date(2026, 3, 5))  # 02-27,03-02,03,04,05
    lot = {"entry": 100.0, "stop": 95.0, "entered_date": "2026-03-02"}
    now = datetime.combine(date(2026, 3, 6), dtime(11, 0), tzinfo=NY)  # 금요일
    snap = _snap(now=now, price=100.0, bars={(SYM, "1d"): daily}, lots={SYM: lot})
    decision = strat.decide(snap, {})
    assert [s for s in decision.signals if s.action is SignalAction.EXIT_LONG] == []


# ============================================================ ⑤ 청산 — 하드 레일

def test_hard_stop_exits_immediately_even_on_entry_day():
    """하드 레일(-5%)은 요일 무관 즉시 — 진입 당일이라도 확인한다."""
    strat = _strategy(hard_stop_pct=5.0)
    lot = {"entry": 100.0, "stop": 95.0, "entered_date": "2026-03-02"}
    # 진입 당일, 4% 하락 — 방어선 위(청산 없음)
    same_day_ok = strat.decide(_snap(
        now=ENTRY_NOW, price=96.0, lots={SYM: lot}), {})
    assert [s for s in same_day_ok.signals if s.action is SignalAction.EXIT_LONG] == []
    # 진입 당일, -6% — 방어선 이탈, 즉시 손절
    same_day_stop = strat.decide(_snap(
        now=ENTRY_NOW, price=94.0, lots={SYM: lot}), {})
    [exit_sig] = same_day_stop.signals
    assert exit_sig.action is SignalAction.EXIT_LONG
    assert "하드 손절" in exit_sig.reason


def test_hard_stop_beats_rsi_exit_when_both_trigger_same_cycle():
    """같은 사이클에 하드 레일과 RSI 청산이 동시에 성립하면(가격이 손절선
    아래로 급락하면서 RSI도 회복) 하드 레일이 우선한다 — 자본 보호가 먼저."""
    strat = _strategy()
    daily = _daily([100.0, 95.0, 90.0, 85.0, 95.0], date(2026, 3, 5))
    lot = {"entry": 100.0, "stop": 95.0, "entered_date": "2026-03-02"}
    now = datetime.combine(date(2026, 3, 6), dtime(11, 0), tzinfo=NY)
    # 가격이 손절선(95) 아래(90)인데, RSI(2) 계산상 90도 회복 구간일 수 있다 —
    # 여기서는 하드 레일이 확인되면 RSI 계산 자체를 하지 않는다.
    snap = _snap(now=now, price=90.0, bars={(SYM, "1d"): daily}, lots={SYM: lot})
    decision = strat.decide(snap, {})
    [exit_sig] = decision.signals
    assert "하드 손절" in exit_sig.reason


# ================================================== ⑤b 방어선 결손 랏(stop 없음)


def test_time_exit_still_fires_when_lot_has_no_stop():
    """stop이 없는 랏(방어선 결손)이라도 하드레일 판정만 건너뛴다 — "지어내지
    않는다" 원칙상 stop을 재계산해 채우지 않는다. 보유기간 초과 청산은 그대로
    걸린다(intraday_momentum과 동일 정책)."""
    strat = _strategy()
    daily = _daily([100.0] * 4, date(2026, 3, 6))  # 03-03,04,05,06
    lot = {"entry": 100.0, "stop": None, "entered_date": "2026-03-02"}
    now = datetime.combine(date(2026, 3, 9), dtime(11, 0), tzinfo=NY)  # 다음 월요일
    snap = _snap(now=now, price=100.0, bars={(SYM, "1d"): daily}, lots={SYM: lot})
    decision = strat.decide(snap, {})
    [exit_sig] = [s for s in decision.signals if s.action is SignalAction.EXIT_LONG]
    assert "보유기간 청산" in exit_sig.reason


def test_rsi_exit_still_fires_when_lot_has_no_stop():
    strat = _strategy()
    daily = _daily([100.0, 95.0, 90.0, 85.0, 95.0], date(2026, 3, 5))
    lot = {"entry": 100.0, "stop": None, "entered_date": "2026-03-02"}
    now = datetime.combine(date(2026, 3, 6), dtime(11, 0), tzinfo=NY)  # 금요일 장중
    snap = _snap(now=now, price=105.0, bars={(SYM, "1d"): daily}, lots={SYM: lot})
    decision = strat.decide(snap, {})
    [exit_sig] = [s for s in decision.signals if s.action is SignalAction.EXIT_LONG]
    assert "RSI(2)=85.7" in exit_sig.reason


# ============================================================ ⑥ 재시작 생존

def test_restart_survival_exit_comes_from_the_lot_not_instance_state():
    """껍질·인스턴스를 통째로 버려도(=프로세스 재시작) 하드 레일 청산이 나온다 —
    방어선이 `next_state`가 아니라 lot에서 온다는 설계를 고정한다."""
    entered = _strategy(trend_sma_days=20)
    entry, decision = _entry_signal(entered, _snap(
        now=ENTRY_NOW, minutes_to_close=5.0, price=120.0, bars=_trend_bars()))
    assert entry is not None
    lot = _lot_from(entry)
    del entered, decision  # 인스턴스도 next_state도 버린다

    reborn = Rsi2DipStrategy([SYM], {"trend_sma_days": 20})  # 새 프로세스의 새 인스턴스
    later = datetime.combine(date(2026, 3, 3), dtime(11, 0), tzinfo=NY)
    decision2 = reborn.decide(_snap(now=later, price=100.0, lots={SYM: lot}), {})  # state 비었음
    [exit_sig] = decision2.signals
    assert exit_sig.action is SignalAction.EXIT_LONG
    assert exit_sig.symbol == SYM
    assert "하드 손절" in exit_sig.reason  # 120 진입 -> stop=114, 현재 100 <= 114


def test_lot_without_entered_date_is_not_managed():
    strat = _strategy()
    lot = {"entry": 100.0, "stop": 95.0}  # entered_date 없음
    now = datetime.combine(date(2026, 3, 6), dtime(11, 0), tzinfo=NY)
    decision = strat.decide(_snap(now=now, price=90.0, lots={SYM: lot}), {})
    assert decision.signals == ()


def test_foreign_lot_is_not_touched():
    strat = _strategy()
    now = datetime.combine(date(2026, 3, 6), dtime(11, 0), tzinfo=NY)
    decision = strat.decide(_snap(now=now, price=1.0, lots={SYM: {}}), {})
    assert [s for s in decision.signals if s.action is SignalAction.EXIT_LONG] == []


# ============================================================ ⑦ 재진입은 청산 다음 날부터

def test_reentry_blocked_same_day_after_exit_allowed_next_day():
    strat = _strategy(trend_sma_days=20)
    lot = {"entry": 200.0, "stop": 190.0, "entered_date": "2026-02-20"}
    # 사이클 1: 보유 중, 하드 레일 이탈 -> 청산 신호 (같은 사이클엔 아직 브로커가
    # 반영 전이라 snap.lots엔 여전히 이 랏이 남아 있다 — 그래서 진입 후보에서
    # 저절로 제외돼 재진입 여부를 이 사이클에서는 관찰할 수 없다).
    exit_snap = _snap(now=ENTRY_NOW, minutes_to_close=5.0, price=180.0,
                      bars=_trend_bars(), lots={SYM: lot})
    exit_decision = strat.decide(exit_snap, {})
    [exit_sig] = [s for s in exit_decision.signals if s.action is SignalAction.EXIT_LONG]
    assert exit_decision.next_state["last_exit_date"][SYM] == ENTRY_DAY.isoformat()

    # 사이클 2: 다음 폴링(같은 날) — 브로커가 청산을 반영해 lots가 비었다.
    # RSI/추세는 진입 조건을 만족하는데도(price=120) 청산 당일이라 재진입 금지.
    same_day_snap = _snap(now=ENTRY_NOW, minutes_to_close=5.0, price=120.0,
                          bars=_trend_bars(), lots={})
    blocked, _ = _entry_signal(strat, same_day_snap, dict(exit_decision.next_state))
    assert blocked is None

    # 사이클 3: 다음 거래일 — 재진입 게이트가 풀린다.
    next_day = datetime.combine(date(2026, 3, 3), dtime(15, 55), tzinfo=NY)
    next_day_bars = {(SYM, "1d"): _daily(_TREND_HIST, date(2026, 3, 2))}
    allowed, _ = _entry_signal(strat, _snap(
        now=next_day, minutes_to_close=5.0, price=120.0, bars=next_day_bars, lots={}),
        dict(exit_decision.next_state))
    assert allowed is not None


# ============================================================ ⑧ requirements() / 일봉 개수

def test_requirements_declares_daily_bars_only():
    strat = _strategy(trend_sma_days=20)
    reqs = strat.requirements()
    assert reqs.bars == ((SYM, "1d", 20 + 20),)  # max(20,5) + 워밍업버퍼(20)
    assert reqs.quotes == (SYM,)
    assert reqs.needs_positions is True


def test_requirements_daily_count_uses_default_trend_sma_when_small_and_floor_when_smaller():
    default_strat = _strategy()
    assert default_strat.requirements().bars == ((SYM, "1d", 200 + 20),)
    tiny_strat = _strategy(trend_sma_days=2)
    # trend_sma_days(2) < RSI 최소 하한(5) — 하한이 이긴다.
    assert tiny_strat.requirements().bars == ((SYM, "1d", 5 + 20),)


# ============================================================ ⑨ 입력 불변 + state 왕복

def test_decide_does_not_mutate_inputs():
    strat = _strategy(trend_sma_days=20)
    state = {"entered_date": {"OTHER": "2026-01-02"}, "last_exit_date": {}}
    lot = {"entry": 100.0, "stop": 95.0, "entered_date": "2026-03-02"}
    lots = {SYM: lot}
    now = datetime.combine(date(2026, 3, 6), dtime(11, 0), tzinfo=NY)
    snap = _snap(now=now, price=90.0, lots=lots)

    state_before = copy.deepcopy(state)
    lots_before = copy.deepcopy(lots)
    strat.decide(snap, state)
    assert state == state_before
    assert lots == lots_before

    entry_state = {"entered_date": {}, "last_exit_date": {}}
    before = copy.deepcopy(entry_state)
    result = strat.decide(_snap(
        now=ENTRY_NOW, minutes_to_close=5.0, price=120.0, bars=_trend_bars()), entry_state)
    assert entry_state == before
    assert result.next_state["entered_date"] == {SYM: ENTRY_DAY.isoformat()}


def test_state_round_trip_one_entry_per_day():
    strat = _strategy(trend_sma_days=20)
    snap = _snap(now=ENTRY_NOW, minutes_to_close=5.0, price=120.0, bars=_trend_bars())
    first = strat.decide(snap, {})
    assert len(first.signals) == 1
    second = strat.decide(snap, first.next_state)
    assert second.signals == ()  # 같은 날 재평가 — 1일 1회


# ============================================================ 파라미터 검증

@pytest.mark.parametrize("params", [
    {"entry_rsi": 0},
    {"entry_rsi": 100},
    {"entry_rsi": -1},
    {"exit_rsi": 0},
    {"exit_rsi": 101},
    {"exit_rsi": 10, "entry_rsi": 10},   # exit <= entry
    {"exit_rsi": 5, "entry_rsi": 10},    # exit < entry
    {"entry_before_close_minutes": 0},
    {"entry_before_close_minutes": -1},
    {"trend_sma_days": 1},
    {"trend_sma_days": 0},
    {"max_hold_days": 0},
    {"hard_stop_pct": 0},
    {"hard_stop_pct": -1},
    {"target_weight": 0},
    {"target_weight": 1.5},
])
def test_constructor_rejects_invalid_params(params):
    with pytest.raises(ValueError):
        Rsi2DipStrategy([SYM], params)


def test_constructor_accepts_defaults():
    strat = Rsi2DipStrategy([SYM], {})
    assert strat.entry_rsi == 10.0
    assert strat.exit_rsi == 60.0
    assert strat.entry_before_close_minutes == 10.0
    assert strat.trend_sma_days == 200
    assert strat.max_hold_days == 5
    assert strat.hard_stop_pct == 5.0
    assert strat.target_weight == 0.5


# ============================================================ 껍질

def test_shell_satisfies_strategy_protocol():
    shell = Rsi2DipShell([SYM], {})
    assert shell.id == "rsi2_dip"
    assert shell.symbols == [SYM]
    assert hasattr(shell, "on_cycle")
    assert isinstance(shell.inner, Rsi2DipStrategy)
