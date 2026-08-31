"""합류(confluence) 투표 전략 — MACD·볼린저·RSI·이동평균·박스(횡보) 인식을
단일 지표가 아니라 다축 투표로 묶어 진입을 판단한다. 롱 온리.

## 규칙 출처와 정직성

기존 3전략(donchian/orb_scan/intraday_scan)은 전부 돌파(breakout) 계열이고
보조지표를 쓰지 않는다. 이 전략은 사용자 요청으로 보조지표 축을 추가하되,
**단일 지표로 매매하지 않는다**는 문헌의 공통 결론(및 이 저장소 원칙)에 따라
5개 독립 축 중 최소 `min_confluence`(기본 3)개 이상 동의해야 진입한다.
파라미터는 전부 문헌 표준값(RSI 14, MACD 12/26/9, 볼린저 20·2σ, SMA 20/50)을
그대로 썼다 — 여러 조합을 시도해 고른 값이 아니다(다중검정 편향 회피).
**백테스트로 검증된 적 없다 [미검증]** — paper 번인(스코어보드의 거래당 bps)이
쌓이기 전까지 수익성 주장은 하지 않는다.

## 5축 투표 (최소 min_confluence개 동의 시 진입)

1. **추세**: 종가 > SMA20 > SMA50 (정배열)
2. **모멘텀**: MACD 골든크로스(선이 시그널선 상향 돌파)가 최근 cross_lookback봉 내 발생
3. **평균회귀 반등**: RSI가 30 아래에서 위로 회복 또는 %B가 0 아래에서 위로 회복
4. **변동성 확장**: 직전 봉이 스퀴즈였고 현재 봉이 볼린저 상단을 종가 돌파
5. **박스 돌파**: 직전 봉까지 박스였고 종가가 그 박스 상단을 돌파 + 거래량이
   최근 평균의 volume_mult배 이상

지표는 `quant/trade/indicators/`의 순수 함수를 그대로 쓴다. **다른 스캐너와
달리 오늘 세션 봉으로 필터링하지 않는다** — SMA50/MACD(26,9)/스퀴즈(20+20봉)는
하루 세션 봉 수(5분봉 기준 KR 78봉/US 78봉)로는 워밍업을 채우기 빠듯하거나
모자라, 세션 경계를 넘어 이어지는 봉 시계열 그대로 계산해야 지표가 의미를
갖는다. 대신 포지션 관리(오버나잇 금지·EoD 청산)는 다른 스캐너와 동일하게
세션 롤 레일을 그대로 따른다 — "지표 계산은 다세션, 포지션 보유는 당일 한정"
이라는 뜻이다.

## 청산

- 손절: 진입봉 기준 ATR(14, **분봉 기준** — atr_period와 같은 인터벌) x
  atr_stop_mult(기본 1.0) 아래. 기존 스캐너(orb_scan/intraday_scan)는 **일봉**
  ATR x 0.35를 쓰지만, 이 전략은 진입 판정 자체가 분봉 지표(MACD/볼린저 등)
  기준이라 손절도 같은 인터벌로 재는 게 일관적이다 — 값이 다른 것은 실수가
  아니라 인터벌이 다르기 때문이다.
- 부분 익절: `scale_out_at_r`(기본 1.5R)에서 `scale_out_fraction`(기본 0.3)
  청산 — 사용자 요구("공식대로 부분 이득"). 발동 시 잔여분 손절을 **항상**
  본전(entry)으로 상향한다(donchian의 `breakeven_after_scale_out`처럼
  옵션이 아니라 스펙상 무조건).
- 전량 청산: RSI가 70 위에서 아래로 이탈, 또는 MACD 데드크로스(선이 시그널선
  하향 이탈), 또는 EoD 강제청산.
- 오버나잇 금지: 세션 롤 시 강제청산(다른 스캐너와 동일 레일).

## 구조 — orb_scan/intraday_scan 패턴 계승

전략별 랏(`pos.ensure_lot(self.id)`), `_owns(pos)` 소유권 필터, 시장별 세션
상태(`_SESSION_OPEN`), 봉 슬롯 게이트(`_evaluated_slot`)는 intraday_scan.py를
그대로 따른다. 단, 봉 슬롯 게이트는 **진입 스캔(관심종목 전체)에만** 쓴다 —
intraday_scan이 이 게이트를 만든 이유는 "5초마다 전 종목 history 조회"가
비쌌기 때문인데, 포지션 관리(`_manage_position`)는 리스크가 이미 상한을 건
소수의 열린 포지션에 대해서만 돌아 같은 규모의 문제가 아니다. 그래서 관리
루프는 매 사이클 필요한 만큼(손절가 감시는 실시간 시세로, RSI/MACD 청산
판정은 매 사이클 history 재조회로) 그대로 돈다 — orb_scan/intraday_scan의
관리 루프가 시세만 보는 것과 달리 이 전략은 지표 기반 청산이 있어 불가피하다.
"""
from __future__ import annotations

from datetime import date as dtdate

import pandas as pd

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction
from quant.trade.indicators import bollinger, detect_box, macd, rsi, sma, sma_atr, squeeze
from quant.trade.strategy.orb_scan import _SESSION_OPEN


def _atr(bars: pd.DataFrame, period: int) -> float:
    """분봉 기준 단순평균 ATR — `quant.trade.indicators.sma_atr` 참고(구현
    근거는 그쪽 docstring). 인터벌만 다르다(여기는 전략의 판정 인터벌 그대로,
    다른 스캐너는 별도 일봉 조회)."""
    return sma_atr(bars, period)


class ConfluenceStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "confluence"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        self.bar_interval_minutes: int = params.get("bar_interval_minutes", 5)
        if self.bar_interval_minutes <= 0:
            raise ValueError("bar_interval_minutes는 양수여야 합니다.")
        self.interval = f"{self.bar_interval_minutes}m"

        self.entry_start_minutes: int = params.get("entry_start_minutes_after_open", 0)
        self.no_entry_minutes_before_close: int = params.get("no_entry_minutes_before_close", 60)

        # 5축 파라미터 — 전부 문헌 표준값 [미검증, 다중검정 없이 표준값 그대로]
        self.sma_fast: int = params.get("sma_fast", 20)
        self.sma_slow: int = params.get("sma_slow", 50)
        self.rsi_period: int = params.get("rsi_period", 14)
        self.macd_fast: int = params.get("macd_fast", 12)
        self.macd_slow: int = params.get("macd_slow", 26)
        self.macd_signal: int = params.get("macd_signal", 9)
        self.cross_lookback: int = params.get("cross_lookback", 3)
        self.bb_period: int = params.get("bb_period", 20)
        self.bb_num_std: float = params.get("bb_num_std", 2.0)
        self.squeeze_lookback: int = params.get("squeeze_lookback", 20)
        self.box_lookback: int = params.get("box_lookback", 20)
        self.box_tolerance_pct: float = params.get("box_tolerance_pct", 1.5)
        self.volume_mult: float = params.get("volume_mult", 1.5)
        self.min_confluence: int = params.get("min_confluence", 3)
        if not 1 <= self.min_confluence <= 5:
            raise ValueError("min_confluence는 1~5 사이여야 합니다.")

        self.atr_period: int = params.get("atr_period", 14)
        self.atr_stop_mult: float = params.get("atr_stop_mult", 1.0)
        self.scale_out_at_r: float = params.get("scale_out_at_r", 1.5)
        self.scale_out_fraction: float = params.get("scale_out_fraction", 0.3)

        self.risk_budget_pct: float = params.get("risk_budget_pct", 1.0)
        self.max_leverage: float = params.get("max_leverage", 1.0)
        self.flatten_minutes: int = params.get("flatten_before_close_minutes", 1)

        # 지표별 최소 필요 봉 수 + 여유분. 세션 경계를 넘어 이어지는 봉 시계열
        # 기준이므로(모듈 docstring 참고) 하루치가 아니라 지표 워밍업 기준으로 잡는다.
        self._min_bars_required = max(
            self.sma_slow,
            self.macd_slow + self.macd_signal,
            self.bb_period + self.squeeze_lookback,
            self.box_lookback,
            self.atr_period,
        ) + max(self.cross_lookback, 2) + 2
        self._lookback_bars = max(params.get("lookback_bars", 0), self._min_bars_required + 10)

        self._session_date: dict[str, dtdate] = {}
        self.max_entries_per_day: int = params.get("max_entries_per_day", 0)
        self.allow_add_while_holding: bool = params.get("allow_add_while_holding", True)
        self._entries_today: dict[str, int] = {}
        self._last_entry_bar: dict[str, object] = {}
        # 봉 슬롯 게이트 — 진입 스캔(관심종목 전체)에만 적용(모듈 docstring 참고).
        self._evaluated_slot: dict[str, int] = {}
        self._pending: dict[str, dict] = {}
        self.last_reject: dict[str, str] = {}

    @staticmethod
    def _market_of(symbol: str) -> str:
        """6자리 숫자 = KR — 저장소 전역과 동일한 구조 추론(orb_scan과 동일)."""
        return "KR" if (symbol.isdigit() and len(symbol) == 6) else "US"

    # ------------------------------------------------------------------ 사이클

    def _owns(self, pos) -> bool:
        """이 포지션을 내가 관리해야 하는가 — orb_scan/intraday_scan의 `_owns`와
        동일 구현(2026-08-11 랏 도입 포함). 세 단계: (1) 내 lot이 있으면 내 것,
        (2) lots 구조 자체가 있는데 내 lot이 없으면 이미 다른 전략이 추적
        중이므로 입양하지 않는다, (3) 태그도 lots도 없으면(순수 레거시/미상)
        내 유니버스 소속일 때만 떠맡는다."""
        if pos.lot(self.id) is not None:
            return True
        meta = pos.meta or {}
        if meta.get("lots") is not None:
            return False
        owner = meta.get("strategy")
        if owner:
            return owner == self.id
        return pos.symbol in self.symbols

    def on_cycle(self, ctx: Context) -> list[Signal]:
        signals: list[Signal] = []
        positions = ctx.broker.positions()

        # 1) 포지션 관리 — 유니버스와 무관하게 열려 있는 자기 포지션을 본다.
        for symbol, pos in positions.items():
            if not pos.is_open or not self._owns(pos):
                continue
            self._ensure_state(symbol, pos)
            signal = self._manage_position(symbol, pos, ctx)
            if signal is not None:
                signals.append(signal)

        # 2) 시장별 진입 창 + 봉 슬롯 게이트
        markets_present = sorted({self._market_of(s) for s in self.symbols})
        for market in markets_present:
            if not ctx.clock.is_market_open(market):
                continue
            if ctx.clock.should_flatten(market, self.flatten_minutes):
                continue
            tz, session_open = _SESSION_OPEN[market]
            now_local = ctx.clock.now().astimezone(tz)
            if now_local.date() != self._session_date.get(market):
                self._session_date[market] = now_local.date()
                self._entries_today = {
                    sym: n for sym, n in self._entries_today.items()
                    if self._market_of(sym) != market
                }
                self._last_entry_bar = {
                    sym: bar for sym, bar in self._last_entry_bar.items()
                    if self._market_of(sym) != market
                }
                self._evaluated_slot = {
                    sym: slot for sym, slot in self._evaluated_slot.items()
                    if self._market_of(sym) != market
                }
                self._pending = {
                    sym: pend for sym, pend in self._pending.items()
                    if self._market_of(sym) != market
                    or (positions.get(sym) is not None and positions.get(sym).is_open)
                }
            if ctx.clock.should_flatten(market, self.no_entry_minutes_before_close):
                continue
            minutes_since_open = (
                now_local.hour * 60 + now_local.minute
                - (session_open.hour * 60 + session_open.minute)
            )
            if minutes_since_open < self.entry_start_minutes:
                continue
            bar_slot = minutes_since_open // self.bar_interval_minutes

            for symbol in self.symbols:
                if self._market_of(symbol) != market:
                    continue
                if self._evaluated_slot.get(symbol) == bar_slot:
                    continue
                if (
                    self.max_entries_per_day
                    and self._entries_today.get(symbol, 0) >= self.max_entries_per_day
                ):
                    continue
                pos = positions.get(symbol)
                if pos is not None and pos.is_open and not self.allow_add_while_holding:
                    continue
                held_qty = pos.lot_qty(self.id) if pos is not None else 0.0
                signal = self._check_entry_for(
                    symbol, market, ctx, held_qty, bar_slot=bar_slot,
                    session_open_minutes=session_open.hour * 60 + session_open.minute,
                )
                if signal is not None:
                    signals.append(signal)
        return signals

    # ------------------------------------------------------------------ 진입

    def _check_entry_for(self, symbol: str, market: str, ctx: Context,
                         held_qty: float = 0.0, bar_slot: int | None = None,
                         session_open_minutes: int | None = None) -> Signal | None:
        self.last_reject.pop(symbol, None)
        bars = ctx.data.history(symbol, self.interval, self._lookback_bars)
        if len(bars) < self._min_bars_required:
            self.last_reject[symbol] = f"데이터 부족: {len(bars)} < {self._min_bars_required}"
            return None

        last_bar_ts = bars.index[-1]

        # 캐시 지연 감지 — intraday_scan과 동일 이유(봉이 닫힌 직후 캐시가 이전
        # 봉까지만 보일 수 있다). 기대한 새 봉이 아니면 슬롯을 소비하지 않는다.
        if bar_slot is not None and session_open_minutes is not None:
            tz, _ = _SESSION_OPEN[market]
            ts_local = last_bar_ts.astimezone(tz)
            bar_minutes = ts_local.hour * 60 + ts_local.minute - session_open_minutes
            if bar_minutes // self.bar_interval_minutes < bar_slot - 1:
                self.last_reject[symbol] = "새 봉 데이터 대기 (캐시 갱신 전)"
                return None
            self._evaluated_slot[symbol] = bar_slot

        if self._last_entry_bar.get(symbol) == last_bar_ts:
            self.last_reject[symbol] = "같은 봉으로 이미 진입 — 새 봉 대기"
            return None

        votes, detail = self._confluence_votes(bars)
        if votes < self.min_confluence:
            self.last_reject[symbol] = f"합류 부족: {votes}/{self.min_confluence} ({detail})"
            return None

        atr = _atr(bars, self.atr_period)
        if atr <= 0:
            self.last_reject[symbol] = "ATR 계산 불가"
            return None

        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            self.last_reject[symbol] = "현재가 없음"
            return None
        entry_price = quote.price
        stop = entry_price - self.atr_stop_mult * atr
        if stop <= 0 or stop >= entry_price:
            self.last_reject[symbol] = "손절가 계산 이상"
            return None
        risk_pct = (entry_price - stop) / entry_price
        target_weight = min((self.risk_budget_pct / 100) / risk_pct, self.max_leverage)

        self._pending[symbol] = {
            "entry": entry_price, "stop": stop, "r0": entry_price - stop,
            "scaled_out": False,
            "session": self._session_date[market].isoformat(),
            "strategy": self.id,
            "qty_at_signal": held_qty,
        }
        self._entries_today[symbol] = self._entries_today.get(symbol, 0) + 1
        self._last_entry_bar[symbol] = last_bar_ts

        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=target_weight,
            reason=(
                f"합류 {votes}/{self.min_confluence} ({detail}) → {symbol} 진입 "
                f"w={target_weight:.2f} R={risk_pct*100:.2f}%"
            ),
            stop=stop,
        )

    def _confluence_votes(self, bars: pd.DataFrame) -> tuple[int, str]:
        """5축 중 몇 개가 동의하는지와 그 태그를 반환한다. 각 축은 독립적으로
        NaN(워밍업 부족)이면 자동으로 미동의 처리된다 — 축마다 필요한 워밍업
        길이가 달라(스퀴즈+볼린저가 가장 길다) 모두 동시에 유효하지 않을 수
        있다."""
        close, high, low, volume = bars["close"], bars["high"], bars["low"], bars["volume"]
        votes = 0
        tags: list[str] = []

        # 1) 추세: 종가 > SMA20 > SMA50
        sma_fast = sma(close, self.sma_fast)
        sma_slow = sma(close, self.sma_slow)
        if (
            pd.notna(sma_fast.iloc[-1]) and pd.notna(sma_slow.iloc[-1])
            and close.iloc[-1] > sma_fast.iloc[-1] > sma_slow.iloc[-1]
        ):
            votes += 1
            tags.append("추세")

        # 2) 모멘텀: MACD 골든크로스가 최근 cross_lookback봉 내 발생
        macd_line, signal_line, _ = macd(close, self.macd_fast, self.macd_slow, self.macd_signal)
        window = min(self.cross_lookback, len(close) - 1)
        golden_cross = False
        for i in range(len(close) - window, len(close)):
            if i < 1:
                continue
            pm, ps, cm, cs = macd_line.iloc[i - 1], signal_line.iloc[i - 1], macd_line.iloc[i], signal_line.iloc[i]
            if pd.notna(pm) and pd.notna(ps) and pd.notna(cm) and pd.notna(cs) and pm <= ps and cm > cs:
                golden_cross = True
                break
        if golden_cross:
            votes += 1
            tags.append("모멘텀")

        # 3) 평균회귀 반등: RSI 30 회복 또는 %B 0 회복
        rsi_series = rsi(close, self.rsi_period)
        _, upper, _lower, _bw, percent_b = bollinger(close, self.bb_period, self.bb_num_std)
        rsi_recover = (
            len(rsi_series) >= 2 and pd.notna(rsi_series.iloc[-2]) and pd.notna(rsi_series.iloc[-1])
            and rsi_series.iloc[-2] < 30 <= rsi_series.iloc[-1]
        )
        pb_recover = (
            len(percent_b) >= 2 and pd.notna(percent_b.iloc[-2]) and pd.notna(percent_b.iloc[-1])
            and percent_b.iloc[-2] < 0 <= percent_b.iloc[-1]
        )
        if rsi_recover or pb_recover:
            votes += 1
            tags.append("평균회귀반등")

        # 4) 변동성 확장: 직전 봉 스퀴즈 + 현재 봉 볼린저 상단 종가 돌파
        sq = squeeze(close, self.bb_period, self.bb_num_std, self.squeeze_lookback)
        if (
            len(sq) >= 2 and bool(sq.iloc[-2])
            and pd.notna(upper.iloc[-1]) and close.iloc[-1] > upper.iloc[-1]
        ):
            votes += 1
            tags.append("변동성확장")

        # 5) 박스 돌파: 직전 봉까지 박스 + 종가가 그 박스 상단 돌파 + 거래량 게이트
        is_box, box_high, _box_low = detect_box(high, low, self.box_lookback, self.box_tolerance_pct)
        if (
            len(is_box) >= 2 and bool(is_box.iloc[-2]) and pd.notna(box_high.iloc[-2])
            and close.iloc[-1] > box_high.iloc[-2]
        ):
            recent = volume.iloc[-(self.box_lookback + 1):-1]
            avg_vol = float(recent.mean()) if len(recent) else 0.0
            if avg_vol > 0 and float(volume.iloc[-1]) >= self.volume_mult * avg_vol:
                votes += 1
                tags.append("박스돌파")

        return votes, ("+".join(tags) if tags else "없음")

    # ------------------------------------------------------------------ 관리

    def _ensure_state(self, symbol: str, pos: Position) -> None:
        lot = pos.ensure_lot(self.id)
        pending = self._pending.get(symbol)
        if pending is not None and lot.get("qty", 0.0) > pending.get("qty_at_signal", 0.0):
            self._pending.pop(symbol, None)
            fresh = {k: v for k, v in pending.items() if k != "qty_at_signal"}
            lot.update(fresh)
            return
        if "entry" in lot:
            return
        # 재시작 복구 — 지표 컨텍스트를 잃었다. 보수적 고정 2% 손절 폴백(다른
        # 스캐너와 동일 관례). "session"은 남기지 않아(orb_scan과 동일) 세션
        # 롤 강제청산이 이 포지션에는 적용되지 않는다(오탐 방지).
        entry = lot.get("avg_cost", pos.avg_cost)
        stop = entry * 0.98
        lot.update(entry=entry, stop=stop, r0=entry - stop, scaled_out=False)

    def _manage_position(self, symbol: str, pos: Position, ctx: Context) -> Signal | None:
        quote = ctx.data.quote(symbol)
        if quote is None:
            return None
        price = quote.price
        lot = pos.ensure_lot(self.id)
        entry, stop = lot["entry"], lot["stop"]

        market = self._market_of(symbol)
        tz, _ = _SESSION_OPEN[market]
        entry_session = lot.get("session")
        if entry_session and entry_session != ctx.clock.now().astimezone(tz).date().isoformat():
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"세션 롤 강제청산(오버나잇 금지): 진입 {entry_session} 현재={price:.2f}",
            )
        if ctx.clock.should_flatten(market, self.flatten_minutes):
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"EoD 청산: entry={entry:.2f} 현재={price:.2f}",
            )
        if price <= stop:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"손절: entry={entry:.2f} stop={stop:.2f} 현재={price:.2f}",
            )

        # RSI 과매수 이탈 / MACD 데드크로스 — 지표 기반 전량 청산. 포지션이
        # 소수(리스크 상한)라 매 사이클 재조회해도 진입 스캔만큼 비싸지 않다
        # (모듈 docstring 참고).
        bars = ctx.data.history(symbol, self.interval, self._lookback_bars)
        if len(bars) >= self._min_bars_required:
            close = bars["close"]
            rsi_series = rsi(close, self.rsi_period)
            if (
                len(rsi_series) >= 2 and pd.notna(rsi_series.iloc[-2]) and pd.notna(rsi_series.iloc[-1])
                and rsi_series.iloc[-2] > 70 >= rsi_series.iloc[-1]
            ):
                return Signal(
                    strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0,
                    reason=f"RSI 과매수 이탈: RSI={rsi_series.iloc[-1]:.1f} 현재={price:.2f}",
                )
            macd_line, signal_line, _ = macd(close, self.macd_fast, self.macd_slow, self.macd_signal)
            if (
                len(macd_line) >= 2
                and pd.notna(macd_line.iloc[-2]) and pd.notna(signal_line.iloc[-2])
                and pd.notna(macd_line.iloc[-1]) and pd.notna(signal_line.iloc[-1])
                and macd_line.iloc[-2] >= signal_line.iloc[-2] and macd_line.iloc[-1] < signal_line.iloc[-1]
            ):
                return Signal(
                    strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0,
                    reason=f"MACD 데드크로스: 현재={price:.2f}",
                )

        # 부분 익절 — 1.5R 도달 시 1회, 이후 잔여분 손절을 항상 본전으로.
        r0 = lot.get("r0")
        if r0 and r0 > 0 and not lot.get("scaled_out") and price >= entry + self.scale_out_at_r * r0:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.SCALE_OUT,
                target_weight=0.0, exit_fraction=self.scale_out_fraction,
                reason=(
                    f"부분 익절 (+{self.scale_out_at_r:g}R): entry={entry:.2f} 현재={price:.2f} "
                    f"손절가 breakeven 이동"
                ),
                state_update={"scaled_out": True, "stop": entry},
            )
        return None
