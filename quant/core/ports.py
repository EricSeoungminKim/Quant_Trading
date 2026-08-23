"""Protocol 경계 — 어댑터가 구현하고, 코어(전략/리스크/루프)는 이것만 본다.

규칙:
- 전략은 DataFeed와 Clock만 읽는다. 브로커/실행을 직접 만지지 않는다.
- history()는 호출 시점 기준 완성봉만 반환한다 (look-ahead 금지).
- 어댑터의 네트워크 예외는 어댑터 내부에서 처리한다 — 코어로 raw 예외를 전파하지 않는다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd

from .models import Fill, Order, OrderState, Position, Quote, Signal


@runtime_checkable
class Clock(Protocol):
    """라이브에선 실제 시간, 백테스트에선 리플레이 시간."""

    def now(self) -> datetime: ...

    def is_market_open(self, market: str) -> bool: ...

    def minutes_to_close(self, market: str) -> float | None:
        """장 마감까지 남은 분. 장이 닫혀 있으면 None."""
        ...

    def cadence_minutes(self) -> float:
        """다음 판단 시점까지의 간격(분). 라이브는 폴링 주기, 백테스트는 봉 간격.

        전략이 "지금 안 하면 다음 기회가 없다"를 판단하는 데 쓴다."""
        ...

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        """마감 전 강제청산 시점인가.

        단순히 `minutes_to_close <= flatten_minutes`로 판정하면 안 된다. 판단
        주기가 청산 창보다 굵으면 창을 통째로 건너뛴다 — 15분봉 백테스트에서
        미국장 마지막 판단 시점은 15:45(잔여 15분)이고 그 다음은 장 마감이라,
        flatten_minutes=10이면 조건이 영원히 성립하지 않는다. 그 결과 오버나이트
        금지가 무력화되고 백테스트가 라이브와 다르게 행동한다.

        올바른 기준은 "다음 판단 시점이 청산 창을 지나치는가"다."""
        ...


class DataSourceError(RuntimeError):
    """데이터 소스가 조회에 실패했다 — "데이터가 없다"와는 다르다.

    DataFeed 구현체는 실패를 빈 프레임/None으로 위장해서는 안 된다. 위장하면
    MarketDataService가 그것을 정상 응답으로 받아들여 다른 소스로 넘어가지
    않고, 전략은 "돌파가 없었다"와 "시세를 못 받았다"를 구분할 수 없게 된다.

    벤더 예외(httpx, TossAPIError 등)는 여전히 코어로 새어나가면 안 된다 —
    어댑터가 이 도메인 예외로 변환하고, MarketDataService가 흡수한다.
    """


@runtime_checkable
class DataFeed(Protocol):
    def quote(self, symbol: str) -> Quote | None: ...

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        """완성봉 n개. columns=[open,high,low,close,volume], index=DatetimeIndex(tz-aware).
        interval: "1m" | "5m" | "15m" | "1d". 현재 형성 중인 봉은 절대 포함하지 않는다."""
        ...


@runtime_checkable
class Broker(Protocol):
    """주문·계좌 어댑터. paper 실행기와 live 브로커(Toss/Kiwoom)가 공히 구현."""

    def place_order(self, order: Order) -> OrderState:
        """주문의 **결과 상태**를 돌려준다 (Phase 6.3).

        예전 계약은 `Fill | None` 이라 표현할 수 있는 결과가 둘뿐이었다 — 다 됐거나,
        아무 일도 없었거나. 그래서 이 넷이 전부 `None` 하나로 뭉개졌다:

        - 브로커가 **거부**했다 (증거금·호가·계좌 문제)
        - 애초에 **안 나갔다** (MODE!=live, 엔진 소유 수량 0, 수량 계산 실패)
        - 나갔는데 **결론을 못 봤다** (폴링 타임아웃 — 서버엔 남아 있을 수 있다)
        - 일부만 채워졌다 (잔량이 조용히 버려졌다)

        구분 규칙: `broker_order_id is None` 이면 브로커에 닿지 못한 것이다.
        비터미널(`is_open`)이면 결론이 없는 것이고, 그 `remaining_qty` 가 대사가
        집어야 할 대상이다. **"모른다"를 상태로 만들지 않는다.**

        원장에 넣을 체결은 `state.fill` 이다 — 수수료·실현손익·현금 스냅샷은
        브로커만 아는 값이라 상태기계가 만들 수 없다.

        어댑터는 `quant.core.oms` 의 조립 헬퍼(`not_submitted`/`rejected_by_broker`/
        `filled_from`/`open_without_fill`)를 쓴다 — 조립이 어댑터마다 갈리지 않게.
        """
        ...

    def positions(self) -> dict[str, Position]: ...

    def cash(self) -> float:
        """기준 통화(KRW) 현금."""
        ...


@runtime_checkable
class RiskManager(Protocol):
    def approve(
        self,
        signal: Signal,
        ctx: "Context",
        risk_multiplier: float = 1.0,
        marks: dict[str, float] | None = None,
    ) -> Order | None:
        """Signal → 사이징·하드레일 적용 → Order. 거부 시 None (사유 로깅).

        marks: 신호 종목 외 보유 종목의 현재가 스냅샷(엔진 루프가 사이클마다 채운다).
        평가금액(MTM) 기반 하드레일(예: daily_loss_limit_pct)에 쓰인다. None이거나
        특정 종목이 빠져 있으면 그 종목은 평균단가로 근사한다."""
        ...


@runtime_checkable
class Notifier(Protocol):
    """실패가 거래에 절대 영향을 주면 안 된다 — 모든 구현은 예외를 삼킨다."""

    def send(self, text: str) -> None: ...


@runtime_checkable
class Narrator(Protocol):
    """결정론적 판정을 사람이 읽을 산문으로 바꾼다. **판단하지 않는다.**

    Phase 5.4 의 어댑터 경계다. 왜 포트가 필요한가: 감시 경보를 LLM 이 쓰게 하되,
    **LLM 이 무엇을 경보할지 정하게 하지 않으려면** 그 역할이 타입으로 좁혀져 있어야
    한다. 여기 들어오는 건 이미 확정된 판정문이고, 나가는 건 문장뿐이다 — 구현체가
    할 수 있는 일이 서술밖에 없다.

    `None` 을 돌려주면 "서술하지 못했다"는 뜻이고, 호출자는 결정론적 형식으로
    떨어져야 한다. **경보가 이 포트에 의존하면 안 된다** — 모델이 죽은 날에도
    이상은 알려져야 한다. 그래서 실패는 예외가 아니라 `None` 이다(Notifier 와 같은
    계약: 실패가 위쪽을 죽이지 않는다).
    """

    def narrate(self, prompt: str) -> str | None: ...


@runtime_checkable
class EventSink(Protocol):
    def on_signal(self, signal: Signal) -> None: ...

    def on_fill(self, fill: Fill) -> None: ...


@runtime_checkable
class OrderSink(Protocol):
    """주문의 **생애**를 받는 싱크. `EventSink` 와 분리한 이유는 선택적이기 때문이다.

    `EventSink` 에 메서드를 더하면 기존 구현체와 테스트 페이크가 전부 깨진다. 대신
    별도 Protocol 을 두고 `isinstance` 로 물어본다 — `reconcile.py` 가 쓰는 것과 같은
    명시적 덕타이핑이다("그 둘이 없으면 대사 자체를 하지 않는다").

    체결(`on_fill`)과 다른 사건이다. 체결은 **일어난 일**이고, 주문 생애는 *시킨 것과
    일어난 일의 차이*다 — 20주를 시켜 8주가 채워졌을 때 못 받은 12주가 여기 남는다.
    """

    def on_order(self, state: OrderState) -> None: ...


@runtime_checkable
class KeyValue(Protocol):
    """휘발성 키-값 저장소 (Redis 등).

    **여기 담아도 되는 것과 안 되는 것을 계약으로 못 박는다.**

    담아도 되는 것: 다시 계산할 수 있는 것 — 수집 중복 판정 집합, 피드 건강도,
    랭킹 캐시, 진행 중 집계. 잃어도 재계산하면 그만인 것들이다.

    **절대 담지 않는 것: 돈이 걸린 상태** — 포지션, 주문 상태, control 플래그,
    체결 원장. 이건 재시작을 견뎌야 하고 휘발성 저장소는 그 보장을 하지 않는다.
    파일이 진실이고, 그 규칙은 `tests/test_architecture.py` 가
    `quant.trade` 의 Redis 임포트를 막는 것으로 강제한다.

    모든 구현은 **예외를 삼키고 실패를 반환값으로 표현한다** — 캐시가 죽었다고
    호출자가 죽으면, 캐시를 안 쓰느니만 못하다.
    """

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> bool: ...

    def sadd(self, key: str, *members: str) -> int:
        """집합에 추가하고 **새로 추가된 개수**를 돌려준다.

        반환값이 곧 "처음 보는 것이었나"다 — 수집 중복 판정이 이 한 번의 왕복으로
        끝난다(지금은 그날 JSONL 전체를 다시 파싱한다).
        """
        ...

    def sismember(self, key: str, member: str) -> bool: ...

    def expire(self, key: str, ttl_seconds: int) -> bool: ...

    def zadd(self, key: str, scores: dict[str, float]) -> int: ...

    def ztop(self, key: str, n: int) -> list[tuple[str, float]]:
        """점수 내림차순 상위 n. 랭킹·리더보드용."""
        ...

    def healthy(self) -> bool:
        """연결이 살아 있나. 죽었으면 호출자는 원본 경로로 폴백한다."""
        ...


class Context:
    """전략·리스크에 주입되는 실행 컨텍스트. run_cycle이 조립한다."""

    def __init__(self, clock: Clock, data: DataFeed, broker: Broker) -> None:
        self.clock = clock
        self.data = data
        self.broker = broker


@runtime_checkable
class Strategy(Protocol):
    id: str
    symbols: list[str]

    def on_cycle(self, ctx: Context) -> list[Signal]:
        """매 사이클 호출. 진입/청산 판단 후 Signal 리스트 반환 (없으면 [])."""
        ...
