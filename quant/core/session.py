"""거래 세션(개장~마감) 조회. Clock이 "지금 장이 열렸나 / 마감까지 몇 분인가"를
판단할 때 쓰는 유일한 출처.

## 왜 하드코딩을 걷어냈나

`clock.py`는 US 세션을 09:30~16:00으로 고정해 두고 있었다. 미국 시장은 매년 여러 번
**조기폐장(13:00 ET)** 한다 — 추수감사절 다음날, 크리스마스 이브, 독립기념일 전날 등.
실측(2016~2026 TQQQ 5분봉): 그런 날이 14세션 있었고, 엔진은 그날 **장이 닫힌 뒤
3시간 동안 포지션을 관리하고 15:55에 시간외 호가창으로 청산 주문을 냈다**. 백테스트
손익 영향은 0.5% 수준이지만 라이브에서는 체결 자체가 위험하다.

## 세 가지 출처, 각각 맞는 자리

| 구현 | 쓰는 곳 | 근거 |
|---|---|---|
| `BarSessionCalendar` | 백테스트 | **그날 실제로 존재하는 봉이 진실이다.** 조기폐장이든 휴장이든 데이터가 그대로 말해준다 — 별도 캘린더가 필요 없다 |
| `TossSessionCalendar` | 라이브 | Toss `GET /api/v1/market-calendar/US`를 **미리** 물어본다 |
| `StaticSessionCalendar` | 폴백/테스트 | 09:30~16:00 고정 |

라이브에서 "시세가 안 나오면 장이 닫힌 것"으로 추론하면 안 된다. 두 가지 이유다:
1. **너무 늦다.** 마감 전 청산은 마감을 *미리* 알아야 한다. 13:00 조기폐장을 13:05에
   알면 12:55 청산 주문을 낼 수 없다.
2. **구분이 안 된다.** "데이터 없음"은 휴장일 수도, API 장애·IP 차단·레이트리밋일 수도
   있다 (실측: Toss가 미등록 IP에 `403 access_denied`를 준다). 장애를 휴장으로 읽으면
   엔진이 조용히 멈춘다 — ADR-0008이 "조회 실패"와 "데이터 없음"을 구분하라고 못박은
   바로 그 실패 모드다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as dtdate, datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger(__name__)

# 하드코딩 폴백 세션. tz, 개장, 마감.
_STATIC_SESSIONS: dict[str, tuple[ZoneInfo, dtime, dtime]] = {
    "US": (ZoneInfo("America/New_York"), dtime(9, 30), dtime(16, 0)),
    "KR": (ZoneInfo("Asia/Seoul"), dtime(9, 0), dtime(15, 30)),
}

_MARKET_TZ: dict[str, ZoneInfo] = {m: tz for m, (tz, _, _) in _STATIC_SESSIONS.items()}

# **가격이 발견되는 연속 거래 구간**. 위 `_STATIC_SESSIONS`(정규장 전체)와 다르다
# — 그 차이가 2026-08-26 에 실제로 돈을 잃게 만들었다.
#
# 한국장 구조(소유자 교정):
#   08:30~09:00  장 시작 동시호가 — 주문만 모으고 **체결하지 않는다**. 09:00 정각에
#                하나의 시가로 일괄 체결.
#   08:30~08:40  장전 시간외 종가 — 전일 종가 고정가. 가격 발견 없음.
#   09:00~15:20  **연속 거래** — 여기서만 호가가 실시간으로 체결된다.
#   15:20~15:30  장 마감 동시호가 — 주문을 모아 15:30 종가로 일괄 체결.
#
# 그동안 엔진은 KR 을 09:00~15:30 연속으로 봤고, 프리마켓 창(08:00~08:50)에서
# 직접 진입까지 했다. 그래서 08:27·08:46 에 **실재할 수 없는 체결**이 기록됐고,
# 09:00 시가 갭이 손절선을 2.8% 지나쳐 의도한 -1% 손실이 -3.8% 가 됐다.
# 페이퍼 브로커는 데이터피드가 준 가격이면 무엇이든 체결시키므로 이 구조적
# 오류를 스스로 잡지 못한다 — 세션 모델이 잡아야 한다.
#
# US 는 정규장 전체가 연속 거래다(프리/애프터는 별도 세션이고, 우리 US 프리마켓
# 진입은 실제로 체결 가능한 연속 호가창을 쓴다 — KR 과 성격이 다르다).
_CONTINUOUS: dict[str, tuple[dtime, dtime]] = {
    "US": (dtime(9, 30), dtime(16, 0)),
    "KR": (dtime(9, 0), dtime(15, 20)),
}


def continuous_window(market: str) -> tuple[dtime, dtime]:
    """그 시장의 연속 거래 (시작, 끝) 로컬 시각. 위 표 참고."""
    return _CONTINUOUS[market]


def in_continuous_session(market: str, now: datetime) -> bool:
    """지금이 **호가가 실시간으로 체결되는** 구간인가.

    동시호가·시간외는 False 다. 진입/청산 판단이 이걸 봐야 "낼 수 없는 주문"을
    내지 않는다. 휴장일은 모른다(주말만 거른다) — 캘린더 판정은 `SessionCalendar`
    구현체가 하고, 이 함수는 **하루 안에서의 구간**만 답한다.
    """
    tz = _MARKET_TZ[market]
    local = now.astimezone(tz)
    if local.weekday() >= 5:
        return False
    start, end = _CONTINUOUS[market]
    return start <= local.time() < end


@dataclass(frozen=True)
class Session:
    """한 거래일의 정규장 구간. 둘 다 tz-aware."""

    open: datetime
    close: datetime


def market_tz(market: str) -> ZoneInfo:
    return _MARKET_TZ[market]


def local_date(market: str, now: datetime) -> dtdate:
    return now.astimezone(_MARKET_TZ[market]).date()


class StaticSessionCalendar:
    """고정 시간표. 휴장일과 조기폐장을 모른다 — 주말만 걸러낸다.

    폴백 전용이다. 이걸로 라이브를 돌리면 위 docstring의 조기폐장 문제가 그대로 남는다.
    """

    def session(self, market: str, now: datetime) -> Session | None:
        tz, open_t, close_t = _STATIC_SESSIONS[market]
        local = now.astimezone(tz)
        if local.weekday() >= 5:
            return None
        return Session(
            open=datetime.combine(local.date(), open_t, tzinfo=tz),
            close=datetime.combine(local.date(), close_t, tzinfo=tz),
        )


class BarSessionCalendar:
    """봉 데이터에서 세션을 유도한다 — 백테스트 전용.

    그날 존재하는 봉이 곧 그날의 세션이다: 개장 = 첫 봉의 시가 시각,
    마감 = 마지막 봉의 시가 시각 + 봉 간격. 조기폐장일에는 봉이 일찍 끊기므로
    마감도 자동으로 당겨지고, 휴장일에는 봉이 없으므로 `None`이 된다.
    별도의 거래소 캘린더나 네트워크 호출이 필요 없다.

    주의: 데이터가 결손된 날은 "일찍 마감한 날"과 구분되지 않는다. 그래도
    16:00으로 고정하는 것보다 낫다 — 없는 시간대에 체결을 만들어내지는 않기 때문이다.
    """

    def __init__(self, index: pd.DatetimeIndex, interval_minutes: int, market: str):
        self.market = market
        tz = _MARKET_TZ[market]
        local = index.tz_convert(tz)
        frame = pd.DataFrame({"ts": local}, index=local.date)
        grouped = frame.groupby(level=0)["ts"]
        closes = grouped.max() + pd.Timedelta(minutes=interval_minutes)
        self._sessions: dict[dtdate, Session] = {
            d: Session(open=grouped.min()[d].to_pydatetime(), close=closes[d].to_pydatetime())
            for d in grouped.min().index
        }

    def session(self, market: str, now: datetime) -> Session | None:
        if market != self.market:
            # 다른 시장의 세션을 이 봉들로 답할 수 없다. 추측해서 답하면 엉뚱한
            # 시장의 개장/마감을 이 시장 기준으로 판정하게 된다.
            raise ValueError(
                f"이 캘린더는 {self.market} 봉으로 만들어졌다 — {market} 세션을 알 수 없다"
            )
        return self._sessions.get(local_date(market, now))


class MultiMarketBarSessionCalendar:
    """시장별 `BarSessionCalendar`를 market 인자로 분배한다 — 백테스트 전용.

    `BarSessionCalendar` 하나는 생성 시 고정된 시장 하나의 봉만 안다(다른 시장을
    물으면 명시적으로 실패한다). 그런데 관심종목(watchlist) 전략들은 보유·평가
    종목마다 `market_of_symbol()`로 시장을 추론해 개별적으로 `is_market_open`을
    묻는다(mean_reversion/cross_momentum/orb_scan/intraday_scan/confluence,
    app/loop.py의 `_build_marks`) — KR/US가 섞인 유니버스를 리플레이하려면 백테스트도
    심볼의 실제 시장별로 세션을 답해야 한다.

    이 봉으로 다룰 수 있는 시장이 아니면(그 시장 심볼이 이 백테스트에 없다) 추측하지
    않고 명시적으로 실패한다 — 조용한 스킵은 run_cycle이 삼켜 "거래 0건"이라는
    가짜 성공으로 보인다(실측: mean_reversion이 그렇게 매 사이클 조용히 죽었다).
    """

    def __init__(self, calendars: dict[str, BarSessionCalendar]):
        self._calendars = calendars

    def session(self, market: str, now: datetime) -> Session | None:
        calendar = self._calendars.get(market)
        if calendar is None:
            raise ValueError(
                f"이 백테스트는 {sorted(self._calendars)} 시장 봉만 갖고 있다 — "
                f"{market} 세션을 알 수 없다 (그 시장 심볼이 --symbols에 없거나 "
                "데이터가 없다)"
            )
        return calendar.session(market, now)


class TossSessionCalendar:
    """Toss `GET /api/v1/market-calendar/{market}`로 **미리** 조회한다 (라이브용).

    하루에 한 번만 조회해 캐시한다(응답이 영업일 단위로만 바뀐다). 조회에 실패하면
    조용히 넘어가지 않고 경고를 남기고 `StaticSessionCalendar`로 내려간다 — 이때는
    조기폐장을 다시 모르게 되므로, 운영자가 로그에서 그 사실을 볼 수 있어야 한다.

    **[미검증]** 응답의 시각 필드 형식은 로컬에서 확인하지 못했다(Toss가 미등록 IP에
    403을 준다). `_parse_session`은 ISO 8601 문자열과 "HH:MM[:SS]" 두 형태를 모두
    받아들이고, 어느 쪽도 아니면 폴백한다.
    """

    def __init__(self, client, fallback: StaticSessionCalendar | None = None):
        self._client = client
        self._fallback = fallback or StaticSessionCalendar()
        self._cache: dict[tuple[str, dtdate], Session | None] = {}
        self._warned: set[tuple[str, dtdate]] = set()

    def session(self, market: str, now: datetime) -> Session | None:
        key = (market, local_date(market, now))
        if key in self._cache:
            return self._cache[key]
        try:
            payload = self._client.market_calendar(market, key[1])
            parsed = self._parse_session(market, key[1], payload)
        except Exception as e:
            if key not in self._warned:
                self._warned.add(key)
                logger.warning(
                    "%s 장 운영 캘린더 조회 실패 (%s: %s) — 고정 시간표로 폴백한다. "
                    "조기폐장일을 인식하지 못하므로 마감 전 청산이 어긋날 수 있다.",
                    market, type(e).__name__, e,
                )
            # 캐시하지 않는다 — 일시적 장애면 다음 사이클에 다시 시도해야 한다.
            return self._fallback.session(market, now)
        self._cache[key] = parsed
        logger.info(
            "%s %s 정규장: %s", market, key[1],
            f"{parsed.open:%H:%M} ~ {parsed.close:%H:%M}" if parsed else "휴장",
        )
        return parsed

    def _parse_session(self, market: str, day: dtdate, payload: dict) -> Session | None:
        today = (payload or {}).get("today") or {}
        if market == "KR":
            # KR 응답은 US와 형태가 다르다 — 세션이 `integrated` 아래 중첩된다
            # (`integrated`가 null이면 전일 휴장). QUICKREF 참고.
            regular = (today.get("integrated") or {}).get("regularMarket")
        else:
            regular = today.get("regularMarket")
        if not regular:
            return None  # 휴장 (QUICKREF 참고)
        tz = _MARKET_TZ[market]
        start = _to_datetime(regular.get("startTime"), day, tz)
        end = _to_datetime(regular.get("endTime"), day, tz)
        if start is None or end is None or end <= start:
            raise ValueError(f"장 운영 시각을 해석할 수 없음: {regular!r}")
        return Session(open=start, close=end)


def _to_datetime(value, day: dtdate, tz: ZoneInfo) -> datetime | None:
    """ISO 8601(오프셋 포함/미포함) 또는 "HH:MM[:SS]"을 market tz의 datetime으로.

    응답은 KST 기준이라고 문서화돼 있으므로, 오프셋이 붙어 오면 그대로 신뢰하고
    시장 tz로 변환한다. 오프셋이 없는 시각 문자열은 **요청한 거래일(day)**의 시장
    로컬 시각으로 본다.

    시각 전용 문자열은 pd.Timestamp로 판별하면 안 된다 — pandas(dateutil)는
    "09:00:00"을 1970년이 아니라 **호출 시점의 오늘 날짜**로 파싱하므로, 예전의
    `ts.year == 1970` 휴리스틱은 절대 걸리지 않는 죽은 분기였다. 그 경로로 빠지면
    세션 개장/마감이 조회한 거래일이 아니라 오늘 날짜로 조용히 계산된다(내일의
    캘린더를 미리 조회하는 순간 마감 판정이 통째로 어긋난다). 그래서 시각 전용
    형식은 pandas에 넘기기 전에 명시적으로 판별한다.
    """
    if not value:
        return None
    text = str(value)
    try:
        # "09:00" / "09:00:00" / "09:00:00.000" — 날짜 없이 시각만 온 경우.
        return datetime.combine(day, dtime.fromisoformat(text), tzinfo=tz)
    except ValueError:
        pass
    try:
        ts = pd.Timestamp(text)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is not None:
        return ts.tz_convert(tz).to_pydatetime()
    return ts.tz_localize(tz).to_pydatetime()
