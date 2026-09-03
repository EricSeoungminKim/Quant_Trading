"""장 막판 역추세 매수(EoD reversal) — 그날 많이 밀린 종목을 마감 45분 전에
사서 **종가에 판다**. 근거는 Baltussen, Da & Soebhag (2024) *"Hedging Macro
Betas"* 계열의 일중(intraday) 수익률 분해 문헌과, KAIST 계열의 KOSPI 일중
반전(intraday reversal) 실증이다. **순수 계약 전용 신규 전략**(레거시 쌍둥이 없음).

문헌의 공통 관찰은 두 가지다. (1) 하루 수익은 **야간(종가→시가)** 과
**주간(시가→종가)** 으로 쪼개면 성격이 정반대다 — 야간은 드리프트(모멘텀),
주간은 반전이다. (2) 주간 반전의 상당 부분이 **장 마지막 한 시간**에 몰려
있다(유동성 공급자의 재고 되돌림, 종가 지수 추종 매매). 이 전략은 그 (2)만
정확히 노린다: 마감 45분 전에 그날의 패자를 사서, 반전을 종가까지만 먹는다.

## 규칙

1. **평가 시점** — 마감까지 남은 시간이 `eval_minutes_before_close`(기본 45)분
   이하가 되면 그때부터 매 사이클 후보를 다시 고른다.
2. **세션 수익률** = (오늘 마지막 1분봉 종가 ÷ 오늘 첫 1분봉 시가 − 1). 연속
   거래 개장 이후 봉만 쓴다(프리마켓/동시호가 봉이 섞이면 "그날 얼마나 밀렸나"가
   아니게 된다).
3. **후보** = 그 시장 심볼을 세션 수익률 오름차순으로 세워 **하위
   `bottom_pct`(기본 20%)** 에 드는 종목 중,
   - 세션 수익률 ≤ −`min_drop_pct`(기본 1.5%) 이고,
   - 세션 누적 거래대금(1분봉 `종가 × 거래량` 합)이
     `min_turnover_krw` ~ `max_turnover_krw` 밴드 안인 것.
4. **진입** — 현재가에 롱. 손절 = 진입가 × (1 − `stop_pct`/100), 기본 1.5%.
   심볼당 세션당 1회.
5. **청산** — 목표 없음. 마감 `eod_exit_min`(기본 2)분 전 전량 청산. 오버나잇
   금지(이 전략의 논지 자체가 "종가까지만"이다 — 하루를 넘기면 야간 드리프트라는
   **다른** 효과에 노출되고, 그건 `overnight_drift` 의 소관이다).
6. **KR 전용(기본)** — `markets` 기본값이 `["KR"]` 이다. 이유는 아래 "왜 KR 인가".

## 왜 KR 인가, 그리고 왜 그게 위험한가 (정직하게)

KOSPI 일중 반전 실증은 **소형·저유동 종목에서 훨씬 강하다**. 그런데 우리가
실제로 체결하는 곳도 바로 거기다 — **효과가 큰 구간이 곧 슬리피지가 큰 구간**
이고, 문헌의 초과수익 대부분은 호가 스프레드와 시장충격에 그대로 먹힐 수 있다.
그래서 3번의 유동성 밴드는 장식이 아니라 이 전략의 생사다: **하한**은 "체결이
되는가", **상한**은 "효과가 남아 있는가"를 자른다. 두 기본값(1억 / 1,000억원)은
문헌 값이 아니라 **우리 추정**이고, 번인 원장이 유일한 판정자다.

거래대금 밴드가 원(KRW) 단위라 US 심볼에는 의미가 없다 — `markets: [KR]` 이
기본인 두 번째 이유다. US 를 켜려면 밴드를 달러로 다시 정해야 한다.

## 데이터

`"1m"` 만 쓴다. 세션 수익률(시가 대비)과 누적 거래대금 둘 다 오늘 하루치
1분봉이면 충분하고, 그 이상은 필요 없다 — 정규장 390분 + 여유 = 400봉.
`"1d"` 를 쓰지 않는 이유: 오늘 일봉은 장중에 완성되지 않아 아예 없다.

## 상태가 두 갈래로 흐른다

| # | 값 | 어디로 | 왜 |
|---|---|---|---|
| 1 | `session_date`/`entries_today`/`last_reject` | `next_state` | 하루 안에서만 의미가 있다 |
| 2 | `entry`/`stop`/`session`/`entered_at` | **`Signal.state_update` → 체결 확인 후 `Position.meta["lots"]`** | 포지션이 살아 있는 한 필요하다 |

## 아직 못 하는 것 (정직하게)

1. **우리 원장 실측 0.** 문헌 인용이고 이 저장소 데이터로 검증된 적이 없다.
   paper 번인이 유일한 검증 경로다.
2. **슬리피지를 모델링하지 않는다.** 위 "왜 KR 인가" 절이 이 전략의 최대
   위험이고, 그 검증은 백테스트가 아니라 실제 체결가 원장에서만 나온다.
3. **하위 `bottom_pct` 는 관심종목 안의 상대 순위다.** 문헌은 시장 전체
   횡단면에서 하위 분위를 고른다 — 관심종목이 10개면 "하위 20%"는 2개이고,
   그 2개가 시장 전체 기준으로는 하위가 아닐 수 있다.
4. **재시작이 "1일 1회" 게이트를 되돌린다.** 보유 중인 심볼은 `snap.lots` 가
   막지만, "진입 → 청산 → 재시작"이 같은 날 일어나면 재진입이 날 수 있다.
5. **고아 포지션을 볼 수 없다**(관심종목 전략 공통 한계).
"""
from __future__ import annotations

import math
from datetime import date as dtdate, datetime
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import continuous_window, in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.strategy import kernel
from quant.trade.strategy.shell import PureStrategyShell

_INTERVAL = "1m"
_FULL_SESSION_MINUTES = 390
_SESSION_BARS = _FULL_SESSION_MINUTES + 10


class EodReversalPureStrategy:
    """장 막판 역추세 매수 — `PureStrategy` 구현. 모듈 docstring 참고."""

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "KR", id: str = "eod_reversal"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        self.eval_minutes_before_close: float = float(
            params.get("eval_minutes_before_close", 45)
        )
        self.bottom_pct: float = float(params.get("bottom_pct", 20.0))
        self.min_drop_pct: float = float(params.get("min_drop_pct", 1.5))
        self.min_turnover_krw: float = float(params.get("min_turnover_krw", 1e8))
        self.max_turnover_krw: float = float(params.get("max_turnover_krw", 1e11))
        self.stop_pct: float = float(params.get("stop_pct", 1.5))
        self.eod_exit_min: float = float(params.get("eod_exit_min", 2))
        self.target_weight: float = float(params.get("target_weight", 0.5))
        self.markets: list[str] = [str(m).upper() for m in params.get("markets", ["KR"])]
        self.min_session_bars: int = int(params.get("min_session_bars", 30))

        if self.eval_minutes_before_close <= 0:
            raise ValueError("eval_minutes_before_close는 양수여야 합니다.")
        if not 0 < self.bottom_pct <= 100:
            raise ValueError("bottom_pct는 0 초과 100 이하여야 합니다.")
        if self.min_drop_pct < 0:
            raise ValueError("min_drop_pct는 0 이상이어야 합니다.")
        if self.min_turnover_krw < 0:
            raise ValueError("min_turnover_krw는 0 이상이어야 합니다.")
        if self.max_turnover_krw <= self.min_turnover_krw:
            raise ValueError("max_turnover_krw는 min_turnover_krw보다 커야 합니다.")
        if not 0 < self.stop_pct < 100:
            raise ValueError("stop_pct는 0 초과 100 미만이어야 합니다.")
        if self.eod_exit_min <= 0:
            raise ValueError("eod_exit_min은 양수여야 합니다.")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")
        if self.eval_minutes_before_close <= self.eod_exit_min:
            raise ValueError(
                "eval_minutes_before_close는 eod_exit_min보다 커야 합니다 "
                "— 평가창이 열리자마자 청산창이면 진입이 무의미합니다."
            )
        if not self.markets:
            raise ValueError("markets는 비어 있을 수 없습니다.")
        for m in self.markets:
            if m not in ("KR", "US"):
                raise ValueError(f"markets에 알 수 없는 시장: {m}")
        if self.min_session_bars < 1:
            raise ValueError("min_session_bars는 1 이상이어야 합니다.")

        self._session_bars_n = max(int(params.get("lookback_bars", _SESSION_BARS)),
                                   _SESSION_BARS)

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """오늘 하루치 1분봉 + 현재가 + 포지션. 봉 수 근거는 모듈 docstring "데이터"."""
        bars = tuple((s, _INTERVAL, self._session_bars_n) for s in self.symbols)
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
        symbols = sorted(s for s in self.symbols if market_of_symbol(s) == market)
        if market not in self.markets:
            for symbol in symbols:
                last_reject[symbol] = (
                    f"{market} 미허용 시장(markets={'+'.join(self.markets)})"
                )
            return

        # 청산창 안이면 진입하지 않는다 — 들어가자마자 EoD 청산이 나가는 왕복이다.
        if self._should_flatten(market, snap):
            return

        remaining = self._minutes_to_close(market, snap)
        if remaining is None or remaining > self.eval_minutes_before_close:
            return  # 아직 평가창 전 — 정상 대기, 사유를 남기지 않는다

        today = snap.now.astimezone(market_tz(market)).date()
        today_iso = today.isoformat()

        # (a) 종목별 세션 수익률/거래대금 — 보유·진입소진 종목도 순위에는 넣는다
        #     (하위 분위는 그날의 시장 사실이지 내 포지션 사정이 아니다).
        stats: dict[str, tuple[float, float]] = {}   # symbol -> (수익률%, 거래대금)
        for symbol in symbols:
            stat = self._session_stats(symbol, market, snap, today)
            if stat is None:
                last_reject[symbol] = "세션 1분봉 확인 불가"
                continue
            stats[symbol] = stat

        if not stats:
            return

        # (b) 하위 bottom_pct 분위.
        ranked = sorted(stats.items(), key=lambda kv: (kv[1][0], kv[0]))
        n_bottom = max(1, math.ceil(len(ranked) * self.bottom_pct / 100))
        for symbol, (ret_pct, _) in ranked[n_bottom:]:
            last_reject[symbol] = (
                f"세션 {ret_pct:+.2f}% — 하위 {self.bottom_pct:g}% 밖"
            )

        # (c) 낙폭·유동성 게이트 → 진입.
        for symbol, (ret_pct, turnover) in ranked[:n_bottom]:
            if ret_pct > -self.min_drop_pct:
                last_reject[symbol] = (
                    f"세션 {ret_pct:+.2f}% — 낙폭 {self.min_drop_pct:g}% 미달"
                )
                continue
            if turnover < self.min_turnover_krw:
                last_reject[symbol] = (
                    f"거래대금 {turnover:,.0f} < 하한 {self.min_turnover_krw:,.0f}"
                )
                continue
            if turnover > self.max_turnover_krw:
                last_reject[symbol] = (
                    f"거래대금 {turnover:,.0f} > 상한 {self.max_turnover_krw:,.0f}"
                )
                continue
            if self._my_lot(snap, symbol) is not None:
                continue  # 보유 중엔 신규 진입 평가 없음
            if entries_today.get(symbol) == today_iso:
                last_reject[symbol] = "1일 1회 진입 소진"
                continue
            signal = self._enter(
                symbol, snap, today_iso, ret_pct, turnover, entries_today, last_reject
            )
            if signal is not None:
                signals.append(signal)

    def _enter(
        self, symbol: str, snap: StrategySnapshot, today_iso: str,
        ret_pct: float, turnover: float,
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
                f"장 막판 역추세 매수: {symbol} w={self.target_weight:.2f} "
                f"세션 {ret_pct:+.2f}% 현재={fmt_price(entry, symbol)} "
                f"손절={fmt_price(stop, symbol)} [거래대금 {turnover:,.0f}]"
            ),
            stop=stop,
            state_update={
                "entry": entry, "stop": stop,
                "session": today_iso, "entered_at": snap.now.isoformat(),
                "strategy": self.id,
            },
        )

    # ------------------------------------------------------------------ 데이터 헬퍼

    def _session_stats(
        self, symbol: str, market: str, snap: StrategySnapshot, today: dtdate
    ) -> tuple[float, float] | None:
        """(세션 수익률 %, 세션 누적 거래대금). 계산 불가면 None — "확인 불가는
        통과가 아니라 거부다"."""
        bars = snap.bars.get((symbol, _INTERVAL))
        if bars is None or bars.empty:
            return None
        tz = market_tz(market)
        open_t, close_t = continuous_window(market)
        local = bars.index.tz_convert(tz)
        session = bars[(local.date == today) & (local.time >= open_t) & (local.time < close_t)]
        if len(session) < self.min_session_bars:
            return None
        open_px = float(session["open"].iloc[0])
        last_px = float(session["close"].iloc[-1])
        if pd.isna(open_px) or pd.isna(last_px) or open_px <= 0:
            return None
        turnover = float((session["close"] * session["volume"]).fillna(0.0).sum())
        return (last_px / open_px - 1) * 100, turnover

    @staticmethod
    def _wall_minutes_to_continuous_close(market: str, now: datetime) -> float:
        tz = market_tz(market)
        local = now.astimezone(tz)
        _, end_t = continuous_window(market)
        return (datetime.combine(local.date(), end_t, tzinfo=tz) - local).total_seconds() / 60

    def _minutes_to_close(self, market: str, snap: StrategySnapshot) -> float | None:
        """평가창 판정용 잔여시간 — 캘린더(`snap.minutes_to_close`, 조기폐장 인지)와
        연속거래 종료 벽시계 중 **이른 쪽**. `kernel.should_flatten_dual` 이
        청산에서 쓰는 것과 같은 이중 근거를 진입 창에도 그대로 쓴다."""
        wall = self._wall_minutes_to_continuous_close(market, snap.now)
        mtc = snap.minutes_to_close.get(market)
        candidates = [r for r in (mtc, wall) if r is not None and r > 0]
        return min(candidates) if candidates else None

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


class EodReversalShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies` 용 얇은 팩토리 — `VolBreakoutShell`
    과 동일 패턴."""

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "KR", id: str = "eod_reversal"):
        super().__init__(EodReversalPureStrategy(symbols, params, market=market, id=id))
