"""변동성 돌파(Larry Williams) — 전일 레인지의 일정 비율을 오늘 시가에 더한
트리거가를 상향 돌파하면 롱 진입, 목표 없이 마감 직전 청산. **순수 계약 전용
신규 전략**(레거시 쌍둥이 없음).

## 규칙

1. **트리거가** = 당일 세션 시가 + `k` × (전일 고가 − 전일 저가). `k` 기본
   0.5 — Larry Williams 원 공식의 표준값.
2. **진입**: 장중 현재가가 트리거가를 상향 돌파하면 롱. 심볼당 세션당 1회.
3. **손절** = 진입가 − 0.5 × `k` × 전일범위. 단 손절폭이 `min_stop_bp`
   (기본 40bp) 미만이면 **진입 자체를 거부**한다 — `pullback_impulse.py`가
   고정한 2026-08-29 실전 첫날 사고(손절폭 3.5bp로 17초 만에 손절, 왕복 비용
   20bp+ 를 진입 순간부터 지는 구조였다)와 같은 게이트다. 여기서 손절폭이
   좁아지는 원인은 다르다(되돌림 저점이 아니라 **전일 레인지 자체가 좁음** —
   즉 변동성이 낮은 날) — 같은 병(비용보다 얕은 손절)에 같은 약을 쓴다.
4. **목표 없음** — 세션 마감까지 보유 후 마감 `eod_exit_min`(기본 5)분 전
   전량 청산. 오버나잇 아님 — 이 전략은 하루 안에서 열고 닫는다.
5. 1일 1회 게이트 소진은 **진입이 실제로 나갔을 때만** 걸린다(min_stop_bp
   거부는 소진하지 않는다) — `pullback_impulse`와 같은 이유: 트리거가가 그날
   고정이라도, 현재가가 트리거보다 높이 올라갈수록 손절폭(bp)은 더 좁아지므로
   (분모인 진입가가 커진다) 뒤늦은 재시도가 통과할 일은 없다. 소진하지 않아도
   무해하고, 소진하면 오히려 "가격이 트리거 근처로 되돌아와 다시 그은 경우"의
   정당한 재시도를 막는다.

## 데이터 — 전일 고저를 어떻게 얻는가 (추측 아님, 계약으로 확인)

`DataFeed.history`의 계약(`quant/core/ports.py:65-67`)이 `interval:
"1m" | "5m" | "15m" | "1d"`를 명시하고, **`"1d"`가 지원 목록에 있다** — 그래서
"분봉에서 전일 세션을 집계"하는 우회를 하지 않고 **일봉을 직접 요청한다**.
장중에는 `MarketDataService`/`TossDataFeed`의 완성봉 필터가 오늘 일봉(마감이
미래)을 잘라내므로(`mr_vwap_quiet.gap_pct` docstring, `overnight_drift._prev_close`
와 같은 전제) 일봉 조회의 마지막 행이 **전일 세션**이 되고, 그 `high`/`low`가
바로 이 전략이 필요로 하는 값이다. 새 집계 로직을 만들지 않는다.

**당일 시가는 일봉으로 얻을 수 없다** — 위와 같은 이유로 오늘 일봉 자체가
아직 없다. 그래서 시가만은 `overnight_drift._session_open`과 같은 방식으로
5분봉에서 얻는다(오늘 **연속 거래 개장 이후** 첫 봉의 시가 — 프리마켓 봉이
섞이면 "당일 시가"가 아니게 된다). 두 봉 간격을 병행 요청하는 것이 어색해
보일 수 있지만, 일봉과 5분봉은 서로 다른 값(전일 고저 vs 당일 시가)을 위한
것이라 하나로 합칠 수 없다.

## 왜 목표가 없는가

Larry Williams 원 전략 자체가 목표가를 두지 않는다 — 트리거 통과 자체가
"오늘 변동성이 레인지를 벗어날 방향으로 터졌다"는 신호이고, 그 방향성이
장 마감까지 이어진다는 것이 논지다. 목표를 두면 그 논지를 스스로 자르는
것이 된다(`overnight_drift` "목표가 없음" 규칙 4와 같은 논리).

## EoD 청산과 판단 주기의 상호작용 (이 저장소의 실제 사고 이력)

`quant/core/clock.py`의 `_should_flatten`은 "다음 판단 시점이 청산 창 안으로
들어오면 지금 청산"을 `remaining - cadence < flatten_minutes`로 계산한다.
`cadence`를 빼지 않으면 판단 주기가 굵을 때(예: 15분봉) 마지막 판단 시점의
잔여시간이 청산 창보다 커서 조건이 **한 번도 성립하지 않고 창을 건너뛴다**
(`clock.py` docstring: 그 결과 실제로 3배 레버리지 ETF 포지션이 며칠씩
살아남은 사고가 있었다). `snap.minutes_to_close`는 `_effective_close`(명목
마감과 연속 거래 끝 중 이른 쪽, `clock.py:21-42`)를 이미 반영하므로 KR
동시호가 문제까지 한 번에 해소된다 — `_should_flatten`이 그 값을 그대로
재현하면 백테스트(5분봉 cadence)와 라이브(10초 cadence) 양쪽에서 청산 창이
사라지지 않는다. `pullback_impulse._should_flatten`과 동일한 재현이다.

## 상태가 두 갈래로 흐른다 (다른 순수 전략과 같은 이유)

| # | 값 | 어디로 | 왜 |
|---|---|---|---|
| 1 | `session_date`/`entries_today`/`last_reject` | `next_state` | 하루 안에서만 의미가 있다. 재시작에 잃어도 다음 사이클에 다시 채워지거나(세션 롤), 무해하다(진단용) |
| 2 | `entry`/`stop`/`session`/`entered_at` | **`Signal.state_update` → 루프가 체결 확인 후 `Position.meta["lots"]`에 기록 → 다음 사이클에 `snap.lots`로 회수** | 포지션이 살아 있는 한 필요하다 |

2번을 `next_state`에 두지 않는 이유는 `mr_vwap_quiet`/`pullback_impulse`와
같다 — 2026-08-28 실제 사건(포지션 8개를 보유한 채 장중 재시작)에서 인스턴스
상태는 증발하지만 브로커 포지션은 남는다. 방어선의 정본이 lot 이면 재시작이
그것을 갈라놓을 수 없다.

## 아직 못 하는 것 (정직하게)

1. **우리 원장 실측이 없다.** Larry Williams 공식은 문헌 인용이고 이 저장소
   데이터로 검증된 적이 없다. paper 번인이 유일한 검증 경로다.
2. **고아 포지션을 볼 수 없다.** `DataNeeds`가 정적으로 `self.symbols`만
   선언하므로 유니버스에서 빠진 뒤 남은 보유분은 관리되지 않는다(관심종목
   기반 전략 공통 한계).
3. **재시작이 "1일 1회" 게이트를 되돌린다.** `entries_today`는 `next_state`라
   재시작에 사라진다 — 보유 중인 심볼은 안전하다(`snap.lots`가 중복 진입을
   막는다). 구멍은 "진입 → 청산 → 재시작"이 같은 날 일어난 경우뿐이고, 그때
   트리거 재돌파가 다시 성립하면 2차 진입이 날 수 있다. 손실이 아니라 빈도
   문제라 `pullback_impulse`와 같은 판단으로 여기서 멈춘다.
4. **손절 하드캡이 없다.** 전일 레인지가 극단적으로 크면 `min_stop_bp`
   게이트를 통과하고도 1회 손실 폭이 클 수 있다. 리스크 레이어의 포지션
   상한이 마지막 방어선이다.
"""
from __future__ import annotations

from datetime import date as dtdate
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import continuous_window, in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.strategy.shell import PureStrategyShell

# 당일 시가 획득용 봉 간격 — 모듈 docstring "데이터" 절. 이미 라이브에서 쓰이는
# 간격이다(orb_scan/overnight_drift 기본값).
_INTERVAL = "5m"

# 정규장 길이(분) — KR 09:00~15:30, US 09:30~16:00 둘 다 390분.
_FULL_SESSION_MINUTES = 390
# 오늘 세션 첫 봉을 확실히 포함하는 5분봉 개수(세션 전체 + 여유).
_SESSION_BARS = _FULL_SESSION_MINUTES // 5 + 2
# 전일 고저용 일봉 개수. 하루치만 쓰지만 휴장일/결손을 감안해 여유를 둔다.
_DAILY_COUNT = 5


class VolBreakoutPureStrategy:
    """변동성 돌파 — `PureStrategy`(quant.core.strategy_api) 구현. 모듈 docstring 참고.

    **"Pure" 접미사가 없는 이유**: 신규 전략이라 이전할 레거시 쌍둥이가 없다
    (`MrVwapQuietStrategy`/`OvernightDriftStrategy`와 같다).
    """

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "US", id: str = "vol_breakout"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        # 트리거 = 시가 + k × 전일범위. 0.5 = Larry Williams 원 공식의 표준값.
        self.k: float = float(params.get("k", 0.5))
        # 손절폭이 이 미만이면 진입 자체를 거부한다(모듈 docstring 규칙 3).
        # 0 이면 비활성 — 다른 전략들의 관례와 같다.
        self.min_stop_bp: float = float(params.get("min_stop_bp", 40.0))
        # 마감 이 시간(분) 전에 전량 청산. clock.py 의 flatten_minutes 하한(≥1)과
        # 같은 이유로 0 을 허용하지 않는다 — 0 이면 cadence 를 뺀 조건이 세션 안
        # 어디에서도 성립하지 않아 청산 창이 통째로 사라진다(clock.py docstring
        # 실사고 이력, `pullback_impulse.py` 같은 검증).
        self.eod_exit_min: float = float(params.get("eod_exit_min", 5))
        # 전략 배정 자본 대비 비중. 심볼당 세션당 1회 진입이라 슬롯이 구조적으로
        # 제한되지만, 유니버스가 여러 종목이면 동시 보유가 가능하므로 보수적 기본값.
        self.target_weight: float = float(params.get("target_weight", 0.5))

        if self.k <= 0:
            raise ValueError("k는 양수여야 합니다.")
        if self.min_stop_bp < 0:
            raise ValueError("min_stop_bp는 0(비활성) 이상이어야 합니다.")
        if self.eod_exit_min <= 0:
            raise ValueError("eod_exit_min은 양수여야 합니다.")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")

        self._session_bars_n = max(int(params.get("lookback_bars", _SESSION_BARS)), _SESSION_BARS)
        self._daily_count = max(int(params.get("daily_bars", _DAILY_COUNT)), 1)

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """5분봉(당일 시가 전용) + 일봉(전일 고저 전용) + 현재가 + 포지션.

        두 간격을 병행 요청하는 이유는 모듈 docstring "데이터" 절 — 일봉은 장중에
        오늘치가 완성되지 않아 당일 시가를 줄 수 없고, 5분봉은 전일 세션 전체
        고저를 다시 집계하는 불필요한 재구현이 된다.
        """
        bars = tuple((s, _INTERVAL, self._session_bars_n) for s in self.symbols)
        bars += tuple((s, "1d", self._daily_count) for s in self.symbols)
        return DataNeeds(bars=bars, quotes=tuple(self.symbols), needs_positions=True)

    @staticmethod
    def _my_lot(snap: StrategySnapshot, symbol: str) -> Mapping[str, Any] | None:
        """내가 **방어선을 써 넣은** 열린 랏만 돌려준다 — `mr_vwap_quiet`/
        `overnight_drift`의 `_my_lot`과 같은 판정(남의 포지션 오인 방지)."""
        lot = snap.lots.get(symbol)
        if not lot or lot.get("entry") is None:
            return None
        return lot

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        # 입력 state 는 절대 in-place 로 건드리지 않는다 — 중첩 dict 까지 복사.
        session_date: dict[str, str] = dict(state.get("session_date", {}))
        entries_today: dict[str, str] = dict(state.get("entries_today", {}))
        last_reject: dict[str, str] = dict(state.get("last_reject", {}))

        signals: list[Signal] = []
        markets = sorted({market_of_symbol(s) for s in self.symbols})

        # 0) 세션 롤 — 관리(1단계)보다 먼저. 하루짜리 값만 지운다(방어선은 lot 에
        #    있으므로 세션 롤이 건드리지 않는다).
        for market in markets:
            if not snap.market_open.get(market, False):
                continue
            today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
            if today_iso == session_date.get(market):
                continue
            session_date[market] = today_iso
            for symbol in [s for s in entries_today if market_of_symbol(s) == market]:
                entries_today.pop(symbol, None)
            for symbol in [s for s in last_reject if market_of_symbol(s) == market]:
                last_reject.pop(symbol, None)

        # 1) 보유 관리 — 진입보다 먼저. 보유의 진실은 `snap.lots` 하나뿐이다.
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

        # 2) 진입 — 연속 거래 구간에서만(동시호가에는 현재가로 체결할 수 없다).
        for market in markets:
            if not snap.market_open.get(market, False):
                continue
            if not in_continuous_session(market, snap.now):
                continue
            today = snap.now.astimezone(market_tz(market)).date()
            today_iso = today.isoformat()
            for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
                if self._my_lot(snap, symbol) is not None:
                    continue  # 보유 중엔 신규 진입 평가 없음
                if entries_today.get(symbol) == today_iso:
                    last_reject[symbol] = "1일 1회 진입 소진"
                    continue
                signal = self._check_entry(symbol, market, snap, today, entries_today, last_reject)
                if signal is not None:
                    signals.append(signal)

        return Decision(
            signals=tuple(signals),
            next_state={
                "session_date": session_date,
                "entries_today": entries_today,
                "last_reject": last_reject,
            },
        )

    # ------------------------------------------------------------------ 데이터 헬퍼

    def _session_open(self, symbol: str, market: str, snap: StrategySnapshot,
                       today: dtdate) -> float | None:
        """오늘 **연속 거래 개장 이후** 첫 5분봉의 시가. `overnight_drift._session_open`
        과 동일 로직 — 프리마켓 봉이 섞이면 "당일 시가"가 아니게 된다."""
        bars = snap.bars.get((symbol, _INTERVAL))
        if bars is None or bars.empty:
            return None
        tz = market_tz(market)
        open_t, _ = continuous_window(market)
        local = bars.index.tz_convert(tz)
        session = bars[(local.date == today) & (local.time >= open_t)]
        if session.empty:
            return None
        value = float(session["open"].iloc[0])
        return None if pd.isna(value) else value

    @staticmethod
    def _prev_high_low(daily_bars: pd.DataFrame | None) -> tuple[float, float] | None:
        """마지막 **완성** 일봉의 (고가, 저가) = 전일 세션. 계산 불가면 None —
        "확인 불가는 통과가 아니라 거부다"(`mr_vwap_quiet` 모듈 docstring과 같은
        원칙). 장중에는 완성봉 필터가 오늘 일봉을 잘라내므로 마지막 행이 전일이다
        (모듈 docstring "데이터" 절)."""
        if daily_bars is None or daily_bars.empty:
            return None
        high = float(daily_bars["high"].iloc[-1])
        low = float(daily_bars["low"].iloc[-1])
        if pd.isna(high) or pd.isna(low):
            return None
        return high, low

    # ------------------------------------------------------------------ 진입

    def _check_entry(
        self, symbol: str, market: str, snap: StrategySnapshot, today: dtdate,
        entries_today: dict[str, str], last_reject: dict[str, str],
    ) -> Signal | None:
        """모듈 docstring 규칙 1~3. 전일 데이터를 확인할 수 없으면 진입하지
        않는다("확인 불가 = 거부")."""
        session_open = self._session_open(symbol, market, snap, today)
        if session_open is None or session_open <= 0:
            last_reject[symbol] = "당일 세션 시가 확인 불가"
            return None

        prev = self._prev_high_low(snap.bars.get((symbol, "1d")))
        if prev is None:
            last_reject[symbol] = "전일 고저 확인 불가"
            return None
        prev_high, prev_low = prev
        prev_range = prev_high - prev_low
        if not (prev_range > 0):
            last_reject[symbol] = "전일 범위 계산 불가(고가<=저가)"
            return None

        trigger = session_open + self.k * prev_range

        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            last_reject[symbol] = "현재가 없음"
            return None
        price = float(quote.price)
        if price < trigger:
            return None  # 아직 트리거 미달 — 정상 대기, 사유를 남기지 않는다

        entry = price
        stop = entry - 0.5 * self.k * prev_range
        if stop >= entry:
            last_reject[symbol] = "손절가 계산 불가(진입가 이상)"
            return None

        # 손절폭 게이트 — 모듈 docstring 규칙 3 (`pullback_impulse`의
        # min_stop_bp 게이트와 같은 패턴).
        stop_bp = (entry - stop) / entry * 1e4
        if self.min_stop_bp and stop_bp < self.min_stop_bp:
            last_reject[symbol] = (
                f"손절폭 {stop_bp:.0f}bp < 최소 {self.min_stop_bp:g}bp — "
                "전일 변동성이 너무 좁다"
            )
            return None

        today_iso = today.isoformat()
        entries_today[symbol] = today_iso
        last_reject.pop(symbol, None)
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(
                f"변동성 돌파 진입: {symbol} w={self.target_weight:.2f} "
                f"트리거={fmt_price(trigger, symbol)} 현재={fmt_price(price, symbol)} "
                f"손절={fmt_price(stop, symbol)} "
                f"[k={self.k:g} 전일범위={fmt_price(prev_range, symbol)} 시가={fmt_price(session_open, symbol)}]"
            ),
            stop=stop,
            # **방어선은 여기로만 나간다** — `next_state`가 아니다. 루프가 체결을
            # 확인한 뒤에만 `Position.meta["lots"][id]`에 쓴다(모듈 docstring
            # "상태가 두 갈래로 흐른다" 절).
            state_update={
                "entry": entry, "stop": stop,
                "session": today_iso, "entered_at": snap.now.isoformat(),
                "strategy": self.id,
            },
        )

    # ------------------------------------------------------------------ 관리

    def _should_flatten(self, market: str, snap: StrategySnapshot) -> bool:
        """`quant.core.clock._should_flatten` 재현 — 모듈 docstring "EoD 청산과
        판단 주기의 상호작용" 절. `snap.minutes_to_close`가 이미 연속 거래 끝
        기준(`_effective_close`)이므로 이 재현만으로 KR 동시호가까지 안전하다."""
        mtc = snap.minutes_to_close.get(market)
        if mtc is None or mtc <= 0:
            return False
        return mtc - snap.cadence_minutes < self.eod_exit_min

    def _manage(
        self, symbol: str, lot: Mapping[str, Any], market: str, snap: StrategySnapshot
    ) -> Signal | None:
        """`lot`은 껍질이 `Position.meta["lots"][id]`에서 순수 조회해 채운 사본
        (`snap.lots[symbol]`) — 읽기만 한다. 판정 순서: 오버나잇 안전망 → EoD →
        손절(목표 없음, 모듈 docstring "왜 목표가 없는가" 절)."""
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None
        price = float(quote.price)
        tz = market_tz(market)
        entry = float(lot["entry"])  # _my_lot 이 None 아님을 이미 보장한다
        stop_raw = lot.get("stop")
        # 방어선이 반쪽인 랏(stop 없음)이라도 EoD·세션 롤 청산은 지켜야 한다 —
        # intraday_momentum과 동일 정책: 하드레일(손절) 판정만 건너뛴다("지어내지
        # 않는다"), 시간 기반(오버나잇/EoD) 청산은 아래에서 그대로 걸린다.
        stop = float(stop_raw) if stop_raw is not None else None

        def _exit(reason: str) -> Signal:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0, reason=reason,
            )

        # 오버나잇 안전망 — 이 전략은 정의상 하루 안에서 닫힌다(EoD 청산이 주
        # 경로다). 그래도 데이터 결손 등으로 EoD 청산을 놓친 경우의 방어선으로
        # 세션 롤 강제청산을 둔다(`mr_vwap_quiet`/`pullback_impulse`와 동일 패턴).
        entry_session = lot.get("session")
        if entry_session and entry_session != snap.now.astimezone(tz).date().isoformat():
            return _exit(
                f"세션 롤 강제청산(오버나잇 금지): entry={fmt_price(entry, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )
        if self._should_flatten(market, snap):
            return _exit(
                f"EoD 청산(마감 {self.eod_exit_min:g}분 전): entry={fmt_price(entry, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )
        if stop is not None and price <= stop:
            return _exit(
                f"손절: entry={fmt_price(entry, symbol)} stop={fmt_price(stop, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )
        return None


class VolBreakoutShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies`가 다른 전략과 같은 방식으로
    (`cls(symbols=..., params=..., market=..., id=...)`) 생성할 수 있게 하는 얇은
    팩토리 — `MrVwapQuietShell`/`OvernightDriftShell`과 동일 패턴.

    **레지스트리 배선은 이 파일 밖이다**(`quant/trade/strategy/__init__.py`의
    `STRATEGY_REGISTRY` + `config/settings.yaml`의 `strategies:` 블록).
    """

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "US", id: str = "vol_breakout"):
        super().__init__(VolBreakoutPureStrategy(symbols, params, market=market, id=id))
