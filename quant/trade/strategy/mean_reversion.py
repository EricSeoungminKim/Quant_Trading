"""평균회귀 반등 전략 — 과매도 구간에서 롱 진입, 오버나이트 보유 허용. 롱 온리.

## 근거와 정직성

NYU Stern 백테스트 서베이(2025)에서 평균회귀 계열(PAMR/CWMR/OLMAR)이 리스크
조정 수익 상위권으로 보고됐다. 기존 3전략(donchian/orb_scan/intraday_scan)이
전부 돌파·모멘텀 계열이라 이 저장소 최초의 평균회귀 축이자, **최초로 오버나이트
보유를 허용하는 전략**이다 — 다른 전략은 전부 EoD 강제 청산인데, 이 전략은
반등을 잡기 위해 갭 리스크를 그대로 감수한다. **백테스트로 검증된 적 없다
[미검증]** — paper 번인(스코어보드의 거래당 bps)이 쌓이기 전까지 수익성 주장은
하지 않는다.

## 레버리지 상품 금지

**3배 ETF에 평균회귀는 쓰지 않는다.** 레버리지 상품은 복리 붕괴(volatility
decay)가 평균회귀 로직과 정면으로 역행한다 — 되돌림을 기다리는 동안 매일의
리밸런싱 손실이 누적돼 "가격은 되돌아왔는데 레버리지 ETF는 안 돌아온" 상태가
된다. 비레버리지 지수 ETF/대형주 전용. `leverage_of`(생성자 인자)가 주입되면 진입
판정에서 |배수|>1인 심볼을 차단한다(배수 미상도 보수적으로 차단) — 주입되지
않으면(기본값 None, 테스트/백테스트) 이 게이트는 꺼진 채 기존 동작 그대로다.

## 규칙

판정은 일봉(`ctx.data.history(symbol, "1d", n)`) 기준, **세션당 1회만**
(intraday_scan의 슬롯 게이트와 같은 이유 — 일봉 전략이 5초 사이클마다 API를
때리면 안 된다). 시장별 세션 날짜가 바뀐 뒤 첫 판정에서:

- 진입(아래 세 조건 모두 만족 시 시장가): 직전 완성 일봉 기준
  (a) 종가의 `zscore_window`일 z-score < `entry_z`(기본 -2.0),
  (b) RSI(`rsi_period`, 기본 2) < `entry_rsi`(기본 10),
  (c) `turnover_window`일 평균 거래대금 >= `min_turnover`.
- 손절: 진입가 - `atr_stop_mult`(기본 2.0) x 일봉 ATR(`atr_period`, 기본 14).
  일봉 판정과 별개로 실시간 시세로 매 사이클 감시한다.
- 청산: (a) 손절, (b) z-score가 `exit_z`(기본 -0.5) 위로 복귀, (c) 보유
  `max_hold_sessions`(기본 5) 세션 초과 — 시간 손절.

## 시장 추론

심볼은 config가 고정하지만(오늘 기준 KR ETF 2종), 세션 날짜·개장 판정은
`market_of_symbol()`(domain.models)로 심볼마다 추론한다 — 생성자의 `market`
인자는 Strategy Protocol 호환용일 뿐 로직에 쓰지 않는다. 매핑을 생성자 인자
하나로 고정하면 설정에 없는 심볼이 "US" 기본값으로 떨어져 세션 판정이 통째로
어긋난다(models.py의 058610 사고 참고) — 그래서 매 사이클 직접 추론한다.

Position.meta에 `entry`/`stop`/`strategy`/`entered_session`/`sessions_held`을
저장한다(다른 전략과 같은 규약). `"strategy": self.id`는 반드시 기록 —
소유권 판정 `_owns`가 이걸 읽는다(orb_scan.py의 것과 동일 구현).
"""
from __future__ import annotations

from datetime import date as dtdate

import pandas as pd

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction, market_of_symbol
from quant.trade.strategy.orb_scan import _SESSION_OPEN

# 재시작 복구용 보수적 손절 폴백 — orb_scan/intraday_scan과 동일한 관례(2%).
_FALLBACK_STOP_PCT = 0.02


def _zscore(closes: pd.Series, window: int) -> float:
    tail = closes.tail(window)
    mean = tail.mean()
    std = tail.std()
    if not std or pd.isna(std):
        return 0.0
    return float((closes.iloc[-1] - mean) / std)


def _rsi(closes: pd.Series, period: int) -> float:
    """고전적 RSI. period가 작아(기본 2) Wilder 평활 대신 단순 이동평균을 쓴다."""
    delta = closes.diff()
    gain = delta.clip(lower=0.0).tail(period).mean()
    loss = (-delta.clip(upper=0.0)).tail(period).mean()
    if not loss:
        return 100.0 if gain else 50.0
    rs = gain / loss
    return float(100 - (100 / (1 + rs)))


def _atr(bars: pd.DataFrame, period: int) -> float:
    high, low, close = bars["high"], bars["low"], bars["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return float(tr.dropna().tail(period).mean())


def _turnover(bars: pd.DataFrame, window: int) -> float:
    return float((bars["close"] * bars["volume"]).tail(window).mean())


class MeanReversionStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "mean_reversion",
                 leverage_of: dict[str, float] | None = None):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론
        # {symbol: 레버리지 배수}. None(기본값)이면 이 게이트 자체가 꺼진 것으로
        # 취급한다(테스트/백테스트가 기존과 동일하게 동작해야 한다). 주입되면
        # (assembly.py가 부팅 시 stock_info로 채운다) 진입 판정에서 레버리지
        # 상품을 막는다 — dict에 없는 심볼(레버리지 미상)은 **차단**한다: 오버나이트
        # 보유(최대 5세션) + 레버리지의 변동성 감쇠는 최악의 조합이고, 모르는
        # 것을 비레버리지로 가정하는 비용은 이 저장소가 반복해서 겪은 실패 패턴이다
        # — 반면 차단의 비용은 기회손실뿐이다.
        self.leverage_of = leverage_of
        self.last_reject: dict[str, str] = {}

        self.entry_z: float = params.get("entry_z", -2.0)
        self.entry_rsi: float = params.get("entry_rsi", 10)
        self.min_turnover: float = params.get("min_turnover", 0)
        self.atr_stop_mult: float = params.get("atr_stop_mult", 2.0)
        self.exit_z: float = params.get("exit_z", -0.5)
        self.max_hold_sessions: int = params.get("max_hold_sessions", 5)
        self.zscore_window: int = params.get("zscore_window", 20)
        self.rsi_period: int = params.get("rsi_period", 2)
        self.atr_period: int = params.get("atr_period", 14)
        self.turnover_window: int = params.get("turnover_window", 20)

        self._lookback_days = max(
            self.zscore_window, self.turnover_window, self.rsi_period + 1, self.atr_period + 1
        ) + 1

        self._session_date: dict[str, dtdate] = {}
        self._pending: dict[str, dict] = {}

    def _owns(self, pos: Position) -> bool:
        """orb_scan.py의 _owns와 동일 구현(2026-08-11 랏 도입 포함) — 소유권 없는
        포지션을 잘못 청산하거나 다른 전략이 이미 추적 중인 lot을 입양하지 않는다."""
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
        stopped_out: set[str] = set()

        # 1) 손절 감시 — 실시간 시세로 매 사이클(일봉 판정과 별개)
        for symbol, pos in positions.items():
            if not pos.is_open or not self._owns(pos):
                continue
            self._ensure_state(symbol, pos)
            quote = ctx.data.quote(symbol)
            if quote is None:
                continue
            lot = pos.ensure_lot(self.id)
            if quote.price <= lot["stop"]:
                signals.append(Signal(
                    strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0,
                    reason=f"손절: entry={lot['entry']:.2f} stop={lot['stop']:.2f} 현재={quote.price:.2f}",
                ))
                stopped_out.add(symbol)

        # 2) 세션당 1회 일봉 판정 — 시장별 세션 날짜가 바뀐 뒤 첫 사이클에서만
        markets_present = sorted({market_of_symbol(s) for s in self.symbols})
        for market in markets_present:
            if not ctx.clock.is_market_open(market):
                continue
            tz, _ = _SESSION_OPEN[market]
            today = ctx.clock.now().astimezone(tz).date()
            if today == self._session_date.get(market):
                continue  # 이미 이번 세션 판정 완료 — history 재조회 없이 건너뛴다
            self._session_date[market] = today

            for symbol in self.symbols:
                if market_of_symbol(symbol) != market or symbol in stopped_out:
                    continue
                pos = positions.get(symbol)
                held = pos is not None and pos.is_open and self._owns(pos)
                signal = self._evaluate(symbol, ctx, held, pos, today)
                if signal is not None:
                    signals.append(signal)

        return signals

    def _evaluate(self, symbol: str, ctx: Context, held: bool, pos: Position | None,
                  today: dtdate) -> Signal | None:
        bars = ctx.data.history(symbol, "1d", self._lookback_days)
        if len(bars) < self._lookback_days:
            return None
        closes = bars["close"]
        z = _zscore(closes, self.zscore_window)

        if held:
            lot = pos.ensure_lot(self.id)
            lot["sessions_held"] = lot.get("sessions_held", 0) + 1
            if z >= self.exit_z:
                return Signal(
                    strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0,
                    reason=f"평균회귀 복귀: z={z:.2f} >= {self.exit_z:g}",
                )
            if lot["sessions_held"] >= self.max_hold_sessions:
                return Signal(
                    strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0,
                    reason=f"시간 손절: {lot['sessions_held']}세션 보유 >= {self.max_hold_sessions}",
                )
            return None

        # 미보유 — 진입 판정
        if self.leverage_of is not None:
            lev = self.leverage_of.get(symbol)
            if lev is None:
                self.last_reject[symbol] = "레버리지 배수 미상 — 오버나이트 전략은 보수적으로 진입 차단"
                return None
            if abs(lev) > 1.0:
                self.last_reject[symbol] = f"레버리지 상품(배수={lev:g}) — mean_reversion은 오버나이트 금지"
                return None
        if z >= self.entry_z:
            return None
        rsi = _rsi(closes, self.rsi_period)
        if rsi >= self.entry_rsi:
            return None
        turnover = _turnover(bars, self.turnover_window)
        if turnover < self.min_turnover:
            return None
        atr = _atr(bars, self.atr_period)
        if atr <= 0:
            return None
        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            return None

        entry_price = quote.price
        stop = entry_price - self.atr_stop_mult * atr
        n_symbols = len(self.symbols) or 1
        target_weight = 1.0 / n_symbols

        self._pending[symbol] = {
            "entry": entry_price, "stop": stop, "strategy": self.id,
            "entered_session": today.isoformat(), "sessions_held": 0,
        }
        return Signal(
            strategy_id=self.id, symbol=symbol, action=SignalAction.ENTER_LONG,
            target_weight=target_weight,
            reason=(
                f"과매도 반등: z={z:.2f} < {self.entry_z:g}, RSI({self.rsi_period})={rsi:.1f} "
                f"< {self.entry_rsi:g}, 거래대금 {turnover:,.0f} >= {self.min_turnover:,.0f}"
            ),
            stop=stop,
        )

    def _ensure_state(self, symbol: str, pos: Position) -> None:
        lot = pos.ensure_lot(self.id)
        if "entry" in lot:
            return
        pending = self._pending.pop(symbol, None)
        if pending is not None:
            lot.update(pending)
            return
        # 재시작 등으로 pending 정보가 없음 — 보수적 폴백(orb_scan과 동일 관례).
        entry = lot.get("avg_cost", pos.avg_cost)
        stop = entry * (1 - _FALLBACK_STOP_PCT)
        lot.update(entry=entry, stop=stop, strategy=self.id,
                   entered_session=None, sessions_held=0)
