"""LLM 트레이더(llm_trader) — 12번째 전략, LLM 판단 실험 레인.

## 소유자 승인 (2026-08-30)

"LLM 자체에게 전략과 판단을 맡기는 게 하나의 전략이 되는 것, 똑같이 1,000만원
모의, 기존 시스템 위에, 한 달 테스트." 다른 11개 전략은 사람이 정한 규칙(돌파·
평균회귀·모멘텀 등)을 코드로 굳힌 것이고, 이 전략은 그 규칙 자체를 LLM 판단에
맡기는 실험이다. 저장소 헌법(루트 CLAUDE.md "절대 하지 말 것")의 "거래 핫패스에
LLM/네트워크 호출 금지"는 그대로 지킨다 — **LLM 호출은 이 파일에도, `quant/trade/`
어디에도 없다.**

**2026-09-03 소유자 결정 갱신**: 자동매매는 단타·스캘핑만 — 오버나이트/장기
아이디어는 자동매매가 아니라 `quant/analyze/manual_recs.py`(텔레그램 추천) 레인으로
간다. 이 전략도 예외가 아니라서 일중 전략으로 전환한다: 마감 전 강제청산을 추가하고
`_OVERNIGHT_STRATEGIES`에서 뺐다(아래 "방어선" 절). LLM이 여전히 매수/매도 판단·
종목 선정을 자율로 하는 실험이라는 본질은 그대로다 — 달라진 것은 "마감까지는 반드시
청산한다"는 하드레일 하나뿐이다.

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
   구간에 도착한 주문은 그 사이클엔 거부되지만 **소비 처리되지 않는다**(2026-09-02
   수정) — 연속거래가 재개되면 같은 id가 다시 평가된다(아래 "상태" 절).
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
오염 없음`). 이후 사이클마다 **하드 손절**과 **EoD 강제청산**을 자동으로 본다
(현재가 ≤ stop → 즉시 전량 청산, `in_continuous_session`일 때만 — `rsi2_dip.
_tradable`과 같은 게이트). **그 밖의 보유 관리는 하지 않는다**:

- **세션 마감 강제청산이 있다(2026-09-03 소유자 결정 전환).** 이전에는 "LLM이
  청산도 스스로 판단하는 실험"이라 오버나이트를 허용했지만, 소유자가 자동매매는
  단타·스캘핑만으로 범위를 좁혔다 — 오버나이트/장기 아이디어는 이 레인이 아니라
  `manual_recs`(텔레그램 추천)로 간다. 그래서 `ctx.clock.should_flatten`(orb_scan/
  news_scalp와 동일 패턴)을 호출해 마감 전 무조건 청산하고, `quant/trade/loop.py`의
  `_OVERNIGHT_STRATEGIES`에서도 뺐다 — `tests/test_position_report_wording.py`가
  소스에서 `should_flatten` 호출 여부를 자동 대조하므로, 둘 중 하나만 바꾸면 그
  테스트가 즉시 실패한다(회귀 가드).
- 목표가·시간 청산이 없다. 청산은 인박스의 `sell` 행, 하드 손절, EoD 강제청산 셋뿐이다.
- **장중 매도는 LLM 자율이다**(2026-08-30 소유자 확인, 2026-09-03 EoD 추가로 일부
  수정). 보유기간 상한도, 익절 목표도 엔진이 부과하지 않는다 — 엔진이 지키는 것은
  `stop_pct` 하드 손절과 마감 전 강제청산 둘뿐이고, 그 위의 모든 매도 판단(언제·왜
  파는가)은 인박스의 `sell` 행이 유일한 경로다.

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

**소비 마킹 시점(2026-09-02 결함 수정).** 형식 검증(심볼/action/horizon)을
통과해 `_enter`/`_exit`가 실제로 `Signal`을 만들어 반환하는 순간에는 **아직**
`_consumed_ids`에 넣지 않는다 — `_process_order`가 신호를 냈다고 그게 체결됐다는
뜻은 아니기 때문이다(loop가 대신 부르는 `risk.approve()`가 콜드 페치 예산 초과
같은 일시적 사유로 실패할 수 있고, 이 전략은 그 결과를 직접 볼 수 없다). 대신
다음 사이클에 `ctx.broker.positions()`로 다시 계산한 `holding`이 원하는 방향으로
뒤집혔을 때(`_enter`가 "이미 보유 중"으로, `_exit`가 "보유 없음"으로 거부하는
바로 그 순간) 비로소 영구 소비 처리한다 — 실사고(2026-09-02 09:25~09:42, KR
078340) 재발 방지: 승인 실패로 판단 자체가 소실됐던 결함(이 절 다음 문단이었던
"동시호가 중 도착한 주문은 재시도하지 않는다"도 같은 원인이라 함께 고쳤다).

## 아직 못 하는 것 (정직하게)

1. **우리 원장 실측이 없다.** 신규 실험 레인 — `validation.status: burn_in`.
2. **`self.symbols`는 항상 빈 리스트다.** `universe: watchlist` opt-in이 아니라
   인박스가 유니버스를 대신하므로(`quant/apps/assembly.py`의
   `rebuild_strategies`가 심볼을 갈아끼우는 대상이 아니다) — `Strategy` Protocol이
   `symbols: list[str]`를 요구해서 존재하는 자리표시자일 뿐이다. 포지션 관리는
   `ctx.broker.positions()`에서 **내 랏이 있는 심볼**을 순회하는 것만으로
   이뤄지므로(정적 유니버스와 무관), orb_scan류의 "유니버스에서 빠진 보유 종목"
   문제가 애초에 없다.
3. **weight가 없는 buy는 거부한다.** 스펙이 "buy 시 목표 비중"을 요구하므로
   비중 없는 매수는 잘못된 행으로 본다.
"""
from __future__ import annotations

import logging
from datetime import date as dtdate, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from quant.core.models import Position, Signal, SignalAction, trading_day
from quant.core.ports import Context
from quant.core.session import in_continuous_session

logger = logging.getLogger(__name__)

DEFAULT_MAX_POSITIONS = 5
DEFAULT_MAX_WEIGHT_PER_POSITION = 0.34
DEFAULT_STOP_PCT = 5.0
# 2026-09-03: 오버나이트 → 일중 전환(모듈 docstring 상단 참고). 다른 KR 단타
# 전략들의 flatten_before_close_minutes 기본값(1~10)과 같은 자리 — 동시호가 직전
# 청산 시도가 씹히지 않도록 여유를 둔다.
DEFAULT_FLATTEN_BEFORE_CLOSE_MINUTES = 10
# 거부/폐기 로그 반복 억제(2026-09-04) — 같은 결정(oid)의 거부가 이 창 안에서
# 다시 나면 로그를 또 남기지 않는다. 예전엔 심볼 단위로 "직전 사유 문자열과
# 다르면 로그"였는데, 한 심볼에 pending 결정이 둘 이상이면 서로 다른 oid가
# 매 사이클 last_reject[symbol]을 번갈아 갱신해 사실상 매번 "새 사유"로 보여
# 무한 재로그됐다(실측: 15:50 재시작 이후 26,445줄 중 16,667줄이 이 거부
# 로그였다). `quant/control/ledger.py`의 REJECT_LOG_COOLDOWN(1시간)과 같은
# 발상이지만 여기는 결정(oid) 단위라 창을 더 짧게(30분) 잡는다.
_REJECT_LOG_THROTTLE = timedelta(minutes=30)

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
        # 2026-09-03: 일중 전환 — 마감 전 이 분수만큼 남으면 무조건 청산(아래
        # on_cycle의 EoD 레일, 모듈 docstring "방어선" 절).
        self.flatten_minutes: float = float(
            params.get("flatten_before_close_minutes", DEFAULT_FLATTEN_BEFORE_CLOSE_MINUTES))

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
        # 진단용 — symbol → 마지막 거부 사유. `_reject`가 갱신한다(로그 여부와 무관).
        self.last_reject: dict[str, str] = {}
        # 거부 로그 반복 억제(위 _REJECT_LOG_THROTTLE) — oid → 마지막으로 실제
        # 로그를 남긴 시각. 프로세스 재시작 시 소실되며, 그래도 무해하다(재시작
        # 직후 한 번 더 로그되는 정도).
        self._reject_logged_at: dict[str, datetime] = {}

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

        # 1) EoD 강제청산 — 내 랏이 있는 심볼만, 마감 전이면 연속거래 구간 여부와
        #    무관하게 무조건 청산(2026-09-03 일중 전환, 모듈 docstring "방어선"
        #    절). should_flatten 자체가 동시호가/장마감 이후는 걸러준다(clock.py).
        flattened: set[str] = set()
        for symbol, pos in positions.items():
            signal = self._check_eod_flatten(symbol, pos, ctx)
            if signal is not None:
                signals.append(signal)
                flattened.add(symbol)

        # 2) 하드 손절 레일 — 내 랏이 있는 심볼만, 연속거래 구간에서만. 이번 사이클에
        #    이미 EoD로 청산한 심볼은 중복 신호를 만들지 않는다.
        if tradable:
            for symbol, pos in positions.items():
                if symbol in flattened:
                    continue
                signal = self._check_hard_stop(symbol, pos, ctx)
                if signal is not None:
                    signals.append(signal)

        # 3) 인박스 처리 — 오늘(거래일) 미소비 주문만.
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

    def _check_eod_flatten(self, symbol: str, pos: Position, ctx: Context) -> Signal | None:
        """마감 전 무조건 청산(2026-09-03 일중 전환) — `orb_scan`/`news_scalp`와
        같은 패턴. LLM의 `sell` 판단을 대신하지 않는다 — LLM이 이미 그 전에 팔았으면
        내 랏이 없어 여기서 아무 것도 하지 않는다."""
        if not pos.is_open:
            return None
        lot = pos.lot(self.id)
        if lot is None or not float(lot.get("qty", 0.0)) > 0:
            return None
        if not ctx.clock.should_flatten(self.market, self.flatten_minutes):
            return None
        quote = ctx.data.quote(symbol)
        price_note = f"{quote.price:.2f}" if quote is not None and quote.price else "조회 실패"
        entry = lot.get("entry")
        return Signal(
            strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
            target_weight=0.0, exit_fraction=1.0,
            reason=f"EoD 강제청산(단타 전환): 진입={entry} 현재={price_note}",
        )

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
        if ts is None:
            # 파싱 불가 — 소비 처리하지 않는다. 매 사이클 다시 걸러도 무해하다
            # (모듈 docstring "상태" 절).
            return None
        decision_day = trading_day(ts)
        if decision_day < today:
            # 거래일이 지난 보류 결정(2026-09-04 수리) — 예전엔 이 분기도 조용히
            # 매 사이클 다시 걸렀을 뿐 로그도, 영구 소비도 하지 않았다. 과거로
            # 되돌아가지 않으니 오늘 다시 봐도 내일 다시 봐도 결론이 같다 —
            # 한 번 로그하고 영구 소비한다(재시작 방어선인 ts 필터 자체는 그대로
            # 다음 거래일에도 걸러낸다. 매 사이클 무의미하게 재평가하며 조용히
            # 쌓이던 낭비만 없앤다).
            self._reject(str(order.get("symbol")),
                         f"보류 결정 폐기: 거래일 경과(#{oid})", oid, ctx.clock.now())
            self._consumed_ids.add(oid)
            return None
        if decision_day != today:
            # 미래 ts(시계 오차 등) — 소비하지 않고 조용히 건너뛴다(기존 동작).
            return None

        # 아래부터 소비 마킹 시점이 갈린다(2026-09-02 결함 B 수정). **형식이 잘못된
        # 행(심볼/action/horizon)은 재평가해도 결론이 똑같으므로 즉시 영구 소비한다**
        # — 이건 기존 동작 그대로다. 반면 "시장 닫힘/동시호가"와 "risk.approve() 승인
        # 실패"는 **일시적**이다: 실사고(2026-09-02 09:25~09:42)에서 콜드 페치 예산
        # 초과로 approve()가 예외를 던지자, 그 시점엔 이미 여기서 oid가 영구 소비돼
        # 있어 판단이 그대로 소실됐다(로그는 "다음 사이클"이라 말했지만 다음 사이클이
        # 없었다). 이 전략은 loop가 대신 부르는 risk.approve()의 결과를 직접 볼 수
        # 없다(신호를 반환했다고 체결된 게 아니다) — 그래서 "형식 검증을 통과해 신호를
        # 만들었다"는 사실만으로는 아직 소비 처리하지 않는다.
        #
        # 대신 **포지션 상태로 간접 확인**한다: buy는 보유 전환, sell은 미보유 전환이
        # 원하는 결과다. 승인이 실패했다면(일시적 사유든 자금 부족처럼 지속되는
        # 사유든) 포지션이 그대로이므로, 다음 사이클에 이 함수가 다시 호출될 때
        # `holding`이 그대로 미확정 → `_enter`/`_exit`가 같은 신호를 다시 만들어
        # 자연히 재시도된다. 실제로 체결됐다면 `holding`이 뒤집혀 `_enter`/`_exit`가
        # "이미 보유 중"/"보유 없음"으로 거부하고, 그 시점에 비로소 영구 소비한다.
        # 재시도는 무한하지 않다 — 거래일이 바뀌면 위 ts 필터가 다음날 이 행을
        # 영구히 걸러내고(_consumed_day 롤과 함께 _consumed_ids도 비워진다),
        # 매수 쪽은 risk.approve() 자체의 미체결 중복 진입 가드가 같은 심볼로의
        # 중복 제출을 막는다(quant/trade/risk/manager.py, 청산은 원래 중복이 안전
        # 하므로 그 가드를 타지 않는다).
        symbol = order.get("symbol")
        if not _is_kr_symbol(symbol):
            self._consumed_ids.add(oid)
            self._reject(str(symbol), f"KR 심볼 아님(#{oid}): {symbol!r}", oid, ctx.clock.now())
            return None

        action = order.get("action")
        if action not in ("buy", "sell"):
            self._consumed_ids.add(oid)
            self._reject(symbol, f"알 수 없는 action(#{oid}): {action!r}", oid, ctx.clock.now())
            return None

        horizon = order.get("horizon")
        if horizon not in _VALID_HORIZONS:
            self._consumed_ids.add(oid)
            self._reject(symbol, f"horizon 값 오류(#{oid}): {horizon!r} (단타/스윙/장기만 허용)",
                        oid, ctx.clock.now())
            return None

        # 포지션 조회는 시장 개폐와 무관하다 — tradable 게이트보다 먼저 계산해
        # 둔다(아래 "이미 무포지션인 매도" 조기 폐기가 이 값을 써야 한다).
        pos = positions.get(symbol)
        my_lot = pos.lot(self.id) if pos is not None else None
        holding = bool(
            pos is not None and pos.is_open and my_lot is not None
            and float(my_lot.get("qty", 0.0)) > 0
        )

        if not tradable:
            if action == "sell" and not holding:
                # 2026-09-04 실사고 수리: 이미 청산된 종목의 대기 매도 결정이
                # "시장 닫힘/동시호가 — 다음 사이클 재시도"로 영원히 재시도됐다
                # (088350/042660/006800이 09:15 청산된 뒤 15:50 재시작 이후
                # 장마감 내내 — 약 17시간, 26,445줄 중 16,667줄). 포지션 없음은
                # 시장이 닫혀 있어도 이미 확정된 사실이므로(포지션 조회는
                # tradable과 무관) 재시도로 미루지 않고 그 자리에서 영구 폐기한다.
                self._reject(symbol, f"보류 결정 폐기: 포지션 없음(#{oid})", oid, ctx.clock.now())
                self._consumed_ids.add(oid)
                return None
            # 그 외엔 소비하지 않는다 — 연속거래 재개 후 같은 oid가 다시
            # 평가된다(위 설명).
            self._reject(symbol, f"시장 닫힘/동시호가(#{oid}) — 다음 사이클 재시도",
                        oid, ctx.clock.now())
            return None

        if action == "buy":
            signal = self._enter(symbol, order, oid, horizon, positions, holding, ctx)
        else:
            signal = self._exit(symbol, order, oid, horizon, holding, ctx)

        if signal is None:
            # _enter/_exit 자체 가드(중복 매수/보유 없음/비중 오류/시세 없음/한도
            # 초과) — 재평가해도 결론이 같으므로 즉시 영구 소비(기존 동작 보존).
            self._consumed_ids.add(oid)
        # signal이 있으면 아직 소비하지 않는다 — 위 긴 설명대로 다음 사이클 포지션
        # 상태로 체결 여부를 간접 확인한 뒤에야 소비한다.
        return signal

    def _enter(
        self, symbol: str, order: Mapping[str, Any], oid: str, horizon: str,
        positions: dict[str, Position], holding: bool, ctx: Context,
    ) -> Signal | None:
        if holding:
            self._reject(symbol, f"이미 보유 중 — 중복 매수 거부(#{oid})", oid, ctx.clock.now())
            return None

        open_count = sum(
            1 for s, p in positions.items()
            if p.is_open and p.lot(self.id) is not None
            and float(p.lot(self.id).get("qty", 0.0)) > 0
        )
        if open_count >= self.max_positions:
            self._reject(symbol, f"동시 보유 한도 초과({open_count}/{self.max_positions}, #{oid})",
                        oid, ctx.clock.now())
            return None

        weight_raw = order.get("weight")
        try:
            weight = float(weight_raw)
        except (TypeError, ValueError):
            self._reject(symbol, f"weight 형식 오류(#{oid}): {weight_raw!r}", oid, ctx.clock.now())
            return None
        if not 0 < weight <= 1:
            self._reject(symbol, f"weight 범위 오류(#{oid}): {weight}", oid, ctx.clock.now())
            return None
        target_weight = min(weight, self.max_weight_per_position)

        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            self._reject(symbol, f"현재가 없음(#{oid})", oid, ctx.clock.now())
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
        ctx: Context,
    ) -> Signal | None:
        if not holding:
            # 시장이 열려 있는데 이미 무포지션인 경우(장중 재평가) — 여기 걸린다.
            # 시장이 닫혀 있을 때의 동일 상황은 `_process_order`가 이 함수를
            # 부르기 전에 이미 조기 폐기한다(모듈 docstring "방어선" 절 참고).
            self._reject(symbol, f"보유 없음 — 매도 거부(#{oid})", oid, ctx.clock.now())
            return None
        reason = str(order.get("reason") or "근거 없음")
        return Signal(
            strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
            target_weight=0.0, exit_fraction=1.0,
            reason=f"[{horizon}] LLM 매도(#{oid}): {reason}",
        )

    def _reject(self, symbol: str, reason: str, oid: str, now: datetime) -> None:
        """거부/폐기 사유를 기록한다. `self.last_reject[symbol]`은 항상 갱신하지만
        (진단용 — 마지막 사유 조회), 실제 `logger.info` 호출은 **결정(oid) 단위로
        `_REJECT_LOG_THROTTLE`(30분)에 한 번**만 남긴다.

        이전엔 "직전 로그한 사유 문자열과 다르면 로그"를 심볼 단위로 판정했다 —
        한 심볼에 pending 결정이 둘 이상이면 서로 다른 oid가 매 사이클
        last_reject[symbol]을 번갈아 갱신해, 사이클마다 "새 사유"로 보여
        무한 재로그됐다(2026-09-04 실측: 15:50 재시작 이후 26,445줄 중
        16,667줄이 이 거부 로그였다). oid 단위 시각 억제로 바꾸면 그 상호간섭이
        없어진다."""
        self.last_reject[symbol] = reason
        last_logged = self._reject_logged_at.get(oid)
        if last_logged is not None and (now - last_logged) < _REJECT_LOG_THROTTLE:
            return
        logger.info("[%s] 진입/청산 거부 %s: %s", self.id, symbol, reason)
        self._reject_logged_at[oid] = now
