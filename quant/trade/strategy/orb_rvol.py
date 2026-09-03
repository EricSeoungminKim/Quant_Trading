"""개장 5분 레인지 돌파 + 개장 상대거래량(rvol) 필터 — Zarattini, Barbon & Aziz
(2024), *"Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF
(SPY)"* 계열의 후속 논문 **"Stocks in Play"**(SSRN 4729284)의 규격을 이 저장소
계약(`quant.core.strategy_api.PureStrategy`)으로 옮긴 것. **순수 계약 전용 신규
전략**(레거시 쌍둥이 없음).

논문의 중심 주장은 "개장 레인지 돌파는 아무 종목에서나 되는 게 아니라, **그날
평소보다 압도적으로 많은 거래가 붙은 종목(stocks in play)** 에서만 된다"이다.
그래서 이 전략의 핵심은 돌파가 아니라 **rvol 로 종목을 고르는 부분**이다.

## 규칙

1. **개장 레인지(OR)** = 연속 거래 개장 직후 **첫 5분봉** 하나(KR 09:00~09:05,
   US 09:30~09:35 현지). 그 봉의 고가가 트리거, 저가/시가/종가는 게이트에 쓴다.
2. **도지 제외** — `|종가 − 시가| < doji_body_frac`(기본 0.10) `× (고가 − 저가)`
   이면 그 종목은 그날 보지 않는다. 방향이 없는 개장봉은 논문의 "in play" 정의
   (개장에 방향성 있는 수급이 붙었다)와 어긋난다.
3. **rvol** = 오늘 첫 5분봉 거래량 ÷ **직전 `rvol_days`(기본 14) 세션의 같은
   첫 5분봉 거래량 평균**. 이 값은 개장 5분이 끝나는 순간 확정되고 그날 내내
   변하지 않는다 — 그래서 아래 순위(4번)도 세션 안에서 흔들리지 않는다.
4. **후보 선별** — `rvol >= rvol_min`(기본 1.0) 인 종목만, 그중 rvol 상위
   `top_k`(기본 5)만 그날의 "stocks in play"다. 순위는 **시장별로** 매긴다
   (KR/US 는 세션이 다르므로 한 줄에 세울 수 없다).
5. **유동성/가격 필터**(논문의 종목 모집단 정의) — 개장봉 종가가 `min_price`
   미만(KR 1,000원 / US $5)이거나, 최근 `avg_volume_days`(14) 일봉 평균 거래량이
   `min_avg_volume` 미만이면 제외.
6. **진입** — 현재가가 OR 고가를 **상향 돌파**하면 롱. 롱 온리다(논문은 숏도
   같이 재지만, 이 저장소는 인버스 ETF 외에 숏 수단이 없다). 개장 후
   `entry_window_min`(기본 60)분이 지나면 그날은 더 보지 않는다. 심볼당 세션당 1회.
7. **손절** = 진입가 − `stop_atr_frac`(기본 0.10) × **ATR14(일봉)**. 목표는
   없다 — 마감 `eod_exit_min`(기본 3)분 전 전량 청산(오버나잇 금지).

## 데이터

- `"5m"` — 개장 레인지 봉 + rvol 의 과거 평균. rvol 이 직전 14 세션을 보므로
  `(rvol_days + 1) × 78 + 여유` 개를 요청한다(정규장 390분 ÷ 5분 = 78봉).
  `HistoryDataFeed`/`TossDataFeed` 둘 다 1분봉에서 5분봉을 리샘플해 서빙한다
  (`quant/adapters/data/history.py`, `quant/adapters/brokers/toss/datafeed.py`)
  — 1분봉으로 직접 5배 많은 봉을 받는 것보다 싸다.
- `"1d"` — ATR14(손절폭) + 평균 거래량(5번 필터). `sma_atr`(단순평균 ATR,
  `quant/trade/indicators/__init__.py`)을 그대로 쓴다. 장중에는 완성봉 필터가
  오늘 일봉을 잘라내므로 마지막 행은 전일이다(`vol_breakout` 과 같은 전제).

**비용 고지(정직하게)**: 5분봉 1,180개 요청은 이 저장소에서 가장 무거운 콜드
페치다 — `TossDataFeed._load_1m` 이 콜드 스타트에 `max(n*20, 200)` 개의 1분봉을
200개씩 페이징해 받는다. 디스크 캐시가 있어 심볼당 사실상 1회지만, 관심종목이
한꺼번에 갈리는 날엔 그 사이클의 `cold_fetch_budget_per_cycle` 을 통째로 먹는다.
이 전략을 켜기 전에 그 예산을 반드시 다시 보라.

## 논문 숫자를 그대로 믿지 말 것 (이 전략의 가장 큰 위험)

1. **논문 성과는 in-sample 이다.** 저자들이 같은 데이터에서 규칙을 고르고 같은
   데이터에서 성과를 쟀다. 우리 원장 표본은 0이다 — paper 번인이 유일한 검증
   경로다.
2. **손절이 우리 비용보다 얕을 수 있다.** `0.10 × ATR14` 는 ATR 이 가격의 2%인
   종목에서 **20bp** 다 — 우리 왕복 비용(≈20bp)과 같다. 즉 논문 규칙 그대로는
   기댓값이 비용에 먹힐 수 있다. 그래서 `min_stop_bp` 게이트를 달아 두되
   **기본값은 0(비활성)** 이다: 첫 측정만은 논문 규칙을 훼손하지 않고 재고,
   번인 결과를 보고 사람이 올린다(`quant/control/governor.py` 의 다른
   `min_stop_bp` 3종과 같은 손잡이).
3. **논문은 미국 대형주 수천 종목에서 상위 rvol 을 골랐다.** 우리 관심종목은
   많아야 수십 개다 — 같은 `top_k`(5)라도 "상위 5"의 의미가 다르다.

## 상태가 두 갈래로 흐른다 (다른 순수 전략과 같은 이유)

| # | 값 | 어디로 | 왜 |
|---|---|---|---|
| 1 | `session_date`/`entries_today`/`last_reject` | `next_state` | 하루 안에서만 의미가 있다 |
| 2 | `entry`/`stop`/`session`/`entered_at` | **`Signal.state_update` → 체결 확인 후 `Position.meta["lots"]` → 다음 사이클 `snap.lots`** | 포지션이 살아 있는 한 필요하다 |

2026-08-28 실사건(포지션 8개를 든 채 장중 재시작)에서 인스턴스 상태는 증발하지만
브로커 포지션은 남는다 — 방어선의 정본이 lot 이면 재시작이 그것을 갈라놓지 못한다.

## 아직 못 하는 것 (정직하게)

1. **우리 원장 실측 0.** 위 "논문 숫자" 절 참고.
2. **rvol 기준일이 모자라면 그 종목을 통째로 건너뛴다.** 신규 편입 종목은
   `min_rvol_sessions`(기본 5) 세션이 쌓이기 전까지 진입 대상이 아니다 —
   "확인 불가는 통과가 아니라 거부다"(`mr_vwap_quiet` 와 같은 원칙).
3. **고아 포지션을 볼 수 없다.** `DataNeeds`가 정적으로 `self.symbols`만 선언하므로
   유니버스에서 빠진 뒤 남은 보유분은 관리되지 않는다(관심종목 전략 공통 한계).
4. **재시작이 "1일 1회" 게이트를 되돌린다.** 보유 중인 심볼은 `snap.lots`가
   막지만, "진입 → 청산 → 재시작"이 같은 날 일어나면 재진입이 날 수 있다.
5. **거래대금이 아니라 주식 수로 유동성을 잰다.** 논문은 달러 거래대금을 쓴다 —
   `min_avg_volume` 은 주식 수라 고가주/저가주 사이에서 공정하지 않다. KR 기본값
   (100,000주)은 논문 근거가 아니라 우리 추정이다.
"""
from __future__ import annotations

from datetime import date as dtdate, datetime
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import continuous_window, in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.indicators import sma_atr
from quant.trade.strategy import kernel
from quant.trade.strategy.shell import PureStrategyShell

# 개장 레인지 = 첫 5분봉 하나. 논문의 opening range 정의 그대로.
_INTERVAL = "5m"
# 정규장 길이(분) — KR 09:00~15:30, US 09:30~16:00 둘 다 390분.
_FULL_SESSION_MINUTES = 390
_BARS_PER_SESSION = _FULL_SESSION_MINUTES // 5  # 78
# 휴장/결손 여유분.
_BAR_SLACK = 10
# 시장별 가격 하한 기본값 — 논문의 US 필터($5)와 그 KR 대응(1,000원, 우리 추정).
_DEFAULT_MIN_PRICE = {"KR": 1000.0, "US": 5.0}
# 시장별 평균 거래량 하한 기본값 — 논문의 US 필터(1M주)와 KR 추정치.
_DEFAULT_MIN_AVG_VOLUME = {"KR": 100_000.0, "US": 1_000_000.0}


def _market_floor(spec: Any, market: str, defaults: Mapping[str, float]) -> float:
    """`{KR: .., US: ..}` 형태의 시장별 하한을 읽는다. 스칼라를 주면 두 시장에
    같은 값을 쓴다(설정 손편집 편의). 선언이 없으면 `defaults`."""
    if spec is None:
        return float(defaults[market])
    if isinstance(spec, Mapping):
        value = spec.get(market)
        return float(defaults[market]) if value is None else float(value)
    return float(spec)


class OrbRvolPureStrategy:
    """개장 레인지 돌파 + rvol 선별 — `PureStrategy` 구현. 모듈 docstring 참고."""

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "KR", id: str = "orb_rvol"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        self.rvol_days: int = int(params.get("rvol_days", 14))
        self.rvol_min: float = float(params.get("rvol_min", 1.0))
        self.top_k: int = int(params.get("top_k", 5))
        self.entry_window_min: float = float(params.get("entry_window_min", 60))
        self.stop_atr_frac: float = float(params.get("stop_atr_frac", 0.10))
        self.atr_period: int = int(params.get("atr_period", 14))
        self.avg_volume_days: int = int(params.get("avg_volume_days", 14))
        self.doji_body_frac: float = float(params.get("doji_body_frac", 0.10))
        self.min_rvol_sessions: int = int(params.get("min_rvol_sessions", 5))
        self.eod_exit_min: float = float(params.get("eod_exit_min", 3))
        self.target_weight: float = float(params.get("target_weight", 0.5))
        # 기본 0 = 비활성. 왜 40 이 아닌지는 모듈 docstring "논문 숫자" 2번.
        self.min_stop_bp: float = kernel.parse_min_stop_bp(params, default=0.0)
        self._min_price = params.get("min_price")
        self._min_avg_volume = params.get("min_avg_volume")

        if self.rvol_days < 1:
            raise ValueError("rvol_days는 1 이상이어야 합니다.")
        if self.rvol_min < 0:
            raise ValueError("rvol_min은 0 이상이어야 합니다.")
        if self.top_k < 1:
            raise ValueError("top_k는 1 이상이어야 합니다.")
        if self.entry_window_min <= 0:
            raise ValueError("entry_window_min은 양수여야 합니다.")
        if self.stop_atr_frac <= 0:
            raise ValueError("stop_atr_frac은 양수여야 합니다.")
        if self.atr_period < 1:
            raise ValueError("atr_period는 1 이상이어야 합니다.")
        if self.avg_volume_days < 1:
            raise ValueError("avg_volume_days는 1 이상이어야 합니다.")
        if not 0 <= self.doji_body_frac < 1:
            raise ValueError("doji_body_frac은 0 이상 1 미만이어야 합니다.")
        if self.min_rvol_sessions < 1:
            raise ValueError("min_rvol_sessions는 1 이상이어야 합니다.")
        if self.eod_exit_min <= 0:
            raise ValueError("eod_exit_min은 양수여야 합니다.")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")

        self._session_bars_n = (self.rvol_days + 1) * _BARS_PER_SESSION + _BAR_SLACK
        self._daily_count = max(self.atr_period, self.avg_volume_days) + 5

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """5분봉(개장 레인지 + rvol 과거 평균) + 일봉(ATR14·평균 거래량) + 현재가
        + 포지션. 봉 수 산정 근거는 모듈 docstring "데이터" 절."""
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

        # 0) 세션 롤 — 관리보다 먼저. 하루짜리 값만 지운다.
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

        # 2) 진입 — 연속 거래 구간 + 개장 진입창 안에서만.
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

        # (a) 종목별 rvol 산출 — 보유/진입소진 종목도 순위에는 넣는다("in play"는
        #     그날의 시장 사실이지 내 포지션 사정이 아니다).
        plays: dict[str, tuple[float, float]] = {}   # symbol -> (rvol, OR 고가)
        for symbol in symbols:
            play = self._rvol_context(symbol, market, snap, today, last_reject)
            if play is not None:
                plays[symbol] = play

        # (b) rvol 상위 top_k 만 그날의 stocks in play.
        ranked = sorted(plays.items(), key=lambda kv: (-kv[1][0], kv[0]))
        for symbol, (rvol, _) in ranked[self.top_k:]:
            last_reject[symbol] = f"rvol {rvol:.2f} — 상위 {self.top_k}위 밖"

        # (c) 돌파 판정.
        for symbol, (rvol, or_high) in ranked[: self.top_k]:
            if self._my_lot(snap, symbol) is not None:
                continue  # 보유 중엔 신규 진입 평가 없음
            if entries_today.get(symbol) == today_iso:
                last_reject[symbol] = "1일 1회 진입 소진"
                continue
            signal = self._check_entry(
                symbol, market, snap, today_iso, rvol, or_high, entries_today, last_reject
            )
            if signal is not None:
                signals.append(signal)

    def _rvol_context(
        self, symbol: str, market: str, snap: StrategySnapshot, today: dtdate,
        last_reject: dict[str, str],
    ) -> tuple[float, float] | None:
        """게이트 2·3·5 를 통과하면 `(rvol, OR 고가)`, 아니면 None + 거부 사유."""
        opening = self._opening_bars(symbol, market, snap)
        today_bar = opening.get(today)
        if today_bar is None:
            last_reject[symbol] = "개장 5분봉 확인 불가"
            return None

        high = float(today_bar["high"])
        low = float(today_bar["low"])
        open_px = float(today_bar["open"])
        close_px = float(today_bar["close"])
        volume = float(today_bar["volume"])
        if pd.isna(high) or pd.isna(low) or pd.isna(open_px) or pd.isna(close_px):
            last_reject[symbol] = "개장 5분봉 값 결손"
            return None

        rng = high - low
        if not (rng > 0):
            last_reject[symbol] = "개장 5분봉 범위 0(고가<=저가)"
            return None
        if abs(close_px - open_px) < self.doji_body_frac * rng:
            last_reject[symbol] = (
                f"개장 5분봉 도지(몸통 {abs(close_px - open_px) / rng * 100:.0f}% "
                f"< {self.doji_body_frac * 100:.0f}%)"
            )
            return None

        min_price = _market_floor(self._min_price, market, _DEFAULT_MIN_PRICE)
        if close_px < min_price:
            last_reject[symbol] = (
                f"저가주 제외: {fmt_price(close_px, symbol)} < {fmt_price(min_price, symbol)}"
            )
            return None

        avg_volume = self._avg_daily_volume(snap.bars.get((symbol, "1d")))
        min_avg_volume = _market_floor(self._min_avg_volume, market, _DEFAULT_MIN_AVG_VOLUME)
        if avg_volume is None:
            last_reject[symbol] = "일봉 평균 거래량 확인 불가"
            return None
        if avg_volume < min_avg_volume:
            last_reject[symbol] = (
                f"평균 거래량 {avg_volume:,.0f} < 최소 {min_avg_volume:,.0f}"
            )
            return None

        prior = [
            float(bar["volume"]) for day, bar in sorted(opening.items()) if day < today
        ][-self.rvol_days:]
        prior = [v for v in prior if not pd.isna(v) and v > 0]
        if len(prior) < self.min_rvol_sessions:
            last_reject[symbol] = (
                f"rvol 기준일 부족({len(prior)}/{self.min_rvol_sessions} 세션)"
            )
            return None
        base = sum(prior) / len(prior)
        if not (base > 0) or pd.isna(volume):
            last_reject[symbol] = "rvol 계산 불가(과거 개장 거래량 0)"
            return None

        rvol = float(volume) / base
        if rvol < self.rvol_min:
            last_reject[symbol] = f"rvol {rvol:.2f} < 최소 {self.rvol_min:g}"
            return None
        return rvol, high

    def _check_entry(
        self, symbol: str, market: str, snap: StrategySnapshot, today_iso: str,
        rvol: float, or_high: float,
        entries_today: dict[str, str], last_reject: dict[str, str],
    ) -> Signal | None:
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            last_reject[symbol] = "현재가 없음"
            return None
        price = float(quote.price)
        if price <= or_high:
            return None  # 아직 돌파 전 — 정상 대기, 사유를 남기지 않는다

        atr = self._atr(snap.bars.get((symbol, "1d")))
        if atr is None or atr <= 0:
            last_reject[symbol] = "ATR 계산 불가(일봉 부족)"
            return None

        entry = price
        stop = entry - self.stop_atr_frac * atr
        if stop >= entry or stop <= 0:
            last_reject[symbol] = "손절가 계산 불가(진입가 이상)"
            return None
        stop_bp = (entry - stop) / entry * 1e4
        if not kernel.stop_bp_gate_ok(stop_bp, self.min_stop_bp):
            last_reject[symbol] = (
                f"손절폭 {stop_bp:.0f}bp < 최소 {self.min_stop_bp:g}bp — "
                "논문 손절(0.10×ATR)이 왕복 비용보다 얕다"
            )
            return None

        entries_today[symbol] = today_iso
        last_reject.pop(symbol, None)
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(
                f"개장 레인지 돌파(rvol {rvol:.2f}): {symbol} w={self.target_weight:.2f} "
                f"OR고가={fmt_price(or_high, symbol)} 현재={fmt_price(price, symbol)} "
                f"손절={fmt_price(stop, symbol)} [ATR{self.atr_period}={fmt_price(atr, symbol)}]"
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
        """연속 거래 개장 이후 경과 분. 개장 전이면 음수."""
        tz = market_tz(market)
        local = now.astimezone(tz)
        open_t, _ = continuous_window(market)
        open_dt = datetime.combine(local.date(), open_t, tzinfo=tz)
        return (local - open_dt).total_seconds() / 60

    def _opening_bars(
        self, symbol: str, market: str, snap: StrategySnapshot
    ) -> dict[dtdate, pd.Series]:
        """세션 날짜 → 그 세션의 **연속 거래 개장 이후 첫 5분봉**. 프리마켓 봉이
        섞이면 개장 레인지가 아니게 되므로 개장 시각 이후만 본다
        (`vol_breakout._session_open` 과 같은 필터)."""
        bars = snap.bars.get((symbol, _INTERVAL))
        if bars is None or bars.empty:
            return {}
        tz = market_tz(market)
        open_t, _ = continuous_window(market)
        local = bars.index.tz_convert(tz)
        mask = local.time >= open_t
        sub = bars[mask]
        if sub.empty:
            return {}
        first_pos: dict[dtdate, int] = {}
        for i, day in enumerate(local[mask].date):
            if day not in first_pos:
                first_pos[day] = i
        return {day: sub.iloc[i] for day, i in first_pos.items()}

    def _avg_daily_volume(self, daily: pd.DataFrame | None) -> float | None:
        if daily is None or daily.empty:
            return None
        vols = daily["volume"].dropna().tail(self.avg_volume_days)
        if vols.empty:
            return None
        return float(vols.mean())

    def _atr(self, daily: pd.DataFrame | None) -> float | None:
        """일봉 ATR(단순평균). 봉이 `atr_period+1` 개 미만이면 계산하지 않는다 —
        `sma_atr`는 있는 만큼 평균하지만(그 함수 docstring), 표본 2~3개짜리 ATR로
        손절폭을 정하는 건 지어내는 것에 가깝다."""
        if daily is None or len(daily) < self.atr_period + 1:
            return None
        value = sma_atr(daily, self.atr_period)
        return None if pd.isna(value) else float(value)

    # ------------------------------------------------------------------ 관리

    def _should_flatten(self, market: str, snap: StrategySnapshot) -> bool:
        """`kernel.should_flatten_dual` — 캘린더(조기폐장) **또는** 연속거래 종료
        벽시계 기준. KR 은 15:20 에 연속매매가 끝나므로 캘린더(15:30)만 보면
        체결될 수 없는 청산 주문이 나간다."""
        return kernel.should_flatten_dual(
            market, snap.now, snap.minutes_to_close.get(market),
            snap.cadence_minutes, self.eod_exit_min,
        )

    def _manage(
        self, symbol: str, lot: Mapping[str, Any], market: str, snap: StrategySnapshot
    ) -> Signal | None:
        """판정 순서: 오버나잇 안전망 → EoD → 손절(목표 없음)."""
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


class OrbRvolShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies` 가 다른 전략과 같은 방식으로
    생성할 수 있게 하는 얇은 팩토리 — `VolBreakoutShell` 과 동일 패턴."""

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "KR", id: str = "orb_rvol"):
        super().__init__(OrbRvolPureStrategy(symbols, params, market=market, id=id))
