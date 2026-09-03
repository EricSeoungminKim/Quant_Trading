"""전일 패자 개장 매수(단기 반전) — 어제 가장 많이 빠진 종목을 **오늘 개장
직후** 사서 종가에 판다. 근거는 한국 시장의 단기 반전(short-term reversal)
실증이다: KCI 등재 국내 연구(2023, KOSPI/KOSDAQ 일간 반전)와 KAIST 계열의
야간/일중 수익률 분해 연구가 모두 "전일 급락 종목의 다음날 **개장 직후 구간**에
반전이 몰린다"는 같은 방향을 가리킨다. **순수 계약 전용 신규 전략**(레거시
쌍둥이 없음).

`eod_reversal`(같은 날 장 막판 반전)과 형제 전략이지만 **다른 효과를 노린다** —
저쪽은 하루 안에서의 일중 반전이고, 이쪽은 **하루를 건너뛴** 일간 반전이다.
둘 다 롱 온리·당일 청산이라 오버나잇 위험은 지지 않는다.

## 규칙

1. **랭킹** — 각 심볼의 **전일 종가-종가 수익률**(마지막 완성 일봉 종가 ÷ 그
   직전 일봉 종가 − 1)을 오름차순으로 세운다. 시장별로 따로 센다.
2. **후보** = 하위 `bottom_k`(기본 3)개 중, 전일 수익률 ≤
   −`min_prev_drop_pct`(기본 2.0%) 인 것.
3. **떨어지는 칼 회피** — 오늘 시가 갭이 전일 종가 대비
   −`max_gap_down_pct`(기본 3.0%) **아래**면 진입하지 않는다. 반전이 아니라
   악재가 이어지는 중일 가능성이 크고, 그건 이 전략이 사려는 것이 아니다.
4. **진입** — 연속 거래 개장 후 `entry_window_min`(기본 15)분 안에서만 현재가로
   롱. 심볼당 세션당 1회. 손절 = 진입가 × (1 − `stop_pct`/100), 기본 2.0%.
5. **청산** — 목표 없음. 마감 `eod_exit_min`(기본 3)분 전 전량 청산, 오버나잇
   금지.

## 데이터

- `"1d"` — 전일·전전일 종가(1번 랭킹)와 전일 종가(3번 갭 기준). 장중에는 완성봉
  필터가 오늘 일봉을 잘라내므로 **마지막 행이 전일 세션**이다(`vol_breakout`
  모듈 docstring "데이터" 절과 같은 전제).
- `"5m"` — 오늘 시가만 얻는다. 일봉으로는 오늘 시가를 알 수 없기 때문이다(위와
  같은 이유로 오늘 일봉 자체가 아직 없다). 진입창이 개장 후 15분이라 필요한
  봉은 몇 개뿐이지만, `entry_window_min` 을 늘려도 개장 첫 봉까지 닿도록
  창 길이에 비례해 요청한다.

## 아직 못 하는 것 (정직하게)

1. **우리 원장 실측 0.** 문헌 인용이고 이 저장소 데이터로 검증된 적이 없다.
   paper 번인이 유일한 검증 경로다.
2. **전일 수익률을 일봉 종가로만 잰다.** 배당락·액면분할 같은 코퍼레이트
   액션을 보정하지 않는다 — 조정되지 않은 일봉을 받으면 "급락"으로 오인해
   진입할 수 있다(우리 일봉 소스가 조정치를 주는지 확인된 바 없다).
3. **하위 `bottom_k` 는 관심종목 안의 상대 순위다.** 문헌은 시장 전체 횡단면의
   하위 분위를 쓴다 — 관심종목이 3개면 세 종목 전부가 "하위 3"이 된다.
   `min_prev_drop_pct` 가 그 경우의 유일한 안전장치다.
4. **갭 상단을 자르지 않는다.** 갭 상승 후 매수는 반전 논지와 무관한 추격이
   될 수 있는데, 이번 범위에서는 상단 게이트를 두지 않았다(문헌이 상단을
   특정하지 않는다 — 지어내지 않는다).
5. **재시작이 "1일 1회" 게이트를 되돌린다**, **고아 포지션을 볼 수 없다** —
   관심종목 기반 순수 전략 공통 한계(`vol_breakout` 과 같은 이유).
"""
from __future__ import annotations

from datetime import date as dtdate, datetime
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import continuous_window, in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.strategy import kernel
from quant.trade.strategy.shell import PureStrategyShell

# 오늘 시가 획득용 봉 간격 — `vol_breakout._session_open` 과 같은 경로.
_INTERVAL = "5m"
# 전일·전전일 종가만 쓰지만 휴장/결손 여유를 둔다.
_DAILY_COUNT = 10
# 진입창이 짧아도 개장 첫 봉까지는 닿아야 한다.
_MIN_SESSION_BARS = 12


class OpenReversalPureStrategy:
    """전일 패자 개장 매수 — `PureStrategy` 구현. 모듈 docstring 참고."""

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "KR", id: str = "open_reversal"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        self.entry_window_min: float = float(params.get("entry_window_min", 15))
        self.bottom_k: int = int(params.get("bottom_k", 3))
        self.min_prev_drop_pct: float = float(params.get("min_prev_drop_pct", 2.0))
        self.max_gap_down_pct: float = float(params.get("max_gap_down_pct", 3.0))
        self.stop_pct: float = float(params.get("stop_pct", 2.0))
        self.eod_exit_min: float = float(params.get("eod_exit_min", 3))
        self.target_weight: float = float(params.get("target_weight", 0.5))

        if self.entry_window_min <= 0:
            raise ValueError("entry_window_min은 양수여야 합니다.")
        if self.bottom_k < 1:
            raise ValueError("bottom_k는 1 이상이어야 합니다.")
        if self.min_prev_drop_pct < 0:
            raise ValueError("min_prev_drop_pct는 0 이상이어야 합니다.")
        if self.max_gap_down_pct <= 0:
            raise ValueError("max_gap_down_pct는 양수여야 합니다.")
        if not 0 < self.stop_pct < 100:
            raise ValueError("stop_pct는 0 초과 100 미만이어야 합니다.")
        if self.eod_exit_min <= 0:
            raise ValueError("eod_exit_min은 양수여야 합니다.")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")

        self._daily_count = max(int(params.get("daily_bars", _DAILY_COUNT)), 3)
        self._session_bars_n = max(
            int(self.entry_window_min // 5) + 6, _MIN_SESSION_BARS
        )

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """일봉(전일 종가-종가 수익률·갭 기준) + 5분봉(오늘 시가) + 현재가 + 포지션."""
        bars = tuple((s, _INTERVAL, self._session_bars_n) for s in self.symbols)
        bars += tuple((s, "1d", self._daily_count) for s in self.symbols)
        return DataNeeds(bars=bars, quotes=tuple(self.symbols), needs_positions=True)

    @staticmethod
    def _my_lot(snap: StrategySnapshot, symbol: str) -> Mapping[str, Any] | None:
        return kernel.my_lot(snap.lots, symbol)

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        session_date: dict[str, str] = dict(state.get("session_date", {}))
        entries_today: dict[str, str] = dict(state.get("entries_today", {}))
        last_reject: dict[str, str] = dict(state.get("last_reject", {}))

        signals: list[Signal] = []
        markets = sorted({market_of_symbol(s) for s in self.symbols})

        # 0) 세션 롤.
        for market in markets:
            if not snap.market_open.get(market, False):
                continue
            today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
            if not kernel.session_rolled(session_date.get(market), today_iso):
                continue
            session_date[market] = today_iso
            for symbol in [s for s in entries_today if market_of_symbol(s) == market]:
                entries_today.pop(symbol, None)
            for symbol in [s for s in last_reject if market_of_symbol(s) == market]:
                last_reject.pop(symbol, None)

        # 1) 보유 관리 — 진입보다 먼저.
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

        # 2) 진입.
        for market in markets:
            if not snap.market_open.get(market, False):
                continue
            if not in_continuous_session(market, snap.now):
                continue
            self._entries_for_market(market, snap, entries_today, last_reject, signals)

        return Decision(
            signals=tuple(signals),
            next_state={
                "session_date": session_date,
                "entries_today": entries_today,
                "last_reject": last_reject,
            },
        )

    # ------------------------------------------------------------------ 진입 파이프라인

    def _entries_for_market(
        self, market: str, snap: StrategySnapshot,
        entries_today: dict[str, str], last_reject: dict[str, str],
        signals: list[Signal],
    ) -> None:
        tz = market_tz(market)
        today = snap.now.astimezone(tz).date()
        today_iso = today.isoformat()
        symbols = sorted(s for s in self.symbols if market_of_symbol(s) == market)

        elapsed = self._minutes_since_open(market, snap.now)
        if elapsed > self.entry_window_min:
            for symbol in symbols:
                if self._my_lot(snap, symbol) is None and entries_today.get(symbol) != today_iso:
                    last_reject[symbol] = (
                        f"개장 후 {self.entry_window_min:g}분 진입창 종료"
                    )
            return

        # (a) 전일 종가-종가 수익률.
        prev_returns: dict[str, tuple[float, float]] = {}  # symbol -> (수익률%, 전일 종가)
        for symbol in symbols:
            value = self._prev_return(snap.bars.get((symbol, "1d")))
            if value is None:
                last_reject[symbol] = "전일 수익률 확인 불가(일봉 부족)"
                continue
            prev_returns[symbol] = value

        if not prev_returns:
            return

        # (b) 하위 bottom_k.
        ranked = sorted(prev_returns.items(), key=lambda kv: (kv[1][0], kv[0]))
        for symbol, (ret_pct, _) in ranked[self.bottom_k:]:
            last_reject[symbol] = f"전일 {ret_pct:+.2f}% — 하위 {self.bottom_k}위 밖"

        # (c) 낙폭·갭 게이트 → 진입.
        for symbol, (ret_pct, prev_close) in ranked[: self.bottom_k]:
            if ret_pct > -self.min_prev_drop_pct:
                last_reject[symbol] = (
                    f"전일 {ret_pct:+.2f}% — 낙폭 {self.min_prev_drop_pct:g}% 미달"
                )
                continue
            session_open = self._session_open(symbol, market, snap, today)
            if session_open is None or session_open <= 0:
                last_reject[symbol] = "당일 세션 시가 확인 불가"
                continue
            gap_pct = (session_open / prev_close - 1) * 100
            if gap_pct < -self.max_gap_down_pct:
                last_reject[symbol] = (
                    f"갭하락 {gap_pct:+.2f}% < -{self.max_gap_down_pct:g}% — 떨어지는 칼"
                )
                continue
            if self._my_lot(snap, symbol) is not None:
                continue  # 보유 중엔 신규 진입 평가 없음
            if entries_today.get(symbol) == today_iso:
                last_reject[symbol] = "1일 1회 진입 소진"
                continue
            signal = self._enter(
                symbol, snap, today_iso, ret_pct, gap_pct, entries_today, last_reject
            )
            if signal is not None:
                signals.append(signal)

    def _enter(
        self, symbol: str, snap: StrategySnapshot, today_iso: str,
        ret_pct: float, gap_pct: float,
        entries_today: dict[str, str], last_reject: dict[str, str],
    ) -> Signal | None:
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            last_reject[symbol] = "현재가 없음"
            return None
        entry = float(quote.price)
        stop = entry * (1 - self.stop_pct / 100)
        if stop >= entry or stop <= 0:
            last_reject[symbol] = "손절가 계산 불가(진입가 이상)"
            return None

        entries_today[symbol] = today_iso
        last_reject.pop(symbol, None)
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(
                f"전일 패자 개장 매수: {symbol} w={self.target_weight:.2f} "
                f"전일 {ret_pct:+.2f}% 갭 {gap_pct:+.2f}% "
                f"현재={fmt_price(entry, symbol)} 손절={fmt_price(stop, symbol)}"
            ),
            stop=stop,
            state_update={
                "entry": entry, "stop": stop,
                "session": today_iso, "entered_at": snap.now.isoformat(),
                "strategy": self.id,
            },
        )

    # ------------------------------------------------------------------ 데이터 헬퍼

    @staticmethod
    def _minutes_since_open(market: str, now: datetime) -> float:
        tz = market_tz(market)
        local = now.astimezone(tz)
        open_t, _ = continuous_window(market)
        open_dt = datetime.combine(local.date(), open_t, tzinfo=tz)
        return (local - open_dt).total_seconds() / 60

    @staticmethod
    def _prev_return(daily: pd.DataFrame | None) -> tuple[float, float] | None:
        """(전일 종가-종가 수익률 %, 전일 종가). 완성 일봉이 2개 미만이면 None —
        "확인 불가는 통과가 아니라 거부다"."""
        if daily is None or daily.empty:
            return None
        closes = daily["close"].dropna()
        if len(closes) < 2:
            return None
        prev_close = float(closes.iloc[-1])
        prior_close = float(closes.iloc[-2])
        if prev_close <= 0 or prior_close <= 0:
            return None
        return (prev_close / prior_close - 1) * 100, prev_close

    @staticmethod
    def _session_open(symbol: str, market: str, snap: StrategySnapshot,
                      today: dtdate) -> float | None:
        """오늘 **연속 거래 개장 이후** 첫 5분봉의 시가 — `vol_breakout._session_open`
        과 동일 로직(프리마켓 봉이 섞이면 "당일 시가"가 아니게 된다)."""
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

    # ------------------------------------------------------------------ 관리

    def _should_flatten(self, market: str, snap: StrategySnapshot) -> bool:
        """`kernel.should_flatten_dual` — KR 15:20 연속매매 종료까지 반영."""
        return kernel.should_flatten_dual(
            market, snap.now, snap.minutes_to_close.get(market),
            snap.cadence_minutes, self.eod_exit_min,
        )

    def _manage(
        self, symbol: str, lot: Mapping[str, Any], market: str, snap: StrategySnapshot
    ) -> Signal | None:
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None
        price = float(quote.price)
        entry = float(lot["entry"])
        stop_raw = lot.get("stop")
        stop = float(stop_raw) if stop_raw is not None else None

        def _exit(reason: str) -> Signal:
            return kernel.exit_signal(self.id, symbol, reason)

        today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
        if kernel.is_overnight_carry(lot, today_iso):
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


class OpenReversalShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies` 용 얇은 팩토리 — `VolBreakoutShell`
    과 동일 패턴."""

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "KR", id: str = "open_reversal"):
        super().__init__(OpenReversalPureStrategy(symbols, params, market=market, id=id))
