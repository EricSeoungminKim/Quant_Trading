"""횡단면(상대) 모멘텀 로테이션 — 관심종목 유니버스에서 최근 강세 상위를 롱.
롱 온리, 오버나이트 보유 허용.

## 근거와 정직성

횡단면 모멘텀 문헌(Jegadeesh & Titman 계열의 상대강도 로테이션)을 따른다:
유니버스 내 상대 수익률 상위 종목을 사고, 상위권에서 밀려나면 판다.
**백테스트로 검증된 적 없다 [미검증]** — paper 번인(스코어보드의 거래당 bps)이
쌓이기 전까지 수익성 주장은 하지 않는다.

## 모멘텀 크래시 위험

문헌상 모멘텀 전략은 시장이 급락 후 급반등하는 국면(예: 2009년)에서 대규모
손실을 낸 사례가 보고돼 있다 — 하락장에서 상대적으로 덜 빠진 종목이 상위에
랭크된 채로 급반등이 오면 로테이션이 그 반등을 놓치고, 개별 손절(ATR 기반)이
여러 종목에서 동시에 발동해 손실이 몰릴 수 있다. 이 전략은 이 리스크를
회피하지 않는다 — 개별 ATR 손절만으로 방어한다.

## 규칙

주 1회, 시장별 세션 날짜가 새로 바뀐 첫 판정에서 그 요일이 `rebalance_weekday`
(기본 0=월요일)와 일치할 때만(같은 ISO 주에는 재실행하지 않음) 유니버스 전체
(`self.symbols`, 시장 혼합 가능)의 `lookback_sessions`(기본 20) 일봉 수익률을
계산해 상위 `top_n`(기본 2) 선정한다:

- 보유 중인데 상위 top_n에서 빠짐 → 청산 신호.
- 새로 상위에 들었는데 미보유 → 진입 신호(target_weight = 1/top_n).
- 진입 시 손절 = 진입가 - `atr_stop_mult`(기본 2.0) x 일봉 ATR(`atr_period`,
  기본 14) — 로테이션 주기(1주) 사이의 급락 방어.

개별 손절 감시는 보유 종목만 매 사이클 실시간 시세로 하되, 랭킹 계산(history
조회)은 주 1회로 제한한다 — intraday_scan의 슬롯 게이트와 같은 이유(관심종목이
수십 종목이면 사이클마다 전 종목 일봉을 부르는 비용이 무시할 수 없다).

Position.meta에 `entry`/`stop`/`strategy`를 저장한다(다른 전략과 같은 규약).
`_owns`는 orb_scan.py의 것과 동일하게 구현한다.

## 시장별 독립 주간 게이트 (2026-08-19 수정)

**사고**: 2026-08-17(월) KR 대체공휴일. `_last_rebalance_week`가 시장 공유
단일 값이던 시절, 그날 US 22:30 트리거가 그 주의 게이트를 소비했다. 랭킹은
KR 상위 2종목(009150/066570)을 뽑아 진입 신호를 냈지만 KR 장이 닫혀 있어
두 신호 모두 "장 마감 — 주문 불가"로 거부됐고, 게이트가 이미 소비돼 그 주
내내 재시도가 없었다(누적 거래 0건의 원인). 이 파일에 log 호출이 한 줄도
없어 8일간 아무도 몰랐다.

수정 후: `_last_rebalance_week`는 시장별 dict다. 목표 요일(`rebalance_weekday`)
*이후* 그 시장이 처음 여는 날 수행한다(`weekday() < rebalance_weekday`면 아직
이르므로 대기, 그 외엔 트리거) — 목표 요일 당일이 정상 케이스, 그날 휴장이면
다음 개장일이 자동으로 캐치업한다(그 주를 통째로 버리는 것보다, 다음 거래일에
늦게라도 최신 종가로 재편입 기회를 주는 편이 낫다는 판단 — ISO 주가 바뀌면
이 조건과 무관하게 자연히 다음 주 사이클로 넘어가므로 목표 요일보다 한참
늦게 캐치업하는 일은 없다).

## 랭킹은 전체 유니버스, 집행은 트리거한 시장만 (2026-08-19 2차 수정)

`_rebalance()`는 여전히 전체 유니버스(시장 혼합)를 한 번에 랭킹한다 — 횡단면
랭킹의 의미(유니버스 전체 상대강도) 자체는 시장을 가리지 않아야 하므로, 트리거한
시장 심볼만으로 로컬 랭킹을 만들지 않는다. 하지만 **청산·진입 신호는 그 사이클을
트리거한 시장의 심볼에 대해서만 낸다** — 다른 시장 심볼은 top_n 계산에는
들어가지만 이번 회차에서 건드리지 않는다(그 심볼의 시장이 자기 트리거를 맞을
때 처리된다).

**왜.** 시장별 게이트만으로는(1차 수정) 한 주에 KR·US가 각각 트리거되면
`_rebalance()`가 주 2회 실행됐다 — 그때마다 청산 루프가 *전체* 보유 포지션을
훑어 "top_n 밖이면 청산"을 적용했으므로, US 트리거가 도는 회차가 (US 자신과
무관한) KR 보유 포지션이 상위권 밖으로 밀려난 걸 감지해 KR 자신의 주간 트리거를
기다리지 않고 즉시 청산해버릴 수 있었다. 이 저장소의 확인된 문제가 "수수료가
엣지보다 크다"는 것이므로(개선-백로그 참고) 회전율을 불필요하게 늘리는 경로는
막는다. 지금은 각 시장이 정확히 주 1회씩만 자기 심볼을 조정한다.

진입 신호는 여전히 `ctx.clock.is_market_open(market_of_symbol(symbol))`도 함께
확인한다(2026-08-17 KR 휴장 사고 대응, 1차 수정) — 시장-스코프 필터링만으로도
이론상 이 조건은 항상 참이 되지만(트리거한 시장이 열려 있어야 그 시장의
`_rebalance()` 호출 자체가 일어나므로), 방어를 두 겹으로 남겨둔다. 청산(로테이션
이탈·손절)은 시장 개장 여부를 별도로 확인하지 않는다 — 시장이 닫혀 있으면 어차피
`RiskManagerImpl.approve()`가 청산 신호도 함께 막는다(risk/manager.py 상단
"장 마감 게이트" 절 참고) — 전략 레벨에서 청산 경로에 시장 개장 확인을 중복으로
넣지 않는다.
"""
from __future__ import annotations

import logging
from datetime import date as dtdate

import pandas as pd

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction, market_of_symbol
from quant.trade.strategy.orb_scan import _SESSION_OPEN

# 재시작 복구용 보수적 손절 폴백 — orb_scan/intraday_scan과 동일한 관례(2%).
_FALLBACK_STOP_PCT = 0.02

logger = logging.getLogger(__name__)


def _atr(bars: pd.DataFrame, period: int) -> float:
    high, low, close = bars["high"], bars["low"], bars["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return float(tr.dropna().tail(period).mean())


class CrossMomentumStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "cross_momentum"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        self.lookback_sessions: int = params.get("lookback_sessions", 20)
        self.top_n: int = params.get("top_n", 2)
        self.rebalance_weekday: int = params.get("rebalance_weekday", 0)
        self.atr_period: int = params.get("atr_period", 14)
        self.atr_stop_mult: float = params.get("atr_stop_mult", 2.0)

        self._fetch_n = max(self.lookback_sessions, self.atr_period) + 1

        self._session_date: dict[str, dtdate] = {}
        self._last_rebalance_week: dict[str, str] = {}  # 시장별 — 2026-08-19, 상단 docstring 참고
        self._pending: dict[str, dict] = {}
        self._current_top: set[str] = set()

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

        # 1) 손절 감시 — 보유 종목만, 매 사이클 실시간 시세(랭킹 계산과 별개)
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

        # 2) 주 1회 랭킹 — 시장별로 독립된 게이트(_last_rebalance_week[market]).
        # 목표 요일 이후 그 시장이 처음 여는 날 트리거(캐치업) — 상단 docstring
        # "시장별 독립 주간 게이트" 절 참고.
        markets_present = sorted({market_of_symbol(s) for s in self.symbols})
        for market in markets_present:
            if not ctx.clock.is_market_open(market):
                continue
            tz, _ = _SESSION_OPEN[market]
            today = ctx.clock.now().astimezone(tz).date()
            if today == self._session_date.get(market):
                continue  # 이미 이번 세션 판정 완료
            self._session_date[market] = today
            if today.weekday() < self.rebalance_weekday:
                continue  # 목표 요일 전 — 아직 이름
            iso_year, iso_week, _ = today.isocalendar()
            week_key = f"{iso_year}-{iso_week:02d}"
            if week_key == self._last_rebalance_week.get(market):
                continue  # 이 시장은 이번 주 이미 리밸런스함
            self._last_rebalance_week[market] = week_key
            signals.extend(self._rebalance(ctx, positions, stopped_out, market))

        return signals

    def _rebalance(self, ctx: Context, positions: dict, stopped_out: set[str], market: str) -> list[Signal]:
        """유니버스 전체로 랭킹하되, `market`(이 회차를 트리거한 시장)의 심볼만
        청산·진입시킨다 — 상단 docstring "랭킹은 전체 유니버스, 집행은 트리거한
        시장만" 절 참고. 다른 시장 심볼은 top_n 계산엔 들어가지만 이번 회차에서
        건드리지 않는다."""
        signals: list[Signal] = []
        bars_by_symbol: dict[str, pd.DataFrame] = {}
        returns: dict[str, float] = {}
        skip_counts: dict[str, int] = {}

        def _skip(reason: str) -> None:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1

        for symbol in self.symbols:
            bars = ctx.data.history(symbol, "1d", self._fetch_n)
            if len(bars) < self.lookback_sessions + 1:
                _skip("랭킹봉부족")
                continue
            start = float(bars["close"].iloc[-(self.lookback_sessions + 1)])
            if start <= 0:
                _skip("시가0이하")
                continue
            returns[symbol] = float(bars["close"].iloc[-1]) / start - 1
            bars_by_symbol[symbol] = bars

        ranked = sorted(returns.items(), key=lambda kv: kv[1], reverse=True)
        top = {sym for sym, _ in ranked[: self.top_n]}
        self._current_top = top

        # 청산 — 보유 중인데 상위권에서 빠짐. 이 회차를 트리거한 시장의 심볼만
        # 청산한다(다른 시장 포지션은 자기 시장 트리거를 기다린다 — 상단
        # docstring "랭킹은 전체 유니버스, 집행은 트리거한 시장만" 절 참고).
        # 개장 여부는 별도로 확인하지 않는다: 닫혀 있으면 RiskManagerImpl.approve()가
        # 청산 신호도 함께 막으므로(상단 docstring 참고), 전략에서 중복으로 게이트할
        # 필요가 없다 — 손절 경로와 동일하게 그대로 둔다.
        exit_count = 0
        for symbol, pos in positions.items():
            if not pos.is_open or not self._owns(pos) or symbol in stopped_out:
                continue
            if market_of_symbol(symbol) != market:
                continue
            if symbol not in top:
                signals.append(Signal(
                    strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0,
                    reason=(
                        f"로테이션 이탈: {symbol} 상위 {self.top_n}권 밖 "
                        f"({self.lookback_sessions}세션 수익률 재랭킹)"
                    ),
                ))
                exit_count += 1

        # 진입 — 새로 상위권에 들었는데 미보유. 이 회차를 트리거한 시장의 심볼만
        # 진입시킨다(다른 시장의 top_n 편입 후보는 자기 시장 트리거를 기다린다).
        # is_market_open 확인은 시장 스코프 필터링과 별개로 방어 이중화 차원에서
        # 그대로 둔다(2026-08-17 KR 휴장 사고 대응, 1차 수정) — 트리거한 시장은
        # on_cycle에서 이미 개장 확인을 거쳤으므로 정상 흐름에서는 항상 참이지만,
        # 닫힌 시장에 신호를 내면 주문이 거부돼 낭비된다는 원 취지를 지운다.
        entry_count = 0
        target_weight = 1.0 / self.top_n if self.top_n else 0.0
        for symbol in top:
            if market_of_symbol(symbol) != market:
                continue
            pos = positions.get(symbol)
            if pos is not None and pos.is_open:
                continue  # 이미 보유(내 포지션이든 남의 포지션이든 중복 매수 안 함)
            if not ctx.clock.is_market_open(market):
                _skip("시장마감")
                continue
            bars = bars_by_symbol.get(symbol)
            if bars is None or len(bars) < self.atr_period + 1:
                _skip("ATR봉부족")
                continue
            atr = _atr(bars, self.atr_period)
            if atr <= 0:
                _skip("ATR실패")
                continue
            quote = ctx.data.quote(symbol)
            if quote is None or quote.price <= 0:
                _skip("quote없음")
                continue
            entry_price = quote.price
            stop = entry_price - self.atr_stop_mult * atr
            self._pending[symbol] = {"entry": entry_price, "stop": stop, "strategy": self.id}
            signals.append(Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.ENTER_LONG,
                target_weight=target_weight,
                reason=(
                    f"로테이션 편입: {symbol} {self.lookback_sessions}세션 수익률 "
                    f"{returns[symbol] * 100:+.2f}% (상위 {self.top_n})"
                ),
                stop=stop,
            ))
            entry_count += 1

        logger.info(
            "cross_momentum 리밸런스[%s 트리거]: 랭킹 %d/%d 성공, top=%s, 진입 %d건, 청산 %d건, skip=%s",
            market, len(returns), len(self.symbols), sorted(top), entry_count, exit_count, skip_counts,
        )
        return signals

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
        lot.update(entry=entry, stop=stop, strategy=self.id)
