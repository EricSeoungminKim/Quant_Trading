"""갭하락 되돌림(gap fade) — 롱 온리, **순수 전용 신규 전략**(레거시 쌍둥이 없음).

## 학술 근거 — 정직하게, 혼재돼 있다

오버나잇~일중 반전 문헌(Della Corte & Kosowski; Akbas et al. 2022)은 "간밤 상승분이
다음날 장중에 되돌려지는" 경향을 보고한다 — 이 전략은 그 거울상(간밤 **하락**분이
장중에 부분적으로 되돌려진다)을 켠다. 그러나 같은 문헌군 안에서도 선물(MNQ 등)을
대상으로 한 반증 연구가 있어 **엣지의 방향성이 자산군에 따라 뒤집힐 수 있다는
근거가 이미 존재한다**. 이 저장소는 개별주/ETF 현물이라 원 논문의 자산군과 다르고,
그래서 이 전략의 엣지가 실제로 존재하는지는 검증되지 않았다 — `mr_vwap_quiet`/
`pullback_impulse`와 같은 처지다: 백테스트 표본이 없다(Toss 1분봉 4거래일 롤링,
`docs/data-availability.md`). **paper 번인이 유일한 검증 경로다.**

## 규칙이 다루려는 실패 모드 — "떨어지는 칼"

갭하락 즉시 매수하면 하락이 계속되는 구간을 그대로 맞는다("떨어지는 칼"). 그래서
이 전략은 갭 자체를 트리거로 쓰지 않는다 — **개장 후 첫 안정화 신호(5분봉 양봉
마감)를 기다렸다가, 그 다음 봉에서** 진입한다. `entry_window_min` 안에 안정화가
안 나오면 그날 그 심볼은 포기한다(추가 상태 없이 자연히 재발생하지 않는다 — 시간
창이 지나면 `find_stabilization_bar`가 항상 None을 반환하므로).

## 규칙

1. **갭 판정**: 당일 세션 시가가 전일 종가 대비 `gap_min_bp`(기본 100bp) 이상
   `gap_max_bp`(기본 400bp) 이하로 **하락**. 상한을 둔 이유 — 그 이상의 폭락 갭은
   구조적 유동성 불균형이 아니라 악재 실체(회계 부정, 상장폐지 사유 등)일 가능성이
   높아 "일시적 과매도" 논지와 맞지 않는다.
2. **안정화 확인**: 세션 개장 이후 `entry_window_min`(기본 30분) 안에 시작한
   5분봉 중 **가장 먼저 양봉(종가>시가)으로 마감한 봉**을 안정화봉으로 삼는다.
   그 창 안에 양봉이 하나도 없으면 그날 그 심볼은 포기한다.
3. **진입**: 안정화봉 **다음 완성봉**부터 진입 평가를 시작한다(그 봉 또는 그 뒤
   아무 완성봉에서나 — 판단 주기가 봉 간격과 어긋나 정확히 다음 봉을 놓쳐도 기회
   자체를 영구히 잃지 않는다). 현재가로 진입.
4. **목표**: 시가 + 갭폭 × `fill_ratio`(기본 0.5) — 갭 절반 메움. 목표가 진입가
   이하면(이미 갭이 메워진 뒤 진입 평가가 이뤄진 경우) 들지 않는다.
5. **손절**: min(당일 저가, 안정화봉 저가) − `atr_buffer_mult`(기본 0.3) × ATR
   (5분봉, Wilder 평활 — `atr_ratio`, `trend_gate.py` 재사용). 손절폭이
   `min_stop_bp`(기본 40bp, US 왕복 비용 20bp의 2배) 미만이면 진입 거부하고
   `last_reject`에 사유를 남긴다 — `pullback_impulse`가 2026-08-29 결함을 고친
   것과 같은 게이트다(손절선을 정할 수 없는 자리는 진입하지 않는다,
   `quant/trade/structure.py`의 손절 철학).
6. **시간 청산**: 진입 후 `max_hold_min`(기본 120분) 경과 시 청산.
7. **EoD 강제청산 + 오버나잇 금지**: 세션 마감 `flatten_before_close_minutes`
   (기본 5분) 전 무조건 청산. **`flatten_before_close_minutes`는 반드시 1 이상**
   이어야 한다 — `Clock._should_flatten`(`quant/core/clock.py`) docstring의 실사고
   기록 그대로다: 0이면 마지막 in-session 판단 시점에도 조건이 성립하지 않아
   청산 창이 통째로 사라진다(3배 레버리지 ETF 포지션이 며칠씩 살아남은 사고).
   여기서도 생성자가 0 이하를 거부해 같은 사고를 재현하지 못하게 막는다.
8. **1일 1회**: 심볼당 하루 진입 1회(`taken`).

## 청산 판정 순서 (보수적)

오버나잇(세션 롤) → EoD → 손절 → 목표 → 시간 청산. 손절과 목표가 같은 사이클에
함께 성립하면(5분 안에 양쪽을 다 지나간 경우) 나쁜 쪽을 택한다 — 봉 안의 체결
순서를 우리는 모른다. `mr_vwap_quiet`/`pullback_impulse`와 같은 순서다.

## 전일 종가 획득 경로 (추측 아님, 코드로 확인)

`ctx.data.history(symbol, "1d", n)` 경로:

1. `MarketDataService.history`(`quant/adapters/data/service.py:222`)가 봉 경계
   캐시를 태운 뒤 `_filter_completed_bars`로 **미완성봉을 잘라낸다**. 장중에는
   오늘 일봉(마감이 미래)이 항상 미완성으로 잘려나가므로, 마지막 행이 정확히
   **전일 종가**가 된다(`_interval_minutes("1d") == 24*60`, `service.py:108`).
2. `TossDataFeed.history`(`quant/adapters/brokers/toss/datafeed.py:200`)는
   `interval == "1d"`일 때 리샘플을 거치지 않고 일봉을 그대로 돌려준다(203행
   `if interval in ("1d", "1m")`). 이미 라이브에서 쓰이는 경로다 —
   `mr_vwap_quiet`가 갭 계산에 같은 방식을 쓴다(그 파일 docstring "데이터" 절).
3. 일봉 조회가 비거나 실패하면(`prior_close()`가 None) **진입하지 않는다** —
   지어내지 않는다. "확인 불가는 통과가 아니라 거부다"(`mr_vwap_quiet` 원칙과
   동일 — 갭 판정 자체가 이 전략 진입 논지의 전제이기 때문).

## 상태를 두 갈래로 흘린다 (장중 재시작 생존)

| 키 | 무엇 | 어디로 | 재시작 |
|---|---|---|---|
| `session_date`/`taken`/`last_reject` | 하루짜리 값 | `next_state` | 잃어도 무해 |
| `entry`/`stop`/`target`/`entered_at`/`session` | **열린 랏의 방어선** | `Signal.state_update` → 루프가 체결 확인 후 `Position.meta["lots"]`에 기록 → 다음 사이클 `snap.lots`로 회수 | **살아남는다** |

`mr_vwap_quiet`/`pullback_impulse`와 같은 근거(2026-08-28 장중 재시작 실사고) —
방어선을 인스턴스/`next_state`에 두면 재시작 순간 손절이 증발한다. `decide()`는
받은 `state`의 **사본만** 고쳐 반환한다.

## 아직 못 하는 것 (정직하게)

1. **성과 근거가 없다.** 위 "학술 근거" 절 — 반증 문헌이 존재하는 채로 시작한다.
2. **고아 포지션을 볼 수 없다.** `DataNeeds`가 정적으로 `self.symbols`만 선언한다
   (관심종목 기반 전략 공통 한계).
3. **조회 최적화가 없다.** 매 사이클 전 심볼의 5분봉+일봉을 재요청한다 —
   `MarketDataService`의 봉 경계 캐시가 실제 호출을 봉당 1회로 누른다.
4. **연속 거래 구간 판정이 캘린더를 모른다.** `in_continuous_session`은 주말만
   거른다 — 공휴일·조기폐장은 `snap.minutes_to_close`(캘린더 기반) 쪽 EoD 경로만
   안다(`mr_vwap_quiet` 한계 5번과 동일).
5. **안정화봉 재탐색이 없다.** 첫 양봉을 안정화봉으로 확정하면, 그 뒤 더 낮은
   저가가 나와도 손절 기준(당일 저가는 계속 갱신되지만 안정화봉 자체는 고정)이
   재조정되지 않는다 — 의도된 단순화다(첫 신호만 취한다는 원칙과 일관).
"""
from __future__ import annotations

from datetime import date as dtdate, datetime, timedelta
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import continuous_window, in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.indicators.trend_gate import atr_ratio
from quant.trade.strategy import kernel
from quant.trade.strategy.shell import PureStrategyShell

_INTERVAL = "5m"

# 갭 계산용 일봉 조회 개수 — 전일 종가 하나만 쓰지만 휴장일/결손을 감안해 여유를 둔다.
_DAILY_COUNT = 5

# 정규장 길이(분) — KR 09:00~15:30, US 09:30~16:00 둘 다 390분. lookback 하한 산정용.
_FULL_SESSION_MINUTES = 390


# ---------------------------------------------------------------- 순수 지표


def prior_close(daily_bars: pd.DataFrame | None) -> float | None:
    """마지막 완성 일봉의 종가 = 전일 종가. 계산 불가면 None(값을 지어내지 않는다).

    모듈 docstring "전일 종가 획득 경로" 절 참고 — 장중에는 `_filter_completed_bars`
    가 오늘 일봉을 잘라내므로 마지막 행이 항상 전일 종가다.
    """
    if daily_bars is None or daily_bars.empty:
        return None
    value = float(daily_bars["close"].iloc[-1])
    if pd.isna(value) or not (value > 0):
        return None
    return value


def gap_down_bp(session_open: float, prev_close: float) -> float | None:
    """(전일 종가 − 당일 시가) / 전일 종가 × 1e4. 하락 갭이면 양수, 상승 갭이면 음수.

    입력이 유효하지 않으면(0 이하) None — 호출부가 갭 상하한 게이트를 통과시키지
    않도록 명시적으로 판단 불가를 알린다.
    """
    if prev_close <= 0 or session_open <= 0:
        return None
    return (prev_close - session_open) / prev_close * 1e4


def find_stabilization_bar(session: pd.DataFrame, window_end: datetime) -> int | None:
    """세션 봉 중 **시가 시각이 `window_end` 이전**이고 종가>시가(양봉)로 마감한
    **가장 이른** 봉의 위치(0-based). 없으면 None.

    "개장 후 첫 안정화"는 세션 첫 봉이 무조건 안정화봉이라는 뜻이 아니라, 창 안에서
    처음으로 나타나는 양봉을 안정화 신호로 본다는 뜻이다 — 그래서 `entry_window_min`
    이 의미를 가진다(창이 없으면 첫 봉 하나만 보면 되고 "몇 분 안에 안 나오면
    포기"라는 개념 자체가 성립하지 않는다).
    """
    for i in range(len(session)):
        ts = session.index[i]
        if ts >= window_end:
            break
        row = session.iloc[i]
        if float(row["close"]) > float(row["open"]):
            return i
    return None


# ---------------------------------------------------------------- 전략


class GapFadePureStrategy:
    """모듈 docstring 참고. `decide()`는 `snap`/`state`만 본다(순수함수 계약,
    `quant.core.strategy_api.PureStrategy`)."""

    def __init__(
        self, symbols: list[str], params: dict, market: str = "US",
        id: str = "gap_fade",
    ):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        # 1) 갭 하한/상한(bp). 하한 미만은 노이즈, 상한 이상은 악재 실체 가능성.
        self.gap_min_bp: float = float(params.get("gap_min_bp", 100.0))
        self.gap_max_bp: float = float(params.get("gap_max_bp", 400.0))
        # 2) 안정화 탐색 창(분, 개장 기준).
        self.entry_window_min: float = float(params.get("entry_window_min", 30.0))
        # 4) 목표 = 시가 + 갭폭 × fill_ratio.
        self.fill_ratio: float = float(params.get("fill_ratio", 0.5))
        # 5) 손절 버퍼 — min(당일 저가, 안정화봉 저가) 아래 ATR 배수.
        self.atr_buffer_mult: float = float(params.get("atr_buffer_mult", 0.3))
        self.atr_period: int = int(params.get("atr_period", 14))
        # 최소 손절폭(bp). 0 = 비활성. 기본 40 = US 왕복 비용(20bp)의 2배 —
        # pullback_impulse의 2026-08-29 결함 수리와 같은 근거.
        self.min_stop_bp: float = kernel.parse_min_stop_bp(params)
        # 6) 시간 청산(분).
        self.max_hold_min: float = float(params.get("max_hold_min", 120.0))
        # 7) EoD 청산 여유(분). 0 이하 금지 — 모듈 docstring 규칙 7번의 실사고 근거.
        self.flatten_minutes: float = float(params.get("flatten_before_close_minutes", 5.0))
        # 사이징 비중.
        self.target_weight: float = float(params.get("target_weight", 0.5))

        if self.gap_min_bp <= 0:
            raise ValueError("gap_min_bp는 양수여야 합니다.")
        if self.gap_max_bp <= self.gap_min_bp:
            raise ValueError("gap_max_bp는 gap_min_bp보다 커야 합니다.")
        if self.entry_window_min <= 0:
            raise ValueError("entry_window_min은 양수여야 합니다.")
        if not 0 < self.fill_ratio <= 1:
            raise ValueError("fill_ratio는 0 초과 1 이하여야 합니다.")
        if self.atr_buffer_mult < 0:
            raise ValueError("atr_buffer_mult는 0 이상이어야 합니다.")
        if self.atr_period < 2:
            raise ValueError("atr_period는 2 이상이어야 합니다.")
        if self.max_hold_min <= 0:
            raise ValueError("max_hold_min은 양수여야 합니다.")
        if self.flatten_minutes <= 0:
            # Clock._should_flatten docstring: 0이면 마지막 in-session 사이클에서도
            # 조건이 성립하지 않아 청산 창이 통째로 사라진다(실제 사고 이력).
            raise ValueError("flatten_before_close_minutes는 양수여야 합니다.")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")

        # 5분봉 조회 개수 — 세션 전체(78봉) + ATR 워밍업을 덮는다.
        self._lookback_bars = max(
            int(params.get("lookback_bars", 120)),
            _FULL_SESSION_MINUTES // 5 + self.atr_period + 1,
        )

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """5분봉(판정 전부) + 일봉(전일 종가 전용) + 현재가 + 포지션."""
        bars = tuple((s, _INTERVAL, self._lookback_bars) for s in self.symbols)
        bars += tuple((s, "1d", _DAILY_COUNT) for s in self.symbols)
        return DataNeeds(bars=bars, quotes=tuple(self.symbols), needs_positions=True)

    @staticmethod
    def _my_lot(snap: StrategySnapshot, symbol: str) -> Mapping[str, Any] | None:
        """내가 방어선을 써 넣은 열린 랏만 돌려준다 — `kernel.my_lot` 참고
        (판정 근거는 그쪽 docstring)."""
        return kernel.my_lot(snap.lots, symbol)

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        # 입력 state는 절대 in-place로 건드리지 않는다.
        session_date: dict[str, str] = dict(state.get("session_date", {}))
        taken: dict[str, str] = dict(state.get("taken", {}))
        last_reject: dict[str, str] = dict(state.get("last_reject", {}))

        signals: list[Signal] = []
        markets = sorted({market_of_symbol(s) for s in self.symbols})

        # 0) 세션 롤 — 관리(1단계)보다 먼저.
        for market in markets:
            if not snap.market_open.get(market, False):
                continue
            today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
            if today_iso == session_date.get(market):
                continue
            session_date[market] = today_iso
            for symbol in [s for s in taken if market_of_symbol(s) == market]:
                taken.pop(symbol, None)
            for symbol in [s for s in last_reject if market_of_symbol(s) == market]:
                last_reject.pop(symbol, None)

        # 1) 보유 관리 — 방어선의 정본은 브로커 lot(`snap.lots`)이다.
        for symbol in self.symbols:
            market = market_of_symbol(symbol)
            if not snap.market_open.get(market, False):
                continue
            lot = self._my_lot(snap, symbol)
            if lot is None:
                continue
            signal = self._manage(symbol, lot, market, snap)
            if signal is not None:
                signals.append(signal)

        # 2) 진입
        for market in markets:
            if not snap.market_open.get(market, False):
                continue
            # KR 15:20~15:30 동시호가에서는 현재가로 체결할 수 없다.
            if not in_continuous_session(market, snap.now):
                continue
            today = snap.now.astimezone(market_tz(market)).date()
            today_iso = today.isoformat()
            for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
                if self._my_lot(snap, symbol) is not None:
                    continue  # 보유 중엔 신규 진입 평가 없음
                if taken.get(symbol) == today_iso:
                    continue
                signal = self._check_entry(symbol, market, snap, today, taken, last_reject)
                if signal is not None:
                    signals.append(signal)

        return Decision(
            signals=tuple(signals),
            next_state={
                "session_date": session_date, "taken": taken, "last_reject": last_reject,
            },
        )

    # ------------------------------------------------------------------ 시간 게이트

    def _should_flatten(self, market: str, snap: StrategySnapshot) -> bool:
        """`Clock._should_flatten`(quant/core/clock.py) 재현 — 스냅샷이 주는
        원재료(`minutes_to_close`, `cadence_minutes`)로 같은 공식을 계산한다.
        `minutes_to_close`는 이미 `_effective_close`(명목 마감과 연속거래 끝 중
        이른 쪽)를 기준으로 하므로 KR 동시호가 구간을 별도로 다시 판정할 필요가
        없다(`pullback_impulse._should_flatten`과 동일 근거)."""
        mtc = snap.minutes_to_close.get(market)
        return kernel.should_flatten_calendar(mtc, snap.cadence_minutes, self.flatten_minutes)

    @staticmethod
    def _session_bars(bars: pd.DataFrame, market: str, today: dtdate) -> pd.DataFrame:
        """오늘 **연속 거래 개장 이후**의 봉만. 갭/안정화 판정 둘 다 "오늘 세션의
        시가"에 의존하므로 프리마켓 봉이 섞이면 전부 틀어진다."""
        tz = market_tz(market)
        open_t, _ = continuous_window(market)
        local = bars.index.tz_convert(tz)
        return bars[(local.date == today) & (local.time >= open_t)]

    # ------------------------------------------------------------------ 진입

    def _check_entry(
        self, symbol: str, market: str, snap: StrategySnapshot, today: dtdate,
        taken: dict[str, str], last_reject: dict[str, str],
    ) -> Signal | None:
        """모듈 docstring "규칙" 절 1~5번. 하나라도 확인 불가/불충족이면 None이고
        사유를 `last_reject`에 남긴다."""
        bars = snap.bars.get((symbol, _INTERVAL))
        if bars is None or bars.empty:
            last_reject[symbol] = "5분봉 없음"
            return None
        session = self._session_bars(bars, market, today)
        if session.empty:
            last_reject[symbol] = "오늘 세션 5분봉 없음"
            return None

        # (1) 갭 판정 — 전일 종가 확인 불가면 거부(지어내지 않는다).
        prev_close = prior_close(snap.bars.get((symbol, "1d")))
        if prev_close is None:
            last_reject[symbol] = "전일 종가 확인 불가"
            return None
        session_open = float(session["open"].iloc[0])
        gap_bp = gap_down_bp(session_open, prev_close)
        if gap_bp is None:
            last_reject[symbol] = "갭 계산 불가"
            return None
        if not (self.gap_min_bp <= gap_bp <= self.gap_max_bp):
            last_reject[symbol] = f"갭 조건 불충족(gap={gap_bp:.0f}bp)"
            return None

        # (2) 안정화 확인 — entry_window_min 안의 첫 양봉.
        tz = market_tz(market)
        open_t, _ = continuous_window(market)
        window_end = datetime.combine(today, open_t, tzinfo=tz) + timedelta(
            minutes=self.entry_window_min
        )
        si = find_stabilization_bar(session, window_end)
        if si is None:
            now_local = snap.now.astimezone(tz)
            if now_local >= window_end:
                last_reject[symbol] = (
                    f"안정화 없음 — entry_window({self.entry_window_min:g}분) 초과, 포기"
                )
            else:
                last_reject[symbol] = "안정화 대기중(양봉 없음)"
            return None

        # (3) 진입 — 안정화봉 다음 완성봉부터.
        if len(session) <= si + 1:
            last_reject[symbol] = "안정화봉 다음 봉 대기중"
            return None

        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            last_reject[symbol] = "현재가 없음"
            return None
        entry = float(quote.price)

        # (5) 손절 — ATR 계산 불가면 손절선을 정할 수 없다(진입하지 않는다).
        ratio = atr_ratio(bars, self.atr_period)
        if ratio is None:
            last_reject[symbol] = f"ATR 계산 불가(5분봉 {len(bars)}개)"
            return None
        atr_abs = ratio * float(bars["close"].iloc[-1])

        day_low = float(session["low"].min())
        stab_low = float(session["low"].iloc[si])
        stop = min(day_low, stab_low) - self.atr_buffer_mult * atr_abs
        if not (stop < entry):
            last_reject[symbol] = "손절가 계산 불가(진입가 이상)"
            return None
        stop_bp = (entry - stop) / entry * 1e4
        if not kernel.stop_bp_gate_ok(stop_bp, self.min_stop_bp):
            last_reject[symbol] = (
                f"손절폭 {stop_bp:.0f}bp < 최소 {self.min_stop_bp:g}bp"
            )
            return None

        # (4) 목표 — 시가 + 갭폭 × fill_ratio.
        gap_width = prev_close - session_open
        target = session_open + gap_width * self.fill_ratio
        if target <= entry:
            last_reject[symbol] = "목표가 진입가 이하(갭이 이미 메워짐)"
            return None

        taken[symbol] = today.isoformat()
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(
                f"갭하락 되돌림 진입: {symbol} w={self.target_weight:.2f} "
                f"갭={gap_bp:.0f}bp 시가={fmt_price(session_open, symbol)} "
                f"현재={fmt_price(entry, symbol)} 목표={fmt_price(target, symbol)} "
                f"손절={fmt_price(stop, symbol)}"
            ),
            stop=stop,
            target=target,
            # 방어선은 여기로 나간다 — next_state가 아니다(모듈 docstring "상태를
            # 두 갈래로" 절).
            state_update={
                "entry": entry, "stop": stop, "target": target,
                "session": today.isoformat(), "entered_at": snap.now.isoformat(),
                "strategy": self.id,
            },
        )

    # ------------------------------------------------------------------ 관리

    def _manage(
        self, symbol: str, lot: Mapping[str, Any], market: str, snap: StrategySnapshot
    ) -> Signal | None:
        """`lot`은 껍질이 `Position.meta["lots"][id]`에서 순수 조회해 채운
        `snap.lots[symbol]`이다. 판정 순서는 보수적이다: 오버나잇 → EoD → 손절 →
        목표 → 시간 청산(`mr_vwap_quiet`/`pullback_impulse`와 동일 순서)."""
        quote = snap.quotes.get(symbol)
        if quote is None:
            return None
        price = quote.price
        tz = market_tz(market)
        entry = float(lot["entry"])  # _my_lot이 None 아님을 이미 보장한다
        stop_raw, target_raw = lot.get("stop"), lot.get("target")
        # 방어선이 반쪽인 랏(stop/target 없음)이라도 EoD·세션 롤·시간 청산은
        # 지켜야 한다 — intraday_momentum과 동일 정책: 하드레일(손절·목표)
        # 판정만 건너뛴다("지어내지 않는다"), 시간 기반 청산은 아래에서 유지된다.
        stop = float(stop_raw) if stop_raw is not None else None
        target = float(target_raw) if target_raw is not None else None

        def _exit(reason: str) -> Signal:
            return kernel.exit_signal(self.id, symbol, reason)

        entry_session = lot.get("session")
        today_iso = snap.now.astimezone(tz).date().isoformat()
        if kernel.is_overnight_carry(lot, today_iso):
            return _exit(
                f"세션 롤 강제청산(오버나잇 금지): 진입 {entry_session} "
                f"현재={fmt_price(price, symbol)}"
            )
        if self._should_flatten(market, snap):
            return _exit(
                f"EoD 청산: entry={fmt_price(entry, symbol)} 현재={fmt_price(price, symbol)}"
            )
        if stop is not None and price <= stop:
            return _exit(
                f"손절: entry={fmt_price(entry, symbol)} stop={fmt_price(stop, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )
        if target is not None and price >= target:
            return _exit(
                f"목표(갭 메움) 도달 청산: entry={fmt_price(entry, symbol)} "
                f"목표={fmt_price(target, symbol)} 현재={fmt_price(price, symbol)}"
            )
        entered_at = lot.get("entered_at")
        if entered_at:
            held = (snap.now - datetime.fromisoformat(entered_at)).total_seconds() / 60
            if held >= self.max_hold_min:
                return _exit(
                    f"시간 청산({self.max_hold_min:g}분): "
                    f"entry={fmt_price(entry, symbol)} 현재={fmt_price(price, symbol)} "
                    f"경과={held:.0f}분"
                )
        return None


class GapFadeShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies`가 다른 전략과 같은 방식으로
    (`cls(symbols=..., params=..., market=..., id=...)`) 생성할 수 있게 하는 얇은
    팩토리 — `MrVwapQuietShell`/`PullbackImpulseShell`과 동일 패턴. 레지스트리
    배선은 이 파일 밖(`quant/trade/strategy/__init__.py`)에서 한다."""

    def __init__(
        self, symbols: list[str], params: dict, market: str = "US",
        id: str = "gap_fade",
    ):
        super().__init__(GapFadePureStrategy(symbols, params, market=market, id=id))
