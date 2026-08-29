"""LLM 트레이더(llm_trader) — 12번째 전략, LLM 판단 실험 레인.

## 소유자 승인 (2026-08-30)

"LLM 자체에게 전략과 판단을 맡기는 게 하나의 전략이 되는 것, 똑같이 1,000만원
모의, 기존 시스템 위에, 한 달 테스트." 다른 11개 전략은 사람이 정한 규칙(돌파·
평균회귀·모멘텀 등)을 코드로 굳힌 것이고, 이 전략은 그 규칙 자체를 LLM 판단에
맡기는 실험이다. 저장소 헌법(루트 CLAUDE.md "절대 하지 말 것")의 "거래 핫패스에
LLM/네트워크 호출 금지"는 그대로 지킨다 — **LLM 호출은 이 파일에도, `quant/trade/`
어디에도 없다.**

## 아키텍처 — LLM은 엔진 밖, 판단은 파일로만 들어온다

별도 프로세스(다른 워커가 구축)가 LLM에게 판단을 묻고, 그 결과를 **주문 인박스**
`data/state/llm_trader_inbox.jsonl`에 한 줄씩 append한다 — `tg_bridge.py`가
`data/watchlist.yaml`을 쓰고 엔진이 `FileWatchlistUniverse`로 읽기만 하는 것과
같은 패턴(수집/판단 레이어가 파일에 쓰고, 거래 레이어는 파일을 읽기만 한다).
행 하나 = 결정 하나:

    {"id": "고유문자열", "ts": "2026-08-30T09:05:00+09:00",
     "action": "buy" | "sell", "symbol": "005930",
     "weight": 0.0~1.0 (buy 시 목표 비중) | null (sell은 항상 전량),
     "horizon": "단타" | "스윙" | "장기",
     "reason": "판단 근거 한 줄"}

`horizon`은 2026-08-30 소유자 추가 지시 — LLM이 이 매매를 어떤 시계로 판단했는지
(하루 안/며칠~몇 주/몇 달 이상)를 원장과 텔레그램 알림에 남기기 위한 **필수** 필드다.
셋 중 하나가 아니면 거부한다(아래 가드레일 2번) — 자유 서술을 허용하면 표시·집계가
흔들린다. `Signal.reason` 앞에 `"[단타] "` 식으로 붙어 원장/텔레그램에 그대로
노출되고, 매수 체결분은 `Signal.state_update`로 `Position.meta["lots"][id]
["horizon"]`에도 남는다(사후 분석 — "LLM이 단타라고 한 매매가 실제로 단타로
끝났는가"를 나중에 lots/원장과 대조할 수 있게).

이 파일의 `LlmTraderStrategy`는 인박스를 **읽기만** 한다 — 실제 파일 I/O는
`quant/apps/assembly.py`(composition root)가 `inbox_reader` 콜러블로 주입한다
(아래 "왜 콜러블로 주입하나" 절). 이 파일 자체는 `quant/trade/strategy/CLAUDE.md`가
금지하는 `quant.adapters.*`/네트워크 임포트를 하지 않는다 — `open()`도, `Path`도
쓰지 않는다.

## 왜 `PureStrategy` 계약이 아니라 레거시 `Strategy` 프로토콜인가

이 저장소의 최근 신규 전략 다수(vol_breakout·rsi2_dip 등)는
`quant.core.strategy_api.PureStrategy`(`decide(snap, state) -> Decision`) +
`PureStrategyShell`을 쓴다. llm_trader는 의도적으로 **레거시 `Strategy` 프로토콜**
(`on_cycle(ctx) -> list[Signal]`, `quant/trade/strategy/CLAUDE.md`의 기본 레시피이자
`quant.core.ports.Strategy`)을 따른다 — 이유는 구조적이다:

`PureStrategyShell`은 전략의 `requirements()`를 **생성자 시점에 딱 한 번만** 불러
`DataNeeds`(조회할 심볼의 정적 목록)를 고정한다(`shell.py.__init__`). 그런데
llm_trader가 살 종목은 **인박스가 사이클마다 새로 알려준다** — 조립 시점
(`symbols: []`, 인박스가 유니버스를 대신한다)에는 어떤 KR 종목이 올지 알 수
없으므로, 정적 `DataNeeds`로 필요한 시세를 미리 선언하는 방식 자체가 이 전략의
전제와 맞지 않는다. 레거시 프로토콜은 `ctx.data.quote(symbol)`을 사이클 안에서
임의 심볼에 바로 물을 수 있어 이 문제가 구조적으로 없다.

부작용 하나: `PureStrategyShell`의 자동 거부 로깅(`_log_rejects`,
`next_state["last_reject"]`)을 못 쓴다. 대신 이 파일이 직접 `self.last_reject`
(symbol → 사유)에 쌓고, 사유가 바뀐 심볼만 즉시 `logger.info`로 남긴다(`_reject`
메서드) — 셸의 거부 로그와 사용자에게 보이는 결과(거부 사유가 로그에 남는다)는
같다, 구현 경로만 다르다.

## 가드레일 — "프롬프트를 못 믿는 게 전제"

엔진은 인박스 행을 신뢰하지 않는다. 매 주문을 아래 순서로 검증하고, 하나라도
걸리면 그 자리에서 거부한다(사유는 `self.last_reject[symbol]`에 남는다):

1. **KR 심볼(6자리 숫자)만.** 이 레인은 KR 전용이다(`capital_fraction: {KR: 1.0,
   US: 0.0}`, `quant.core.models.market_of_symbol`과 동일 판정). 그 외는 전부 거부.
2. **`horizon`이 "단타"/"스윙"/"장기" 중 하나여야 한다**(2026-08-30 소유자 추가
   지시). 그 외 값·누락은 거부.
3. **시장이 열려 있고 연속거래 구간(`in_continuous_session`)일 때만.** 동시호가
   구간에 도착한 주문은 그 사이클에 거부되고 재시도되지 않는다(아래 "아직 못
   하는 것" 참고) — LLM이 다시 판단해 새 id로 주문을 내야 한다.
4. **동시 보유 `max_positions`(기본 5) 초과 매수 거부.**
5. **같은 심볼 중복 매수 거부** — 이미 내 랏이 열려 있으면 buy를 무시한다(추가
   매수 개념 없음 — LLM이 비중을 늘리고 싶으면 먼저 sell 후 재매수해야 한다.
   단순함 우선, `CLAUDE.md` §2).
6. **weight 상한** — 요청 비중과 `max_weight_per_position`(기본 0.34) 중 작은 값.
   `weight`가 없거나 (0, 1] 범위를 벗어나면 거부(0/null은 "비중 없음"으로 지어내지
   않는다).

**유니버스 제한은 의도적으로 없다**(2026-08-30 소유자 확인: "리포트 외 종목도
웹서치로 추가 가능"). 위 목록에 "관심종목(watchlist) 소속 여부"나 "회사 리포트
편입 여부" 검사가 **없는 것은 누락이 아니라 설계다** — LLM이 이 저장소의 관심종목
파이프라인 밖에서 발굴한 KR 종목도 그대로 받는다. 검증은 위 여섯 항목(형식·
horizon·세션·보유수·중복·비중)뿐이고, 그중 어떤 것도 종목의 출처를 묻지 않는다.

## 방어선 — LLM이 손절을 안 정하니 엔진이 하드레일을 깐다

매수 체결 시 `stop = 진입가 × (1 - stop_pct/100)`(기본 5%)을 `Signal.state_update`
로 실어 보낸다 — `quant/trade/loop.py`의 `_execute_signal`이 **체결 확인 후에만**
`Position.meta["lots"][id]`에 적용한다(이 저장소 공통 계약, `거부/미체결 시 상태
오염 없음`). 이후 사이클마다 **하드 손절만** 자동으로 본다(현재가 ≤ stop → 즉시
전량 청산, `in_continuous_session`일 때만 — `rsi2_dip._tradable`과 같은 게이트).
**그 밖의 보유 관리는 하지 않는다**:

- **세션 마감 강제청산이 없다.** 오버나이트를 허용한다 — "LLM이 청산도 스스로
  판단하는 실험"이 설계 의도이기 때문(소유자 승인, 위 절). 마감 전 강제청산
  판정 호출이 이 파일에 없으므로 `quant/trade/loop.py`의
  `_OVERNIGHT_STRATEGIES`에 반드시 등재해야 한다 — `tests/
  test_position_report_wording.py`가 소스에서 그 호출이 없는 모듈을 자동
  대조하므로, 등재를 빼먹으면 그 테스트가 즉시 실패한다(회귀 가드).
- 목표가·시간 청산이 없다. 청산은 인박스의 `sell` 행 또는 하드 손절, 둘뿐이다.
- **매도는 LLM 자율이다**(2026-08-30 소유자 확인). 보유기간 상한도, 익절 목표도
  엔진이 부과하지 않는다 — 엔진이 지키는 것은 `stop_pct` 하드 손절 하나뿐이고,
  그 위의 모든 매도 판단(언제·왜 파는가)은 인박스의 `sell` 행이 유일한 경로다.

## 상태 — "당일" 필터가 실질적 재시작 방어선이다

`_consumed_ids`는 **인스턴스 필드**(프로세스 재시작 시 소실)다. "재시작 시 과거
주문을 재실행하지 않는다"를 실제로 보장하는 것은 이게 아니라 **ts 필터**다 —
인박스 행의 `ts`가 속한 거래일(`quant.core.models.trading_day` — KST 08:00 경계,
이 저장소 전역의 "오늘" 정의, 회로차단기·일일 주문 상한과 같은 기준)이 지금
거래일과 다르면 **소비 여부와 무관하게 무조건 무시**한다. 이 판정은 상태가 없어
매 사이클 다시 계산해도 같은 결과가 나오므로, 재시작으로 `_consumed_ids`가
비어도 어제 이전 주문은 영원히 걸러진다.

`_consumed_ids`는 그 위에 얹힌 **하루 안에서의** 중복 처리 방지 장치일 뿐이다 —
재시작으로 소실돼도 최악의 경우는 "같은 거래일 안에서 이미 평가한 주문을 다시
평가한다"이고, 그 결과는 구조적으로 대부분 안전하다:

- 매수 재평가: 이미 랏이 열려 있으면 "같은 심볼 중복 매수 거부"에 걸린다.
- 매도 재평가: 이미 청산됐으면(랏 없음) "보유 없음 — 매도 거부"에 걸린다.

하드 손절로 청산된 직후 같은 매수 id가 재시작으로 재평가되는 경우만 실질적
허점인데(청산 직후 재평가 시 "이미 보유 중" 가드가 더 이상 걸리지 않는다), 이는
"재시작이 정확히 손절과 다음 사이클 사이의 좁은 창에 겹쳐야" 하는 데다, 재평가
결과도 LLM이 애초에 낸 정당한 매수 의도의 재실행이라 자본 안전상 치명적이지
않다 — 지어내지 않고 정직하게 남겨 둔다.

## 아직 못 하는 것 (정직하게)

1. **우리 원장 실측이 없다.** 신규 실험 레인 — `validation.status: burn_in`.
2. **동시호가 중 도착한 주문은 재시도하지 않는다.** 그 사이클에 거부되면 끝 —
   LLM이 다음 판단 때 새 id로 다시 내야 한다.
3. **`self.symbols`는 항상 빈 리스트다.** `universe: watchlist` opt-in이 아니라
   인박스가 유니버스를 대신하므로(`quant/apps/assembly.py`의
   `rebuild_strategies`가 심볼을 갈아끼우는 대상이 아니다) — `Strategy` Protocol이
   `symbols: list[str]`를 요구해서 존재하는 자리표시자일 뿐이다. 포지션 관리는
   `ctx.broker.positions()`에서 **내 랏이 있는 심볼**을 순회하는 것만으로
   이뤄지므로(정적 유니버스와 무관), orb_scan류의 "유니버스에서 빠진 보유 종목"
   문제가 애초에 없다.
4. **weight가 없는 buy는 거부한다.** 스펙이 "buy 시 목표 비중"을 요구하므로
   비중 없는 매수는 잘못된 행으로 본다.
"""
from __future__ import annotations

import logging
from datetime import date as dtdate, datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from quant.core.models import Position, Signal, SignalAction, trading_day
from quant.core.ports import Context
from quant.core.session import in_continuous_session

logger = logging.getLogger(__name__)

DEFAULT_MAX_POSITIONS = 5
DEFAULT_MAX_WEIGHT_PER_POSITION = 0.34
DEFAULT_STOP_PCT = 5.0

_KR_MARKET = "KR"

# horizon 허용값(2026-08-30 소유자 추가 지시) — 자유 서술이 아니라 이 3값 중
# 하나만 받는다(모듈 docstring "인박스 스키마" 절).
_VALID_HORIZONS = frozenset({"단타", "스윙", "장기"})


def _parse_ts(raw: object) -> datetime | None:
    """인박스 행의 `ts`를 파싱한다. 실패하면 None(호출부가 "무시" 처리).

    naive datetime(타임존 정보 없음)은 UTC로 가정한다 —
    `quant/collect/collector.py`의 `_local_hhmm`과 같은 관례. 쓰는 쪽(판단
    스크립트)이 tz-aware ISO(`+09:00` 등)를 쓰는 것이 정상 경로다."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_kr_symbol(symbol: object) -> bool:
    return isinstance(symbol, str) and symbol.isdigit() and len(symbol) == 6


class LlmTraderStrategy:
    """LLM 판단 인박스를 Signal로 변환한다 — `quant.core.ports.Strategy` 구현.

    설계 근거·가드레일·상태 규칙은 전부 모듈 docstring에 있다. `inbox_reader`는
    `() -> Iterable[Mapping[str, Any]]` 콜러블 — 실제 파일 I/O는 조립
    (`quant/apps/assembly.py`)이 주입하고, 이 클래스는 콜러블만 안다(테스트에서
    `lambda: [...]`로 바로 대체 가능). `None`이면 항상 빈 목록을 돌려주는
    콜러블로 대체한다(아직 아무 판단도 안 들어오지 않은 정상 상태).
    """

    def __init__(
        self, symbols: list[str], params: dict, market: str = _KR_MARKET,
        id: str = "llm_trader",
        inbox_reader: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
    ) -> None:
        self.id = id
        # 자리표시자 — 모듈 docstring "아직 못 하는 것" 3번. settings.yaml은 항상
        # symbols: []다(인박스가 유니버스를 대신한다).
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 판정은 항상 KR 고정
        self.inbox_reader = inbox_reader or (lambda: [])

        self.max_positions: int = int(params.get("max_positions", DEFAULT_MAX_POSITIONS))
        self.max_weight_per_position: float = float(
            params.get("max_weight_per_position", DEFAULT_MAX_WEIGHT_PER_POSITION))
        self.stop_pct: float = float(params.get("stop_pct", DEFAULT_STOP_PCT))

        if self.max_positions <= 0:
            raise ValueError("max_positions는 양수여야 합니다.")
        if not 0 < self.max_weight_per_position <= 1:
            raise ValueError("max_weight_per_position는 (0, 1] 범위여야 합니다.")
        if self.stop_pct <= 0:
            raise ValueError("stop_pct는 양수여야 합니다.")

        # 하루 단위 소비 마커 — 실질적 재시작 방어선은 아니다(모듈 docstring
        # "상태" 절). 거래일이 바뀌면 통째로 비운다.
        self._consumed_day: dtdate | None = None
        self._consumed_ids: set[str] = set()
        # 진단용 — symbol → 마지막 거부 사유. `_reject`가 갱신하고 즉시 로그한다.
        self.last_reject: dict[str, str] = {}

    # ------------------------------------------------------------------ 사이클

    def on_cycle(self, ctx: Context) -> list[Signal]:
        signals: list[Signal] = []
        now = ctx.clock.now()
        today = trading_day(now)
        if today != self._consumed_day:
            self._consumed_day = today
            self._consumed_ids = set()

        positions = ctx.broker.positions()
        tradable = ctx.clock.is_market_open(_KR_MARKET) and in_continuous_session(_KR_MARKET, now)

        # 1) 하드 손절 레일 — 내 랏이 있는 심볼만, 연속거래 구간에서만.
        if tradable:
            for symbol, pos in positions.items():
                signal = self._check_hard_stop(symbol, pos, ctx)
                if signal is not None:
                    signals.append(signal)

        # 2) 인박스 처리 — 오늘(거래일) 미소비 주문만.
        for order in self._read_inbox():
            signal = self._process_order(order, positions, tradable, ctx, today)
            if signal is not None:
                signals.append(signal)

        return signals

    def _read_inbox(self) -> list[Mapping[str, Any]]:
        try:
            return list(self.inbox_reader())
        except Exception as e:  # noqa: BLE001 — 인박스 조회 실패가 엔진을 죽이면 안 된다
            logger.warning("[%s] 인박스 조회 실패: %s: %s", self.id, type(e).__name__, e)
            return []

    # ------------------------------------------------------------------ 보유 관리

    def _check_hard_stop(self, symbol: str, pos: Position, ctx: Context) -> Signal | None:
        if not pos.is_open:
            return None
        lot = pos.lot(self.id)
        if lot is None or lot.get("stop") is None:
            return None
        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            return None
        price = float(quote.price)
        stop = float(lot["stop"])
        if price > stop:
            return None
        entry = lot.get("entry")
        return Signal(
            strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
            target_weight=0.0, exit_fraction=1.0,
            reason=(f"LLM 트레이더 하드레일 손절(-{self.stop_pct:g}%): "
                    f"진입={entry} 손절선={stop:.2f} 현재={price:.2f}"),
        )

    # ------------------------------------------------------------------ 인박스 처리

    def _process_order(
        self, order: Mapping[str, Any], positions: dict[str, Position],
        tradable: bool, ctx: Context, today: dtdate,
    ) -> Signal | None:
        oid = order.get("id")
        if not isinstance(oid, str) or not oid:
            logger.warning("[%s] 인박스 행에 id 없음 — 무시: %r", self.id, order)
            return None
        if oid in self._consumed_ids:
            return None

        ts = _parse_ts(order.get("ts"))
        if ts is None or trading_day(ts) != today:
            # 과거/미상 ts — 소비 처리하지 않는다. 매 사이클 다시 걸러도 무해하고
            # (모듈 docstring "상태" 절), 소비 마킹을 안 해야 "오늘의 자정 이후
            # 재평가" 같은 경계 사고를 만들지 않는다.
            return None

        self._consumed_ids.add(oid)

        symbol = order.get("symbol")
        if not _is_kr_symbol(symbol):
            self._reject(str(symbol), f"KR 심볼 아님(#{oid}): {symbol!r}")
            return None

        action = order.get("action")
        if action not in ("buy", "sell"):
            self._reject(symbol, f"알 수 없는 action(#{oid}): {action!r}")
            return None

        horizon = order.get("horizon")
        if horizon not in _VALID_HORIZONS:
            self._reject(symbol, f"horizon 값 오류(#{oid}): {horizon!r} (단타/스윙/장기만 허용)")
            return None

        if not tradable:
            self._reject(symbol, f"시장 닫힘/동시호가(#{oid}) — 재시도 없음")
            return None

        pos = positions.get(symbol)
        my_lot = pos.lot(self.id) if pos is not None else None
        holding = bool(
            pos is not None and pos.is_open and my_lot is not None
            and float(my_lot.get("qty", 0.0)) > 0
        )

        if action == "buy":
            return self._enter(symbol, order, oid, horizon, positions, holding, ctx)
        return self._exit(symbol, order, oid, horizon, holding)

    def _enter(
        self, symbol: str, order: Mapping[str, Any], oid: str, horizon: str,
        positions: dict[str, Position], holding: bool, ctx: Context,
    ) -> Signal | None:
        if holding:
            self._reject(symbol, f"이미 보유 중 — 중복 매수 거부(#{oid})")
            return None

        open_count = sum(
            1 for s, p in positions.items()
            if p.is_open and p.lot(self.id) is not None
            and float(p.lot(self.id).get("qty", 0.0)) > 0
        )
        if open_count >= self.max_positions:
            self._reject(symbol, f"동시 보유 한도 초과({open_count}/{self.max_positions}, #{oid})")
            return None

        weight_raw = order.get("weight")
        try:
            weight = float(weight_raw)
        except (TypeError, ValueError):
            self._reject(symbol, f"weight 형식 오류(#{oid}): {weight_raw!r}")
            return None
        if not 0 < weight <= 1:
            self._reject(symbol, f"weight 범위 오류(#{oid}): {weight}")
            return None
        target_weight = min(weight, self.max_weight_per_position)

        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            self._reject(symbol, f"현재가 없음(#{oid})")
            return None
        price = float(quote.price)
        stop = price * (1 - self.stop_pct / 100)
        reason = str(order.get("reason") or "근거 없음")

        return Signal(
            strategy_id=self.id, symbol=symbol, action=SignalAction.ENTER_LONG,
            target_weight=target_weight,
            reason=f"[{horizon}] LLM 매수(#{oid}): {reason} w={target_weight:.2f}",
            stop=stop,
            # 체결 확인 후에만 lot에 적용된다(모듈 docstring "방어선" 절). horizon도
            # 함께 싣는다 — 사후 분석용(모듈 docstring "인박스 스키마" 절).
            state_update={
                "entry": price, "stop": stop, "horizon": horizon,
                "entered_at": ctx.clock.now().isoformat(), "strategy": self.id,
            },
        )

    def _exit(
        self, symbol: str, order: Mapping[str, Any], oid: str, horizon: str, holding: bool,
    ) -> Signal | None:
        if not holding:
            self._reject(symbol, f"보유 없음 — 매도 거부(#{oid})")
            return None
        reason = str(order.get("reason") or "근거 없음")
        return Signal(
            strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
            target_weight=0.0, exit_fraction=1.0,
            reason=f"[{horizon}] LLM 매도(#{oid}): {reason}",
        )

    def _reject(self, symbol: str, reason: str) -> None:
        if self.last_reject.get(symbol) != reason:
            logger.info("[%s] 진입/청산 거부 %s: %s", self.id, symbol, reason)
        self.last_reject[symbol] = reason
