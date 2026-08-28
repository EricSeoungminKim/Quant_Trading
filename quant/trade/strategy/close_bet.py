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

## 이 전략의 정체 (2026-08-26 소유자 정정 — 원문이 스펙이다)

> "우리 종가배팅은 종가 단타가 아니라 **오후장에 구매해서 다음날 아침 상승갭을
> 미리 예상하고 판단해서 진입**하는거야. 즉 **다음날 아침에 팔아야해**."

즉 "종가에 산다"가 목적이 아니다 — **오후장(15:00~15:20 선정, 소유자 원 스펙)**
에 그날의 마감 강도를 보고 사서, 오버나이트 갭을 기다렸다가 **다음 거래일
아침에 반드시 판다**(익절 +2% / 손절 -1% / 시초 30분 데드라인 정리 — 어느
경로든 아침에 끝난다). 같은 날 한때 진입 창을 15:15~15:19 로 좁혔던 것은
"15:30 단일가에 최대한 가깝게"라는 **잘못 읽은 의도의 과최적화**였고, 이
정정으로 되돌렸다.

## 마감 동시호가 구조 (진입 체결의 하한선)

KRX는 09:00~15:20만 연속 거래고, **15:20~15:30은 동시호가**다(주문만 모았다가
15:30 정각 단일가로 일괄 체결 — Toss `GET /api/v1/market-calendar/KR` 의
`singlePriceAuctionStartTime`=15:20 과 일치. 실측: 2026-08-26).

**같은 날 세션에서 진행 중인 다른 수리와 결이 같다.** `quant/core/session.py`의
`in_continuous_session`(2026-08-26, scalp_1m 프리마켓 오사고 수리)이 세운 원칙을
그대로 따른다: **가격이 실시간으로 발견되지 않는 구간의 "현재가"로 체결을
모델링하면 실재하지 않는 손익이 생긴다.** 그래서 진입 창의 상한은 15:19 이고
`in_continuous_session` 이 이중 방어한다 — 15:20 이후로는 어떤 진입도 없다.
오후장 안이면 어느 사이클이든 판정과 체결이 함께 일어난다(분리하지 않는다 —
분리하면 체결이 동시호가 구간으로 밀려 실재하지 않는 값이 된다).

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
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction
from quant.core.session import in_continuous_session
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.strategy.shell import PureStrategyShell

_KST = ZoneInfo("Asia/Seoul")
_TAG = "CLOSE_BET"

# 진입 판정용 1분봉 조회 폭 — 레거시 `_evaluate_entry`의 리터럴과 같은 값이다
# (당일 세션만 쓰고 여유를 둔다). 순수 구현은 `DataNeeds`로 미리 선언해야 하므로
# 상수로 뽑았다.
_INTERVAL = "1m"
_BARS = 400


class CloseBetStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "KR",
                 id: str = "close_bet", tags_of: dict[str, list[str]] | None = None):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Protocol 호환 — 실제 판정은 KR 전용(아래 가드)
        self.tags_of = tags_of

        # 진입 창: **오후장**(소유자 원 스펙 "15:00~15:20 선정") — 종가 근접이
        # 목적이 아니다(모듈 docstring "이 전략의 정체" 절). 상한 15:19 는
        # 동시호가(15:20) 직전 = 연속 거래로 체결 가능한 마지막 분.
        self.entry_start = dtime(*params.get("entry_start_hhmm", (15, 0)))
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


class CloseBetPureStrategy:
    """`CloseBetStrategy`와 동일한 판단을 하는 순수함수 구현 — 엔진 분리 설계
    Phase A(`docs/superpowers/specs/2026-08-19-engine-separation-design.md`)의
    세 번째 이전 대상. `decide()`는 `ctx`도, 인스턴스 가변 상태도 읽지 않는다.

    **이 전략만 다른 점 — 오버나이트다.** donchian/scalp_1m 은 하루 안에서 열고
    닫는다(세션 롤 = 강제청산). 종가배팅은 정반대로 **오후장에 사서 다음 거래일
    아침에 판다**(모듈 docstring "이 전략의 정체", 2026-08-26 소유자 정정). 그래서
    "세션 경계를 넘어 살아남아야 하는 값"이 실재한다 — entry/stop/target/session.
    아래 표의 3번 행이 그것이고, **그 값들은 `next_state`로 다니지 않는다.**

    ## 가변 상태 전수 조사 → `next_state` 매핑

    | # | 레거시 | 무엇 | 순수 구현에서 | 세션 경계 |
    |---|---|---|---|---|
    | 1 | `self._entered_date: dict[str, date]` | 심볼별 마지막 진입일(하루 1회 게이트) | `next_state["entered_date"]: dict[str, str]` (ISO 문자열 — 프로세스 밖으로 직렬화 가능하게) | **넘을 필요 없음.** 같은 날 안에서만 의미가 있다(날짜가 바뀌면 값이 달라 게이트가 저절로 풀린다) |
    | 2 | `self.last_reject: dict[str, str]` | 진단용 거부 사유 | `next_state["last_reject"]: dict[str, str]` | 넘을 필요 없음(진단) |
    | 3 | `Position.meta["lots"][id]`의 `entry`/`stop`/`target`/`session` | 진입가·방어선·진입일 | **`next_state`가 아니다.** 진입 `Signal.state_update`로 실어 보내면 루프가 **체결 후에** lot 에 쓰고(`loop.py:409-419`), 다음날 껍질이 `StrategySnapshot.lots`로 되돌려준다 | **여기서 넘는다** |

    인스턴스 가변 dict 는 2개뿐이다(1·2번). 3번은 원래도 인스턴스에 없었다 —
    레거시가 `pos.ensure_lot(self.id)`로 브로커 포지션에서 읽던 값이다.

    ## 세션 경계를 넘는 값을 어떻게 다뤘나 (이 이관의 핵심)

    표 3번을 `next_state`에 복사해 넣고 싶은 유혹이 있다. **하지 않았다**, 이유가
    둘이다.

    1. **`next_state`는 오버나이트를 견디지 못한다.** 껍질(`shell.py`)은 그것을
       인스턴스 필드(`self._state`)에 들고만 있다 — 프로세스가 재시작하면 사라진다.
       엔진은 하루 한 번 이상 재시작될 수 있고(핫 리로드·배포·크래시), 이 전략은
       **밤을 넘겨 포지션을 들고 있다**. 진입가·손절선을 거기 두면 아침에 손절
       기준을 잃은 채 포지션만 남는다 — 실제로 돈을 잃는 실패 모드다.
       donchian_pure/scalp_1m_pure 가 "재시작 복구는 범위 밖"이라고 적을 수 있었던
       것은 그들이 **당일 안에 닫히기 때문**이다. 여기서는 같은 변명이 성립하지
       않는다.
    2. **이미 영속 경로가 있다.** `Signal.state_update` → `loop._execute_signal`이
       **체결을 확인한 뒤에만** `Position.meta["lots"][id]`에 적용 → 포지션은
       브로커/상태파일에 영속된다 → 다음날 껍질이 `snap.lots[symbol]`로 넘겨준다.
       레거시가 쓰던 바로 그 경로이고, Phase A 가 아직 못 하는 "체결 후에만 상태
       적용"까지 이미 만족한다. 순수 구현이 그 경로를 그대로 쓰면 동치이면서
       **재시작에도 강하다**.

    즉 이 전략의 상태는 두 갈래로 흐른다 — **하루 안에서만 사는 값은
    `next_state`로(1·2번), 밤을 넘겨야 하는 값은 `state_update`→lot→`snap.lots`로**
    (3번). `tests/test_close_bet_pure.py`가 이 왕복(당일 진입 → 체결 반영 → 익일
    아침 청산)을 세션 경계째로 고정한다.

    ## 왜 `self._legacy` 인스턴스를 들고 있는가

    파라미터 파싱·검증(`ValueError` 조건 3개)·기본값(진입 창 15:00~15:19,
    `min_close_strength=0.7` 등)을 이중으로 유지하면 두 구현이 조용히 갈라진다.
    `Scalp1mPureStrategy` 선례대로 생성자에 위임한다 — `self._legacy.on_cycle`은
    **절대 호출하지 않는다**(설정값 읽기 전용).

    ## 구조적으로 없어지는 버그

    - 레거시는 `self._entered_date[symbol] = today`(문장 A)와 `return Signal(...)`
      (문장 B)이 별개다 — 향후 리팩터링이 둘을 갈라놓으면 "신호는 안 났는데 하루
      1회는 소진" 또는 그 반대가 생긴다. 순수 구현에서는 둘이 같은 `Decision`의
      두 필드로 묶여 있어 그런 경로가 **코드 구조상 존재할 수 없다**.
    - `decide()`는 인자로 받은 `state`의 **사본**만 고쳐 반환한다(원본 dict 를
      in-place mutate 하지 않는다) — 같은 인스턴스를 여러 사이클/스레드에서
      재진입 호출해도 상태가 서로 오염되지 않는다.

    ## 아직 못 하는 것 (정직하게)

    1. **`pos.avg_cost` 폴백이 없다.** 레거시 `_manage`는
       `entry = lot.get("entry") or pos.avg_cost`로, lot 에 `entry`가 없어도
       심볼 합산 평단으로 방어선을 세운다. `StrategySnapshot.lots`는 lot 필드만
       주고 심볼 합산 필드(`avg_cost`)는 주지 않는다(`shell.py`). lot 안에도
       `avg_cost`가 있지만 그건 **lot 단위 평단**이지 레거시가 읽던 값이 아니라서
       쓰지 않았다 — 손절가 계산에 레거시가 보지 않는 값을 몰래 끼워 넣는 것은
       실계좌 전략에서 할 일이 아니다. 결과: `entry`가 없는 lot 은 관리하지 않고
       건너뛴다(donchian_pure/scalp_1m_pure 의 "복구 불가, 건너뛴다"와 같은 경로).
       **정상 경로에서는 발생하지 않는다** — 진입 `Signal.state_update`가 항상
       `entry`를 싣는다.
    2. **`_owns()`의 입양 판정 3단 중 1단만 재현한다.** 레거시는 (a) 내 lot 이
       있으면 내 것, (b) `meta["lots"]` 구조가 있는데 내 lot 이 없으면 남의 것,
       (c) 태그가 아예 없으면 유니버스 소속만으로 입양 — 순으로 판정한다.
       스냅샷은 `pos.lot(self.id)` 결과만 주므로(없으면 `{}`) (a)만 재현되고,
       (c)의 "태그 없는 레거시 포지션 입양"은 불가능하다. `ensure_lot`의
       평평한 meta 이행(migration)도 마찬가지다 — 껍질은 순수 조회 `lot()`만
       쓴다(의도된 설계: 스냅샷 조립이 Position 을 mutate 하면 안 된다).
    3. **고아 포지션을 볼 수 없다.** 레거시 `on_cycle`은
       `ctx.broker.positions()` **전체**를 돌아 `self.symbols`에서 빠진 뒤에도
       남은 보유분까지 관리한다. `DataNeeds`는 정적으로 `self.symbols`만
       선언하므로(껍질이 그 목록으로만 lots 를 채운다) 이 구현은 유니버스에서
       빠진 심볼을 볼 수조차 없다 — 관심종목 기반 전략 공통 문제
       (`Scalp1mPureStrategy` "아직 못 하는 것" 4번과 동일). 오버나이트 전략에서는
       더 아픈 한계다(밤새 유니버스가 갈리면 아침에 청산 주체가 사라진다) —
       실배선 전에 **유니버스 리로드가 보유 심볼을 유지하는지** 확인이 필요하다.
    4. **`KR` 시장 조회는 심볼에서 유도된다.** 레거시는 `ctx.clock`에 무조건
       `"KR"`을 묻는다. 껍질은 `market_of_symbol(symbol)`로 물을 시장을 정하므로
       (`shell.py`, 백테스트 Clock 이 데이터 없는 시장에 예외를 내기 때문),
       `symbols`에 KR 심볼이 하나도 없으면 `snap.market_open`에 `"KR"`이 없어
       모든 판정이 조용히 꺼진다. 이 전략의 진입 후보는 어차피 6자리 KR 코드만
       (`_TAG` + `isdigit()` 게이트)이라 신호 차이는 없지만, KR 심볼이 0개인
       구성에서 US 심볼 보유분을 관리하던 레거시 경로는 사라진다.
    5. **조회 최적화가 사라진다.** 레거시는 태그·창·연속거래 게이트를 전부 통과한
       후보에게만 1분봉을 조회한다. `DataNeeds`는 정적이라 껍질이 매 사이클 전
       심볼의 1분봉을 조회한다 — 데이터 내용은 같으므로 **신호 정확성에는 영향이
       없고**(순수 조회 횟수/지연 회귀), `close_bet_pure`는 아직
       `config/settings.yaml`에 배선돼 있지 않아 운영 영향도 없다.
    6. Phase A 공통 한계: `next_state`(표 1·2번)는 체결 여부와 무관하게 매 사이클
       적용된다(`shell.py` docstring). 즉 risk 거부/미체결이어도 "하루 1회" 게이트는
       소진된다 — **레거시도 완전히 동일**하므로 동치성은 유지된다.
    """

    def __init__(self, symbols: list[str], params: dict, market: str = "KR",
                 id: str = "close_bet_pure", tags_of: dict[str, list[str]] | None = None):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Protocol 호환 — 실제 판정은 KR 전용(레거시와 동일)
        self.tags_of = tags_of

        # 파라미터 파싱/검증/기본값은 레거시에 위임한다(클래스 docstring
        # "왜 self._legacy" 절). on_cycle 은 절대 호출하지 않는다.
        self._legacy = CloseBetStrategy(list(symbols), params, market=market,
                                        id=f"{id}__helper", tags_of=tags_of)
        self.entry_start = self._legacy.entry_start
        self.entry_end = self._legacy.entry_end
        self.min_close_strength = self._legacy.min_close_strength
        self.target_weight = self._legacy.target_weight
        self.stop_pct = self._legacy.stop_pct
        self.take_profit_pct = self._legacy.take_profit_pct
        self.exit_deadline_min = self._legacy.exit_deadline_min

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """레거시가 `_evaluate_entry`에서 조건부로 부르던 조회를 정적으로 선언한다
        (클래스 docstring "아직 못 하는 것" 5번). 인자는 레거시와 동일:
        `history(symbol, "1m", 400)`, `quote(symbol)`, 그리고 보유 관리를 위한
        포지션."""
        return DataNeeds(
            bars=tuple((s, _INTERVAL, _BARS) for s in self.symbols),
            quotes=tuple(self.symbols),
            needs_positions=True,
        )

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        entered_date: dict[str, str] = dict(state.get("entered_date", {}))
        last_reject: dict[str, str] = dict(state.get("last_reject", {}))

        signals: list[Signal] = []
        now_kst = snap.now.astimezone(_KST)
        today = now_kst.date()

        # 1) 보유 관리 — 다음날 시초 창의 익절/손절/데드라인.
        #    레거시는 positions() 전체를 돌지만 여기는 self.symbols 만 본다
        #    (클래스 docstring "아직 못 하는 것" 3번). `snap.lots`에 심볼이 있다는
        #    것 자체가 `pos.is_open`과 동치이고(`strategy_api.py`), 비어 있지 않은
        #    lot 은 `pos.lot(self.id) is not None` = `_owns` 1단과 동치다.
        for symbol in self.symbols:
            lot = snap.lots.get(symbol)
            if not lot:
                continue
            sig = self._manage(symbol, lot, snap, now_kst)
            if sig is not None:
                signals.append(sig)

        # 2) 진입 — 레거시 on_cycle 과 같은 순서/같은 게이트.
        if not snap.market_open.get("KR", False):
            return Decision(signals=tuple(signals),
                            next_state=self._next(entered_date, last_reject))
        if not (self.entry_start <= now_kst.time() <= self.entry_end):
            return Decision(signals=tuple(signals),
                            next_state=self._next(entered_date, last_reject))
        if not in_continuous_session("KR", now_kst):
            # entry_end 설정 실수로 동시호가에 걸쳐도 이중 방어 (모듈 docstring
            # "마감 동시호가 구조" 절).
            return Decision(signals=tuple(signals),
                            next_state=self._next(entered_date, last_reject))

        candidates = [
            s for s in self.symbols
            if s.isdigit() and len(s) == 6  # KR 전용
            and _TAG in (self.tags_of or {}).get(s, [])
        ]
        today_iso = today.isoformat()
        for symbol in candidates:
            if entered_date.get(symbol) == today_iso:
                continue  # 하루 1회
            sig = self._evaluate_entry(symbol, snap, today, last_reject)
            if sig is not None:
                entered_date[symbol] = today_iso
                signals.append(sig)

        return Decision(signals=tuple(signals),
                        next_state=self._next(entered_date, last_reject))

    @staticmethod
    def _next(entered_date: dict[str, str], last_reject: dict[str, str]) -> dict[str, Any]:
        return {"entered_date": entered_date, "last_reject": last_reject}

    # ------------------------------------------------------------------ 진입

    def _evaluate_entry(self, symbol: str, snap: StrategySnapshot, today: dtdate,
                        last_reject: dict[str, str]) -> Signal | None:
        """`CloseBetStrategy._evaluate_entry`와 같은 판정 — 조회만 스냅샷으로,
        `self._entered_date`/`self.last_reject` 쓰기는 호출부/인자로 옮겼다."""
        bars = snap.bars.get((symbol, _INTERVAL))
        if bars is None or len(bars) < 30:
            last_reject[symbol] = "당일 1분봉 부족"
            return None
        day = bars[bars.index.tz_convert(_KST).date == today] if hasattr(
            bars.index, "tz_convert") else bars
        if len(day) < 30:
            last_reject[symbol] = "당일 봉 30개 미만"
            return None

        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            last_reject[symbol] = "현재가 없음"
            return None
        price = quote.price

        day_open = float(day.iloc[0]["open"])
        day_high = float(day["high"].max())
        day_low = float(day["low"].min())
        if day_high <= day_low:
            last_reject[symbol] = "레인지 0"
            return None

        if price <= day_open:
            last_reject[symbol] = f"양봉 아님 (시가 {day_open:,.0f} ≥ 현재가)"
            return None
        strength = (price - day_low) / (day_high - day_low)
        if strength < self.min_close_strength:
            last_reject[symbol] = f"마감 강도 부족 {strength:.2f} < {self.min_close_strength}"
            return None

        stop = price * (1 - self.stop_pct / 100)
        target = price * (1 + self.take_profit_pct / 100)
        return Signal(
            strategy_id=self.id, symbol=symbol, action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(f"종가배팅: 마감강도 {strength:.2f} 양봉 · 내일 시초 갭 노림 "
                    f"(손절 -{self.stop_pct:g}% / 익절 +{self.take_profit_pct:g}%)"),
            stop=stop, target=target,
            # **세션 경계를 넘는 값은 여기로만 나간다** (클래스 docstring
            # "세션 경계를 넘는 값을 어떻게 다뤘나" 절) — 루프가 체결 후에만
            # lot 에 적용하고, 다음날 `snap.lots`로 돌아온다.
            state_update={"entry": price, "stop": stop, "target": target,
                          "session": today.isoformat(), "strategy": self.id},
        )

    # ------------------------------------------------------------------ 보유 관리

    def _manage(self, symbol: str, lot: Mapping[str, Any], snap: StrategySnapshot,
                now_kst) -> Signal | None:
        """`CloseBetStrategy._manage`와 같은 판정 — `pos.ensure_lot(self.id)` 대신
        껍질이 순수 조회로 채운 `snap.lots[symbol]`을 읽는다. `pos.avg_cost`
        폴백은 재현할 수 없다(클래스 docstring "아직 못 하는 것" 1번)."""
        entry = lot.get("entry")
        if not entry:
            return None
        entry_session = lot.get("session")
        today = now_kst.date().isoformat()
        if entry_session == today:
            return None  # 진입 당일은 들고 간다 — 그게 이 전략이다

        # 다음날: 장중에만 판정(시세가 세션 밖 잔가일 수 있다).
        if not snap.market_open.get("KR", False):
            return None
        quote = snap.quotes.get(symbol)
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


class CloseBetPureShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies`가 기존 전략과 같은 방식으로
    (`cls(symbols=..., params=..., market=..., id=..., tags_of=...)`) 생성할 수
    있도록 하는 얇은 팩토리. `CloseBetPureStrategy` + `PureStrategyShell`을
    조립한다.

    `tags_of`를 생성자에서 받는다 — 배선 시 `build_strategies`의
    `_TAGS_OF_CONSUMERS`에도 함께 등재해야 한다
    (`tests/test_tag_assignment.py`가 배정표와 대조한다)."""

    def __init__(self, symbols: list[str], params: dict, market: str = "KR",
                 id: str = "close_bet_pure", tags_of: dict[str, list[str]] | None = None):
        super().__init__(CloseBetPureStrategy(symbols, params, market=market, id=id,
                                              tags_of=tags_of))
