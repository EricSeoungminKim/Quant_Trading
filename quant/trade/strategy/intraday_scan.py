"""장중 세션 신고가 돌파 스캐너 — orb_scan이 놓치는 개장 이후 움직임을 잡는다.

## 규칙 출처와 정직성

자체 설계다(2026-08-10): 세션 신고가 돌파 + 거래량 게이트. ORB 문헌(stocks-in-play,
RVOL 선별)과 같은 논리 축이지만 **백테스트로 검증된 적 없다 [미검증]** — paper
번인(세션 성적표의 거래당 bps)이 쌓이기 전까지 수익성 주장은 없다. orb_scan과
같은 관심종목 유니버스를 공유하되 진입 창이 달라 서로 배타적이다: orb_scan은
개장 직후 1회 판정, 이 전략은 그 뒤(기본 개장 +30분)부터 마감 전까지 본다.

## 규칙

- 진입: 오늘 세션의 마지막 완성봉 종가가 **그 이전 오늘 봉들의 최고가**를 넘고
  (세션 신고가 돌파), 그 봉의 거래량이 오늘 이전 봉 평균의 volume_mult배 이상.
  롱 온리. **같은 완성봉으로는 1회**만 진입하고(그 봉이 마지막인 동안 조건이
  계속 참이라 가드가 없으면 사이클마다 신호가 쏟아진다), 새 봉에서 다시 신호가
  나면 보유 중이어도 추가 진입한다(2026-08-10 사용자 결정 — 유효 신호를 '중복'
  이라는 이유로 막지 않는다. 사이징 증분·총노출은 리스크 레이어가 통제).
- 진입 창: 개장 +entry_start_minutes_after_open 이후 ~ 마감 no_entry_minutes_before_close
  전까지. 창 밖은 판정 자체를 건너뛴다.
- 손절: 일봉 ATR(atr_period) x atr_stop_mult (orb_scan과 동일 방식/파라미터 계승).
- 청산: 손절 / (설정 시) 목표가 / EoD 강제청산 + 세션 롤 오버나잇 금지 레일 —
  orb_scan의 _manage_position 레일을 그대로 따른다.
- 선택적 추세 필터(`require_trend_filter`, 기본 false): true면 종가가
  `trend_filter_period`(기본 20) SMA 위일 때만 진입을 허용한다(2026-08-12
  추가). 기본값이 false라 이 필터를 켜지 않으면 기존 동작이 바뀌지 않는다
  — 회귀 0을 테스트로 고정.

혼합 KR/US 유니버스의 시장별 세션 상태(_session_date/_entered_today/_pending 롤)는
orb_scan에서 실측으로 다듬어진 패턴을 그대로 계승한다 — 단일 market 필드로 처리하면
한쪽 시장이 조용히 죽는다(orb_scan.py 주석 참고).

## 시장 리스크오프 게이트 (2026-08-18 관측 기반, shadow 기본)

판정 로직은 `quant/trade/indicators/breadth.py`(모듈 docstring "측정 결과"
절 참고). 이 전략은 news_momentum/news_scalp와 달리 개장 후
entry_start_minutes_after_open(기본 30분)부터 진입을 평가하므로 앵커 데이터가
이미 존재한다 — **실측(2026-08-18)에서 이 전략의 손실(096770 13:30, 그날
최대 손실 -2.37%)은 세 테스트 임계(X=-0.3/-0.5/-1.0%) 전부에서 걸렸다**(그
시각 앵커 KODEX200이 시가 대비 -2.83%). 다만 표본 1일뿐이고 다른 두 전략에는
게이트가 사실상 무력했으므로("측정 결과" 절), 세 전략 모두 기본은 여전히
**shadow**로 통일한다(전략마다 기본 모드가 다르면 운영 중 혼란이 크다 — 근거가
갈리면 더 보수적인 공통값을 쓴다). `market_risk_gate_mode`(off/shadow/block)·
`market_risk_max_drawdown_pct`(기본 0.5%)로 조정. 앵커 조회는 시장당 분 경계
캐시(`_anchor_cache`)로 사이클마다 재조회하지 않는다.
"""
from __future__ import annotations

from datetime import date as dtdate

import pandas as pd

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction
from quant.trade.indicators import sma
from quant.trade.indicators.breadth import ANCHOR_SYMBOLS, anchor_drawdown
from quant.trade.strategy.orb_scan import _ATR_LOOKBACK_EXTRA, _SESSION_OPEN


class IntradayScanStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "intraday_scan"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        self.bar_interval_minutes: int = params.get("bar_interval_minutes", 5)
        if self.bar_interval_minutes <= 0:
            raise ValueError("bar_interval_minutes는 양수여야 합니다.")
        self.interval = f"{self.bar_interval_minutes}m"

        self.entry_start_minutes: int = params.get("entry_start_minutes_after_open", 30)
        self.no_entry_minutes_before_close: int = params.get("no_entry_minutes_before_close", 60)
        self.min_session_bars: int = params.get("min_session_bars", 6)
        self.volume_mult: float = params.get("volume_mult", 2.0)

        self.atr_period: int = params.get("atr_period", 14)
        self.atr_stop_mult: float = params.get("atr_stop_mult", 0.35)
        self.atr_interval: str = params.get("atr_interval", "1d")

        # 선택적 추세 필터 — 기본 꺼짐(모듈 docstring 참고). 켜면 종가가
        # trend_filter_period SMA 위일 때만 진입 허용.
        self.require_trend_filter: bool = params.get("require_trend_filter", False)
        self.trend_filter_period: int = params.get("trend_filter_period", 20)

        self.profit_target_r: float | None = params.get("profit_target_r")
        # 손절 거리 상한(bp). 0 = 비활성(기본) — 미설정이면 동작이 지금과 100% 같다
        # (donchian 의 stop_min_bps 와 같은 관례, 방향만 반대).
        #
        # 2026-08-21 실측: 라이브 원장 종결 63건 중 1분봉 재생 가능한 27건에 사전
        # 지정 청산 규칙 4종을 재생했다. 현행 중앙 -85.4bp/평균 -77.3bp/승률 19%,
        # "익절 +100bp · 손절 -100bp" 중앙 -7.6bp/평균 -13.8bp/승률 48%. ATR 손절은
        # 실측 R=1.5~4.5% 라 이 상한보다 훨씬 넓어, 이익 구간(MFE 중앙 +113bp)을
        # 잡지 못한 채 되돌림을 그대로 맞았다.
        #
        # **사이징은 건드리지 않는다** — target_weight 는 원래의 넓은 risk_pct 로
        # 계산된 값을 그대로 쓴다. 좁아진 손절로 사이징을 다시 하면 명목이
        # max_leverage 까지 부풀어 2배 이상 커진다(frgn_accumulate 사이징 사고와
        # 같은 부류). 사이징을 그대로 두면 1회 손절 손실이 리스크 예산보다
        # 작아지므로 보수적인 방향이다.
        self.stop_max_bps: float = params.get("stop_max_bps", 0)
        self.risk_budget_pct: float = params.get("risk_budget_pct", 1.0)
        self.max_leverage: float = params.get("max_leverage", 1.0)
        self.flatten_minutes: int = params.get("flatten_before_close_minutes", 1)

        # 세션 전체를 봐야 하므로 여유 있게 — 5분봉 기준 KR 정규장(390분/5=78봉) 커버
        self._lookback_bars = max(params.get("lookback_bars", 90), self.min_session_bars + 2)

        self._session_date: dict[str, dtdate] = {}
        self.max_entries_per_day: int = params.get("max_entries_per_day", 0)
        self.allow_add_while_holding: bool = params.get("allow_add_while_holding", True)
        self._entries_today: dict[str, int] = {}
        self._last_entry_bar: dict[str, object] = {}
        # 봉 슬롯 게이트 — 판정은 새 완성봉이 닫힌 직후 1회면 충분하다. 5초 사이클마다
        # 전 종목 history를 부르면 사이클이 2.5초까지 늘어지는 것을 실측(2026-08-10,
        # 16종목)했다 — 슬롯당 1회 판정으로 조회량을 봉 간격/폴링 주기 배수만큼 줄인다.
        self._evaluated_slot: dict[str, int] = {}
        self._pending: dict[str, dict] = {}
        self.last_reject: dict[str, str] = {}

        # 시장 리스크오프 게이트(모듈 docstring 절, quant/trade/indicators/
        # breadth.py) — scalp_1m의 trend_gate_mode와 동일 관례.
        mode = str(params.get("market_risk_gate_mode", "shadow")).strip().lower()
        if mode not in ("off", "shadow", "block"):
            mode = "shadow"
        self.market_risk_gate_mode: str = mode
        self.market_risk_max_drawdown_pct: float = params.get("market_risk_max_drawdown_pct", 0.5)
        if self.market_risk_max_drawdown_pct <= 0:
            raise ValueError("market_risk_max_drawdown_pct는 양수여야 합니다.")
        self._anchor_cache: dict[str, tuple[str, pd.DataFrame]] = {}
        self.market_risk_verdict: dict[str, bool] = {}

    @staticmethod
    def _market_of(symbol: str) -> str:
        """6자리 숫자 = KR — 저장소 전역과 동일한 구조 추론(orb_scan과 동일)."""
        return "KR" if (symbol.isdigit() and len(symbol) == 6) else "US"

    # ------------------------------------------------------------------ 사이클

    def _owns(self, pos) -> bool:
        """이 포지션을 내가 관리해야 하는가.

        2026-08-11 실운영 결함: orb_scan이 산 088350·229200을 intraday_scan이
        청산했다. 두 스캐너가 같은 관심종목 유니버스를 공유하는데 청산 루프가
        `ctx.broker.positions()` 전체를 돌면서 **누가 연 포지션인지 확인하지
        않았다.** 그 결과 원장의 매수/매도가 서로 다른 strategy_id에 기록돼
        라운드트립이 영원히 종결되지 않았고, 스코어보드에서 최대 수익(+73,172원)과
        최대 손실(-36,452원)이 통째로 누락됐다.

        태그가 없는 포지션(재시작으로 meta를 잃었거나 다른 전략이 연 것)은 내
        유니버스에 있을 때만 떠맡는다 — **아무도 청산하지 않는 상태가 이중 청산보다
        훨씬 위험하기 때문**이다(3배 ETF 오버나이트). 이중 청산은 브로커가
        `min(order.qty, pos.qty)`로 잘라내 실질 피해가 없다.

        2026-08-11 랏(lot) 도입: 여러 전략이 같은 심볼을 동시 보유할 수 있게 되며
        판정을 세 단계로 확장한다 — (1) 내 lot이 있으면 내 것, (2) lots 구조 자체가
        있는데 내 lot이 없으면 이미 다른 전략(들)이 추적 중이므로 입양하지 않는다
        (심볼이 내 유니버스에 있어도 마찬가지), (3) lots도 평평한 태그도 없으면
        (순수 레거시/미상) 기존처럼 유니버스 소속으로 입양한다.
        """
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

        # 1) 포지션 관리 — 유니버스에서 빠져도 열린 포지션 관리는 계속(orb_scan과 동일)
        for symbol, pos in positions.items():
            if not pos.is_open or not self._owns(pos):
                continue
            self._ensure_state(symbol, pos)
            signal = self._manage_position(symbol, pos, ctx)
            if signal is not None:
                signals.append(signal)

        # 2) 시장별 진입 창
        markets_present = sorted({self._market_of(s) for s in self.symbols})
        for market in markets_present:
            if not ctx.clock.is_market_open(market):
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
                # 미체결 pending 세션 롤 정리 — orb_scan과 같은 이유(낡은 stop이
                # 나중에 수동 포지션에 붙는 것 방지)
                self._pending = {
                    sym: pend for sym, pend in self._pending.items()
                    if self._market_of(sym) != market
                    or (positions.get(sym) is not None and positions.get(sym).is_open)
                }
                self._anchor_cache.pop(market, None)
            if ctx.clock.should_flatten(market, self.no_entry_minutes_before_close):
                continue  # 마감 임박 — 신규 진입 없음 (관리 신호는 위 1)에서 계속)
            minutes_since_open = (
                now_local.hour * 60 + now_local.minute
                - (session_open.hour * 60 + session_open.minute)
            )
            if minutes_since_open < self.entry_start_minutes:
                continue
            bar_slot = minutes_since_open // self.bar_interval_minutes

            # 시장 리스크오프 게이트 — 시장당 1회 판정(모듈 docstring 절 참고).
            market_blocked, market_note = self._market_risk_note(market, ctx)

            for symbol in self.symbols:
                if self._market_of(symbol) != market:
                    continue
                if self._evaluated_slot.get(symbol) == bar_slot:
                    continue  # 이 봉 슬롯은 이미 판정 — 새 봉이 닫힐 때까지 조회 자체를 생략
                if (
                    self.max_entries_per_day
                    and self._entries_today.get(symbol, 0) >= self.max_entries_per_day
                ):
                    continue
                pos = positions.get(symbol)
                if pos is not None and pos.is_open and not self.allow_add_while_holding:
                    continue
                if market_blocked:
                    self.last_reject[symbol] = (
                        f"시장 리스크오프 차단(앵커={ANCHOR_SYMBOLS.get(market)})"
                    )
                    continue
                # 체결 확인 기준은 **내 lot** 수량이다(합산 pos.qty가 아니다) — 다른
                # 전략이 같은 사이클에 이 심볼을 사고팔아도 내 체결 여부 판정이
                # 흔들리면 안 된다. 이 시점엔 위 1)단계(_ensure_state)가 이미 돌아
                # 내 lot이 최신 상태다.
                held_qty = pos.lot_qty(self.id) if pos is not None else 0.0
                signal = self._check_entry_for(symbol, market, ctx, held_qty,
                                               bar_slot=bar_slot,
                                               session_open_minutes=session_open.hour * 60 + session_open.minute,
                                               market_note=market_note)
                if signal is not None:
                    signals.append(signal)
        return signals

    # ------------------------------------------------------------ 시장 리스크오프

    def _get_anchor_bars(self, market: str, ctx: Context) -> pd.DataFrame:
        """분 경계 캐시 — news_momentum.py `_get_anchor_bars`와 동일 패턴."""
        anchor = ANCHOR_SYMBOLS.get(market)
        if anchor is None:
            return pd.DataFrame()
        tz, _ = _SESSION_OPEN[market]
        minute_key = ctx.clock.now().astimezone(tz).strftime("%Y-%m-%d %H:%M")
        cached = self._anchor_cache.get(market)
        if cached is not None and cached[0] == minute_key:
            return cached[1]
        bars = ctx.data.history(anchor, "1m", 400)
        self._anchor_cache[market] = (minute_key, bars)
        return bars

    def _market_risk_note(self, market: str, ctx: Context) -> tuple[bool, str]:
        """news_momentum.py `_market_risk_note`와 동일 판정."""
        if self.market_risk_gate_mode == "off":
            return False, ""
        bars = self._get_anchor_bars(market, ctx)
        dd = anchor_drawdown(bars)
        if dd is None:
            return False, ""
        risk_off = dd <= -self.market_risk_max_drawdown_pct
        self.market_risk_verdict[market] = risk_off
        if not risk_off:
            return False, ""
        note = f" [시장:리스크오프 {ANCHOR_SYMBOLS[market]} {dd:+.2f}%]"
        return (self.market_risk_gate_mode == "block"), note

    # ------------------------------------------------------------------ 진입

    def _check_entry_for(self, symbol: str, market: str, ctx: Context,
                         held_qty: float = 0.0, bar_slot: int | None = None,
                         session_open_minutes: int | None = None,
                         market_note: str = "") -> Signal | None:
        self.last_reject.pop(symbol, None)
        bars = ctx.data.history(symbol, self.interval, self._lookback_bars)
        if bars.empty:
            self.last_reject[symbol] = "봉 없음"
            return None

        tz, _ = _SESSION_OPEN[market]
        bar_dates = bars.index.tz_convert(tz).date
        today_bars = bars[bar_dates == self._session_date.get(market)]
        if len(today_bars) < self.min_session_bars + 1:
            self.last_reject[symbol] = (
                f"세션 봉 부족: {len(today_bars)} < {self.min_session_bars + 1}"
            )
            return None

        last_bar_ts = today_bars.index[-1]

        # 슬롯 소비는 **기대한 새 봉이 실제로 도착했을 때만** 한다. 시세 캐시(1분봉
        # 55초 TTL) 때문에 봉이 닫힌 직후엔 이전 봉까지만 보일 수 있다 — 그때 슬롯을
        # 소비하면 그 봉의 신호를 통째로 놓친다. 낡은 데이터면 소비 없이 다음
        # 사이클에 재시도한다(캐시가 갱신되면 판정).
        if bar_slot is not None and session_open_minutes is not None:
            ts_local = last_bar_ts.astimezone(tz)
            bar_minutes = ts_local.hour * 60 + ts_local.minute - session_open_minutes
            if bar_minutes // self.bar_interval_minutes < bar_slot - 1:
                self.last_reject[symbol] = "새 봉 데이터 대기 (캐시 갱신 전)"
                return None
            self._evaluated_slot[symbol] = bar_slot

        # 같은 완성봉으로 두 번 진입하지 않는다. 돌파 조건은 그 봉이 마지막인 동안
        # 계속 참이므로, 이 가드가 없으면 5초 사이클마다 진입 신호가 쏟아진다.
        if self._last_entry_bar.get(symbol) == last_bar_ts:
            self.last_reject[symbol] = "같은 봉으로 이미 진입 — 새 봉 대기"
            return None

        last = today_bars.iloc[-1]
        prior = today_bars.iloc[:-1]
        session_high = float(prior["high"].max())
        last_close = float(last["close"])
        if last_close <= session_high:
            self.last_reject[symbol] = (
                f"세션 신고가 미돌파: 종가 {last_close:.2f} <= 고가 {session_high:.2f}"
            )
            return None

        avg_vol = float(prior["volume"].mean())
        last_vol = float(last["volume"])
        if avg_vol <= 0 or last_vol < self.volume_mult * avg_vol:
            self.last_reject[symbol] = (
                f"거래량 미충족: {last_vol:,.0f} < {self.volume_mult}x{avg_vol:,.0f}"
            )
            return None

        if self.require_trend_filter:
            # SMA는 오늘 세션 봉만으로는 워밍업이 안 될 수 있어 세션 경계를 넘는
            # 전체 lookback 시계열(bars, today_bars 아님) 기준으로 계산한다.
            trend_sma = sma(bars["close"], self.trend_filter_period)
            trend_sma_last = trend_sma.iloc[-1]
            if pd.isna(trend_sma_last) or last_close <= trend_sma_last:
                self.last_reject[symbol] = (
                    f"추세 필터 미충족: 종가 {last_close:.2f} <= "
                    f"SMA{self.trend_filter_period} {trend_sma_last}"
                )
                return None

        risk_pct = self._risk_pct(symbol, ctx, reference_price=last_close)
        if risk_pct is None or risk_pct <= 0:
            self.last_reject.setdefault(symbol, "손절 거리 계산 불가")
            return None

        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            self.last_reject[symbol] = "현재가 없음"
            return None
        entry_price = quote.price
        stop = entry_price * (1 - risk_pct)
        stop = self._cap_stop(entry_price, stop)
        target = (
            entry_price + self.profit_target_r * (entry_price - stop)
            if self.profit_target_r
            else None
        )
        target_weight = min((self.risk_budget_pct / 100) / risk_pct, self.max_leverage)

        self._pending[symbol] = {
            "entry": entry_price, "stop": stop, "target": target,
            "session": self._session_date[market].isoformat(),
            "strategy": self.id,
            # 체결 확인용 — 이 수량보다 늘어야 실제로 체결된 것으로 본다(거부된
            # 추가 진입이 기존 포지션의 손절을 덮어쓰는 것을 막는다).
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
                f"세션 신고가 돌파 (종가 {last_close:.2f} > 고가 {session_high:.2f}, "
                f"거래량 {last_vol / avg_vol:.1f}x) → {symbol} 진입 "
                f"w={target_weight:.2f} R={risk_pct * 100:.2f}%{market_note}"
            ),
            stop=stop,
            target=target,
        )

    def _cap_stop(self, entry_price: float, stop: float) -> float:
        """손절가를 진입가 -stop_max_bps 보다 멀어지지 않게 끌어올린다.

        `max`인 이유: 손절이 **좁아진다 = 가격이 높아진다**(롱 기준). 상한이
        0(기본)이면 받은 값을 그대로 돌려준다 — 미설정 시 무동작 보장."""
        if not self.stop_max_bps:
            return stop
        return max(stop, entry_price * (1 - self.stop_max_bps / 1e4))

    def _risk_pct(self, symbol: str, ctx: Context, reference_price: float) -> float | None:
        """일봉 ATR 배수 손절 — orb_scan의 atr_pct 모드와 동일 산식."""
        if reference_price <= 0:
            return None
        bars = ctx.data.history(
            symbol, self.atr_interval, self.atr_period + _ATR_LOOKBACK_EXTRA
        )
        if len(bars) < self.atr_period + 1:
            self.last_reject[symbol] = f"{self.atr_interval} ATR 계산 불가(데이터 부족)"
            return None
        high, low, close = bars["high"], bars["low"], bars["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        atr = float(tr.dropna().tail(self.atr_period).mean())
        if atr <= 0:
            return None
        risk = self.atr_stop_mult * atr
        return risk / reference_price if risk > 0 else None

    # ------------------------------------------------------------------ 관리 (orb_scan 계승)

    def _ensure_state(self, symbol: str, pos: Position) -> None:
        lot = pos.ensure_lot(self.id)
        pending = self._pending.get(symbol)
        if pending is not None and lot.get("qty", 0.0) > pending.get("qty_at_signal", 0.0):
            # 신호가 실제로 체결돼 수량이 늘었다 — 최신 신호 기준으로 갱신한다.
            self._pending.pop(symbol, None)
            fresh = {k: v for k, v in pending.items() if k != "qty_at_signal"}
            if pending.get("qty_at_signal", 0.0) > 0:
                # 추가 진입 — 진짜 진입가는 내 lot의 블렌딩된 평균단가다(심볼 합산
                # avg_cost가 아니다).
                fresh["entry"] = lot.get("avg_cost", pos.avg_cost)
                old_stop = lot.get("stop")
                if old_stop is not None and fresh.get("stop") is not None:
                    # 추가 진입이 기존 보유분의 손절을 **넓히지는** 않는다.
                    fresh["stop"] = max(old_stop, fresh["stop"])
            lot.update(fresh)
            return
        if "entry" in lot:
            return
        entry = lot.get("avg_cost", pos.avg_cost)
        stop = entry * 0.98  # 재시작 복구 — 보수적 고정 2% 폴백(orb_scan과 동일)
        # 재시작 경로에도 같은 상한을 건다 — 안 걸면 재시작만으로 손절이 200bp 로
        # 넓어져 정상 진입분(상한 적용)과 다른 규칙으로 관리된다.
        stop = self._cap_stop(entry, stop)
        target = entry + self.profit_target_r * (entry - stop) if self.profit_target_r else None
        lot.update(entry=entry, stop=stop, target=target)

    def _manage_position(self, symbol: str, pos: Position, ctx: Context) -> Signal | None:
        quote = ctx.data.quote(symbol)
        if quote is None:
            return None
        price = quote.price
        lot = pos.ensure_lot(self.id)
        entry, stop, target = lot["entry"], lot["stop"], lot.get("target")

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
        if target is not None and price >= target:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"목표가 도달: entry={entry:.2f} target={target:.2f} 현재={price:.2f}",
            )
        return None
