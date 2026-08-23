"""단타 자동매매 갈래(A) — 개장 진입·당일 청산 고정. 자체 리포트 파이프라인
(intraday_scorer v4, H-2 — `quant.analyze.intraday_score`)이 뽑은 단타 후보만
대상으로, 그 종목이 속한 시장이 열리는 즉시 시장가로 사고 그날 마감 전에
무조건 청산한다. 롱 온리.

## 스펙 근거 (2026-08-17 사용자 결정, §5)

`docs/superpowers/specs/2026-08-17-quant-automation-design.md` §5의 확정 사항:
- 배분 A 20% (config/settings.yaml, KR 전용 — 데이터 소스가 한국 종목만 다룬다)
- A의 진입 전략은 기존 news_momentum 재사용이 아니라 **신규 전략**(이 파일) —
  개장 진입·당일 청산을 고정 규칙으로 못박는다(백테스트가 측정한 방식 그대로,
  `quant/backtest/intraday_verify.py`의 open→close bp 측정과 진입/청산 정의를
  맞춘다).
- 승격 판정 최소 A 10거래일(§4 참고 — 실제 판정식은 `n_symbol_days>=30`으로
  더 보수적으로 잡혀 있다. `quant/control/ledger.py` 참고).

## news_momentum과의 차이 (왜 별도 파일인가)

`news_momentum.py`는 부분익절 사다리(−2%/+5%/+10%/30분 타임아웃)로 단타를
정교하게 관리한다. 이 전략은 그 반대 — **의도적으로 단순하다.** 고정 손절
하나 + EoD 강제청산 둘 뿐이고, 부분익절·시간청산·목표가 사다리를 만들지
않는다(사용자 지시: "비대칭 청산 고도화는 fitness 근거 후로 미룸" — 승격
판정에 쓸 표본이 먼저 쌓여야, 그 표본을 근거로 청산 규칙을 개선하는 것이
순서에 맞다. 검증되지 않은 청산 로직을 미리 쌓으면 그 표본이 무엇을 측정한
것인지 불명확해진다).

## 2단 구조 (ADR-0002: 거래 핫패스에 LLM 금지) — news_momentum과 동일 원칙

이 전략은 뉴스/리포트를 읽지 않는다. 후보 선정은 intraday_scorer v4(자체
리포트 파이프라인)가 장 시작 전에 끝내고, 확신도 게이트(watch_scorer)를
통과한 종목만 관심종목 파일에 `EVENT_SCALP` 태그로 남는다(배선은 다음
워커의 own_brief 오후판/크론 작업 범위 — 이 전략은 그 태그만 읽는다).

## 태그: `EVENT_SCALP` (news_momentum의 `EVENT`와 별개)

news_momentum이 읽는 `EVENT` 태그는 회사 데일리 리포트(08:40) 파이프라인이
붙인다. 이 전략의 `EVENT_SCALP`는 다른 데이터 소스(intraday_scorer v4 —
뉴스 z-score·외국인 v2·증권사 리서치 목록 등, H-2 커밋 이력 참고)에서 나온다.
같은 "EVENT" 이름을 그대로 쓰면 두 파이프라인의 후보가 하나의 태그에 뒤섞여
원장에서 어느 판정으로 들어온 종목인지 구분할 수 없다 — 이름을 분리해 그
경계를 유지한다. `tags_of`가 없거나(None/빈 dict) 이 태그가 없는 심볼은
절대 진입 대상이 아니다(news_momentum과 동일한 안전 기본값 철학 — 근거 없이
아무 종목이나 사는 것으로 조용히 후퇴하지 않는다).

## 진입

- 대상: `tags_of`에 `EVENT_SCALP`가 포함된 관심종목만(`self.symbols` 교집합).
- 시각: 그 종목이 속한 시장이 연 직후 `entry_window_seconds`(기본 120초) 안.
  창을 놓치면 그날은 진입하지 않는다(news_momentum과 동일 이유 — 개장 갭을
  노리는 전략인데 몇 분 뒤에 들어가면 다른 전략이 된다).
- `ctx.data.quote()` 기준 시장가 진입 — 완성봉을 요구하지 않는다(개장 직후엔
  완성봉이 0개다). intraday_verify.py가 측정하는 "그날 시가" 진입과 최대한
  맞춘 정의다.
- 종목당 세션 1회, 재진입 없음(`_entered_today` — 같은 심볼이 당일 청산된
  뒤에도 다시 사지 않는다). 세션(하루) 최대 진입 종목 수는
  `max_entries_per_session`(기본 3).

## 청산 — 손절 + EoD 둘 뿐

- `stop_loss_pct`(기본 3.0%): 진입가 대비 이하로 내려가면 전량 청산. 이
  값은 백테스트로 최적화한 것이 아니라 **과도 손실 꼬리를 자르는 보수적
  기본값**이다(스펙 방침 — 비대칭 청산 고도화는 표본이 쌓인 뒤로 미룬다).
- EoD 강제청산(`flatten_before_close_minutes`) + 세션 롤 오버나잇 금지
  레일(다른 스캐너와 동일) — "당일 세션 마감 전 무조건 청산"을 두 겹으로
  보장한다.
- 부분익절·목표가·시간 손절은 **없다** — 추가 규칙을 발명하지 않는다.

## 사이징

후보를 세션 상한(`max_entries_per_session`) 슬롯으로 균등 분할한다 —
`target_weight = 1 / max_entries_per_session` (donchian의
`1 / max_concurrent_names`와 동일한 균등 분배 철학). 실제 후보가 상한보다
적으면 남는 슬롯만큼 그날 자본이 덜 쓰이지만(당일 편입분을 다 못 채울 수
있다), 슬롯 수를 후보 수에 맞춰 매 사이클 다시 계산하지 않는 편이 더
예측 가능하고 결정론적이다.

## 랏·소유권

`news_momentum.py`/`orb_scan.py`와 동일한 `_owns` 3단 판정과 `ensure_lot`
패턴을 따른다.

## 정직성 경고 [미검증]

- news_momentum과 마찬가지로 개장가 진입은 구조적으로 불리하다 — 호재가
  있으면 이미 갭업으로 시작한 가격을 사는 것이다.
- 손절 -3%는 백테스트 분포를 보고 튜닝한 값이 아니라, "과도 손실 꼬리를
  자르는" 임의의 보수적 기본값이다.
- paper 번인(스코어보드 거래당 bps, `quant/control/ledger.py`)이 쌓이기
  전까지 수익성 주장 없음.

## 시장 리스크오프 게이트 (2026-08-18 관측 기반, shadow 기본)

news_momentum.py와 동일 배선 — 판정 로직은 `quant/trade/indicators/
breadth.py`(모듈 docstring "측정 결과" 절 참고), 여기선 배선만. 이 전략도
개장 후 entry_window_seconds(기본 120초) 안에서만 진입하므로 실측 근거가
그대로 적용된다: 앵커의 당일 첫 1분봉이 그 시점엔 아직 없어 게이트가 사실상
무력하다 — 그래서 기본 shadow. `market_risk_gate_mode`(off/shadow/block)·
`market_risk_max_drawdown_pct`(기본 0.5%)로 조정한다.
"""
from __future__ import annotations

from datetime import date as dtdate, datetime

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction, market_of_symbol
from quant.trade.indicators.breadth import ANCHOR_SYMBOLS, anchor_drawdown
from quant.trade.strategy.orb_scan import _SESSION_OPEN

# news_momentum의 EVENT(회사 데일리 리포트 기반)와 의도적으로 다른 이름 —
# 모듈 docstring "태그" 절 참고.
_SCALP_TAG = "EVENT_SCALP"


class NewsScalpStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "news_scalp",
                 tags_of: dict[str, list[str]] | None = None):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        # {symbol: [태그, ...]}. None/빈 dict면(테스트·백테스트·미배선) EVENT_SCALP
        # 대상이 하나도 없어 진입 판정 자체를 건너뛴다.
        self.tags_of = tags_of

        self.entry_window_seconds: float = params.get("entry_window_seconds", 120)
        self.max_entries_per_session: int = params.get("max_entries_per_session", 3)
        # 3.0%: 백테스트 최적값이 아니라 과도 손실 꼬리를 자르는 보수적 기본값
        # (모듈 docstring "청산" 절 참고).
        self.stop_loss_pct: float = params.get("stop_loss_pct", 3.0)
        self.flatten_minutes: float = params.get("flatten_before_close_minutes", 1)

        # 시장 리스크오프 게이트(모듈 docstring 절, quant/trade/indicators/
        # breadth.py) — news_momentum.py와 동일 관례.
        mode = str(params.get("market_risk_gate_mode", "shadow")).strip().lower()
        if mode not in ("off", "shadow", "block"):
            mode = "shadow"
        self.market_risk_gate_mode: str = mode
        self.market_risk_max_drawdown_pct: float = params.get("market_risk_max_drawdown_pct", 0.5)
        if self.market_risk_max_drawdown_pct <= 0:
            raise ValueError("market_risk_max_drawdown_pct는 양수여야 합니다.")

        if self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct는 양수여야 합니다.")
        if self.entry_window_seconds <= 0:
            raise ValueError("entry_window_seconds는 양수여야 합니다.")
        if self.max_entries_per_session <= 0:
            raise ValueError("max_entries_per_session은 양수여야 합니다.")

        self._session_date: dict[str, dtdate] = {}
        self._entries_this_session: dict[str, int] = {}
        self._entered_today: set[str] = set()
        self._pending: dict[str, dict] = {}
        self.last_reject: dict[str, str] = {}
        self._anchor_cache: dict[str, tuple[str, object]] = {}
        self.market_risk_verdict: dict[str, bool] = {}

    # ------------------------------------------------------------------ 사이클

    def _owns(self, pos: Position) -> bool:
        """orb_scan.py/news_momentum.py의 `_owns`와 동일 구현 — 소유권 없는 포지션을
        잘못 청산하거나 다른 전략이 이미 추적 중인 lot을 입양하지 않는다."""
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

        # 2) EVENT_SCALP 태그 대상만 진입 후보로 삼는다.
        if not self.tags_of:
            return signals
        candidates = [s for s in self.symbols if _SCALP_TAG in self.tags_of.get(s, [])]
        if not candidates:
            return signals

        markets_present = sorted({market_of_symbol(s) for s in candidates})
        for market in markets_present:
            if not ctx.clock.is_market_open(market):
                continue
            tz, session_open = _SESSION_OPEN[market]
            now_local = ctx.clock.now().astimezone(tz)
            today = now_local.date()
            if today != self._session_date.get(market):
                self._session_date[market] = today
                self._entered_today = {
                    s for s in self._entered_today if market_of_symbol(s) != market
                }
                self._entries_this_session[market] = 0
                self._pending = {
                    s: p for s, p in self._pending.items()
                    if market_of_symbol(s) != market
                    or (positions.get(s) is not None and positions.get(s).is_open)
                }
                self._anchor_cache.pop(market, None)

            if self._entries_this_session.get(market, 0) >= self.max_entries_per_session:
                continue

            session_open_dt = datetime.combine(today, session_open, tzinfo=tz)
            seconds_since_open = (now_local - session_open_dt).total_seconds()
            if not 0 <= seconds_since_open <= self.entry_window_seconds:
                continue

            # 시장 리스크오프 게이트 — 시장당 1회 판정(모듈 docstring 절 참고).
            market_blocked, market_note = self._market_risk_note(market, ctx)

            # 동점(우선순위 없음) — 심볼 오름차순으로 결정론적으로 고른다.
            for symbol in sorted(s for s in candidates if market_of_symbol(s) == market):
                if self._entries_this_session.get(market, 0) >= self.max_entries_per_session:
                    break
                pos = positions.get(symbol)
                held_qty = pos.lot_qty(self.id) if pos is not None else 0.0
                if held_qty > 0 or symbol in self._entered_today:
                    continue  # 세션당 1회 — 재진입 없음
                if market_blocked:
                    self.last_reject[symbol] = (
                        f"시장 리스크오프 차단(앵커={ANCHOR_SYMBOLS.get(market)})"
                    )
                    continue
                signal = self._check_entry_for(symbol, market, ctx, market_note=market_note)
                if signal is not None:
                    signals.append(signal)
        return signals

    # ------------------------------------------------------------ 시장 리스크오프

    def _get_anchor_bars(self, market: str, ctx: Context):
        """분 경계 캐시 — news_momentum.py `_get_anchor_bars`와 동일 패턴."""
        anchor = ANCHOR_SYMBOLS.get(market)
        if anchor is None:
            return None
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
        dd = anchor_drawdown(bars) if bars is not None else None
        if dd is None:
            return False, ""
        risk_off = dd <= -self.market_risk_max_drawdown_pct
        self.market_risk_verdict[market] = risk_off
        if not risk_off:
            return False, ""
        note = f" [시장:리스크오프 {ANCHOR_SYMBOLS[market]} {dd:+.2f}%]"
        return (self.market_risk_gate_mode == "block"), note

    # ------------------------------------------------------------------ 진입

    def _check_entry_for(
        self, symbol: str, market: str, ctx: Context, *, market_note: str = "",
    ) -> Signal | None:
        self.last_reject.pop(symbol, None)
        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            self.last_reject[symbol] = "현재가 없음"
            return None
        entry_price = quote.price
        stop = entry_price * (1 - self.stop_loss_pct / 100)
        target_weight = 1.0 / self.max_entries_per_session

        self._pending[symbol] = {
            "entry": entry_price,
            "session": self._session_date[market].isoformat(),
            "strategy": self.id,
            "qty_at_signal": 0.0,
        }
        self._entered_today.add(symbol)
        self._entries_this_session[market] = self._entries_this_session.get(market, 0) + 1

        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=target_weight,
            reason=(
                f"단타 개장진입(EVENT_SCALP): {symbol} w={target_weight:.2f} "
                f"손절 -{self.stop_loss_pct:g}%{market_note}"
            ),
            stop=stop,
        )

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
        # 재시작 복구 — 진짜 진입 시각/세션을 모른다. 손절·EoD·세션 롤 레일만으로
        # 관리를 이어간다(이 전략에는 시간 손절이 없어 news_momentum처럼 "즉시
        # 시간손절 유도" 트릭이 필요 없다).
        entry = lot.get("avg_cost", pos.avg_cost)
        lot.update(entry=entry, session=None)

    def _manage_position(self, symbol: str, pos: Position, ctx: Context) -> Signal | None:
        quote = ctx.data.quote(symbol)
        if quote is None:
            return None
        price = quote.price
        lot = pos.ensure_lot(self.id)
        entry = lot["entry"]
        market = market_of_symbol(symbol)
        tz, _ = _SESSION_OPEN[market]

        # 오버나잇 금지 — should_flatten 하나에 기대지 않는다(orb_scan/news_momentum과 동일).
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

        stop_price = entry * (1 - self.stop_loss_pct / 100)
        if price <= stop_price:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=(
                    f"손절(-{self.stop_loss_pct:g}%): entry={entry:.2f} "
                    f"stop={stop_price:.2f} 현재={price:.2f}"
                ),
            )
        return None
