"""외국인 적립 갈래(B) — 외국인 수급 추세 태그를 일 1회 평가해 고정액을 사고,
이탈 신호가 뜨면 보유분을 나눠 판다. 롱 온리, 오버나이트 보유가 전략의 본질.

## 스펙 근거 (2026-08-17 사용자 결정, §5)

`docs/superpowers/specs/2026-08-17-quant-automation-design.md` §5의 확정 사항:
- 배분 B 30% (config/settings.yaml, KR 전용 — 외국인 수급 데이터가 한국
  종목만 다룬다).
- 매수 단위: 1주씩이 아니라 **고정액(100,000 KRW)**씩 — 사용자 원안(1주씩)은
  종목 가격이 제각각인 관심종목에서 종목마다 노출 규모가 수십 배씩 벌어진다.
  고정액이 종목 간 균등 노출을 보장한다.
- 승격 판정 최소 B 20거래일(§4 — `quant/control/ledger.py`의 paper 원장
  거래일 카운트 기준).

## 태그 (다음 워커가 배선한다)

`FRGN`(매수 신호)/`FRGN_EXIT`(이탈 신호)는 이 전략이 기대하는 태그 **이름**만
정의한다 — 실제로 관심종목 파일에 이 태그를 붙이는 배선(own_brief 오후판/
크론 작업의 외국인 수급 추세 판정 → watch-add --tags 경로)은 이번 워커
범위 밖이다. 그때까지는 `tags_of`에 이 태그가 전혀 나타나지 않으므로 이
전략은 조용히 아무 것도 하지 않는다 — 그래서 `config/settings.yaml`에
`enabled: false`로 등록한다. 태그 경로가 완성된 뒤 오케스트레이터가 켠다.

## 판단 빈도: 일 1회

세션마다(시장별) 개장 후 `eval_after_minutes_after_open`분이 지난 첫
사이클에 1회만 평가한다 — `cross_momentum.py`의 주 1회 랭킹과 같은 패턴
(세션 롤 + 게이트 조건)을 쓰되, 요일 대신 "그날 첫 평가 시각 이후"를
게이트로 삼고 매 거래일 평가한다는 점이 다르다. 이 평가 게이트
(`_evaluated_date`)는 프로세스 메모리에만 있어 재시작 시 리셋된다 —
`cross_momentum._last_rebalance_week`와 같은 알려진 한계다(최악의 경우
재시작 당일 평가를 한 번 더 하게 될 뿐, 안전 문제는 아니다).

## 매수 — 고정 수량(주) [2026-08-19 고정액→고정수량 전환]

`FRGN` 태그가 있는 심볼마다 평가 시각마다 `buy_qty`(기본 1주)만큼 산다 —
이미 보유 중이어도 태그가 유지되는 한 계속 산다(이것이 "적립"의 정의:
하루치 판단으로 그치지 않고 태그가 살아있는 동안 계속 편입한다).

**왜 고정액이 아니라 고정 수량인가.** 한국 주식은 정수 주 단위로만 살 수
있다 — "일정 금액만 사는" 것 자체가 불가능하다(사용자 원안이 애초에
1주씩이었던 이유). 이전 구현("고정액 사이징의 근사")은 `Signal`이 수량이
아니라 목표 **비중**만 표현할 수 있다는 제약 때문에 `fixed_amount_krw /
assumed_capital_krw`로 비중을 근사했는데, 2026-08-19 전략별 독립 명목계정
도입으로 전략 자본이 500만 → 1,000만원으로 바뀌면서 `assumed_capital_krw`가
낡은 값(500만)에 고정된 채 남아 실제 매수액이 의도(10만원)의 2배(20만원)로
튀는 사고가 났다(001450: 4주@50,013=200,050원, 실측). 자본 규모가 바뀌면
비중 근사가 통째로 어긋나는 구조적 취약점이었다.

이제는 `Signal.target_qty`(core 확장, 2026-08-19)로 **수량 자체**를
risk 레이어에 전달한다 — 전략 자본 규모를 추정할 필요가 아예 없어졌다.
risk 레이어는 `target_qty`를 우선 예산으로 쓰되 하드레일(max_position_pct/
max_symbol_pct_total/max_order_notional_pct/전략별 현금 게이트)은 비중
기반 사이징과 동일하게 적용한다 — 상한에 걸리면 수량이 줄거나(로그:
`RiskManagerImpl.last_block`) 주문 자체가 거부된다(`quant/trade/risk/
manager.py` `_approve_entry_per_strategy`/`approve` 참고).

`fixed_amount_krw`/`assumed_capital_krw`는 더 이상 쓰이지 않아 제거했다 —
비중 근사 계산 자체가 없어졌으므로 남겨둬도 죽은 설정일 뿐이고, 죽은
설정이 그대로 있으면 "여전히 이 값이 매수액을 결정한다"는 오해가
재발한다(이번 사고가 정확히 그 오해에서 시작됐다). `config/settings.yaml`의
해당 항목도 `buy_qty`로 교체한다.

## 청산 — 이탈 태그 2회 연속이면 전량, 아니면 절반

`FRGN_EXIT` 태그가 뜬 평가일에 보유의 `exit_fraction_first`(기본 50%)를
판다(SCALE_OUT). **다음 평가일에도 연속으로** `FRGN_EXIT`이면 잔량을 전부
청산한다(EXIT_LONG, exit_fraction=1.0) — "연속 2일 이탈이면 전량 소진"
(사용자 시나리오). 태그가 중간에 사라지면(중립 — 매수도 이탈도 아님)
**연속이 끊긴다**: 보유는 그대로 유지하고("잔여 관망" — 이탈 신호가 없어진
것은 청산 신호가 아니다), 다음에 다시 `FRGN_EXIT`이 뜨면 처음부터(절반)
다시 센다. 이 연속 카운터(`_exit_streak`)도 재시작 시 리셋되는 알려진
한계를 갖는다(재시작 직후 이탈 판정이 한 단계 늦어질 뿐 안전 문제는 아님).

## 오버나이트 · 손절 없음

세션 롤 강제청산·EoD 강제청산 레일이 **없다** — 다른 전략과 반대로
오버나이트 보유가 이 전략의 정의 자체다(적립). 가격 기반 손절도 없다 —
태그 기반 판단만으로 진입/청산한다(사용자 시나리오에 가격 기반 손절이
명시되지 않았고, 스펙 방침대로 추가 규칙을 발명하지 않는다).
"""
from __future__ import annotations

from datetime import date as dtdate
from typing import Any, Mapping

from quant.core.ports import Context
from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.strategy.orb_scan import _SESSION_OPEN
from quant.trade.strategy.shell import PureStrategyShell

_FRGN_TAG = "FRGN"
_FRGN_EXIT_TAG = "FRGN_EXIT"


class FrgnAccumulateStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "frgn_accumulate",
                 tags_of: dict[str, list[str]] | None = None):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        # {symbol: [태그, ...]}. None/빈 dict면(테스트·백테스트·미배선) 매수/이탈
        # 대상이 하나도 없어 판정 자체를 건너뛴다.
        self.tags_of = tags_of

        # 고정 매수 수량(주) — 모듈 docstring "매수 — 고정 수량(주)" 참고.
        # 2026-08-19: fixed_amount_krw/assumed_capital_krw(고정액 근사)를 대체.
        self.buy_qty: float = params.get("buy_qty", 1)
        self.eval_after_minutes_after_open: int = params.get("eval_after_minutes_after_open", 60)
        self.exit_fraction_first: float = params.get("exit_fraction_first", 0.5)

        if self.buy_qty <= 0 or self.buy_qty != int(self.buy_qty):
            raise ValueError("buy_qty는 양의 정수여야 합니다.")
        if not 0 < self.exit_fraction_first < 1:
            raise ValueError("exit_fraction_first는 0과 1 사이여야 합니다.")

        self._evaluated_date: dict[str, dtdate] = {}
        self._exit_streak: dict[str, int] = {}
        self.last_reject: dict[str, str] = {}

    # ------------------------------------------------------------------ 사이클

    def on_cycle(self, ctx: Context) -> list[Signal]:
        signals: list[Signal] = []
        if not self.tags_of:
            return signals
        positions = ctx.broker.positions()

        markets_present = sorted({market_of_symbol(s) for s in self.symbols})
        for market in markets_present:
            if not ctx.clock.is_market_open(market):
                continue
            tz, session_open = _SESSION_OPEN[market]
            now_local = ctx.clock.now().astimezone(tz)
            today = now_local.date()
            if self._evaluated_date.get(market) == today:
                continue  # 오늘 이미 평가함 — 일 1회
            minutes_since_open = (
                now_local.hour * 60 + now_local.minute
                - (session_open.hour * 60 + session_open.minute)
            )
            if minutes_since_open < self.eval_after_minutes_after_open:
                continue  # 지정 평가 시각 전
            self._evaluated_date[market] = today
            signals.extend(self._evaluate(market, ctx, positions))

        return signals

    def _evaluate(self, market: str, ctx: Context, positions: dict) -> list[Signal]:
        signals: list[Signal] = []
        for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
            tags = self.tags_of.get(symbol, [])
            pos = positions.get(symbol)
            held_qty = pos.lot_qty(self.id) if pos is not None else 0.0

            if _FRGN_EXIT_TAG in tags:
                signal = self._check_exit_for(symbol, held_qty)
                if signal is not None:
                    signals.append(signal)
            elif _FRGN_TAG in tags:
                # 매수 재개 — 이전 이탈 연속 카운터를 끊는다.
                self._exit_streak[symbol] = 0
                signal = self._check_buy_for(symbol, ctx)
                if signal is not None:
                    signals.append(signal)
            else:
                # 중립 — 매수도 이탈도 아님. 보유 유지("잔여 관망"), 연속 카운터만 끊는다.
                self._exit_streak[symbol] = 0
        return signals

    # ------------------------------------------------------------------ 매수

    def _check_buy_for(self, symbol: str, ctx: Context) -> Signal | None:
        self.last_reject.pop(symbol, None)
        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            self.last_reject[symbol] = "현재가 없음"
            return None
        price = quote.price
        # target_weight는 target_qty를 쓸 때 risk 레이어가 참조하지 않는 자리표시자.
        # 자본 규모와 무관하게 항상 buy_qty주를 요청한다 — 하드레일에 걸리면
        # risk.manager가 수량을 줄이거나 거부하고 사유를 남긴다(last_block).
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=0.0,
            target_qty=self.buy_qty,
            reason=f"외국인 적립 매수(FRGN): {symbol} {self.buy_qty:g}주 @ {price:,.0f}",
        )

    # ------------------------------------------------------------------ 청산

    def _check_exit_for(self, symbol: str, held_qty: float) -> Signal | None:
        if held_qty <= 0:
            self._exit_streak[symbol] = 0  # 보유가 없으면 연속 카운터도 의미 없음
            return None
        streak = self._exit_streak.get(symbol, 0)
        if streak == 0:
            self._exit_streak[symbol] = 1
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.SCALE_OUT,
                target_weight=0.0, exit_fraction=self.exit_fraction_first,
                reason=(
                    f"외국인 이탈(FRGN_EXIT) 1일차: {symbol} 보유 "
                    f"{self.exit_fraction_first * 100:.0f}% 청산"
                ),
            )
        self._exit_streak[symbol] = 0
        return Signal(
            strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
            target_weight=0.0, exit_fraction=1.0,
            reason=f"외국인 이탈(FRGN_EXIT) 연속 2일차: {symbol} 잔량 전량 청산",
        )


class FrgnAccumulatePureStrategy:
    """`FrgnAccumulateStrategy`와 동일한 판단을 하는 순수함수 구현 — 엔진 분리
    설계 Phase A(`docs/superpowers/specs/2026-08-19-engine-separation-design.md`)
    세 번째 이전 대상. `donchian_pure`/`scalp_1m_pure`와 같은 원칙: `decide()`는
    `ctx`도 인스턴스 가변 상태도 읽지 않고, 평가 게이트와 이탈 연속 카운터는
    전부 `state`↔`next_state`로만 다닌다.

    ## 외국인 수급 데이터 의존을 어떻게 다루는가 — **(b) 이번 범위 밖, 정직하게 기록**

    이 전략에는 **파일 I/O 가 없다**(추측이 아니라 코드 전수 확인). 외국인 수급
    원장 `data/ledger/frgn_flow.jsonl` 을 읽고 쓰는 코드는 전부 다른 평면에 있다:
    `quant/control/frgn_flow.py`(원장 append/조회)와
    `quant/backtest/report_replay.py`(백테스트 재생 시 원장 채우기). 거래 평면의
    이 전략이 보는 것은 그 수급 판정의 **결과물인 태그**(`FRGN`/`FRGN_EXIT`)뿐이고,
    그 태그는 `build_strategies(... tags_of=...)`(`quant/trade/strategy/__init__.py`
    의 `_TAGS_OF_CONSUMERS`)가 **생성자로 주입**한다 — 원천은
    `quant/apps/assembly.py`가 세션 롤마다 새로 읽는 관심종목 파일 스냅샷
    (`FileWatchlistUniverse.tags()`)이다. 즉 수급 데이터는 이미 "껍질(조립부)이
    읽어서 넣는" 구조이고, 전략은 `symbols`/`params`와 동일한 **생성 시점 불변
    설정**으로 받는다. `decide()`가 같은 `(snap, state)`에 대해 같은 `Decision`을
    돌려준다는 순수 계약은 그대로 성립한다.

    그래서 (a) `StrategySnapshot` 승격은 **하지 않았다**. 설계 스펙 원안에는
    `tags: Mapping[str, tuple[str, ...]]` 필드가 있었지만, 껍질
    (`PureStrategyShell._snapshot`)이 태그를 채우려면 `Context`(`quant/core/ports.py`)
    자체에 태그 출처가 있어야 한다 — 지금 `Context`는 `clock`/`data`/`broker`
    셋뿐이다. 그러려면 core 포트 + 껍질 + `quant/apps/assembly.py` 배선을 한꺼번에
    바꿔야 하고, 그건 이 전략 하나의 이관이 아니라 **계약 전체의 변경**이라
    (다른 전략·다른 워커의 파일을 건드린다) 이번 범위 밖이다. 승격이 필요해지는
    시점은 "태그가 사이클 중간에 바뀌어야 할 때"인데, 현재 운영은 세션 롤마다
    전략 인스턴스를 새로 만들어(`quant/trade/loop.py` 1435행 주석: symbols/tags_of
    는 이월하지 않는다) 그 요구가 없다.

    ## 가변 상태 전수 조사 → `next_state` 매핑

    | 레거시 인스턴스 가변 상태 | 타입 | `next_state` 키 | 비고 |
    |---|---|---|---|
    | `_evaluated_date` | `dict[market, date]` | `"evaluated_date"` | 일 1회 평가 게이트 |
    | `_exit_streak` | `dict[symbol, int]` | `"exit_streak"` | `FRGN_EXIT` 연속 카운터 |
    | `last_reject` | `dict[symbol, str]` | **(없음)** | 진단 전용 — 프로덕션 소비처 0건(grep: `tests/`만 읽는다). `scalp_1m_pure` 선례대로 순수 계약에 싣지 않는다 |

    생성 시점 불변(가변 상태 아님, 그대로 인스턴스 속성): `id`, `symbols`,
    `market`, `tags_of`, `buy_qty`, `eval_after_minutes_after_open`,
    `exit_fraction_first`.

    ## 왜 레거시 인스턴스(`self._legacy`)를 들고 있는가

    파라미터 파싱·검증(`buy_qty` 양의 정수, `exit_fraction_first` 0<x<1)만
    위임한다 — `scalp_1m_pure`와 같은 이유로, 이중 유지하면 두 구현의 검증 규칙이
    조용히 갈라진다. 판정 로직 자체는 40줄 남짓이라 여기서 직접 쓴다(레거시
    헬퍼는 `self.last_reject`·`self._exit_streak`를 인스턴스에 직접 쓰므로 순수
    재사용이 불가능하다 — `scalp_1m`의 패턴 판정 헬퍼들이 순수했던 것과 다르다).
    `self._legacy.on_cycle()`은 **절대 호출하지 않는다**.

    ## 구조적으로 없어지는 버그

    레거시 `_check_exit_for`는 신호를 만들면서 `self._exit_streak`를 **그 자리에서**
    올린다. 그래서 같은 사이클을 두 번 흘리면(드라이런·리플레이·테스트 재호출)
    같은 하루에 스트릭이 0→1→전량청산까지 진행돼 버린다 — 관측 행위가 상태를
    바꾼다. 순수 구현은 인자로 받은 `state`의 **사본**만 고쳐 `Decision`에
    담아 돌려주므로, 같은 `(snap, state)`로 `decide()`를 몇 번 부르든 결과가
    같고 원본 상태는 오염되지 않는다. 신호와 상태 변경이 한 반환값의 두 필드로
    묶여 있어 "상태만 바뀌고 신호는 안 나가는" 경로도 구조상 존재할 수 없다.

    ## 아직 못 하는 것 (정직하게)

    1. `next_state`는 체결 확인과 무관하게 매 사이클 적용된다(Phase A 공통 한계,
       `shell.py` 참고). risk 거부/미체결에도 "오늘 평가했다"와 이탈 스트릭은
       되돌릴 수 없다 — **레거시도 동일해 동치성은 유지된다**.
    2. 재시작 시 상태 유실(오늘 평가를 한 번 더 하거나 이탈 판정이 한 단계
       늦어짐)은 레거시 모듈 docstring이 이미 인정한 한계 그대로다. 다만 이제
       상태가 `next_state`로 외부화돼 있어, 껍질/루프가 영속화하기로 하면 이
       전략 코드를 고치지 않고 해결할 수 있는 자리로 바뀌었다.
    3. 조회 회귀: `DataNeeds`는 정적 선언이라 껍질이 **매 사이클 전 심볼의
       현재가**를 조회한다(레거시는 하루 1회, `FRGN` 태그가 붙은 심볼만
       조회했다). 신호 정확성에는 영향이 없지만(같은 값) 순수한 조회 횟수
       회귀다 — `scalp_1m_pure`의 같은 한계와 동일한 성질이고, 이 전략도
       `config/settings.yaml`에 아직 배선돼 있지 않아 당장의 운영 영향은 없다.
    4. `snap.lots`는 껍질이 `pos.is_open`(심볼 합산 qty>0)일 때만 채운다.
       레거시는 `positions.get(symbol).lot_qty(self.id)`를 `is_open` 게이트 없이
       읽으므로, "심볼 합산 qty=0인데 내 lot qty>0"인 모순 상태에서만 두 구현이
       갈린다(실제로는 lot 합이 곧 심볼 qty라 발생하지 않는다).
    """

    def __init__(self, symbols: list[str], params: dict, market: str = "US",
                 id: str = "frgn_accumulate_pure",
                 tags_of: dict[str, list[str]] | None = None):
        self.id = id
        self.symbols = list(symbols)
        self.market = market
        self.tags_of = tags_of

        # 파라미터 파싱/검증만 레거시에 위임(클래스 docstring 참고).
        # on_cycle은 절대 호출하지 않는다.
        self._legacy = FrgnAccumulateStrategy(
            list(symbols), params, market=market, id=f"{id}__helper", tags_of=tags_of,
        )
        self.buy_qty = self._legacy.buy_qty
        self.eval_after_minutes_after_open = self._legacy.eval_after_minutes_after_open
        self.exit_fraction_first = self._legacy.exit_fraction_first

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """봉은 쓰지 않는다(이 전략은 가격 지표를 보지 않는다 — 태그 판정만).
        현재가는 매수 신호의 사유 문구와 "현재가 없음" 게이트에 필요하고,
        보유 수량(`lots`)은 이탈 판정에 필요하다."""
        return DataNeeds(quotes=tuple(self.symbols), needs_positions=True)

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        evaluated_date: dict[str, dtdate] = dict(state.get("evaluated_date", {}))
        exit_streak: dict[str, int] = dict(state.get("exit_streak", {}))

        signals: list[Signal] = []
        if not self.tags_of:
            # 레거시 on_cycle 첫 줄과 동치 — 태그 배선 전에는 아무 것도 하지 않는다.
            return Decision(
                signals=(),
                next_state={"evaluated_date": evaluated_date, "exit_streak": exit_streak},
            )

        markets_present = sorted({market_of_symbol(s) for s in self.symbols})
        for market in markets_present:
            if not snap.market_open.get(market, False):
                continue
            tz, session_open = _SESSION_OPEN[market]
            now_local = snap.now.astimezone(tz)
            today = now_local.date()
            if evaluated_date.get(market) == today:
                continue  # 오늘 이미 평가함 — 일 1회
            minutes_since_open = (
                now_local.hour * 60 + now_local.minute
                - (session_open.hour * 60 + session_open.minute)
            )
            if minutes_since_open < self.eval_after_minutes_after_open:
                continue  # 지정 평가 시각 전
            evaluated_date[market] = today
            signals.extend(self._evaluate(market, snap, exit_streak))

        return Decision(
            signals=tuple(signals),
            next_state={"evaluated_date": evaluated_date, "exit_streak": exit_streak},
        )

    # ------------------------------------------------------------------ 평가

    def _evaluate(
        self, market: str, snap: StrategySnapshot, exit_streak: dict[str, int]
    ) -> list[Signal]:
        """레거시 `_evaluate`와 같은 순서·같은 분기. `exit_streak`는 `decide()`가
        만든 **사본**이므로 여기서 고쳐도 호출자의 원본 state는 오염되지 않는다."""
        signals: list[Signal] = []
        for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
            tags = self.tags_of.get(symbol, [])
            lot = snap.lots.get(symbol)
            held_qty = float(lot.get("qty", 0.0)) if lot else 0.0

            if _FRGN_EXIT_TAG in tags:
                signal = self._check_exit_for(symbol, held_qty, exit_streak)
                if signal is not None:
                    signals.append(signal)
            elif _FRGN_TAG in tags:
                # 매수 재개 — 이전 이탈 연속 카운터를 끊는다.
                exit_streak[symbol] = 0
                signal = self._check_buy_for(symbol, snap)
                if signal is not None:
                    signals.append(signal)
            else:
                # 중립 — 보유 유지("잔여 관망"), 연속 카운터만 끊는다.
                exit_streak[symbol] = 0
        return signals

    def _check_buy_for(self, symbol: str, snap: StrategySnapshot) -> Signal | None:
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None  # 현재가 없음(레거시 last_reject — 클래스 docstring 매핑 표)
        price = quote.price
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=0.0,
            target_qty=self.buy_qty,
            reason=f"외국인 적립 매수(FRGN): {symbol} {self.buy_qty:g}주 @ {price:,.0f}",
        )

    def _check_exit_for(
        self, symbol: str, held_qty: float, exit_streak: dict[str, int]
    ) -> Signal | None:
        if held_qty <= 0:
            exit_streak[symbol] = 0  # 보유가 없으면 연속 카운터도 의미 없음
            return None
        streak = exit_streak.get(symbol, 0)
        if streak == 0:
            exit_streak[symbol] = 1
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.SCALE_OUT,
                target_weight=0.0, exit_fraction=self.exit_fraction_first,
                reason=(
                    f"외국인 이탈(FRGN_EXIT) 1일차: {symbol} 보유 "
                    f"{self.exit_fraction_first * 100:.0f}% 청산"
                ),
            )
        exit_streak[symbol] = 0
        return Signal(
            strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
            target_weight=0.0, exit_fraction=1.0,
            reason=f"외국인 이탈(FRGN_EXIT) 연속 2일차: {symbol} 잔량 전량 청산",
        )


class FrgnAccumulatePureShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies`가 기존 전략과 같은 방식으로 생성할
    수 있게 하는 얇은 팩토리 — `donchian.py`의 `DonchianPureShell`,
    `scalp_1m.py`의 `Scalp1mPureShell`과 동일 패턴. 이 전략은 태그 소비자라
    `tags_of` kwarg를 추가로 받아 안쪽 순수 구현에 그대로 넘긴다(레지스트리
    배선 시 `__init__.py`의 `_TAGS_OF_CONSUMERS`에 함께 등록해야 한다)."""

    def __init__(self, symbols: list[str], params: dict, market: str = "US",
                 id: str = "frgn_accumulate_pure",
                 tags_of: dict[str, list[str]] | None = None):
        super().__init__(
            FrgnAccumulatePureStrategy(symbols, params, market=market, id=id, tags_of=tags_of)
        )
