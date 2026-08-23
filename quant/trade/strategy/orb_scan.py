"""ORB 스캐너 — 관심종목 유니버스를 소비하는 다종목·롱 온리 변형.

## 계보와 차이

`orb.py`(Zarattini & Aziz, SSRN 4416622)의 규칙을 그대로 쓰되, TQQQ/SQQQ
고정 쌍 대신 **임의의 종목 목록**을 종목별로 독립 판정한다:

| | orb.py (원본) | orb_scan (이 파일) |
|---|---|---|
| 대상 | TQQQ/SQQQ 고정 쌍 | 관심종목 유니버스 (매 세션 갱신) |
| 음봉 처리 | 인버스(SQQQ) 매수로 숏 표현 | **스킵** — 임의 종목엔 인버스 쌍이 없다 |
| 동시 포지션 | 한 다리만 | 종목별 독립 (상한은 risk의 max_concurrent_positions) |
| 시장 | US | **종목별 시장 인식** — 6자리 숫자=KR, 그 외 US. 혼합 유니버스를 한 인스턴스가 처리한다 |

규칙 자체(방향=첫 봉 부호, 진입=두 번째 봉 시가, 도지 임계, 손절=첫 봉 저가
또는 일봉 ATR 배수, EoD 청산, 세션당 종목별 1회)는 orb.py와 동일하다 — 근거와
검증 이력은 그쪽 docstring이 원본이다.

## 정직성 경고

- **이 규칙이 임의 종목에서 통한다는 증거는 없다.** 우리가 재현·측정한 것은 전부
  TQQQ(와 논문의 QQQ)다. 논문 [B](SSRN 4729284)의 종목 횡단 결과는 "RelVol 상위
  선택"이 엣지의 원천이라고 말한다 — 사용자 관심종목이 그 선택 기준을 만족한다는
  보장은 없다. 이 전략의 1차 용도는 **관심종목 파이프라인(추적→시그널→주문)의
  운반체**이고, 수익성 주장은 하지 않는다. 그래서 기본 enabled: false다.
- 롱 온리 편향: 음봉 날은 전부 스킵하므로 원본 orb보다 거래가 절반쯤 준다.
  하락장에서는 거의 놀게 된다 — 국면(regime) 배수와 방향이 겹치는 부분이 있다.

## 유니버스 연동

`symbols`는 조립 시점의 스냅샷이다. 관심종목이 바뀌면 세션 롤에서 전략이
재조립된다(assembly의 유니버스 재해석 참고). **유니버스에서 빠진 종목의 열린
포지션도 이 인스턴스가 남아 있는 한 계속 관리된다** — 재조립 시 새 symbols에
없는 종목이라도 `on_cycle`의 포지션 관리 루프는 `broker.positions()` 기준으로
돌기 때문이다. 유니버스는 신규 진입 대상 선정이지 청산 대상 선정이 아니다.
"""
from __future__ import annotations

from datetime import date as dtdate, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction

_SESSION_OPEN: dict[str, tuple[ZoneInfo, dtime]] = {
    "US": (ZoneInfo("America/New_York"), dtime(9, 30)),
    "KR": (ZoneInfo("Asia/Seoul"), dtime(9, 0)),
}

# 일봉 ATR 조회 여유분 — orb.py와 동일.
_ATR_LOOKBACK_EXTRA = 6


class OrbScanStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "orb_scan"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market

        self.bar_interval_minutes: int = params.get("bar_interval_minutes", 5)
        self.opening_range_minutes: int = params.get("opening_range_minutes", 5)
        if self.bar_interval_minutes <= 0 or self.opening_range_minutes <= 0:
            raise ValueError("opening_range_minutes와 bar_interval_minutes는 양수여야 합니다.")
        if self.opening_range_minutes % self.bar_interval_minutes != 0:
            raise ValueError(
                f"opening_range_minutes({self.opening_range_minutes})는 "
                f"bar_interval_minutes({self.bar_interval_minutes})의 배수여야 합니다."
            )
        self.interval = f"{self.bar_interval_minutes}m"
        self.or_bar_count = self.opening_range_minutes // self.bar_interval_minutes

        # 도지 임계 기본 0.50% — TQQQ 전구간 측정의 in-sample 최적(고원 0.5~0.8%,
        # settings의 orb 블록 주석 참고). 임의 종목에서 이 값이 맞다는 근거는 없다 —
        # 변동성이 다른 종목에는 ATR 대비 상대 임계가 맞을 수 있다. [미검증]
        self.doji_threshold_pct: float = params.get("doji_threshold_pct", 0.50)

        self.stop_mode: str = params.get("stop_mode", "atr_pct")
        if self.stop_mode not in ("or_extreme", "atr_pct"):
            raise ValueError(f"알 수 없는 stop_mode: {self.stop_mode!r} (or_extreme | atr_pct)")
        self.atr_period: int = params.get("atr_period", 14)
        self.atr_stop_mult: float = params.get("atr_stop_mult", 0.35)
        self.atr_interval: str = params.get("atr_interval", "1d")

        self.profit_target_r: float | None = params.get("profit_target_r")
        # 손절 거리 상한(bp). 0 = 비활성(기본). 근거·주의사항은 intraday_scan 의
        # 같은 파라미터 주석 참고 — 두 전략은 손절/목표가 산식이 같아 같은 규칙을
        # 쓴다. **사이징(target_weight)은 원래의 risk_pct 기준 그대로 둔다.**
        self.stop_max_bps: float = params.get("stop_max_bps", 0)
        self.risk_budget_pct: float = params.get("risk_budget_pct", 1.0)
        self.max_leverage: float = params.get("max_leverage", 1.0)
        self.flatten_minutes: int = params.get("flatten_before_close_minutes", 1)

        self._lookback_bars = max(self.or_bar_count * 4, 8)
        # 시장별 세션 상태. 관심종목은 KR(6자리)과 US가 섞여 들어오는데, 세션
        # 날짜·진입 창·마감 판정이 시장마다 다르다 — 단일 market 필드로 처리하면
        # 한쪽 시장 종목은 진입 창이 영원히 안 열려 조용히 죽는다(발견 당시:
        # 혼합 유니버스에서 첫 심볼의 시장이 전체를 지배). 생성자의 market 인자는
        # Strategy Protocol 호환용으로만 남긴다.
        self._session_date: dict[str, dtdate] = {}
        # 하루 진입 횟수 상한(0 = 무제한). 2026-08-10 사용자 결정: 신호가 나면
        # 진입한다 — '이미 보유/이미 진입'을 이유로 유효 신호를 막지 않는다.
        # 폭주는 봉 단위 가드(_last_entry_bar)와 리스크 레일이 막는다.
        self.max_entries_per_day: int = params.get("max_entries_per_day", 0)
        self.allow_add_while_holding: bool = params.get("allow_add_while_holding", True)
        self._entries_today: dict[str, int] = {}
        # 같은 완성봉으로 두 번 진입하지 않는다 — 조건은 그 봉이 유지되는 동안
        # 계속 참이라, 이 가드가 없으면 5초마다 진입 신호가 쏟아진다.
        self._last_entry_bar: dict[str, object] = {}
        self._pending: dict[str, dict] = {}
        self.last_reject: dict[str, str] = {}

    @staticmethod
    def _market_of(symbol: str) -> str:
        """6자리 숫자 = KR, 그 외 = US — 저장소 전역과 동일한 구조 추론
        (docs/api/toss/QUICKREF, brokers/toss/broker.py, assembly의 시장 매핑)."""
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
        (심볼이 내 유니버스에 있어도 마찬가지 — lots가 있다는 것 자체가 "추적되고
        있다"는 뜻이라 orphan이 아니다), (3) lots도 평평한 태그도 없으면(순수
        레거시/미상) 기존처럼 유니버스 소속으로 입양한다.
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

        # 1) 포지션 관리 — 유니버스와 무관하게 **열려 있는 자기 포지션**을 본다.
        #    (self.symbols가 아니라 broker.positions() 기준 — 관심종목에서 빠져도
        #    청산 관리가 끊기면 안 된다. 모듈 docstring 참고.)
        for symbol, pos in positions.items():
            if not pos.is_open or not self._owns(pos):
                continue
            self._ensure_state(symbol, pos)
            signal = self._manage_position(symbol, pos, ctx)
            if signal is not None:
                signals.append(signal)

        # 2) 시장별 진입 창 판정. 관심종목에 KR/US가 섞이므로 창은 시장 단위로
        #    계산하고, 열린 창의 시장에 속한 종목만 판정한다. 창 밖 시장은 종목 수와
        #    무관하게 즉시 건너뛴다(orb.py의 최적화 패턴 — 조회 0회).
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
                # 세션 롤은 시장별이다 — KR이 새 날이 됐다고 미국 종목의 "오늘
                # 진입함" 기록을 지우면 US 장중에 재진입이 열린다.
                self._entries_today = {
                    sym: n for sym, n in self._entries_today.items()
                    if self._market_of(sym) != market
                }
                self._last_entry_bar = {
                    sym: bar for sym, bar in self._last_entry_bar.items()
                    if self._market_of(sym) != market
                }
                # _pending은 신호 발동 시점에 기록한 entry/stop/target — 체결 확인 후
                # _ensure_state가 소비해 pos.meta로 옮긴다. 이 소비는 위 1)단계에서
                # **이번 사이클 안에** 이미 끝났으므로, 지금 이 시점에 열린 포지션이
                # 없는 pending은 그 진입이 끝내 체결되지 않았다는 뜻이다(노출 상한 등에
                # 막힘) — 세션을 넘기면 몇 주 뒤 사용자가 같은 종목을 손으로 매수했을 때
                # 이 낡은 stop/target이 그 포지션에 붙어 엉뚱한 EXIT_LONG을 낸다.
                self._pending = {
                    sym: pend for sym, pend in self._pending.items()
                    if self._market_of(sym) != market
                    or (positions.get(sym) is not None and positions.get(sym).is_open)
                }
            minutes_since_open = (
                now_local.hour * 60 + now_local.minute
                - (session_open.hour * 60 + session_open.minute)
            )
            elapsed_past_or = minutes_since_open - self.opening_range_minutes
            if not 0 <= elapsed_past_or < self.bar_interval_minutes:
                continue

            # 3) 이 시장 종목들만 독립 판정
            for symbol in self.symbols:
                if self._market_of(symbol) != market:
                    continue
                if (
                    self.max_entries_per_day
                    and self._entries_today.get(symbol, 0) >= self.max_entries_per_day
                ):
                    continue
                pos = positions.get(symbol)
                if pos is not None and pos.is_open and not self.allow_add_while_holding:
                    continue
                # 체결 확인 기준은 **내 lot** 수량이다(합산 pos.qty가 아니다) — 다른
                # 전략이 같은 사이클에 이 심볼을 사고팔아도 내 체결 여부 판정이
                # 흔들리면 안 된다. 이 시점엔 위 1)단계(_ensure_state)가 이미 돌아
                # 내 lot이 최신 상태다.
                held_qty = pos.lot_qty(self.id) if pos is not None else 0.0
                signal = self._check_entry_for(symbol, market, ctx, held_qty)
                if signal is not None:
                    signals.append(signal)
        return signals

    # ------------------------------------------------------------------ 진입

    def _check_entry_for(self, symbol: str, market: str, ctx: Context,
                         held_qty: float = 0.0) -> Signal | None:
        self.last_reject.pop(symbol, None)
        bars = ctx.data.history(symbol, self.interval, self._lookback_bars)
        if bars.empty:
            self.last_reject[symbol] = "봉 없음"
            return None

        tz, _ = _SESSION_OPEN[market]
        bar_dates = bars.index.tz_convert(tz).date
        today_bars = bars[bar_dates == self._session_date.get(market)]
        if len(today_bars) != self.or_bar_count:
            self.last_reject[symbol] = (
                f"OR 봉 수 불일치: {len(today_bars)} != {self.or_bar_count}"
            )
            return None

        # 같은 OR 봉 집합으로 두 번 진입하지 않는다 — 진입 창이 하루 1회라 사실상
        # 발동하지 않지만, intraday_scan과 같은 레일을 두어 규칙을 하나로 유지한다.
        last_bar_ts = today_bars.index[-1]
        if self._last_entry_bar.get(symbol) == last_bar_ts:
            self.last_reject[symbol] = "같은 봉으로 이미 진입 — 새 봉 대기"
            return None

        or_open = float(today_bars["open"].iloc[0])
        or_close = float(today_bars["close"].iloc[-1])
        or_low = float(today_bars["low"].min())
        if or_open <= 0:
            self.last_reject[symbol] = "OR 시가 이상"
            return None

        move_pct = (or_close - or_open) / or_open * 100
        if move_pct <= self.doji_threshold_pct:
            # 도지 **또는 음봉** — 롱 온리라 음봉은 진입 대상이 아니다(원본 orb는
            # 인버스 매수로 숏을 표현하지만 임의 종목엔 인버스 쌍이 없다).
            self.last_reject[symbol] = f"양봉 아님/도지 (OR 변동 {move_pct:+.3f}%)"
            return None

        risk_pct = self._risk_pct(symbol, ctx, reference_price=or_close, or_low=or_low)
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
                f"OR 양봉 ({move_pct:+.2f}%) → {symbol} 진입 "
                f"w={target_weight:.2f} R={risk_pct*100:.2f}%"
            ),
            stop=stop,
            target=target,
        )

    def _cap_stop(self, entry_price: float, stop: float) -> float:
        """손절가를 진입가 -stop_max_bps 보다 멀어지지 않게 끌어올린다(롱 기준이라
        좁아진다 = 가격이 높아진다). 상한 0(기본)이면 무동작."""
        if not self.stop_max_bps:
            return stop
        return max(stop, entry_price * (1 - self.stop_max_bps / 1e4))

    def _risk_pct(self, symbol: str, ctx: Context, reference_price: float,
                  or_low: float) -> float | None:
        if reference_price <= 0:
            return None
        if self.stop_mode == "or_extreme":
            risk = reference_price - or_low
        else:  # atr_pct — 일봉 ATR 배수 (조용히 분봉으로 대체하지 않는다, orb.py와 동일)
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

    # ------------------------------------------------------------------ 관리

    def _ensure_state(self, symbol: str, pos: Position) -> None:
        lot = pos.ensure_lot(self.id)
        pending = self._pending.get(symbol)
        if pending is not None and lot.get("qty", 0.0) > pending.get("qty_at_signal", 0.0):
            # 신호가 실제로 체결돼 수량이 늘었다 — 최신 신호 기준으로 갱신한다.
            self._pending.pop(symbol, None)
            fresh = {k: v for k, v in pending.items() if k != "qty_at_signal"}
            if pending.get("qty_at_signal", 0.0) > 0:
                # 추가 진입 — 진짜 진입가는 내 lot의 블렌딩된 평균단가다(심볼 합산
                # avg_cost가 아니다 — 다른 전략의 lot이 섞이면 안 된다).
                fresh["entry"] = lot.get("avg_cost", pos.avg_cost)
                old_stop = lot.get("stop")
                if old_stop is not None and fresh.get("stop") is not None:
                    # 추가 진입이 기존 보유분의 손절을 **넓히지는** 않는다.
                    fresh["stop"] = max(old_stop, fresh["stop"])
            lot.update(fresh)
            return
        if "entry" in lot:
            return
        # 재시작 복구 — OR 컨텍스트를 잃었다. 보수적 고정 2% 손절 폴백(안전망).
        entry = lot.get("avg_cost", pos.avg_cost)
        stop = entry * 0.98
        # 재시작 경로에도 같은 상한을 건다 — 안 걸면 재시작만으로 손절이 200bp 로
        # 넓어져 정상 진입분과 다른 규칙으로 관리된다(intraday_scan 과 동일).
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

        # 오버나잇 금지 — should_flatten 하나에 기대지 않는다(orb.py의 세션 롤
        # 레일과 동일. 그 레일이 없어 포지션이 며칠씩 살아남은 실측 이력이 있다).
        # 시장은 종목에서 추론한다 — KR 포지션의 세션 날짜를 미국 시간으로 재면
        # KST 새벽(미국 장중)에 "어제 진입"으로 오판해 조기 청산한다.
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
