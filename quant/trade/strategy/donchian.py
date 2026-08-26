"""Donchian 15분 채널 브레이크아웃 전략. stock-algo-trade의 danta.py 로직 이식. 롱 온리.

진입: 완성된 interval봉의 종가가 직전 lookback_bars봉 고가의 최댓값을 돌파하고,
거래량이 lookback 평균의 volume_mult배를 넘을 때. stop = max(해당 봉 저가,
entry*(1-stop_fallback_pct%)) — 둘 중 손실이 더 작은 쪽. 단 stop_min_bps가 설정되면
그만큼은 반드시 벌린다(왕복 거래비용보다 좁은 손절 방지). target = entry + risk_reward*(entry-stop).
청산 우선순위: 마감 전 flatten -> stop -> target -> position_mgmt(scale_out/scale_in).

포지션당 상태(entry/stop/target/adds_done/scaled_out)는 Position.meta에 저장한다 —
broker.positions()가 돌려주는 실제 객체를 그대로 mutate하므로 Portfolio 영속화에 얹혀
재시작에도 살아남는다. 신호 발행 시점(체결 전)의 상태는 self._pending에 잠시 보관했다가,
다음 사이클에 포지션이 실제로 열린 것을 확인하면 Position.meta로 옮긴다.

scale_out/scale_in도 같은 원칙을 따른다: Signal 생성 시점에는 meta를 직접 건드리지
않고 Signal.state_update에 변경분만 담아 반환한다 — risk.approve()가 거부하거나
broker.place_order()가 체결에 실패하면 아무 것도 적용되지 않는다. run_cycle이 실제
Fill을 받은 뒤에만 이 state_update를 살아있는 Position.meta에 적용한다.

allow_same_day_reentry=False면 종목별 마지막 진입 거래일(_last_entry_date)을 추적해
같은 거래일 내 재진입(ENTER_LONG)을 차단한다 — A/B 테스트 대상 파라미터.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.strategy.shell import PureStrategyShell

# min_session_bars_before_entry 게이팅에만 쓰는 세션 시가 — Clock은 is_market_open/
# minutes_to_close만 제공하므로, "세션 시작 후 N봉"은 전략이 직접 판단해야 한다.
_SESSION_OPEN: dict[str, tuple[ZoneInfo, dtime]] = {
    "US": (ZoneInfo("America/New_York"), dtime(9, 30)),
    "KR": (ZoneInfo("Asia/Seoul"), dtime(9, 0)),
}


class DonchianStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "donchian"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market

        self.interval_minutes: int = params["interval_minutes"]
        self.interval = f"{self.interval_minutes}m"
        self.lookback_bars: int = params["lookback_bars"]
        self.volume_mult: float = params["volume_mult"]
        self.stop_fallback_pct: float = params["stop_fallback_pct"]
        self.risk_reward: float = params["risk_reward"]
        self.max_concurrent_names: int = params["max_concurrent_names"]
        self.flatten_minutes: int = params["flatten_before_close_minutes"]
        self.min_session_bars: int = params.get("min_session_bars_before_entry", 0)
        # 손절폭 하한(bp). 돌파봉 저가가 종가에 붙으면 손절폭이 왕복 거래비용보다
        # 좁아져, 손절될 때마다 설계한 리스크의 몇 배를 실현한다. r0도 0에 수렴해
        # 목표가가 진입가에 붙고 포지션 관리 산식이 무의미해진다. 0 = 비활성.
        self.stop_min_bps: float = params.get("stop_min_bps", 0)
        self.pm: dict | None = params.get("position_mgmt")
        self.allow_same_day_reentry: bool = params.get("allow_same_day_reentry", True)

        self._last_bar_ts: dict[str, object] = {}
        self._pending: dict[str, dict] = {}  # symbol -> 체결 확인 대기 중인 진입 상태
        self._last_entry_date: dict[str, object] = {}  # symbol -> 마지막 진입 거래일 (재진입 차단용)

    def on_cycle(self, ctx: Context) -> list[Signal]:
        signals: list[Signal] = []
        positions = ctx.broker.positions()
        open_count = sum(1 for s in self.symbols if (p := positions.get(s)) is not None and p.is_open)

        for symbol in self.symbols:
            pos = positions.get(symbol)
            if pos is not None and pos.is_open:
                self._ensure_state(symbol, pos)
                signal = self._manage_position(symbol, pos, ctx)
                if signal is not None:
                    signals.append(signal)
                continue

            if not ctx.clock.is_market_open(self.market):
                continue
            if ctx.clock.should_flatten(self.market, self.flatten_minutes):
                continue  # 곧 청산할 시각에 새로 들어가지 않는다
            if open_count >= self.max_concurrent_names:
                continue

            signal = self._check_entry(symbol, ctx)
            if signal is not None:
                signals.append(signal)
                open_count += 1

        return signals

    def _ensure_state(self, symbol: str, pos: Position) -> None:
        """포지션이 열려 있는데 내 lot에 아직 entry가 없으면(체결 직후, 혹은 재시작
        복구) 채워 넣는다. donchian은 다른 전략과 달리 meta에 "strategy" 태그를
        남기지 않지만(TQQQ/SQQQ 고정 쌍이라 원래 소유권 모호성이 없었다), 관심종목에
        같은 심볼이 편입되면 다른 전략도 이 심볼에 lot을 가질 수 있다 — 그래서
        donchian도 자기 몫을 `Position.ensure_lot(self.id)`의 lot에 저장한다
        (2026-08-11 사용자 지시: 전략별 랏 규율. `Position.ensure_lot`이 평평한
        레거시 meta를 첫 접근 시 이행한다)."""
        lot = pos.ensure_lot(self.id)
        if "entry" in lot:
            return
        pending = self._pending.pop(symbol, None)
        if pending is not None:
            lot.update(pending)
            return
        # 재시작 등으로 pending 정보가 없음 — lot avg_cost(없으면 심볼 합산
        # avg_cost) 기반 보수적 복구.
        entry = lot.get("avg_cost", pos.avg_cost)
        stop = entry * (1 - self.stop_fallback_pct / 100)
        target = entry + self.risk_reward * (entry - stop)
        lot.update(entry=entry, stop=stop, target=target)
        if self.pm:
            lot.update(r0=entry - stop, adds_done=self.pm.get("max_scale_ins", 0), scaled_out=False)

    def _check_entry(self, symbol: str, ctx: Context) -> Signal | None:
        bars = ctx.data.history(symbol, self.interval, self.lookback_bars + 1)
        if len(bars) < self.lookback_bars + 1:
            return None

        bar_ts = bars.index[-1]
        if self._last_bar_ts.get(symbol) == bar_ts:
            return None
        self._last_bar_ts[symbol] = bar_ts

        entry_date = bar_ts.date()
        if not self.allow_same_day_reentry and self._last_entry_date.get(symbol) == entry_date:
            return None

        if self.min_session_bars:
            tz, open_t = _SESSION_OPEN[self.market]
            bar_local = bar_ts.astimezone(tz)
            open_dt = datetime.combine(bar_local.date(), open_t, tzinfo=tz)
            if bar_local < open_dt + timedelta(minutes=self.min_session_bars * self.interval_minutes):
                return None

        last_bar = bars.iloc[-1]
        lookback = bars.iloc[-(self.lookback_bars + 1):-1]
        lb_high = lookback["high"].max()
        lb_vol_mean = lookback["volume"].mean()

        if not (last_bar["close"] > lb_high and last_bar["volume"] > self.volume_mult * lb_vol_mean):
            return None

        entry_price = float(last_bar["close"])
        stop = max(float(last_bar["low"]), entry_price * (1 - self.stop_fallback_pct / 100))
        if self.stop_min_bps:
            stop = min(stop, entry_price * (1 - self.stop_min_bps / 1e4))
        target = entry_price + self.risk_reward * (entry_price - stop)

        state = {"entry": entry_price, "stop": stop, "target": target}
        if self.pm:
            initial_frac = self.pm.get("initial_tranche_frac", 1.0)
            max_scale_ins = self.pm.get("max_scale_ins", 0)
            target_weight = initial_frac / self.max_concurrent_names
            add_weight = (
                (1 - initial_frac) / self.max_concurrent_names / max_scale_ins
                if max_scale_ins > 0 else 0.0
            )
            state.update(r0=entry_price - stop, adds_done=0, scaled_out=False, add_weight=add_weight)
        else:
            target_weight = 1.0 / self.max_concurrent_names

        self._pending[symbol] = state
        if not self.allow_same_day_reentry:
            self._last_entry_date[symbol] = entry_date

        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=target_weight,
            reason=(
                f"{self.interval_minutes}m 종가 {self.lookback_bars}봉 신고가 돌파 "
                f"({entry_price:.2f} > {lb_high:.2f}) + 거래량 {last_bar['volume']:.0f} > "
                f"{self.volume_mult}x평균({lb_vol_mean:.0f})"
            ),
            stop=stop,
            target=target,
        )

    def _manage_position(self, symbol: str, pos: Position, ctx: Context) -> Signal | None:
        quote = ctx.data.quote(symbol)
        if quote is None:
            return None
        price = quote.price
        lot = pos.ensure_lot(self.id)
        entry, stop, target = lot["entry"], lot["stop"], lot["target"]

        minutes_to_close = ctx.clock.minutes_to_close(self.market)
        if ctx.clock.should_flatten(self.market, self.flatten_minutes):
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"마감 전 청산: entry={entry:.2f} stop={stop:.2f} target={target:.2f} 현재={price:.2f}",
            )
        if price <= stop:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"손절: entry={entry:.2f} stop={stop:.2f} 현재={price:.2f}",
            )
        if price >= target:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"목표가 도달: entry={entry:.2f} target={target:.2f} 현재={price:.2f}",
            )

        if not self.pm:
            return None

        r0 = lot.get("r0") or (entry - stop)
        if r0 <= 0:
            return None

        scale_out_at_r = self.pm.get("scale_out_at_r", 0)
        if scale_out_at_r and not lot.get("scaled_out") and price >= entry + scale_out_at_r * r0:
            frac = self.pm.get("scale_out_fraction", 0.5)
            state_update: dict = {"scaled_out": True}
            note = ""
            if self.pm.get("breakeven_after_scale_out") and stop < entry:
                state_update["stop"] = entry
                note = ", 손절가 breakeven 이동"
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.SCALE_OUT,
                target_weight=0.0, exit_fraction=frac,
                reason=(
                    f"부분 익절 (+{scale_out_at_r:g}R): entry={entry:.2f} 현재={price:.2f} "
                    f"잔여 목표 {target:.2f} 유지{note}"
                ),
                state_update=state_update,
            )

        scale_in_at_r = self.pm.get("scale_in_at_r", 0)
        max_scale_ins = self.pm.get("max_scale_ins", 0)
        adds_done = lot.get("adds_done", 0)
        no_add_minutes = self.pm.get("no_add_minutes_before_close", 0)
        if (
            scale_in_at_r and adds_done < max_scale_ins and not lot.get("scaled_out")
            and price >= entry + scale_in_at_r * r0
            and (minutes_to_close is None or minutes_to_close > no_add_minutes)
        ):
            add_weight = lot.get("add_weight", 0.0)
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.SCALE_IN,
                target_weight=add_weight, exit_fraction=1.0,
                reason=(
                    f"추가매수 (+{scale_in_at_r:g}R 순행, {adds_done + 1}/{max_scale_ins}): "
                    f"entry={entry:.2f} 현재={price:.2f}"
                ),
                state_update={"adds_done": adds_done + 1},
            )

        return None


class DonchianPureStrategy:
    """`DonchianStrategy`와 동일한 판단을 하는 순수함수 구현 — 엔진 분리 설계
    Phase A 파일럿. `decide()`는 `ctx`도, 인스턴스 가변 상태도 읽지 않는다:
    entry/stop/target/r0/adds_done/scaled_out 전부 `state`(→`next_state`)로만
    다닌다.

    `DonchianStrategy`와 근본적으로 다른 점 하나: 원본은 진입 정보(entry/stop/
    target)를 `self._pending`에 두었다가 포지션이 실제로 열리면
    `Position.meta["lots"][id]`에 **직접 mutate**해 넣는다(`_ensure_state`).
    이 순수 버전은 `Position.meta`에 아무것도 쓰지 않는다 — 모든 것을
    `next_state`로만 넘긴다(scale_out/scale_in의 `Signal.state_update`는 기존
    루프 메커니즘과의 하위호환을 위해 그대로 채워 넣지만, 이 전략 스스로는 그걸
    다시 읽지 않는다). 재시작 시 `state`가 사라지면 열린 포지션의 entry/stop/
    target을 복구할 방법이 없다 — 이번 범위 밖이다(체결 연동은 다음 단계, 클래스
    docstring과 `shell.py` 참고). 단일 프로세스 백테스트/paper 세션 안에서는
    `next_state`가 사이클마다 그대로 이어지므로 이 한계가 드러나지 않는다.
    """

    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "donchian_pure"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market

        self.interval_minutes: int = params["interval_minutes"]
        self.interval = f"{self.interval_minutes}m"
        self.lookback_bars: int = params["lookback_bars"]
        self.volume_mult: float = params["volume_mult"]
        self.stop_fallback_pct: float = params["stop_fallback_pct"]
        self.risk_reward: float = params["risk_reward"]
        self.max_concurrent_names: int = params["max_concurrent_names"]
        self.flatten_minutes: int = params["flatten_before_close_minutes"]
        self.min_session_bars: int = params.get("min_session_bars_before_entry", 0)
        self.stop_min_bps: float = params.get("stop_min_bps", 0)
        self.pm: dict | None = params.get("position_mgmt")
        self.allow_same_day_reentry: bool = params.get("allow_same_day_reentry", True)

    def requirements(self) -> DataNeeds:
        bars = tuple((symbol, self.interval, self.lookback_bars + 1) for symbol in self.symbols)
        return DataNeeds(bars=bars, quotes=tuple(self.symbols), needs_positions=True)

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        pending: dict[str, dict] = dict(state.get("pending", {}))
        open_: dict[str, dict] = {sym: dict(lot) for sym, lot in state.get("open", {}).items()}
        last_bar_ts: dict[str, object] = dict(state.get("last_bar_ts", {}))
        last_entry_date: dict[str, object] = dict(state.get("last_entry_date", {}))

        open_symbols = set(snap.lots.keys())  # == "이 심볼 포지션이 지금 열려 있다"
        open_count = len(open_symbols)
        signals: list[Signal] = []

        for symbol in self.symbols:
            if symbol in open_symbols:
                if symbol not in open_:
                    promoted = pending.pop(symbol, None)
                    if promoted is None:
                        # 복구 불가(재시작 등, state 유실) — 이번 범위 밖. 클래스
                        # docstring 참고. 이 심볼은 관리하지 않고 다음 사이클로 넘어간다.
                        continue
                    open_[symbol] = dict(promoted)
                signal = self._manage(symbol, open_[symbol], snap)
                if signal is not None:
                    signals.append(signal)
                continue

            # 포지션이 열려 있지 않다 — 외부 요인(체결 확정 등)으로 청산됐다면
            # 잔여 상태를 정리한다.
            open_.pop(symbol, None)

            if not snap.market_open.get(self.market, False):
                continue
            if self._should_flatten(snap):
                continue  # 곧 청산할 시각에 새로 들어가지 않는다
            if open_count >= self.max_concurrent_names:
                continue

            signal, new_ts = self._check_entry(symbol, snap, last_bar_ts.get(symbol), pending, last_entry_date)
            if new_ts is not None:
                last_bar_ts[symbol] = new_ts
            if signal is not None:
                signals.append(signal)
                open_count += 1

        next_state = {
            "pending": pending, "open": open_,
            "last_bar_ts": last_bar_ts, "last_entry_date": last_entry_date,
        }
        return Decision(signals=tuple(signals), next_state=next_state)

    def _should_flatten(self, snap: StrategySnapshot) -> bool:
        """`quant/core/clock.py`의 `_should_flatten`을 스냅샷 원재료로 재현한다."""
        mtc = snap.minutes_to_close.get(self.market)
        if mtc is None:
            return False
        # mtc <= 0 = 연속 거래 종료(동시호가 구간) — 원본과 동일하게 False
        # (2026-08-26, clock._should_flatten 의 remaining<=0 게이트 재현).
        if mtc <= 0:
            return False
        return mtc - snap.cadence_minutes < self.flatten_minutes

    def _check_entry(
        self, symbol: str, snap: StrategySnapshot, last_bar_ts: object,
        pending: dict[str, dict], last_entry_date: dict[str, object],
    ) -> tuple[Signal | None, object]:
        """반환값 두번째 원소는 "새로 관측한 완성봉 시각"(last_bar_ts에 반영할 값) —
        원본이 봉을 새로 볼 때마다(진입 여부와 무관하게) `_last_bar_ts`를 갱신하던
        것과 동치. 이번 사이클에 새 봉을 못 봤으면(같은 봉 재방문, 데이터 부족) None."""
        bars = snap.bars.get((symbol, self.interval))
        if bars is None or len(bars) < self.lookback_bars + 1:
            return None, None

        bar_ts = bars.index[-1]
        if last_bar_ts == bar_ts:
            return None, None

        entry_date = bar_ts.date()
        if not self.allow_same_day_reentry and last_entry_date.get(symbol) == entry_date:
            return None, bar_ts

        if self.min_session_bars:
            tz, open_t = _SESSION_OPEN[self.market]
            bar_local = bar_ts.astimezone(tz)
            open_dt = datetime.combine(bar_local.date(), open_t, tzinfo=tz)
            if bar_local < open_dt + timedelta(minutes=self.min_session_bars * self.interval_minutes):
                return None, bar_ts

        last_bar = bars.iloc[-1]
        lookback = bars.iloc[-(self.lookback_bars + 1):-1]
        lb_high = lookback["high"].max()
        lb_vol_mean = lookback["volume"].mean()

        if not (last_bar["close"] > lb_high and last_bar["volume"] > self.volume_mult * lb_vol_mean):
            return None, bar_ts

        entry_price = float(last_bar["close"])
        stop = max(float(last_bar["low"]), entry_price * (1 - self.stop_fallback_pct / 100))
        if self.stop_min_bps:
            stop = min(stop, entry_price * (1 - self.stop_min_bps / 1e4))
        target = entry_price + self.risk_reward * (entry_price - stop)

        pending_state = {"entry": entry_price, "stop": stop, "target": target}
        if self.pm:
            initial_frac = self.pm.get("initial_tranche_frac", 1.0)
            max_scale_ins = self.pm.get("max_scale_ins", 0)
            target_weight = initial_frac / self.max_concurrent_names
            add_weight = (
                (1 - initial_frac) / self.max_concurrent_names / max_scale_ins
                if max_scale_ins > 0 else 0.0
            )
            pending_state.update(r0=entry_price - stop, adds_done=0, scaled_out=False, add_weight=add_weight)
        else:
            target_weight = 1.0 / self.max_concurrent_names

        pending[symbol] = pending_state
        if not self.allow_same_day_reentry:
            last_entry_date[symbol] = entry_date

        signal = Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=target_weight,
            reason=(
                f"{self.interval_minutes}m 종가 {self.lookback_bars}봉 신고가 돌파 "
                f"({entry_price:.2f} > {lb_high:.2f}) + 거래량 {last_bar['volume']:.0f} > "
                f"{self.volume_mult}x평균({lb_vol_mean:.0f})"
            ),
            stop=stop,
            target=target,
        )
        return signal, bar_ts

    def _manage(self, symbol: str, lot: dict, snap: StrategySnapshot) -> Signal | None:
        """`lot`은 `decide()`가 만든 이번 사이클 로컬 사본이다 — 여기서의 in-place
        갱신(`lot.update(...)`)은 `next_state`에만 반영되고 `Position.meta`는
        건드리지 않는다."""
        quote = snap.quotes.get(symbol)
        if quote is None:
            return None
        price = quote.price
        entry, stop, target = lot["entry"], lot["stop"], lot["target"]

        if self._should_flatten(snap):
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"마감 전 청산: entry={entry:.2f} stop={stop:.2f} target={target:.2f} 현재={price:.2f}",
            )
        if price <= stop:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"손절: entry={entry:.2f} stop={stop:.2f} 현재={price:.2f}",
            )
        if price >= target:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"목표가 도달: entry={entry:.2f} target={target:.2f} 현재={price:.2f}",
            )

        if not self.pm:
            return None

        r0 = lot.get("r0") or (entry - stop)
        if r0 <= 0:
            return None

        scale_out_at_r = self.pm.get("scale_out_at_r", 0)
        if scale_out_at_r and not lot.get("scaled_out") and price >= entry + scale_out_at_r * r0:
            frac = self.pm.get("scale_out_fraction", 0.5)
            state_update: dict = {"scaled_out": True}
            note = ""
            if self.pm.get("breakeven_after_scale_out") and stop < entry:
                state_update["stop"] = entry
                note = ", 손절가 breakeven 이동"
            lot.update(state_update)
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.SCALE_OUT,
                target_weight=0.0, exit_fraction=frac,
                reason=(
                    f"부분 익절 (+{scale_out_at_r:g}R): entry={entry:.2f} 현재={price:.2f} "
                    f"잔여 목표 {target:.2f} 유지{note}"
                ),
                state_update=state_update,
            )

        scale_in_at_r = self.pm.get("scale_in_at_r", 0)
        max_scale_ins = self.pm.get("max_scale_ins", 0)
        adds_done = lot.get("adds_done", 0)
        no_add_minutes = self.pm.get("no_add_minutes_before_close", 0)
        minutes_to_close = snap.minutes_to_close.get(self.market)
        if (
            scale_in_at_r and adds_done < max_scale_ins and not lot.get("scaled_out")
            and price >= entry + scale_in_at_r * r0
            and (minutes_to_close is None or minutes_to_close > no_add_minutes)
        ):
            add_weight = lot.get("add_weight", 0.0)
            state_update = {"adds_done": adds_done + 1}
            lot.update(state_update)
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.SCALE_IN,
                target_weight=add_weight, exit_fraction=1.0,
                reason=(
                    f"추가매수 (+{scale_in_at_r:g}R 순행, {adds_done + 1}/{max_scale_ins}): "
                    f"entry={entry:.2f} 현재={price:.2f}"
                ),
                state_update=state_update,
            )

        return None


class DonchianPureShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies`가 기존 전략과 같은 방식으로
    (`cls(symbols=..., params=..., market=..., id=...)`) 생성할 수 있도록 하는
    얇은 팩토리. `DonchianPureStrategy` + `PureStrategyShell`을 조립한다."""

    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "donchian_pure"):
        super().__init__(DonchianPureStrategy(symbols, params, market=market, id=id))
