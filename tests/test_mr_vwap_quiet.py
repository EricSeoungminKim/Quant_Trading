"""`MrVwapQuietStrategy`(저거래량 VWAP 평균회귀) 계약 고정.

이 전략은 **신규**라 동치를 비교할 레거시 쌍둥이가 없다. 그래서
`test_scalp_1m_pure.py`(legacy vs pure 나란히 비교) 방식이 아니라, 규칙 하나하나를
합성 5분봉으로 직접 고정한다.

`StrategySnapshot` 을 손으로 조립해 `decide()` 를 직접 부른다 — 이 전략의 계약이
곧 순수함수 계약이므로(`quant/core/strategy_api.py`) 껍질을 태우는 것보다 이쪽이
계약을 정확히 겨냥한다. 껍질 배선(`MrVwapQuietShell`)은 별도 테스트 1건으로 확인한다.

## 합성 시나리오 설계

기준 시나리오(`_scenario`)는 **조용한 횡보 + 급락 2봉 + 밴드 안 복귀 1봉**이다:

- 36봉: 진폭 ±2.25 의 결정론적 톱니(횡보) — ADX 를 20 아래로 유지하기 위한
  배경 노이즈다. 배경이 너무 평평하면 마지막 급락이 방향성 지표를 독점해
  ADX 가 90 을 넘는다(실제로 그렇게 관측했다).
- 2봉: 95.5 → 95.0 급락 (종가가 그 시점 하단 밴드 **아래**로 마감)
- 1봉: 98.0 복귀 (종가가 하단 밴드 **위**, VWAP 아래)

이 상태에서 VWAP≈99.76 / 하단≈97.34 / ADX≈17.8 / RVOL=1.0 / 갭=0% 이라
기본 파라미터(band_k=2.0, rvol_max=1.2, adx_max=20, max_gap_pct=1.0,
target_min_bp=60)를 전부 통과한다. 필터 테스트는 이 기준에서 **한 가지만**
바꿔 거부를 확인한다.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.models import Quote, SignalAction
from quant.core.strategy_api import StrategySnapshot
from quant.trade.strategy.mr_vwap_quiet import (
    MrVwapQuietShell,
    MrVwapQuietStrategy,
    gap_pct,
    relative_volume,
    session_vwap_bands,
)

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
DAY = date(2026, 1, 5)   # 월요일
US_SYM = "TSTU"
KR_SYM = "005930"

# 배경 횡보 톱니(진폭 ±2.25) — 모듈 docstring "합성 시나리오 설계" 절.
# 진폭이 작으면 마지막 급락이 방향성 지표를 독점해 ADX 가 90 을 넘는다(실측).
_CHOP = [0.0, 2.25, -1.5, 1.2, -2.4, 1.8, -1.05, 1.95, -2.25, 0.75, -1.8, 1.5]
_BASE_N = 36
_DIP = [95.5, 95.0]
_RECOVER = 98.0


# ============================================================ 합성 봉

def _bars(closes: list[float], start: datetime, volumes: list[float] | None = None
          ) -> pd.DataFrame:
    """종가 시퀀스 → 5분봉 OHLCV. 시가 = 직전 종가, 고/저는 ±0.05 여유."""
    idx = pd.date_range(start=start, periods=len(closes), freq="5min")
    opens, highs, lows = [], [], []
    prev = closes[0]
    for c in closes:
        opens.append(prev)
        highs.append(max(prev, c) + 0.05)
        lows.append(min(prev, c) - 0.05)
        prev = c
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "volume": volumes if volumes is not None else [1000.0] * len(closes)},
        index=idx,
    )


def _scenario_closes(recover: float = _RECOVER) -> list[float]:
    base = [100 + _CHOP[i % len(_CHOP)] for i in range(_BASE_N)]
    return base + _DIP + [recover]


def _daily(prev_close: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(end=datetime(2026, 1, 2, tzinfo=NY), periods=3, freq="1D")
    return pd.DataFrame(
        {"open": [prev_close] * 3, "high": [prev_close] * 3, "low": [prev_close] * 3,
         "close": [prev_close] * 3, "volume": [1e6] * 3},
        index=idx,
    )


def _snapshot(
    *, symbol: str = US_SYM, market: str = "US", closes: list[float] | None = None,
    volumes: list[float] | None = None, price: float | None = None,
    now: datetime | None = None, session_start: datetime | None = None,
    prev_close: float = 100.0, minutes_to_close: float = 195.0,
    lots: dict | None = None, market_open: bool = True,
) -> StrategySnapshot:
    tz = NY if market == "US" else KST
    open_t = 9.5 if market == "US" else 9.0
    if session_start is None:
        h = int(open_t)
        session_start = datetime(DAY.year, DAY.month, DAY.day, h,
                                 30 if market == "US" else 0, tzinfo=tz)
    closes = _scenario_closes() if closes is None else closes
    bars = _bars(closes, session_start, volumes)
    if now is None:
        now = bars.index[-1].to_pydatetime() + timedelta(minutes=5)
    quote_price = closes[-1] if price is None else price
    return StrategySnapshot(
        now=now,
        market_open={market: market_open},
        minutes_to_close={market: minutes_to_close},
        cadence_minutes=5.0 / 60,
        bars={(symbol, "5m"): bars, (symbol, "1d"): _daily(prev_close)},
        quotes={symbol: Quote(symbol=symbol, ts=now, price=quote_price)},
        lots=lots if lots is not None else {},
    )


def _strategy(symbol: str = US_SYM, **params) -> MrVwapQuietStrategy:
    return MrVwapQuietStrategy([symbol], params)


def _entries(decision) -> list:
    return [s for s in decision.signals if s.action is SignalAction.ENTER_LONG]


def _exits(decision) -> list:
    return [s for s in decision.signals if s.action is SignalAction.EXIT_LONG]


# ============================================================ ① VWAP 손계산 대조

def test_vwap_matches_hand_calculation():
    """VWAP = Σ(typical × volume) / Σvolume 를 손으로 계산한 값과 대조한다.

    3봉, typical = (H+L+C)/3:
      봉1 H=11 L=9  C=10 → tp=10,  vol=100
      봉2 H=13 L=11 C=12 → tp=12,  vol=300
      봉3 H=23 L=19 C=21 → tp=21,  vol=100
    누적 VWAP:
      i=0: 10
      i=1: (10*100 + 12*300) / 400 = 4600/400 = 11.5
      i=2: (4600 + 21*100) / 500 = 6700/500 = 13.4
    """
    idx = pd.date_range(datetime(2026, 1, 5, 10, 0, tzinfo=NY), periods=3, freq="5min")
    bars = pd.DataFrame(
        {"open": [10.0, 11.0, 19.0], "high": [11.0, 13.0, 23.0],
         "low": [9.0, 11.0, 19.0], "close": [10.0, 12.0, 21.0],
         "volume": [100.0, 300.0, 100.0]},
        index=idx,
    )
    vwap, lower, upper = session_vwap_bands(bars, 2.0)
    assert vwap.iloc[0] == pytest.approx(10.0)
    assert vwap.iloc[1] == pytest.approx(11.5)
    assert vwap.iloc[2] == pytest.approx(13.4)

    # σ 는 typical price 의 모집단 표준편차(ddof=0). 첫 봉은 정의 불가 → NaN.
    assert pd.isna(lower.iloc[0]) and pd.isna(upper.iloc[0])
    # i=1: tp {10, 12}, 평균 11, σ = 1.0 → 밴드 = VWAP ± 2.0
    assert lower.iloc[1] == pytest.approx(11.5 - 2.0)
    assert upper.iloc[1] == pytest.approx(11.5 + 2.0)


def test_vwap_bands_have_no_lookahead():
    """각 시점의 VWAP 은 그 시점까지의 봉만 쓴다 — 뒤에 봉을 더 붙여도 앞의
    값이 바뀌지 않는다. 진입 조건("직전 봉 종가가 *그때의* 하단 밖이었나")이
    사후확증이 아니라는 근거다."""
    closes = _scenario_closes()
    full = _bars(closes, datetime(2026, 1, 5, 9, 30, tzinfo=NY))
    head = full.iloc[:20]
    v_full, l_full, _ = session_vwap_bands(full, 2.0)
    v_head, l_head, _ = session_vwap_bands(head, 2.0)
    pd.testing.assert_series_equal(v_full.iloc[:20], v_head)
    pd.testing.assert_series_equal(l_full.iloc[:20], l_head)


def test_vwap_none_when_no_volume():
    """누적 거래량 0 이면 VWAP 은 NaN — 0 으로 채우거나 직전 값을 끌어오지 않는다."""
    bars = _bars([10.0, 10.0], datetime(2026, 1, 5, 10, 0, tzinfo=NY), volumes=[0.0, 0.0])
    vwap, _, _ = session_vwap_bands(bars, 2.0)
    assert vwap.isna().all()
    assert session_vwap_bands(bars.iloc[:0], 2.0) is None


# ============================================================ ② 밴드 밖 → 안 복귀

def test_enters_only_on_return_inside_band():
    decision = _strategy().decide(_snapshot(), {})
    entries = _entries(decision)
    assert len(entries) == 1
    sig = entries[0]
    assert sig.symbol == US_SYM
    assert sig.strategy_id == "mr_vwap_quiet"
    # 목표는 VWAP(중심선), 손절은 신호봉 저가 아래.
    assert sig.target == pytest.approx(99.757, abs=0.01)
    assert sig.stop == pytest.approx(94.95 * 0.997, abs=0.01)
    assert sig.stop < sig.target
    assert "VWAP 평균회귀" in sig.reason


def test_no_entry_while_still_outside_band():
    """밴드 밖에 머문 채 마감하면 무신호 — '복귀 마감'이 조건이다."""
    # 복귀봉 종가 97.0 은 그 시점 하단(≈97.23) 아래 → 아직 밖.
    snap = _snapshot(closes=_scenario_closes(recover=97.0))
    assert _entries(_strategy().decide(snap, {})) == []


def test_no_entry_without_prior_breach():
    """급락 없이 밴드 안에서만 논 세션은 무신호 — 이탈이 없으면 회귀도 없다."""
    closes = [100 + _CHOP[i % len(_CHOP)] for i in range(39)]
    assert _entries(_strategy().decide(_snapshot(closes=closes), {})) == []


def test_no_entry_above_vwap():
    """복귀가 과해 종가가 VWAP 위면 무신호 — 목표가 진입가 아래가 된다."""
    closes = _scenario_closes(recover=105.0)
    assert _entries(_strategy().decide(_snapshot(closes=closes), {})) == []


def test_no_entry_when_target_below_cost_threshold():
    """목표(VWAP)까지의 거리가 왕복 비용 문턱을 못 넘으면 무신호.

    기준 시나리오는 179bp 라 통과한다 — 문턱을 300bp 로 올리면 같은 데이터가
    거부된다(비용 게이트만 격리 검증)."""
    assert _entries(_strategy(target_min_bp=300).decide(_snapshot(), {})) == []
    assert len(_entries(_strategy(target_min_bp=100).decide(_snapshot(), {}))) == 1


# ============================================================ ③ RVOL

def test_no_entry_when_rvol_too_high():
    """마지막 봉 거래량이 튀면(= 시끄러운 종목) 무신호. 우리 -0.46 실측의 구현."""
    vols = [1000.0] * 38 + [5000.0]   # RVOL = 5.0
    snap = _snapshot(volumes=vols)
    assert _entries(_strategy().decide(snap, {})) == []
    # 문턱을 올리면 같은 데이터로 진입한다 — 막은 것이 RVOL 필터임을 확정한다.
    assert len(_entries(_strategy(rvol_max=10.0).decide(snap, {}))) == 1


def test_relative_volume_excludes_the_bar_itself():
    """분자 봉을 분모에서 뺀다 — 포함하면 서지가 자기 평균을 끌어올린다."""
    bars = _bars([100.0] * 5, datetime(2026, 1, 5, 10, 0, tzinfo=NY),
                 volumes=[100.0, 100.0, 100.0, 100.0, 400.0])
    assert relative_volume(bars, 4) == pytest.approx(4.0)   # 400 / 100
    assert relative_volume(bars, 10) is None                # baseline 부족 → 판단 불가


# ============================================================ ④ ADX

def test_no_entry_when_adx_too_high():
    """추세 구간이면 무신호 — 밴드 하단이 계속 새로 갱신되는 장세다.

    기준 시나리오 ADX≈17.8. `adx_max` 를 5 로 조이면 같은 데이터가 거부된다."""
    assert _entries(_strategy(adx_max=5.0).decide(_snapshot(), {})) == []


def test_no_entry_when_adx_uncomputable():
    """ADX 를 계산할 수 없으면(봉 부족) **거부**한다 — 확인 불가는 통과가 아니다
    (모듈 docstring "확인 불가는 통과가 아니라 거부다" 절, trend_gate 관례와 반대)."""
    snap = _snapshot()
    short = snap.bars[(US_SYM, "5m")].tail(10)   # ADX(14)는 28봉이 필요하다
    snap = StrategySnapshot(
        now=snap.now, market_open=snap.market_open,
        minutes_to_close=snap.minutes_to_close, cadence_minutes=snap.cadence_minutes,
        bars={(US_SYM, "5m"): short, (US_SYM, "1d"): snap.bars[(US_SYM, "1d")]},
        quotes=snap.quotes, lots=snap.lots,
    )
    assert _entries(_strategy().decide(snap, {})) == []


# ============================================================ ⑤ 갭

def test_no_entry_when_gap_too_large():
    """당일 시가가 전일 종가에서 크게 벌어지면(= 뉴스) 무신호."""
    snap = _snapshot(prev_close=98.0)      # 시가 100 vs 전일 98 → 갭 2.04%
    assert _entries(_strategy().decide(snap, {})) == []
    assert len(_entries(_strategy(max_gap_pct=3.0).decide(snap, {}))) == 1


def test_no_entry_when_gap_uncomputable():
    """일봉이 없으면 갭을 확인할 수 없다 → 거부."""
    snap = _snapshot()
    snap = StrategySnapshot(
        now=snap.now, market_open=snap.market_open,
        minutes_to_close=snap.minutes_to_close, cadence_minutes=snap.cadence_minutes,
        bars={(US_SYM, "5m"): snap.bars[(US_SYM, "5m")]},
        quotes=snap.quotes, lots=snap.lots,
    )
    assert _entries(_strategy().decide(snap, {})) == []
    assert gap_pct(None, 100.0) is None


# ============================================================ ⑥ 시간대

def test_no_entry_before_entry_window_opens():
    """개장+60분 전에는 무신호 — VWAP 표본이 얇아 밴드가 무의미하다."""
    snap = _snapshot(now=datetime(2026, 1, 5, 10, 15, tzinfo=NY))  # 개장+45분
    assert _entries(_strategy().decide(snap, {})) == []
    # 창을 열면 같은 데이터로 진입한다.
    assert len(_entries(_strategy(entry_after_open_minutes=30).decide(snap, {}))) == 1


def test_no_entry_near_close():
    """마감-60분 안쪽이면 무신호 — 목표까지 갈 시간이 없다."""
    snap = _snapshot(now=datetime(2026, 1, 5, 15, 30, tzinfo=NY), minutes_to_close=30)
    assert _entries(_strategy().decide(snap, {})) == []


def test_no_entry_when_market_closed():
    snap = _snapshot(market_open=False)
    assert _strategy().decide(snap, {}).signals == ()


# ============================================================ ⑦ 목표(VWAP) 청산

def _day_state() -> dict:
    """`next_state` 에 사는 것은 **하루짜리 값 둘뿐**이다 — 방어선은 lot 에 산다."""
    return {"session_date": {"US": DAY.isoformat()}, "entries_today": {US_SYM: 1}}


def _lot(**over) -> dict:
    """브로커 포지션의 `meta["lots"][id]` — 껍질이 `snap.lots` 로 돌려주는 값.

    진입 `Signal.state_update` 가 루프를 거쳐 여기 쓰인 결과를 흉내 낸다."""
    lot = {"entry": 98.0, "stop": 94.66, "target": 99.757,
           "session": DAY.isoformat(),
           "entered_at": datetime(2026, 1, 5, 12, 45, tzinfo=NY).isoformat(),
           "strategy": "mr_vwap_quiet"}
    lot.update(over)
    return lot


def test_exits_at_vwap_target():
    snap = _snapshot(price=99.80, lots={US_SYM: _lot()})
    exits = _exits(_strategy().decide(snap, _day_state()))
    assert len(exits) == 1
    assert "목표(VWAP) 도달" in exits[0].reason
    assert exits[0].exit_fraction == 1.0


def test_exits_at_stop():
    snap = _snapshot(price=94.00, lots={US_SYM: _lot()})
    exits = _exits(_strategy().decide(snap, _day_state()))
    assert len(exits) == 1 and "손절" in exits[0].reason


def test_holds_between_stop_and_target():
    snap = _snapshot(price=98.5, lots={US_SYM: _lot()})
    assert _strategy().decide(snap, _day_state()).signals == ()


def test_ignores_position_owned_by_another_strategy():
    """`snap.lots[symbol] == {}` 는 '남이 들고 있다' 또는 '내 lot 필드가 아직
    없다' 둘 중 하나다(`shell.py`) — 어느 쪽이든 내 관리 대상이 아니므로 청산
    주문을 내면 안 된다. `_my_lot` 이 `entry` 유무로 판정한다."""
    snap = _snapshot(price=94.00, lots={US_SYM: {}})
    assert _exits(_strategy().decide(snap, _day_state())) == []


def test_skips_lot_with_half_written_defense_lines():
    """`stop`/`target` 중 하나가 없는 랏은 관리하지 않는다 — 방어선을 지어내지
    않는다(클래스 docstring "아직 못 하는 것" 2번)."""
    broken = _lot()
    del broken["stop"]
    snap = _snapshot(price=94.00, lots={US_SYM: broken})
    assert _strategy().decide(snap, _day_state()).signals == ()


# ============================================================ ⑧ 타임아웃

def test_exits_on_timeout():
    now = datetime(2026, 1, 5, 12, 45, tzinfo=NY)
    entered = (now - timedelta(minutes=80)).isoformat()
    snap = _snapshot(price=98.5, now=now, lots={US_SYM: _lot(entered_at=entered)})
    exits = _exits(_strategy().decide(snap, _day_state()))
    assert len(exits) == 1 and "타임아웃" in exits[0].reason
    # 74분이면 아직 안 나간다(기본 75분).
    held74 = (now - timedelta(minutes=74)).isoformat()
    snap74 = _snapshot(price=98.5, now=now, lots={US_SYM: _lot(entered_at=held74)})
    assert _strategy().decide(snap74, _day_state()).signals == ()


# ============================================================ ⑨ EoD / 오버나잇

def test_exits_at_eod():
    """정규장 마감 직전 강제청산 — 오버나잇 금지."""
    snap = _snapshot(price=98.5, now=datetime(2026, 1, 5, 15, 59, tzinfo=NY),
                     minutes_to_close=1.0, lots={US_SYM: _lot()})
    exits = _exits(_strategy().decide(snap, _day_state()))
    assert len(exits) == 1 and "EoD 청산" in exits[0].reason


def test_kr_eod_fires_before_closing_auction():
    """KR 은 **15:20**(연속 거래 종료) 기준으로 청산한다.

    `Clock.minutes_to_close` 는 정규장 마감(15:30)까지를 세므로 그것만 믿으면
    청산 신호가 동시호가 안에서 나간다 — 체결될 수 없는 주문이다
    (2026-08-26 실사고와 같은 계열의 결함)."""
    now = datetime(2026, 1, 5, 15, 19, tzinfo=KST)
    state = {"session_date": {"KR": DAY.isoformat()}, "entries_today": {}}
    snap = _snapshot(symbol=KR_SYM, market="KR", price=98.5, now=now,
                     minutes_to_close=11.0, lots={KR_SYM: _lot()},
                     session_start=datetime(2026, 1, 5, 12, 0, tzinfo=KST))
    exits = _exits(MrVwapQuietStrategy([KR_SYM], {}).decide(snap, state))
    assert len(exits) == 1 and "EoD 청산" in exits[0].reason


def test_exits_on_session_roll():
    """다음 세션 첫 사이클에 남아 있는 포지션은 강제청산한다(오버나잇 레일).

    세션 롤이 `entries_today` 를 비우는 것과 **같은 사이클**에 이 청산이 나가야
    한다 — 진입 세션(`lot["session"]`)은 lot 에 있으므로 롤 정리가 지우지 못한다."""
    snap = _snapshot(price=98.5, now=datetime(2026, 1, 6, 12, 45, tzinfo=NY),
                     session_start=datetime(2026, 1, 6, 9, 30, tzinfo=NY),
                     lots={US_SYM: _lot()})
    exits = _exits(_strategy().decide(snap, _day_state()))
    assert len(exits) == 1 and "오버나잇 금지" in exits[0].reason


# ============================================================ ⑩ KR 15:20 이후 진입 없음

def _kr_snapshot(now: datetime, minutes_to_close: float) -> StrategySnapshot:
    """마지막 완성봉이 15:10 에 시작하도록 세션을 배치한다 — 15:19 와 15:21 을
    같은 데이터로 비교하기 위해서다."""
    return _snapshot(
        symbol=KR_SYM, market="KR", now=now, minutes_to_close=minutes_to_close,
        session_start=datetime(2026, 1, 5, 12, 0, tzinfo=KST),
    )


def test_kr_no_entry_after_continuous_session_ends():
    """15:20~15:30 동시호가에는 현재가로 체결되지 않는다 — 진입 금지.

    창 파라미터를 0 으로 열어 `in_continuous_session` 가드만 남긴다."""
    params = {"entry_after_open_minutes": 0, "entry_before_close_minutes": 0}
    strat = MrVwapQuietStrategy([KR_SYM], params)

    before = strat.decide(_kr_snapshot(datetime(2026, 1, 5, 15, 19, tzinfo=KST), 11.0), {})
    assert len(_entries(before)) == 1, "15:19 는 연속 거래 구간이라 진입 가능해야 한다"

    after = strat.decide(_kr_snapshot(datetime(2026, 1, 5, 15, 21, tzinfo=KST), 9.0), {})
    assert _entries(after) == [], "15:20 이후에는 진입이 없어야 한다"


# ============================================================ ⑪ state 왕복 + 재시작

def _apply_fill(signal) -> dict:
    """`loop._execute_signal` 이 **체결 확인 후** 하는 일의 최소 재현
    (`quant/trade/loop.py:412-422`): 진입 신호의 `state_update` 를
    `Position.meta["lots"][id]` 에 쓴다. 껍질은 그걸 `snap.lots` 로 돌려준다."""
    return dict(signal.state_update)


def test_defense_lines_leave_via_state_update_not_next_state():
    """**방어선이 `next_state` 에 있었다면 실패하는 테스트.**

    entry/stop/target/entered_at 은 `Signal.state_update` 로만 나가야 한다 —
    `next_state` 에 새어 들어가면 장중 재시작이 손절을 지운다(2026-08-28 사건)."""
    d = _strategy().decide(_snapshot(), {})
    sig = _entries(d)[0]
    assert set(sig.state_update) == {
        "entry", "stop", "target", "session", "entered_at", "strategy",
    }
    assert sig.state_update["entry"] == pytest.approx(_RECOVER)   # 진입 시점 현재가
    assert sig.state_update["session"] == DAY.isoformat()
    assert sig.state_update["entered_at"].startswith("2026-01-05T12:45")
    assert sig.state_update["stop"] == sig.stop
    assert sig.state_update["target"] == sig.target
    assert sig.state_update["strategy"] == "mr_vwap_quiet"

    # next_state 는 하루짜리 값 둘뿐이고, 어느 중첩 dict 에도 방어선이 없다.
    assert set(d.next_state) == {"session_date", "entries_today"}
    nested = {k for v in d.next_state.values() if isinstance(v, dict) for k in v}
    assert not ({"entry", "stop", "target", "entered_at"} & (set(d.next_state) | nested))


def test_defense_lines_survive_process_restart():
    """**장중 재시작 시나리오** (2026-08-28 실제 사건).

    진입 → 체결 → **전략 인스턴스를 통째로 버린다**(= 엔진 재시작, next_state
    유실). 포지션만 브로커에 남는다. 그래도 다음 사이클에 손절·목표·타임아웃이
    그대로 나와야 한다 — 방어선이 `Position.meta["lots"]` 에 영속돼
    `snap.lots` 로 되돌아오기 때문이다."""
    d1 = _strategy().decide(_snapshot(), {})
    lot = _apply_fill(_entries(d1)[0])

    # === 재시작: 새 인스턴스, state 는 빈 dict. 포지션(lot)만 살아남았다. ===
    restarted = _strategy()

    # 손절: 진입 98.0, 손절 ≈ 94.66.
    d_stop = restarted.decide(_snapshot(price=94.0, lots={US_SYM: lot}), {})
    assert len(_exits(d_stop)) == 1 and "손절" in _exits(d_stop)[0].reason

    # 목표(VWAP): lot 에 저장된 진입 시점 VWAP 이 그대로 쓰인다.
    d_tgt = _strategy().decide(_snapshot(price=99.80, lots={US_SYM: lot}), {})
    assert len(_exits(d_tgt)) == 1 and "목표(VWAP) 도달" in _exits(d_tgt)[0].reason

    # 타임아웃: `entered_at` 이 살아남아야 경과 시간을 셀 수 있다.
    later = datetime(2026, 1, 5, 12, 45, tzinfo=NY) + timedelta(minutes=80)
    d_to = _strategy().decide(_snapshot(price=98.5, now=later, lots={US_SYM: lot}), {})
    assert len(_exits(d_to)) == 1 and "타임아웃" in _exits(d_to)[0].reason

    # 그 사이 값에서는 계속 보유한다 — 재시작이 관리를 켜기만 하는 게 아니다.
    d_hold = _strategy().decide(_snapshot(price=98.5, lots={US_SYM: lot}), {})
    assert d_hold.signals == ()


def test_no_duplicate_entry_within_session():
    """1사이클 진입 → next_state 를 되먹이면 2사이클엔 재진입하지 않는다
    (세션당 심볼당 상한, 기본 1회). 5초 루프가 같은 완성봉을 반복 평가해도
    중복 주문이 나지 않는 근거 — 체결 전이라 `snap.lots` 가 아직 비어 있어도
    `entries_today` 가 막는다."""
    strat = _strategy()
    snap = _snapshot()
    d1 = strat.decide(snap, {})
    assert len(_entries(d1)) == 1
    assert d1.next_state["entries_today"] == {US_SYM: 1}
    assert d1.next_state["session_date"] == {"US": DAY.isoformat()}

    d2 = strat.decide(snap, d1.next_state)          # 체결 전 재평가
    assert _entries(d2) == []

    lot = _apply_fill(_entries(d1)[0])              # 체결 반영
    d3 = strat.decide(_snapshot(lots={US_SYM: lot}), d2.next_state)
    assert _entries(d3) == [], "보유 중엔 신규 진입 평가가 없다"

    d4 = strat.decide(_snapshot(), d3.next_state)   # 청산 후에도 상한 유지
    assert _entries(d4) == []


def test_max_entries_per_session_is_configurable():
    strat = _strategy(max_entries_per_session=2)
    d1 = strat.decide(_snapshot(), {})
    assert len(_entries(d1)) == 1
    # 청산 완료 가정(snap.lots 비어 있음) — 두 번째 진입은 허용된다.
    d2 = strat.decide(_snapshot(), d1.next_state)
    assert len(_entries(d2)) == 1
    assert d2.next_state["entries_today"] == {US_SYM: 2}
    d3 = strat.decide(_snapshot(), d2.next_state)
    assert _entries(d3) == []


def test_entries_today_resets_on_session_roll():
    strat = _strategy()
    d1 = strat.decide(_snapshot(), {})
    assert len(_entries(d1)) == 1
    day2 = _snapshot(now=datetime(2026, 1, 6, 12, 45, tzinfo=NY),
                     session_start=datetime(2026, 1, 6, 9, 30, tzinfo=NY))
    d2 = strat.decide(day2, d1.next_state)
    assert len(_entries(d2)) == 1, "새 세션이면 상한이 풀린다"
    assert d2.next_state["session_date"] == {"US": "2026-01-06"}


# ============================================================ ⑫ 입력 state 불변

def test_decide_does_not_mutate_input_state():
    """순수 계약: `decide()` 는 받은 state 를 (중첩 dict 까지) 건드리지 않는다."""
    strat = _strategy()
    state = _day_state()
    before = copy.deepcopy(state)
    strat.decide(_snapshot(price=99.80, lots={US_SYM: _lot()}), state)
    assert state == before

    # 진입 경로도 마찬가지 — entries_today 를 원본에 쓰지 않는다.
    entry_state = {"session_date": {}, "entries_today": {}}
    entry_before = copy.deepcopy(entry_state)
    d = strat.decide(_snapshot(), entry_state)
    assert entry_state == entry_before
    assert d.next_state["entries_today"] == {US_SYM: 1}


def test_decide_does_not_mutate_snapshot_lots():
    """관리 경로가 `snap.lots` 의 랏을 in-place 로 고치지 않는다 — 껍질이 준
    사본이라 고쳐도 영속되지 않으므로, 고치는 것 자체가 거짓 상태를 만든다."""
    lot = _lot()
    before = copy.deepcopy(lot)
    _strategy().decide(_snapshot(price=98.5, lots={US_SYM: lot}), _day_state())
    assert lot == before


def test_decide_is_deterministic():
    strat = _strategy()
    snap, state = _snapshot(), {}
    a, b = strat.decide(snap, state), strat.decide(snap, state)
    assert [s.reason for s in a.signals] == [s.reason for s in b.signals]
    assert a.next_state == b.next_state


# ============================================================ 파라미터 검증 / 배선

@pytest.mark.parametrize("params", [
    {"band_k": 0}, {"reentry_lookback": 0}, {"rvol_max": 0}, {"rvol_lookback": 0},
    {"adx_max": 0}, {"adx_period": 1}, {"max_gap_pct": 0},
    {"entry_after_open_minutes": -1}, {"entry_before_close_minutes": -1},
    {"target_min_bp": -1}, {"stop_buffer_pct": -1}, {"timeout_minutes": 0},
    {"flatten_before_close_minutes": -1}, {"max_entries_per_session": 0},
    {"target_weight": 0}, {"target_weight": 1.5},
])
def test_rejects_invalid_params(params):
    with pytest.raises(ValueError):
        MrVwapQuietStrategy([US_SYM], params)


def test_requirements_declares_5m_and_daily():
    strat = _strategy()
    needs = strat.requirements()
    intervals = {(sym, iv) for sym, iv, _ in needs.bars}
    assert intervals == {(US_SYM, "5m"), (US_SYM, "1d")}
    assert needs.quotes == (US_SYM,)
    assert needs.needs_positions is True
    # 5분봉 lookback 은 세션 전체(78봉) + ADX 워밍업 + RVOL baseline 을 덮는다.
    lookback = next(n for _, iv, n in needs.bars if iv == "5m")
    assert lookback >= 78 + 2 * strat.adx_period + strat.rvol_lookback


def test_shell_satisfies_strategy_protocol():
    """`MrVwapQuietShell` 이 껍질을 통해 기존 `Strategy` Protocol(on_cycle)을
    만족하는지 — 레지스트리 배선 없이 껍질 자체만 확인한다."""
    shell = MrVwapQuietShell([US_SYM], {})
    assert shell.id == "mr_vwap_quiet"
    assert shell.symbols == [US_SYM]
    assert hasattr(shell, "on_cycle")
    assert isinstance(shell.inner, MrVwapQuietStrategy)
