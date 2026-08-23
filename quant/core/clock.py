"""Clock 구현: SimClock(백테스트, 설정 가능한 now) / WallClock(실거래, 실제 시간).

세션: US(America/New_York 09:30-16:00), KR(Asia/Seoul 09:00-15:30). DST는 zoneinfo가 처리.

두 구현 모두 판단 주기(cadence)를 알고 있어야 한다 — 마감 전 강제청산이
"다음 판단 시점이 청산 창을 지나치는가"로 결정되기 때문이다. 자세한 배경은
domain/interfaces.py의 Clock.should_flatten docstring 참고.
"""
from __future__ import annotations

from datetime import datetime, timezone

from quant.core.session import StaticSessionCalendar


def _is_open(calendar, market: str, now: datetime) -> bool:
    session = calendar.session(market, now)
    return session is not None and session.open <= now < session.close


def _minutes_to_close(calendar, market: str, now: datetime) -> float | None:
    session = calendar.session(market, now)
    if session is None or not (session.open <= now < session.close):
        return None
    return (session.close - now).total_seconds() / 60


def _should_flatten(calendar, market: str, now: datetime, flatten_minutes: float, cadence: float) -> bool:
    """다음 판단 시점(now + cadence)이 청산 창 안으로 들어오면 지금 청산한다.

    cadence를 빼지 않으면 판단 주기가 굵을 때 창을 건너뛴다. 15분봉이면
    15:45(잔여 15분) 다음 판단이 장 마감이므로, 15 - 15 = 0 < 10 → 지금 청산.
    라이브(10초 주기)에서는 cadence≈0.17분이라 사실상 flatten_minutes 그대로
    동작한다 — 즉 백테스트와 라이브가 같은 의도로 수렴한다.

    **마감 이후에는 False다.** 이미 장이 닫혔으면 청산 신호를 만들어도 체결할 시장이
    없고, 라이브에서는 시간외 호가창으로 주문이 나간다. 대신 청산은 **세션 안의 마지막
    판단 시점**에 걸려야 한다 — 그래서 체결가는 마감가가 아니라 한 봉 앞의 종가다
    (5분봉이면 15:55, 15분봉이면 15:45). 이 5~15분 차이는 의도된 것이고, 마감가에
    붙이려면 브로커의 MOC(장마감 지정가) 주문이 필요하다.

    이 함수가 조용히 False를 반환해 청산 창이 통째로 사라진 적이 있다: 마감 시각에
    `is_market_open`이 False가 되며 `minutes_to_close`가 None을 반환했고, 그때
    `flatten_minutes`가 0이면 세션 안 어느 시점에도 조건이 성립하지 않았다. 그 결과
    3배 레버리지 ETF 포지션이 며칠씩 살아남았다(10년 백테스트에서 청산 17건).
    **`flatten_minutes`는 반드시 1 이상이어야 한다** — 마지막 in-session 사이클의
    잔여시간이 정확히 cadence와 같기 때문이다(mtc - cadence == 0).
    """
    session = calendar.session(market, now)
    if session is None or not (session.open <= now < session.close):
        return False
    return (session.close - now).total_seconds() / 60 - cadence < flatten_minutes


class SimClock:
    """백테스트 리플레이용 — now()는 set()으로만 바뀐다.

    cadence는 리플레이 봉 간격(분). 봉 마감마다 한 번 판단하므로 이 값이
    곧 "다음 판단까지의 간격"이다.
    """

    def __init__(self, now: datetime, cadence_minutes: float = 15.0, calendar=None):
        self._now = now
        self._cadence = float(cadence_minutes)
        self._calendar = calendar or StaticSessionCalendar()

    def set(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, market: str) -> bool:
        return _is_open(self._calendar, market, self._now)

    def minutes_to_close(self, market: str) -> float | None:
        return _minutes_to_close(self._calendar, market, self._now)

    def cadence_minutes(self) -> float:
        return self._cadence

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return _should_flatten(self._calendar, market, self._now, flatten_minutes, self._cadence)


class WallClock:
    """실거래/paper용 — 실제 시스템 시간(UTC 기준, 세션 판정 시 각 시장 tz로 변환).

    cadence는 엔진 폴링 주기(초 → 분). settings.yaml의 engine.poll_seconds와
    일치시켜야 백테스트-라이브 동작이 어긋나지 않는다.
    """

    def __init__(self, poll_seconds: float = 10.0, calendar=None):
        self._cadence = float(poll_seconds) / 60.0
        self._calendar = calendar or StaticSessionCalendar()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def is_market_open(self, market: str) -> bool:
        return _is_open(self._calendar, market, self.now())

    def minutes_to_close(self, market: str) -> float | None:
        return _minutes_to_close(self._calendar, market, self.now())

    def cadence_minutes(self) -> float:
        return self._cadence

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return _should_flatten(self._calendar, market, self.now(), flatten_minutes, self._cadence)
