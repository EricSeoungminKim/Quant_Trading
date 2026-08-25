"""MarketDataService — 시장 데이터 Anti-Corruption Layer.

Toss는 시세는 있지만 1분봉 히스토리가 며칠뿐이고 웹소켓이 없다. 로컬 Parquet에는
깊은 과거 히스토리가 있다. Kiwoom은 나중에 실시간 스트리밍을 제공할 수도 있다.
전략 코드가 "이 심볼은 어디서 오는가"를 알아야 한다면 의존성 방향 불변식이 깨진다.

이 모듈은 domain.interfaces.DataFeed를 구현하는 단일 진입점을 제공하고, 내부적으로
캐퍼빌리티 기반 라우팅 정책(SourceRoute 리스트, 순서=우선순위)에 따라 실제 소스로
위임한다. 규칙:

- 벤더 payload는 여기를 절대 안쪽으로 통과하지 않는다 — 각 소스가 이미 domain
  모델(Quote)/문서화된 OHLCV DataFrame 형태로 변환해서 넘겨준다고 가정한다.
- 라우팅은 벤더 이름이 아니라 캐퍼빌리티(Capability)와 SourceRoute 선언(symbols/
  intervals)으로만 결정한다.
- provenance()/health()로 "누가 응답했는지"와 "폴백이 조용히 일어나지 않았는지"를
  항상 드러낸다.
- history()는 클록 기준으로 완성되지 않은 봉을 마지막 방어선으로 한 번 더 걸러낸다
  (소스가 sloppy해도 look-ahead가 새어나가지 않게).
- brokers/를 import하지 않는다 — 소스는 코디네이터가 주입한다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

import pandas as pd

from quant.core.ports import Clock
from quant.core.models import Quote

logger = logging.getLogger(__name__)

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class Capability(str, Enum):
    QUOTE = "quote"
    BARS = "bars"
    STREAM = "stream"  # 실시간 스트리밍용 예약 캐퍼빌리티 — 아직 구현하지 않음


@runtime_checkable
class MarketDataSource(Protocol):
    """서비스가 라우팅 대상으로 삼는 최소 표면. StubDataFeed/HistoryDataFeed/
    TossDataFeed 등 기존 DataFeed 구현체는 구조적으로 이미 이 Protocol을 만족한다."""

    def quote(self, symbol: str) -> Quote | None: ...

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame: ...


@dataclass(frozen=True)
class SourceRoute:
    """라우팅 정책 한 항목. MarketDataService에 넘기는 리스트의 순서가 곧 우선순위
    (먼저 오는 항목이 더 높은 우선순위)다. 심볼/벤더 이름을 라우팅 로직에 하드코딩하지
    않기 위해 이 선언만으로 "무엇을 서빙할 수 있는지"를 표현한다."""

    name: str
    source: MarketDataSource
    capabilities: frozenset[Capability]
    symbols: frozenset[str] | None = None  # None = 모든 심볼 지원
    intervals: frozenset[str] | None = None  # BARS 전용. None = 모든 interval 지원


@dataclass
class SourceHealth:
    name: str
    healthy: bool = True
    consecutive_failures: int = 0
    last_error: str | None = None


@dataclass
class ServiceHealth:
    sources: dict[str, SourceHealth]
    degraded: bool


def _normalize_quote(quote: Quote) -> Quote:
    ts = quote.ts
    ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)
    return replace(quote, ts=ts)


def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df
    if df.index.tz is None:
        return df.tz_localize("UTC")
    if str(df.index.tz) != "UTC":
        return df.tz_convert("UTC")
    return df


def _filter_completed_bars(df: pd.DataFrame, interval: str, now: datetime) -> pd.DataFrame:
    """봉 마감 시각(open + interval)이 now 이후인 행은 미완성봉으로 간주하고 버린다 —
    소스가 sloppy하게 미래 봉을 반환해도 여기서 최종적으로 막는다."""
    if df.empty:
        return df
    minutes = 24 * 60 if interval == "1d" else int(interval.rstrip("m"))
    bar_close = df.index + pd.Timedelta(minutes=minutes)
    return df[bar_close <= now]


class MarketDataService:
    """domain.interfaces.DataFeed를 구현하는 Anti-Corruption Layer.

    호출자는 심볼/interval만 요청한다 — 어떤 소스가 응답했는지는 provenance()/health()로만
    드러난다. 1차 소스가 실패하면 우선순위대로 다음 소스로 폴백하되, 조용히 넘어가지 않고
    경고 로그 + health() 상태로 남긴다.
    """

    def __init__(self, routes: list[SourceRoute], clock: Clock,
                 quote_cache_seconds: float = 0.0) -> None:
        self._routes = list(routes)
        self._clock = clock
        self._health: dict[str, SourceHealth] = {r.name: SourceHealth(name=r.name) for r in self._routes}
        self._provenance: dict[tuple[str, Capability], str] = {}
        # 가장 최근 요청이 **어떤 소스로도** 서빙되지 못했는가. health().degraded의
        # 근거다 — 소스 하나의 실패가 아니라 최종 데이터 손실만 알람 대상이다.
        # 기동 직후엔 아직 요청이 없으므로 False(정상)에서 시작한다.
        self._last_unserved = False
        # 사이클 내 quote 캐시. 0이면 비활성(기존 동작 그대로).
        self._quote_cache_seconds = float(quote_cache_seconds)
        self._quote_cache: dict[str, tuple[float, Quote | None]] = {}

    def quote(self, symbol: str) -> Quote | None:
        """한 사이클 안에서 같은 심볼을 여러 번 물어도 소스는 한 번만 친다.

        속도만의 문제가 아니다. 한 사이클에서 `quote()`는 최소 세 번 불린다 —
        전략의 포지션 관리, `RiskManagerImpl.approve`의 사이징, `PaperBroker`의
        체결가. 캐시가 없으면 **셋이 서로 다른 HTTP 응답을 본다**: 승인 시점 가격으로
        수량을 계산해 놓고 다른 가격에 체결되므로 주문 금액 상한 검증이 무의미해지고,
        체결 로그와 리스크 판단이 다른 이야기를 하게 된다. 캐시는 그 셋을 같은
        스냅샷으로 묶는다.

        TTL은 반드시 폴링 주기보다 짧아야 한다(기본 2초 vs poll 10초) — 사이클이
        바뀌면 새 시세를 받아야 한다. 시각은 `time.monotonic()`을 쓴다: 벽시계
        보정(NTP)이나 서머타임에 흔들리지 않아야 하고, `self._clock`은 백테스트에서
        점프하므로 캐시 만료 판정에 쓸 수 없다.
        """
        if self._quote_cache_seconds > 0:
            hit = self._quote_cache.get(symbol)
            if hit is not None and (time.monotonic() - hit[0]) < self._quote_cache_seconds:
                return hit[1]

        result = self._quote_uncached(symbol)
        if self._quote_cache_seconds > 0:
            self._quote_cache[symbol] = (time.monotonic(), result)
        return result

    def _quote_uncached(self, symbol: str) -> Quote | None:
        for route in self._candidates(Capability.QUOTE, symbol):
            try:
                result = route.source.quote(symbol)
            except Exception as e:
                self._record_failure(route.name, e)
                logger.warning(
                    "MarketDataService: %s.quote(%s) 실패, 폴백 시도: %s: %s",
                    route.name, symbol, type(e).__name__, e,
                )
                continue
            self._record_success(route.name)
            self._provenance[(symbol, Capability.QUOTE)] = route.name
            self._last_unserved = False  # 폴백이었더라도 끝내 받았으면 정상이다
            return None if result is None else _normalize_quote(result)
        # 후보 소스를 전부 소진 — 이때만 데이터 손실이다.
        self._last_unserved = True
        return None

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        # 소스에는 n+1을 요청한다(2026-08-24). 어댑터들은 요청 개수로 잘라
        # 반환하는데, 아래 완성봉 필터가 형성 중인 마지막 봉을 버리면 소비자는
        # n-1개를 받는다 — cross_momentum(월요일 **장중** 리밸런스)이 일봉
        # 21개를 요구하며 항상 20개를 받아 '랭킹봉부족: 21'로 태어나서 한 번도
        # 랭킹하지 못했다(전 종목, 전 회차). 형성 중일 수 있는 봉은 어느
        # interval이든 마지막 1개뿐이므로 여유분은 +1이면 충분하고, 마지막
        # tail(n)이 초과분을 잘라 장 마감 후에도 결과는 정확히 n개다.
        for route in self._candidates(Capability.BARS, symbol, interval):
            try:
                result = route.source.history(symbol, interval, n + 1)
            except Exception as e:
                self._record_failure(route.name, e)
                logger.warning(
                    "MarketDataService: %s.history(%s,%s) 실패, 폴백 시도: %s: %s",
                    route.name, symbol, interval, type(e).__name__, e,
                )
                continue
            self._record_success(route.name)
            self._provenance[(symbol, Capability.BARS)] = route.name
            self._last_unserved = False
            normalized = _normalize_frame(result)
            completed = _filter_completed_bars(normalized, interval, self._clock.now())
            return completed.tail(n)
        self._last_unserved = True
        return pd.DataFrame(columns=_OHLCV_COLUMNS)

    def provenance(self, symbol: str, capability: Capability) -> str | None:
        """가장 최근에 해당 심볼/캐퍼빌리티를 실제로 서빙한 소스 이름 (없으면 None)."""
        return self._provenance.get((symbol, capability))

    def health(self) -> ServiceHealth:
        """소스별 현재 상태 스냅샷과 전체 degraded 여부.

        `degraded`는 **"끝내 데이터를 못 받았다"**는 뜻이다 — 체인 어딘가에서 소스
        하나가 실패했다는 뜻이 아니다. 폴백이 성공했다면 그건 설계대로 동작한 것이지
        장애가 아니다.

        예전 정의(`any(not h.healthy)`)는 US 세션 내내 오보를 냈다: 키움 웹소켓은
        해외 틱을 구조적으로 못 주므로 US 심볼에 대해 **항상** 실패 상태인데,
        Toss 폴백이 정상 서빙해도 degraded=True가 되어 `⚠️ 시세 조회 연속 3회 실패`
        알림이 5분마다 떴다(2026-08-12 실측). **항상 울리는 경고는 없는 경고보다
        나쁘다** — 진짜 장애를 무시하게 만든다.

        소스별 실패는 사라지지 않는다: `sources` 스냅샷과 폴백 경고 로그에 그대로
        남아 관측 가능하다. 여기서 바뀌는 것은 **알람을 울릴 기준**뿐이다.
        """
        sources = {name: replace(h) for name, h in self._health.items()}
        return ServiceHealth(sources=sources, degraded=self._last_unserved)

    def _candidates(self, capability: Capability, symbol: str, interval: str | None = None) -> list[SourceRoute]:
        candidates = []
        for route in self._routes:
            if capability not in route.capabilities:
                continue
            if route.symbols is not None and symbol not in route.symbols:
                continue
            if capability is Capability.BARS and route.intervals is not None and interval not in route.intervals:
                continue
            candidates.append(route)
        return candidates

    def _record_failure(self, name: str, exc: Exception) -> None:
        h = self._health[name]
        h.consecutive_failures += 1
        h.healthy = False
        h.last_error = f"{type(exc).__name__}: {exc}"

    def _record_success(self, name: str) -> None:
        h = self._health[name]
        h.consecutive_failures = 0
        h.healthy = True
        h.last_error = None
