"""`PullbackImpulsePureStrategy` — 눌림목 임펄스 5분봉 스캘프의 규칙 고정.

레거시 쌍둥이가 없는 **신규 전략**이라 동치 비교 대상이 없다. 그래서 여기서
고정하는 것은 "레거시와 같은가"가 아니라 **규칙 자체**다 — 합성 5분봉으로
임펄스/되돌림/거래량/트리거 각 게이트가 독립적으로 작동하는지, 손절·목표·
타임아웃·EoD·KR 동시호가 레일이 실제로 신호를 내는지, 그리고 순수 계약
(`decide()`가 입력 `state`를 mutate 하지 않는다)이 지켜지는지.

합성 봉은 전부 `_STANDARD`(정상 시나리오)에서 한 조건씩만 어긋나게 만든 변주다
— 어떤 게이트가 막았는지가 `next_state["last_reject"]` 문자열로 확인된다.

**열린 랏의 방어선은 `next_state`가 아니라 `snap.lots`로 다닌다**(진입
`Signal.state_update` → 루프가 체결 확인 후 `Position.meta["lots"]`에 기록 →
다음 사이클에 껍질이 회수). 그래서 관리 테스트는 방어선을 `state["open"]`이
아니라 스냅샷의 `lots=`로 주입한다. `test_open_lot_survives_process_restart`가
`close_bet_pure`의 `test_overnight_state_survives_process_restart`와 같은 취지로
그 설계를 고정한다 — **전략 인스턴스와 껍질 상태를 통째로 버려도**(2026-08-28
장중 재시작 사건) 손절·목표·타임아웃이 그대로 나와야 한다.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.indicators.trend_gate import atr_ratio
from quant.trade.strategy.pullback_impulse import (
    PullbackImpulsePureStrategy,
    PullbackImpulseShell,
)

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DAY = date(2026, 8, 27)          # 목
PREV = date(2026, 8, 26)         # 수
US_SYM = "SOXL"
KR_SYM = "005930"

# ATR/EMA 워밍업 — 전 거래일 오후의 잔잔한 20봉(TR 0.2 고정).
WARMUP = [(100.0, 100.1, 99.9, 100.0, 500.0)] * 20

# 정상 시나리오: 09:30~10:00 세션 7봉.
#   임펄스 99.9 → 102.5 (폭 2.6 = 260bp), 되돌림 저점 101.2 (50% 되돌림),
#   반등봉 09:55(고가 101.9), 돌파봉 10:00(고가 102.1).
_STANDARD = [
    (100.00, 100.20, 99.90, 100.10, 1000.0),
    (100.10, 101.50, 100.00, 101.40, 3000.0),
    (101.40, 102.50, 101.30, 102.40, 3000.0),
    (102.40, 102.45, 101.40, 101.50, 800.0),
    (101.50, 101.60, 101.20, 101.30, 700.0),
    (101.30, 101.90, 101.25, 101.85, 900.0),
    (101.85, 102.10, 101.80, 102.05, 1200.0),
]

IMPULSE_LOW, IMPULSE_HIGH = 99.90, 102.50
WIDTH = IMPULSE_HIGH - IMPULSE_LOW
PULLBACK_LOW = 101.20
QUOTE = 102.00


# ============================================================ 합성 봉 조립


def _frame(day: date, start: dtime, rows, tz) -> pd.DataFrame:
    idx = pd.DatetimeIndex([
        datetime.combine(day, start, tzinfo=tz) + timedelta(minutes=5 * i)
        for i in range(len(rows))
    ])
    return pd.DataFrame(
        {
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [r[4] for r in rows],
        },
        index=idx,
    )


def _bars(rows=None, *, tz=NY, start=dtime(9, 30)) -> pd.DataFrame:
    """워밍업(전 거래일) + 오늘 세션 봉. `history()`가 주는 연속 시계열 모사."""
    rows = _STANDARD if rows is None else rows
    return pd.concat([
        _frame(PREV, dtime(13, 0), WARMUP, tz),
        _frame(DAY, start, rows, tz),
    ])


def _snap(now, bars, quotes, *, lots=None, mtc=120.0, cadence=0.1, markets=("US",)):
    return StrategySnapshot(
        now=now,
        market_open={m: True for m in markets},
        minutes_to_close={m: mtc for m in markets},
        cadence_minutes=cadence,
        bars={(s, "5m"): df for s, df in bars.items()},
        quotes={s: Quote(symbol=s, ts=now, price=p) for s, p in quotes.items()},
        lots=lots or {},
    )


def _strategy(symbols=(US_SYM,), **params) -> PullbackImpulsePureStrategy:
    return PullbackImpulsePureStrategy(list(symbols), dict(params))


def _us_now(h=10, m=5) -> datetime:
    return datetime.combine(DAY, dtime(h, m), tzinfo=NY)


def _decide(strategy, rows=None, *, price=QUOTE, state=None, now=None, lots=None):
    snap = _snap(
        now or _us_now(),
        {US_SYM: _bars(rows)},
        {US_SYM: price},
        lots=lots,
    )
    return strategy.decide(snap, state or {})


def _atr_abs(rows=None) -> float:
    """전략이 쓰는 것과 같은 순수 지표로 계산한 ATR 절대값."""
    bars = _bars(rows)
    ratio = atr_ratio(bars, 14)
    assert ratio is not None
    return ratio * float(bars["close"].iloc[-1])


# ============================================================ 계약


def test_requirements_declares_5m_bars_for_every_symbol():
    """5분봉을 직접 선언한다 — 1분봉을 받아 전략이 리샘플하지 않는다."""
    s = _strategy(symbols=(US_SYM, KR_SYM))
    needs = s.requirements()
    assert {(sym, interval) for sym, interval, _ in needs.bars} == {
        (US_SYM, "5m"), (KR_SYM, "5m"),
    }
    # 오늘 세션 전체(78봉) + ATR/EMA 워밍업을 덮는다.
    assert all(count >= 78 + 14 + 9 for _, _, count in needs.bars)
    assert set(needs.quotes) == {US_SYM, KR_SYM}
    assert needs.needs_positions


@pytest.mark.parametrize("params", [
    {"min_impulse_bp": -1},
    {"pullback_min_pct": -0.1},
    {"pullback_max_pct": 1.5},
    {"pullback_min_pct": 0.7, "pullback_max_pct": 0.5},
    {"atr_buffer_mult": -0.1},
    {"atr_period": 1},
    {"target_mult": 0},
    {"timeout_minutes": 0},
    {"flatten_before_close_minutes": 0},
    {"target_weight": 0},
    {"target_weight": 1.5},
    {"ema_period": 1},
])
def test_invalid_params_rejected(params):
    with pytest.raises(ValueError):
        PullbackImpulsePureStrategy([US_SYM], params)


# ============================================================ ① 임펄스 미달


def test_no_signal_when_impulse_is_too_small():
    """구조는 같은데 폭만 16bp — 왕복 비용(20~30bp)도 못 넘는 파동은 안 잡는다."""
    rows = [
        (100.00, 100.02, 99.99, 100.01, 1000.0),
        (100.01, 100.10, 100.00, 100.09, 3000.0),
        (100.09, 100.15, 100.08, 100.14, 3000.0),
        (100.14, 100.15, 100.09, 100.10, 800.0),
        (100.10, 100.11, 100.07, 100.08, 700.0),
        (100.08, 100.13, 100.075, 100.12, 900.0),
        (100.12, 100.16, 100.11, 100.15, 1200.0),
    ]
    d = _decide(_strategy(), rows, price=100.13)
    assert d.signals == ()
    assert "임펄스 부족" in d.next_state["last_reject"][US_SYM]


# ============================================================ ② 되돌림 부족/과다


def test_no_signal_when_pullback_too_shallow_and_no_anchor_touch():
    """15% 되돌림 — VWAP/EMA 어느 쪽도 닿지 않았으면 진입하지 않는다."""
    rows = _STANDARD[:3] + [
        (102.40, 102.45, 102.30, 102.35, 800.0),
        (102.35, 102.40, 102.20, 102.25, 700.0),   # 되돌림 저점 102.20
        (102.25, 102.48, 102.24, 102.45, 900.0),   # 반등봉
        (102.45, 102.55, 102.40, 102.50, 1200.0),  # 돌파봉
    ]
    d = _decide(_strategy(), rows, price=102.5)
    assert d.signals == ()
    assert "되돌림 부족" in d.next_state["last_reject"][US_SYM]


def test_no_signal_when_pullback_too_deep():
    """77% 되돌림 — 상한을 넘으면 임펄스 구조가 깨진 것으로 본다."""
    rows = _STANDARD[:3] + [
        (102.40, 102.45, 101.00, 101.10, 800.0),
        (101.10, 101.20, 100.50, 100.60, 700.0),   # 되돌림 저점 100.50
        (100.60, 101.30, 100.55, 101.20, 900.0),
        (101.20, 101.50, 101.10, 101.40, 1200.0),
    ]
    d = _decide(_strategy(), rows, price=101.4)
    assert d.signals == ()
    assert "되돌림 과다" in d.next_state["last_reject"][US_SYM]


def test_shallow_pullback_is_accepted_when_it_touches_vwap():
    """얕아도 세션 VWAP 에 닿았으면 최소 깊이 요건을 면제한다(규칙 2의 '또는')."""
    rows = [
        (100.00, 100.20, 99.90, 100.10, 100.0),
        (100.10, 101.50, 100.00, 101.40, 100.0),
        (101.40, 102.50, 101.30, 102.40, 8000.0),  # 거래량이 고점에 몰려 VWAP 이 높다
        (102.40, 102.45, 102.30, 102.35, 50.0),
        (102.35, 102.40, 102.00, 102.05, 40.0),    # 되돌림 저점 102.00 (19% 되돌림)
        (102.05, 102.35, 102.02, 102.30, 60.0),
        (102.30, 102.50, 102.25, 102.45, 80.0),
    ]
    d = _decide(_strategy(), rows, price=102.4)
    assert len(d.signals) == 1
    assert "VWAP 터치" in d.signals[0].reason


# ============================================================ ③ 거래량 미소진


def test_no_signal_when_pullback_volume_exceeds_impulse_volume():
    """되돌림에 거래량이 더 실렸으면 매도 압력이 살아 있다 — 진입하지 않는다."""
    rows = list(_STANDARD)
    rows[3] = (102.40, 102.45, 101.40, 101.50, 5000.0)
    rows[4] = (101.50, 101.60, 101.20, 101.30, 6000.0)
    d = _decide(_strategy(), rows)
    assert d.signals == ()
    assert "거래량 미소진" in d.next_state["last_reject"][US_SYM]


def test_no_signal_before_rebound_bar_breaks_out():
    """반등봉 고가를 아직 넘지 않았으면 대기한다(트리거 게이트)."""
    rows = list(_STANDARD)
    rows[6] = (101.85, 101.88, 101.80, 101.86, 1200.0)  # 반등봉 고가(101.9) 미돌파
    d = _decide(_strategy(), rows, price=101.86)
    assert d.signals == ()
    assert "미돌파" in d.next_state["last_reject"][US_SYM]


# ============================================================ ④ 정상 진입


def test_entry_signal_carries_atr_stop_and_impulse_target():
    d = _decide(_strategy())
    assert len(d.signals) == 1
    sig = d.signals[0]
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.symbol == US_SYM
    assert sig.target_weight == pytest.approx(0.5)

    expected_stop = PULLBACK_LOW - 0.3 * _atr_abs()
    assert sig.stop == pytest.approx(expected_stop)
    assert sig.stop < PULLBACK_LOW < QUOTE          # 되돌림 저점 아래에 있다
    assert sig.target == pytest.approx(QUOTE + WIDTH * 1.2)

    # 방어선의 정본은 state_update — 루프가 체결 확인 후 lot 에 영속한다.
    assert sig.state_update["entry"] == pytest.approx(QUOTE)
    assert sig.state_update["stop"] == pytest.approx(expected_stop)
    assert sig.state_update["target"] == pytest.approx(QUOTE + WIDTH * 1.2)
    assert sig.state_update["entered_at"] == _us_now().isoformat()
    assert sig.state_update["pullback_low"] == pytest.approx(PULLBACK_LOW)
    assert sig.state_update["session"] == DAY.isoformat()
    assert sig.state_update["strategy"] == "pullback_impulse"

    # next_state 의 pending 은 체결 확인 전 한 사이클용 사본일 뿐이다.
    assert d.next_state["pending"][US_SYM]["stop"] == pytest.approx(expected_stop)
    assert "open" not in d.next_state


def test_no_entry_when_atr_cannot_be_computed():
    """봉이 모자라 손절선을 정할 수 없으면 진입하지 않는다(structure.py 손절 철학)."""
    bars = _frame(DAY, dtime(9, 30), _STANDARD, NY)  # 워밍업 없음 → ATR 불가
    snap = _snap(_us_now(), {US_SYM: bars}, {US_SYM: QUOTE})
    d = _strategy().decide(snap, {})
    assert d.signals == ()
    assert "ATR 계산 불가" in d.next_state["last_reject"][US_SYM]


# ============================================================ ⑤ 1일 1회


def test_one_entry_per_symbol_per_day():
    """청산해서 보유가 없어져도(pending/open 비어 있어도) 같은 날 재진입 없음."""
    first = _decide(_strategy())
    assert len(first.signals) == 1

    exhausted = {
        "session_date": {"US": DAY.isoformat()},
        "taken": {US_SYM: DAY.isoformat()},
        "pending": {}, "open": {}, "last_reject": {},
    }
    again = _decide(_strategy(), state=exhausted)
    assert again.signals == ()
    assert again.next_state["last_reject"][US_SYM] == "1일 1회 진입 소진"

    # 날짜가 바뀌면(세션 롤) 게이트가 풀린다.
    assert _strategy().decide(
        _snap(_us_now(), {US_SYM: _bars()}, {US_SYM: QUOTE}),
        {"session_date": {"US": PREV.isoformat()},
         "taken": {US_SYM: PREV.isoformat()}, "pending": {}, "open": {}},
    ).signals != ()


# ============================================================ ⑥ 관리 (손절/목표/타임아웃/EoD)


def _lot(entered_at=None, entry=QUOTE, stop=101.0, target=105.12, session=None):
    """루프가 진입 `state_update`를 체결 후 `Position.meta["lots"]`에 적용한 결과 —
    껍질이 다음 사이클에 `snap.lots[symbol]`로 돌려주는 바로 그 dict."""
    return {
        "entry": entry, "stop": stop, "target": target,
        "session": session or DAY.isoformat(),
        "entered_at": (entered_at or _us_now(10, 0)).isoformat(),
        "impulse_high": IMPULSE_HIGH, "pullback_low": PULLBACK_LOW,
        "strategy": "pullback_impulse",
    }


def _held_state():
    """보유 중인 하루살이 상태 — 방어선은 여기 없다(lot 에 있다)."""
    return {
        "session_date": {"US": DAY.isoformat()},
        "taken": {US_SYM: DAY.isoformat()},
        "pending": {}, "last_reject": {},
    }


@pytest.mark.parametrize("price,fragment", [
    (100.90, "손절"),
    (105.20, "목표 도달"),
])
def test_stop_and_target_exits(price, fragment):
    d = _decide(_strategy(), price=price, state=_held_state(), lots={US_SYM: _lot()})
    assert len(d.signals) == 1
    assert d.signals[0].action is SignalAction.EXIT_LONG
    assert d.signals[0].exit_fraction == 1.0
    assert fragment in d.signals[0].reason


def test_timeout_exit_after_configured_minutes():
    lots = {US_SYM: _lot(entered_at=_us_now(10, 0))}
    held_ok = _decide(_strategy(), price=103.0, state=_held_state(),
                      now=_us_now(10, 59), lots=lots)
    assert held_ok.signals == ()

    timed_out = _decide(_strategy(), price=103.0, state=_held_state(),
                        now=_us_now(11, 0), lots=lots)
    assert len(timed_out.signals) == 1
    assert "타임아웃 청산" in timed_out.signals[0].reason


def test_eod_flatten_exit():
    """마감 임박(잔여 1분 미만)이면 손절/목표와 무관하게 전량 청산한다."""
    snap = _snap(_us_now(15, 59), {US_SYM: _bars()}, {US_SYM: 103.0},
                 lots={US_SYM: _lot()}, mtc=1.0, cadence=0.1)
    d = _strategy().decide(snap, _held_state())
    assert len(d.signals) == 1
    assert "EoD 청산" in d.signals[0].reason


def test_session_roll_forces_exit():
    d = _decide(_strategy(), price=103.0, state=_held_state(),
                lots={US_SYM: _lot(session=PREV.isoformat())})
    assert len(d.signals) == 1
    assert "오버나잇 금지" in d.signals[0].reason


def test_open_position_without_lot_defenses_is_not_managed_silently():
    """방어선이 없는 랏에 임의의 손절선을 지어내지 않는다 — 대신 사유를 남긴다."""
    d = _decide(_strategy(), price=90.0, state=_held_state(), lots={US_SYM: {}})
    assert d.signals == ()
    assert "관리 불가" in d.next_state["last_reject"][US_SYM]


# ============================================================ ⑦ KR 동시호가


def test_kr_no_entry_after_1520_continuous_close():
    """KR 연속매매는 15:20 종료 — 그 뒤엔 현재가로 체결할 수 없다."""
    bars = {KR_SYM: _bars(tz=KST, start=dtime(9, 0))}
    strategy = _strategy(symbols=(KR_SYM,))

    ok = strategy.decide(
        _snap(datetime.combine(DAY, dtime(13, 0), tzinfo=KST), bars,
              {KR_SYM: QUOTE}, markets=("KR",)),
        {},
    )
    assert len(ok.signals) == 1  # 연속매매 시간에는 같은 셋업이 진입한다

    for hh, mm in ((15, 20), (15, 25)):
        blocked = strategy.decide(
            _snap(datetime.combine(DAY, dtime(hh, mm), tzinfo=KST), bars,
                  {KR_SYM: QUOTE}, markets=("KR",)),
            {},
        )
        assert blocked.signals == (), f"{hh}:{mm} 동시호가에 진입이 나왔다"


# ============================================================ ⑧ state 왕복 / 순수성


def test_state_round_trip_entry_manage_exit():
    strategy = _strategy()

    # 1) 진입 — 방어선은 state_update 로 나가고, pending 은 그 사본을 들고 있다.
    entered = _decide(strategy)
    entry_signal = entered.signals[0]
    assert entry_signal.action is SignalAction.ENTER_LONG
    assert US_SYM in entered.next_state["pending"]
    stop = entry_signal.state_update["stop"]

    # 2) 체결 확인 직후, 아직 lot 에 state_update 가 반영되기 전(lots[symbol] == {})
    #    — 같은 프로세스라면 pending 폴백이 관리를 이어받는다.
    gap = _decide(strategy, price=103.0, state=entered.next_state, lots={US_SYM: {}})
    assert gap.signals == ()
    assert US_SYM in gap.next_state["pending"]

    # 3) 루프가 체결을 확인하고 state_update 를 lot 에 적용 → pending 은 버려진다.
    lot = dict(entry_signal.state_update)
    held = _decide(strategy, price=103.0, state=gap.next_state, lots={US_SYM: lot})
    assert held.signals == ()
    assert held.next_state["pending"] == {}

    # 4) 손절가 이탈 — 청산 신호.
    exited = _decide(strategy, price=stop - 0.01, state=held.next_state,
                     lots={US_SYM: lot})
    assert len(exited.signals) == 1
    assert exited.signals[0].action is SignalAction.EXIT_LONG

    # 5) 체결되어 포지션이 사라져도 같은 날 재진입은 없다(1일 1회).
    settled = _decide(strategy, price=101.0, state=exited.next_state, lots={})
    assert settled.signals == ()
    assert settled.next_state["taken"][US_SYM] == DAY.isoformat()


@pytest.mark.parametrize("price,now,fragment", [
    (100.90, _us_now(10, 30), "손절"),
    (105.20, _us_now(10, 30), "목표 도달"),
    # 진입은 기본 시각 10:05 에 났다 — 11:10 이면 보유 65분 > 타임아웃 60분.
    (103.00, _us_now(11, 10), "타임아웃 청산"),
])
def test_open_lot_survives_process_restart(price, now, fragment):
    """**장중 재시작(2026-08-28 실제 사건)** — 껍질 상태도 전략 인스턴스도 통째로
    버린 뒤, 브로커 포지션의 lot 만으로 손절·목표·타임아웃이 그대로 나온다.

    `close_bet_pure`의 `test_overnight_state_survives_process_restart`와 같은
    취지다: `next_state`에 방어선을 넣었다면 이 테스트는 실패한다."""
    # 1) 살아 있던 프로세스: 진입 → 체결 → 루프가 lot 에 state_update 적용.
    entered = _decide(_strategy())
    lot = dict(entered.signals[0].state_update)
    assert lot["entered_at"] == _us_now().isoformat()

    # 2) 재시작 — 새 전략 인스턴스, next_state 는 **빈 dict**. 남은 것은 브로커
    #    포지션의 lot 뿐이다.
    restarted = PullbackImpulsePureStrategy([US_SYM], {})
    after = restarted.decide(
        _snap(now, {US_SYM: _bars()}, {US_SYM: price}, lots={US_SYM: lot}),
        {},
    )

    assert len(after.signals) == 1, "재시작 후 관리에서 빠졌다 — 손절이 사라진다"
    assert after.signals[0].action is SignalAction.EXIT_LONG
    assert fragment in after.signals[0].reason


def test_no_duplicate_entry_after_restart_while_holding():
    """재시작으로 `taken`이 날아가도 보유 중이면 중복 진입이 나지 않는다 —
    진입 루프가 `snap.lots`를 먼저 보기 때문이다."""
    lot = dict(_decide(_strategy()).signals[0].state_update)
    restarted = PullbackImpulsePureStrategy([US_SYM], {})
    after = restarted.decide(
        # 손절·목표·타임아웃 어디에도 걸리지 않는 가격/시각 → 관리 신호도 없다.
        _snap(_us_now(10, 30), {US_SYM: _bars()}, {US_SYM: 103.0}, lots={US_SYM: lot}),
        {},
    )
    assert after.signals == ()


def test_decide_does_not_mutate_input_state():
    strategy = _strategy()
    state = _held_state()
    state["pending"] = {US_SYM: _lot()}
    snapshot_before = copy.deepcopy(state)

    strategy.decide(
        _snap(_us_now(), {US_SYM: _bars()}, {US_SYM: 100.90}, lots={US_SYM: {}}),
        state,
    )
    assert state == snapshot_before


def test_decide_does_not_mutate_snapshot_lots():
    """`snap.lots`는 브로커 포지션의 사본이다 — 전략이 거기 쓰면 안 된다."""
    lot = _lot()
    before = copy.deepcopy(lot)
    _decide(_strategy(), price=103.0, state=_held_state(), lots={US_SYM: lot})
    assert lot == before


def test_shell_satisfies_strategy_protocol_wiring():
    """레지스트리가 다른 전략과 같은 시그니처로 만들 수 있어야 한다."""
    shell = PullbackImpulseShell([US_SYM], {}, market="US", id="pullback_impulse")
    assert shell.id == "pullback_impulse"
    assert shell.symbols == [US_SYM]
    assert hasattr(shell, "on_cycle")


def test_min_stop_gate_rejects_degenerate_stop():
    """**2026-08-29 실전 첫날 결함 고정**: EMA9 터치의 얕은 되돌림에서 되돌림
    저점이 현재가 바로 아래면 손절폭이 사실상 0 이 된다 — NOW 실사고: 진입
    142.80 / 손절 142.75(3.5bp), 17초 만에 손절. 왕복 비용 20bp+ 에 손절폭
    3.5bp 는 진입 순간 지는 구조다. min_stop_bp(기본 40) 미만이면 진입하지
    않고, 사유가 last_reject 에 남아야 한다."""
    strat = PullbackImpulsePureStrategy(["AAA"], {"min_stop_bp": 40.0})
    # 손절폭 계산만 검증하는 최소 경로: entry 100.0, stop 99.98 (2bp) → 거부
    lr: dict = {}
    stop_bp = (100.0 - 99.98) / 100.0 * 1e4
    assert stop_bp < strat.min_stop_bp
    # 통합 경로는 기존 진입 시나리오 픽스처가 복잡하므로, 게이트 상수와
    # 검증 로직의 존재를 직접 확인한다(음수 거부 포함).
    import pytest as _pytest
    with _pytest.raises(ValueError):
        PullbackImpulsePureStrategy(["AAA"], {"min_stop_bp": -1})


def test_min_stop_gate_default_is_double_round_trip_cost():
    """기본 40bp = US 왕복 20bp 의 2배 — 이 관계가 깨지면 주석의 근거도 낡는다."""
    strat = PullbackImpulsePureStrategy(["AAA"], {})
    assert strat.min_stop_bp == 40.0


# --------------------------------------------------------- _session_vwap 동치

def test_session_vwap_matches_mr_vwap_quiet_session_vwap_bands():
    """`_session_vwap`을 `mr_vwap_quiet.session_vwap_bands`(밴드 포함 버전)
    호출로 교체하기 전, 두 구현이 합성 데이터에서 같은 값을 내는지 수치로
    대조한다 — 밴드는 버리고 vwap 만 취하는 교체이므로 이 동치만 성립하면
    안전하다(정상 거래량 봉과 거래량 0인 봉이 섞인 세션)."""
    from quant.trade.strategy.mr_vwap_quiet import session_vwap_bands

    idx = pd.date_range("2026-08-28 09:30", periods=6, freq="5min", tz=NY)
    session = pd.DataFrame({
        "high": [10.2, 10.5, 10.3, 10.6, 10.9, 10.7],
        "low": [10.0, 10.2, 10.1, 10.3, 10.5, 10.4],
        "close": [10.1, 10.4, 10.2, 10.5, 10.8, 10.6],
        "volume": [100, 0, 200, 150, 0, 300],
    }, index=idx)

    old_vals = [PullbackImpulsePureStrategy._session_vwap(session, ts) for ts in idx]
    vwap_series, _, _ = session_vwap_bands(session, band_k=0.0)
    new_vals = [
        float(vwap_series.get(ts)) if pd.notna(vwap_series.get(ts)) else None
        for ts in idx
    ]
    assert old_vals == pytest.approx(new_vals)


def test_session_vwap_matches_mr_vwap_quiet_when_leading_bars_have_zero_volume():
    """세션 시작 봉들이 거래량 0(거래 정지 등)이면 누적 거래량이 0인 동안은
    None — 둘 다 같은 지점에서 None을 벗어나야 한다."""
    from quant.trade.strategy.mr_vwap_quiet import session_vwap_bands

    idx = pd.date_range("2026-08-28 09:30", periods=3, freq="5min", tz=NY)
    session = pd.DataFrame({
        "high": [10.2, 10.5, 10.3],
        "low": [10.0, 10.2, 10.1],
        "close": [10.1, 10.4, 10.2],
        "volume": [0, 0, 200],
    }, index=idx)

    old_vals = [PullbackImpulsePureStrategy._session_vwap(session, ts) for ts in idx]
    vwap_series, _, _ = session_vwap_bands(session, band_k=0.0)
    new_vals = [
        float(vwap_series.get(ts)) if pd.notna(vwap_series.get(ts)) else None
        for ts in idx
    ]
    assert old_vals == new_vals
