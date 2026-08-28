"""`OvernightDriftStrategy`(오버나이트 드리프트) 계약 고정.

신규 전략이라 동치를 비교할 레거시 쌍둥이가 없다 — `test_mr_vwap_quiet.py` 와
같은 방식으로, 규칙 하나하나를 손으로 조립한 `StrategySnapshot` 으로 직접
고정한다(`decide()` 가 곧 이 전략의 계약이다).

**이 전략에서 진짜로 증명해야 하는 것은 단일 사이클 판정이 아니라 세션 경계를
넘는 상태의 왕복이다.** 진입은 DAY1 마감 5분 전, 청산은 DAY2 개장 5분 안 —
그 사이에 프로세스가 죽었다 살아나도 청산이 나와야 한다. `test_restart_*` 가
그 경로를 고정한다: 껍질도 인스턴스도 통째로 버리고(=`next_state` 전부 소실)
`Signal.state_update` → `Position.meta["lots"]` → `snap.lots` 로만 흘러온 값으로
익일 청산이 나오는지 본다. 방어선을 `next_state` 에 뒀다면 이 테스트가 실패한다.

시각은 전부 America/New_York 로 조립한다 — US ETF 전용 의도이기 때문이고,
서머타임 전환은 tz 변환이 처리한다(전략은 시각을 하드코딩하지 않는다).
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.strategy.overnight_drift import (
    OvernightDriftShell,
    OvernightDriftStrategy,
)

NY = ZoneInfo("America/New_York")
DAY1 = date(2026, 1, 5)   # 월
DAY2 = date(2026, 1, 6)   # 화
SYM = "TSTU"

ENTRY_NOW = datetime.combine(DAY1, dtime(15, 55), tzinfo=NY)   # 마감 5분 전
EXIT_NOW = datetime.combine(DAY2, dtime(9, 33), tzinfo=NY)     # 개장 +3분
ENTRY_PRICE = 100.0


# ============================================================ 합성 스냅샷

def _session_bars(open_price: float, close_price: float,
                  day: date = DAY1) -> pd.DataFrame:
    """당일 09:30 부터의 5분봉. 첫 봉의 시가 = 세션 시가(필터 계산의 입력)."""
    start = datetime.combine(day, dtime(9, 30), tzinfo=NY)
    idx = pd.date_range(start=start, periods=6, freq="5min")
    closes = [open_price] * 5 + [close_price]
    return pd.DataFrame(
        {"open": [open_price] * 6, "high": [max(open_price, close_price)] * 6,
         "low": [min(open_price, close_price)] * 6, "close": closes,
         "volume": [1000.0] * 6},
        index=idx,
    )


def _daily(prev_close: float) -> pd.DataFrame:
    idx = pd.date_range(end=datetime(2026, 1, 2, tzinfo=NY), periods=3, freq="1D")
    return pd.DataFrame(
        {"open": [prev_close] * 3, "high": [prev_close] * 3, "low": [prev_close] * 3,
         "close": [prev_close] * 3, "volume": [1e6] * 3},
        index=idx,
    )


def _snap(*, now: datetime = ENTRY_NOW, market_open: bool = True,
          minutes_to_close: float | None = 5.0, price: float = ENTRY_PRICE,
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


def _strategy(**params) -> OvernightDriftStrategy:
    return OvernightDriftStrategy([SYM], params)


def _entry_signal(strat: OvernightDriftStrategy, snap: StrategySnapshot):
    decision = strat.decide(snap, {})
    entries = [s for s in decision.signals if s.action is SignalAction.ENTER_LONG]
    return (entries[0] if entries else None), decision


def _lot_from(signal) -> dict:
    """루프가 체결 확인 후 `Position.meta["lots"][id]` 에 쓰는 것과 같은 내용 —
    껍질은 다음 사이클에 이걸 `snap.lots[symbol]` 로 되돌려준다."""
    return dict(signal.state_update)


# ============================================================ ① 진입 창

def test_enters_only_inside_the_window_before_close():
    strat = _strategy()
    # 창 밖(마감 10분 전) — 무신호
    early, _ = _entry_signal(strat, _snap(minutes_to_close=10.0))
    assert early is None
    # 창 안(마감 5분 전) — 진입
    inside, _ = _entry_signal(strat, _snap(minutes_to_close=5.0))
    assert inside is not None
    assert inside.symbol == SYM
    assert inside.target_weight == pytest.approx(strat.target_weight)
    # 창 안 더 깊은 곳(마감 1분 전)도 진입한다 — 하한을 두지 않았다.
    deep, _ = _entry_signal(strat, _snap(minutes_to_close=1.0))
    assert deep is not None
    # 마감 이후(남은 시간 0 이하) — 무신호
    after, _ = _entry_signal(strat, _snap(minutes_to_close=0.0))
    assert after is None


def test_no_entry_when_session_is_unknown():
    """`minutes_to_close` 가 None(그 시장 세션을 모른다)이면 진입하지 않는다."""
    entry, _ = _entry_signal(_strategy(), _snap(minutes_to_close=None))
    assert entry is None


def test_no_entry_outside_continuous_session():
    """정규장 마감까지 5분이어도 연속 거래 구간 밖이면 진입하지 않는다.

    시장 개장 플래그만 믿으면 실재할 수 없는 가격으로 체결이 모델링된다
    (2026-08-26 실사고 — `quant.core.session`).
    """
    after_close = datetime.combine(DAY1, dtime(16, 30), tzinfo=NY)
    entry, _ = _entry_signal(_strategy(), _snap(now=after_close, minutes_to_close=5.0))
    assert entry is None


# ============================================================ ② 시장 닫힘

def test_no_signal_when_market_closed():
    strat = _strategy()
    # 진입 창인데 장이 닫혀 있다 — 무신호
    entry, _ = _entry_signal(strat, _snap(market_open=False))
    assert entry is None
    # 보유 중이어도 장이 닫혀 있으면 청산 신호를 내지 않는다(체결 불가).
    lot = {"entry": ENTRY_PRICE, "stop": 97.0, "session": DAY1.isoformat(),
           "strategy": strat.id}
    decision = strat.decide(
        _snap(now=EXIT_NOW, market_open=False, price=101.0, lots={SYM: lot}), {})
    assert decision.signals == ()


# ============================================================ ③ 1일 1회

def test_one_entry_per_day():
    strat = _strategy()
    snap = _snap()
    first = strat.decide(snap, {})
    assert len(first.signals) == 1
    # 같은 날 다음 사이클 — 체결/랏이 아직 없어도(리스크 거부·미체결) 재진입 없음
    second = strat.decide(snap, first.next_state)
    assert second.signals == ()
    # 날이 바뀌면 게이트가 풀린다
    next_day = datetime.combine(DAY2, dtime(15, 55), tzinfo=NY)
    third = strat.decide(_snap(now=next_day), first.next_state)
    assert len(third.signals) == 1


def test_no_second_position_while_holding():
    """심볼당 1포지션 — 보유 중이면 진입 창이어도 신규 진입하지 않는다."""
    strat = _strategy()
    lot = {"entry": ENTRY_PRICE, "stop": 97.0, "session": DAY1.isoformat()}
    decision = strat.decide(_snap(lots={SYM: lot}), {})
    assert [s for s in decision.signals if s.action is SignalAction.ENTER_LONG] == []


# ============================================================ ④ 익일 개장 청산

def test_exits_within_the_open_window_next_session():
    strat = _strategy()
    entry, _ = _entry_signal(strat, _snap())
    lot = _lot_from(entry)

    # 진입 당일에는 청산하지 않는다 — 밤을 넘기는 것이 이 전략이다.
    same_day = strat.decide(
        _snap(now=datetime.combine(DAY1, dtime(15, 58), tzinfo=NY),
              price=101.0, lots={SYM: lot}), {})
    assert [s for s in same_day.signals if s.action is SignalAction.EXIT_LONG] == []

    # 익일 개장 +3분 — 전량 청산
    decision = strat.decide(_snap(now=EXIT_NOW, price=101.0, lots={SYM: lot}), {})
    [exit_sig] = decision.signals
    assert exit_sig.action is SignalAction.EXIT_LONG
    assert exit_sig.exit_fraction == 1.0
    assert exit_sig.target_weight == 0.0
    assert "지연" not in exit_sig.reason and "손절" not in exit_sig.reason


def test_late_exit_when_the_open_window_was_missed():
    """창을 놓쳐도(재시작·데이터 지연) 포지션을 계속 들고 있지 않는다 —
    남는 것은 문헌이 음수라고 말하는 인트라데이 노출뿐이다. 사유는 구분한다."""
    strat = _strategy()
    lot = {"entry": ENTRY_PRICE, "stop": 97.0, "session": DAY1.isoformat()}
    late = datetime.combine(DAY2, dtime(11, 0), tzinfo=NY)
    [exit_sig] = strat.decide(_snap(now=late, price=101.0, lots={SYM: lot}), {}).signals
    assert exit_sig.action is SignalAction.EXIT_LONG
    assert "지연" in exit_sig.reason


# ============================================================ ⑤ 갭다운 보호 레일

def test_gap_down_beyond_stop_exits_with_stop_reason():
    strat = _strategy(stop_pct=3.0)
    entry, _ = _entry_signal(strat, _snap())
    lot = _lot_from(entry)
    assert lot["stop"] == pytest.approx(97.0)

    # 시가가 -4% 갭다운 → 손절 사유로 즉시 청산
    [exit_sig] = strat.decide(_snap(now=EXIT_NOW, price=96.0, lots={SYM: lot}), {}).signals
    assert exit_sig.action is SignalAction.EXIT_LONG
    assert "손절" in exit_sig.reason

    # -2% 갭다운은 방어선 위 → 평범한 개장 청산(손절 사유가 아니다)
    [ok] = strat.decide(_snap(now=EXIT_NOW, price=98.0, lots={SYM: lot}), {}).signals
    assert "손절" not in ok.reason


def test_entry_sets_stop_but_no_target():
    """드리프트를 자르면 안 되므로 목표가는 두지 않는다."""
    entry, _ = _entry_signal(_strategy(stop_pct=3.0), _snap())
    assert entry.stop == pytest.approx(97.0)
    assert entry.target is None


# ============================================================ ⑥ 재시작 생존

def test_restart_survival_exit_comes_from_the_lot_not_instance_state():
    """껍질·인스턴스를 통째로 버려도(=프로세스 재시작) 익일 청산이 나온다.

    밤을 넘는 값이 `next_state` 에 있었다면 여기서 청산이 나오지 않는다 —
    이 테스트가 "상태 두 갈래" 설계를 고정한다.
    """
    entered = _strategy()
    entry, decision = _entry_signal(entered, _snap())
    lot = _lot_from(entry)
    del entered, decision  # 인스턴스도 next_state 도 버린다

    reborn = OvernightDriftStrategy([SYM], {})     # 새 프로세스의 새 인스턴스
    [exit_sig] = reborn.decide(
        _snap(now=EXIT_NOW, price=101.0, lots={SYM: lot}), {}).signals   # state 비었음
    assert exit_sig.action is SignalAction.EXIT_LONG
    assert exit_sig.symbol == SYM


def test_lot_without_session_is_not_managed():
    """진입일을 모르는 랏은 건드리지 않는다 — 진입 당일에 팔아 전략을 자기
    손으로 무효화하는 것이 최악이다(모듈 docstring "아직 못 하는 것" 5번)."""
    strat = _strategy()
    lot = {"entry": ENTRY_PRICE, "stop": 97.0}   # session 없음
    assert strat.decide(_snap(now=EXIT_NOW, price=96.0, lots={SYM: lot}), {}).signals == ()


def test_foreign_lot_is_not_touched():
    """`entry` 가 없는 랏(다른 전략의 포지션 / 체결 직후)은 내 것이 아니다."""
    strat = _strategy()
    decision = strat.decide(_snap(now=EXIT_NOW, price=96.0, lots={SYM: {}}), {})
    assert [s for s in decision.signals if s.action is SignalAction.EXIT_LONG] == []


# ============================================================ ⑦ 진입 필터

def test_filters_are_disabled_by_default_and_entry_is_unconditional():
    """문헌의 근거는 "무조건 오버나이트 보유"다 — 기본 구성은 갭업이든 음봉이든
    진입한다. 봉 데이터가 아예 없어도 진입한다(필터가 꺼져 있으면 필요 없다)."""
    strat = _strategy()
    assert strat.max_gap_up_pct == 0.0
    assert strat.min_close_vs_open_pct == 0.0
    # 봉을 하나도 주지 않아도 진입한다
    entry, _ = _entry_signal(strat, _snap(bars={}))
    assert entry is not None
    # 요구 데이터 선언에도 봉이 없다 — 켜지 않은 필터 때문에 조회하지 않는다.
    assert _strategy().requirements().bars == ()


def test_max_gap_up_filter_blocks_when_enabled():
    bars = {(SYM, "5m"): _session_bars(105.0, 100.0), (SYM, "1d"): _daily(100.0)}
    # 당일 시가 105 / 전일 종가 100 = +5% 갭업
    blocked, _ = _entry_signal(_strategy(max_gap_up_pct=3.0), _snap(bars=bars))
    assert blocked is None
    allowed, _ = _entry_signal(_strategy(max_gap_up_pct=10.0), _snap(bars=bars))
    assert allowed is not None
    assert "갭업=+5.00%" in allowed.reason


def test_min_close_vs_open_filter_blocks_when_enabled():
    bars = {(SYM, "5m"): _session_bars(100.0, 98.0)}
    # 당일 시가 100 / 현재가 98 = -2%
    blocked, _ = _entry_signal(
        _strategy(min_close_vs_open_pct=0.001), _snap(bars=bars, price=98.0))
    assert blocked is None
    allowed, _ = _entry_signal(
        _strategy(min_close_vs_open_pct=-3.0), _snap(bars=bars, price=98.0))
    assert allowed is not None


def test_enabled_filter_without_data_refuses_entry():
    """확인 불가는 통과가 아니라 거부다 — 운영자가 켜 둔 전제를 확인하지 못한 채
    진입하는 것은 다른 전략을 실행하는 것이다."""
    # 5분봉이 없다(세션 시가를 모른다)
    assert _entry_signal(_strategy(max_gap_up_pct=3.0), _snap(bars={}))[0] is None
    # 5분봉은 있는데 일봉이 없다(전일 종가를 모른다)
    only_intraday = {(SYM, "5m"): _session_bars(100.0, 101.0)}
    assert _entry_signal(_strategy(max_gap_up_pct=3.0), _snap(bars=only_intraday))[0] is None


def test_requirements_declare_bars_only_for_enabled_filters():
    both = OvernightDriftStrategy([SYM], {"max_gap_up_pct": 3.0}).requirements()
    assert {(s, iv) for s, iv, _ in both.bars} == {(SYM, "5m"), (SYM, "1d")}
    intraday_only = OvernightDriftStrategy(
        [SYM], {"min_close_vs_open_pct": 0.5}).requirements()
    assert {(s, iv) for s, iv, _ in intraday_only.bars} == {(SYM, "5m")}
    assert intraday_only.quotes == (SYM,)
    assert intraday_only.needs_positions is True


# ============================================================ ⑧ state 왕복

def test_state_round_trip():
    strat = _strategy()
    decision = strat.decide(_snap(), {})
    assert decision.next_state == {"entered_date": {SYM: DAY1.isoformat()}}
    # 되돌려 주면 그대로 유지된다(창 밖 사이클에서도 소실되지 않는다)
    carried = strat.decide(_snap(minutes_to_close=60.0), decision.next_state)
    assert carried.next_state == decision.next_state


# ============================================================ ⑨ 입력 불변

def test_decide_does_not_mutate_inputs():
    strat = _strategy()
    state = {"entered_date": {"OTHER": "2026-01-02"}}
    lot = {"entry": ENTRY_PRICE, "stop": 97.0, "session": DAY1.isoformat()}
    lots = {SYM: lot}
    snap = _snap(now=EXIT_NOW, price=96.0, lots=lots)

    state_before = copy.deepcopy(state)
    lots_before = copy.deepcopy(lots)
    strat.decide(snap, state)
    assert state == state_before
    assert lots == lots_before

    # 진입 경로도 같다(next_state 는 사본이어야 한다).
    entry_state = {"entered_date": {}}
    before = copy.deepcopy(entry_state)
    result = strat.decide(_snap(), entry_state)
    assert entry_state == before
    assert result.next_state["entered_date"] == {SYM: DAY1.isoformat()}


# ============================================================ 파라미터 검증

@pytest.mark.parametrize("params", [
    {"entry_before_close_minutes": 0},
    {"exit_after_open_minutes": 0},
    {"stop_pct": 0},
    {"stop_pct": -1},
    {"max_gap_up_pct": -1},
    {"target_weight": 0},
    {"target_weight": 1.5},
])
def test_constructor_rejects_invalid_params(params):
    with pytest.raises(ValueError):
        OvernightDriftStrategy([SYM], params)


def test_negative_min_close_vs_open_is_valid():
    """음수 하한("−1% 까지는 허용")은 유효한 설정이다."""
    assert OvernightDriftStrategy([SYM], {"min_close_vs_open_pct": -1.0}).min_close_vs_open_pct == -1.0


# ============================================================ 껍질

def test_shell_satisfies_strategy_protocol():
    shell = OvernightDriftShell([SYM], {})
    assert shell.id == "overnight_drift"
    assert shell.symbols == [SYM]
    assert hasattr(shell, "on_cycle")
    assert isinstance(shell.inner, OvernightDriftStrategy)
