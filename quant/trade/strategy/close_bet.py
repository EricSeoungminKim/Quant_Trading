"""종가배팅(close bet) — 마감 무렵 강한 종목을 종가에 사서 다음날 시초 갭에 판다.

2026-08-25 소유자 지시(전략 4종 체제의 ③) + 웹 리서치 종합. 통용 규칙:

- **선정(15:00~15:20)**: 당일 외국인·기관 순매수가 강하고, 거래대금이 하루 종일
  상위권이며, 주도 테마/지속성 뉴스가 있는 종목. 차트로는 **고가 근처에서
  마감하는 양봉**(윗꼬리 짧음) — 마감까지 매수세가 살아 있다는 증거.
- **매도(다음날)**: 갭/시초 슈팅에 판다. 갭이 없고 **전일 종가 아래로 떨어지면
  실망 매물이 쏟아지기 전에 빠르게 정리**한다.
- 근거: wikidocs 수급 시스템트레이딩 §종가베팅, 파이낸스데일리 종가매매 조건,
  실전 블로그 다수(2026-08-25 웹 리서치 — 검색 로그는 변경기록 참고).

## 역할 분담 — 수급·뉴스는 리포트가, 차트·시각은 전략이

외인/기관 수급·거래대금·뉴스 지속성은 **장중 리포트(14:50)** 가 채점해
`CLOSE_BET` 태그로 넘긴다(뉴스→유니버스 경로, 평면 규칙 그대로 — 전략은
수급 데이터를 직접 만지지 않는다). 이 전략은 태그된 종목에 대해 **차트 확인**
(양봉 + 고가 근처 마감)만 결정론으로 판정한다.
소유자도 같은 리포트를 보고 실계좌에서 직접 종가배팅을 할 수 있다 — 프로그램과
사람이 같은 근거를 쓴다.

## 마감 동시호가 구조를 반영한다 (2026-08-26)

소유자 정정: KRX는 09:00~15:20만 연속 거래고, **15:20~15:30은 동시호가**다
(주문만 모았다가 15:30 정각 단일가로 일괄 체결 — Toss `GET
/api/v1/market-calendar/KR` 응답의 `regularMarket.singlePriceAuctionStartTime`도
15:20, `endTime`도 15:30로 이 구조와 일치한다. 실측: 2026-08-26 조사).
"종가배팅"의 의도는 그 15:30 단일가로 사는 것이지, 아직 움직이는 연속 거래가로
사는 게 아니다 — 예전 진입 창(14:55~15:19)이 15:00 근처 가격으로 진입한 건 그
의도와 거리가 있었다.

**같은 날 세션에서 진행 중인 다른 수리와 결이 같다.** `quant/core/session.py`의
`in_continuous_session`(2026-08-26, scalp_1m 프리마켓 오사고 수리)이 세운 원칙을
그대로 따른다: **가격이 실시간으로 발견되지 않는 구간의 "현재가"로 체결을
모델링하면 실재하지 않는 손익이 생긴다.** KR 동시호가(15:20~15:30)는 정확히 그
구간이다 — 우리 데이터 모델도 실거래도 그 30분 동안은 새로운 체결가가 없다
(있다면 15:30 딱 한 번). 그래서 이 전략은 **판정과 체결을 시간상 분리하지
않는다** — 그러면 체결을 동시호가 구간 안으로 밀어 넣게 되고, 그 시점의
`DataFeed.quote()`는 (프리마켓 사례와 똑같이) 우리 엔진 안에서 실재할 수 없는
값이 된다. 대신 **진입 창 자체를 연속 거래가 끝나는 순간(15:20 직전)에 바짝
붙인다** — 마감강도·양봉 판정과 진입 체결이 여전히 같은 사이클에서 일어나지만,
그 사이클이 이제 14:55가 아니라 15:15~15:19다. 연속 거래 구간(`continuous_window
("KR") == (09:00, 15:20)`) 안에서 잡을 수 있는 가장 마감에 가까운 실제 체결가를
쓰는 것이, 존재하지 않는 15:30 단일가를 그전에 흉내 내는 것보다 정직하다.

**한계**: 이 진입가는 여전히 진짜 15:30 종가가 아니라 15:15~15:19 사이의
마지막 연속 거래가 근사치다. 이 저장소는 동시호가 자체를 시뮬레이션하는
인프라가 없다(전용 데이터 소스도, `quant/adapters/execution/paper.py`의 체결
로직도 "즉시 체결"만 안다) — 그런 인프라 없이 15:20~15:30 구간의 값을 진짜
체결가처럼 쓰는 건 위 원칙을 그대로 위반하는 것이므로, 여기서는 시도하지 않는다.

**Toss(실거래) 쪽 한계**: Toss 주문 API(`docs/api/toss/openapi.json`)에
"종가 지정가"(`timeInForce: CLS`, LOC)가 있지만 **미국 주식 지정가 전용**이라고
명시돼 있다(`"종가 주문(CLS)은 미국 주식 지정가 주문에만 사용할 수 있습니다"`,
`allowedConditions: {marketCountry: US, orderType: LIMIT}`) — KR 전용 동시호가
주문 유형은 문서에 없다. KR에서 동시호가에 참여하려면 그 구간(15:20~15:30)에
일반 LIMIT/MARKET 주문을 내는 수밖에 없고, 그 주문이 어떻게 큐잉·체결되는지는
Toss 문서에 별도 설명이 없다 — KRX 거래소 자체의 동시호가 매칭 규칙(그 구간에
접수된 모든 주문이 15:30 단일가로 모여 체결)에 의존한다고 **추정**할 뿐,
Toss API 문서로 확인된 사실은 아니다. 소유자가 리포트를 보고 실계좌에서 직접
동시호가에 주문을 낼 수는 있다(이 전략은 판정까지만 대신한다).

## 왜 오버나이트가 허용되는가

이 전략의 수익 원천이 **오버나이트 갭 그 자체**다. 하루짜리 전략들의 EoD 청산
규칙(오버나이트 금지)을 여기 적용하면 전략이 성립하지 않는다 —
`loop._OVERNIGHT_STRATEGIES` 에 등재돼 마감 문구/강제청산에서 제외된다.

## 방어선 (잃을 땐 적게)

- 진입 다음날, 가격이 **진입가(≈전일 종가) − stop_pct** 아래면 즉시 전량 손절 —
  "갭 시나리오가 죽었다"는 판정이지 버티기 대상이 아니다.
- 시초 `exit_deadline_minutes_after_open`(기본 30분) 안에 익절도 손절도 안 됐으면
  **그냥 정리한다**. 이 전략의 엣지는 시초 슈팅이고, 그 창이 지나면 남는 것은
  방향 없는 오버나이트 리스크뿐이다.
- 진입은 종목당 하루 1회, 태그된 종목만.

백테스트 [미검증] — validation.status: burn_in, 표본이 판정한다(experiments 루프).
"""
from __future__ import annotations

from datetime import date as dtdate, time as dtime
from zoneinfo import ZoneInfo

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction
from quant.core.session import in_continuous_session

_KST = ZoneInfo("Asia/Seoul")
_TAG = "CLOSE_BET"


class CloseBetStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "KR",
                 id: str = "close_bet", tags_of: dict[str, list[str]] | None = None):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Protocol 호환 — 실제 판정은 KR 전용(아래 가드)
        self.tags_of = tags_of

        # 진입 창: 연속 거래 마지막 구간, 동시호가(15:20) 직전까지 — 판정과
        # 체결이 여기서 함께 일어난다(동시호가 구간으로 체결을 밀어 넣지 않는
        # 이유는 모듈 docstring "마감 동시호가 구조를 반영한다" 참고).
        self.entry_start = dtime(*params.get("entry_start_hhmm", (15, 15)))
        self.entry_end = dtime(*params.get("entry_end_hhmm", (15, 19)))
        # 마감 강도 하한: (현재가-당일저가)/(당일고가-당일저가). 0.7 = 고가에서
        # 레인지의 30% 안쪽 — "고가 근처 마감 양봉"의 수치화.
        self.min_close_strength: float = params.get("min_close_strength", 0.7)
        self.target_weight: float = params.get("target_weight", 0.1)
        # 다음날 방어선/목표 (진입가 대비 %) — 손절폭 < 익절폭 (잃을 땐 적게).
        self.stop_pct: float = params.get("stop_pct", 1.0)
        self.take_profit_pct: float = params.get("take_profit_pct", 2.0)
        self.exit_deadline_min: int = params.get("exit_deadline_minutes_after_open", 30)

        if self.stop_pct <= 0 or self.take_profit_pct <= 0:
            raise ValueError("stop_pct/take_profit_pct 는 양수여야 합니다.")
        if not 0 < self.min_close_strength <= 1:
            raise ValueError("min_close_strength 는 (0, 1] 이어야 합니다.")

        self._entered_date: dict[str, dtdate] = {}  # symbol → 마지막 진입일
        self.last_reject: dict[str, str] = {}

    # ------------------------------------------------------------------ 소유권

    def _owns(self, pos: Position) -> bool:
        """orb_scan._owns 와 같은 3단 판정 — lot 있으면 내 것, lots 구조가 있는데
        내 lot 이 없으면 남의 것, 아무 태그도 없으면 유니버스 소속만 입양."""
        if pos.lot(self.id) is not None:
            return True
        meta = pos.meta or {}
        if meta.get("lots") is not None:
            return False
        owner = meta.get("strategy")
        if owner:
            return owner == self.id
        return pos.symbol in self.symbols

    # ------------------------------------------------------------------ 사이클

    def on_cycle(self, ctx: Context) -> list[Signal]:
        signals: list[Signal] = []
        now_kst = ctx.clock.now().astimezone(_KST)
        today = now_kst.date()

        # 1) 보유 관리 — 다음날 시초 창의 익절/손절/데드라인.
        for symbol, pos in ctx.broker.positions().items():
            if not pos.is_open or not self._owns(pos):
                continue
            sig = self._manage(symbol, pos, ctx, now_kst)
            if sig is not None:
                signals.append(sig)

        # 2) 진입 — KR 장중, 진입 창 안에서만, 그리고 실제로 가격이 발견되는
        # 연속 거래 구간 안에서만(동시호가 15:20~15:30에는 신뢰할 수 있는
        # "현재가"가 없다 — quant.core.session.in_continuous_session 참고).
        if not ctx.clock.is_market_open("KR"):
            return signals
        if not (self.entry_start <= now_kst.time() <= self.entry_end):
            return signals
        if not in_continuous_session("KR", now_kst):
            return signals  # entry_end 설정 실수로 동시호가에 걸쳐도 이중 방어

        candidates = [
            s for s in self.symbols
            if s.isdigit() and len(s) == 6  # KR 전용
            and _TAG in (self.tags_of or {}).get(s, [])
        ]
        for symbol in candidates:
            if self._entered_date.get(symbol) == today:
                continue  # 하루 1회
            sig = self._evaluate_entry(symbol, ctx, today)
            if sig is not None:
                signals.append(sig)
        return signals

    def _evaluate_entry(self, symbol: str, ctx: Context, today: dtdate) -> Signal | None:
        bars = ctx.data.history(symbol, "1m", 400)  # 여유 — 당일 세션만 쓴다
        if bars is None or len(bars) < 30:
            self.last_reject[symbol] = "당일 1분봉 부족"
            return None
        day = bars[bars.index.tz_convert(_KST).date == today] if hasattr(
            bars.index, "tz_convert") else bars
        if len(day) < 30:
            self.last_reject[symbol] = "당일 봉 30개 미만"
            return None

        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            self.last_reject[symbol] = "현재가 없음"
            return None
        price = quote.price

        day_open = float(day.iloc[0]["open"])
        day_high = float(day["high"].max())
        day_low = float(day["low"].min())
        if day_high <= day_low:
            self.last_reject[symbol] = "레인지 0"
            return None

        if price <= day_open:
            self.last_reject[symbol] = f"양봉 아님 (시가 {day_open:,.0f} ≥ 현재가)"
            return None
        strength = (price - day_low) / (day_high - day_low)
        if strength < self.min_close_strength:
            self.last_reject[symbol] = f"마감 강도 부족 {strength:.2f} < {self.min_close_strength}"
            return None

        self._entered_date[symbol] = today
        stop = price * (1 - self.stop_pct / 100)
        target = price * (1 + self.take_profit_pct / 100)
        return Signal(
            strategy_id=self.id, symbol=symbol, action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(f"종가배팅: 마감강도 {strength:.2f} 양봉 · 내일 시초 갭 노림 "
                    f"(손절 -{self.stop_pct:g}% / 익절 +{self.take_profit_pct:g}%)"),
            stop=stop, target=target,
            state_update={"entry": price, "stop": stop, "target": target,
                          "session": today.isoformat(), "strategy": self.id},
        )

    def _manage(self, symbol: str, pos: Position, ctx: Context, now_kst) -> Signal | None:
        lot = pos.ensure_lot(self.id)
        entry = lot.get("entry") or pos.avg_cost
        if not entry:
            return None
        entry_session = lot.get("session")
        today = now_kst.date().isoformat()
        if entry_session == today:
            return None  # 진입 당일은 들고 간다 — 그게 이 전략이다

        # 다음날: 장중에만 판정(시세가 세션 밖 잔가일 수 있다).
        if not ctx.clock.is_market_open("KR"):
            return None
        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            return None
        price = quote.price
        stop = lot.get("stop") or entry * (1 - self.stop_pct / 100)
        target = lot.get("target") or entry * (1 + self.take_profit_pct / 100)

        if price <= stop:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"종가배팅 손절: 전일 종가 이탈 (진입 {entry:,.0f} → {price:,.0f})",
            )
        if price >= target:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"종가배팅 익절: 시초 갭 실현 (진입 {entry:,.0f} → {price:,.0f})",
            )
        # 시초 데드라인 — 갭 창이 지나면 남는 건 방향 없는 리스크뿐.
        open_min = (now_kst.hour - 9) * 60 + now_kst.minute
        if open_min >= self.exit_deadline_min:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=(f"종가배팅 정리: 시초 {self.exit_deadline_min}분 내 미결 "
                        f"(진입 {entry:,.0f} → {price:,.0f})"),
            )
        return None
