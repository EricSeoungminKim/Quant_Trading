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
- health()로 "폴백이 조용히 일어나지 않았는지"를 항상 드러낸다.
- history()는 클록 기준으로 완성되지 않은 봉을 마지막 방어선으로 한 번 더 걸러낸다
  (소스가 sloppy해도 look-ahead가 새어나가지 않게).
- quote()/history() 모두 사이클 내 공유 캐시를 갖는다 — 전략이 여러 개 붙어도
  소스(브로커 API)를 때리는 횟수는 심볼 수에 비례하지, 전략 수에 비례하지 않는다.
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

from quant.core.ports import ColdFetchBudgetExceeded, Clock, DataSourceError
from quant.core.models import Quote

logger = logging.getLogger(__name__)

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

# 봉 캐시 항목 수 상한. 심볼×interval 단위로만 쌓이므로(같은 조합의 지난 경계는
# 저장 시 즉시 버린다) 정상 운용에서는 유니버스 크기(수십)를 넘지 않는다. 이 상한은
# 유니버스가 폭주하거나 예상 못 한 심볼이 밀려들 때를 막는 백스톱이다 — 이 저장소는
# 1.8GB EC2에서 무인으로 며칠씩 돌기 때문에 "언젠가는 정리되겠지"가 통하지 않는다.
_DEFAULT_BAR_CACHE_MAX_ENTRIES = 512

# 라우트 실패 로그 스로틀(2026-09-05, D5/D6) — 4일간 146,451줄(WARNING 이상)의
# 대부분이 키움 실시간(stale 틱)/미국(쿨다운) 라우트가 매 quote() 호출마다
# "실패, 폴백 시도"를 WARNING으로 찍은 것이었는데, 두 경우 다 다음 라우트
# (toss)가 정상 응답해 실제로는 데이터 손실이 아니었다(설계대로 동작한
# 폴백). 규칙: 그 호출이 결국 다른 라우트로 서빙됐으면 항상 DEBUG(정상
# 동작) — 끝내 아무 라우트도 못 살렸을 때(진짜 데이터 손실)만 (route,symbol)별
# 첫 발생은 WARNING, 10분 안의 재발은 DEBUG로 눌러 담고, 그 라우트가 다시
# 성공하면 WARNING 한 줄로 회복을 알린다.
_FAILURE_WARN_INTERVAL_SECONDS = 600.0


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


def _interval_minutes(interval: str) -> int:
    return 24 * 60 if interval == "1d" else int(interval.rstrip("m"))


def _filter_completed_bars(df: pd.DataFrame, interval: str, now: datetime) -> pd.DataFrame:
    """봉 마감 시각(open + interval)이 now 이후인 행은 미완성봉으로 간주하고 버린다 —
    소스가 sloppy하게 미래 봉을 반환해도 여기서 최종적으로 막는다.

    불린 마스크 인덱싱은 **항상 새 프레임을 만든다**. 봉 캐시가 같은 프레임을 여러
    전략에 나눠주는데, 호출자가 지표 컬럼을 붙이는 일이 흔하다 — 이 복사가 캐시
    원본을 보호한다. 여기를 뷰 반환으로 바꾸면 한 전략의 계산이 다른 전략의
    입력을 오염시킨다.
    """
    if df.empty:
        return df
    bar_close = df.index + pd.Timedelta(minutes=_interval_minutes(interval))
    return df[bar_close <= now]


def _bar_boundary(now: datetime, interval: str) -> datetime:
    """now가 속한 봉의 시작 시각(= interval 크기로 내림).

    캐시 유효성 판정에 TTL이 아니라 이 경계를 쓰는 이유: **1분봉은 1분에 한 번만
    바뀐다.** TTL이면 "2초 전 값"이라는 이유로 같은 분의 동일한 데이터를 다시
    받아오지만, 경계를 키에 넣으면 같은 분 안의 모든 호출이 정확히 한 번만
    소스를 때리고, 분이 바뀌는 순간 지체 없이 새 봉을 받는다.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    step = _interval_minutes(interval) * 60
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % step), tz=timezone.utc)


@dataclass
class _BarCacheEntry:
    frame: pd.DataFrame  # 정규화만 된 원본(완성봉 필터·tail은 매 호출 새로 적용한다)
    n: int  # 이 프레임을 받을 때 소스에 요청한 개수


class MarketDataService:
    """domain.interfaces.DataFeed를 구현하는 Anti-Corruption Layer.

    호출자는 심볼/interval만 요청한다 — 어떤 소스가 응답했는지는 health()로만 드러난다.
    1차 소스가 실패하면 우선순위대로 다음 소스로 폴백하되, 조용히 넘어가지 않고
    경고 로그 + health() 상태로 남긴다.
    """

    def __init__(self, routes: list[SourceRoute], clock: Clock,
                 quote_cache_seconds: float = 0.0,
                 bar_cache_enabled: bool = True,
                 bar_cache_max_entries: int = _DEFAULT_BAR_CACHE_MAX_ENTRIES,
                 cold_fetch_budget_per_cycle: int | None = None) -> None:
        if cold_fetch_budget_per_cycle is not None and cold_fetch_budget_per_cycle <= 0:
            raise ValueError(
                "cold_fetch_budget_per_cycle는 양수이거나 None(무제한)이어야 한다: "
                f"{cold_fetch_budget_per_cycle!r}"
            )
        self._routes = list(routes)
        self._clock = clock
        self._health: dict[str, SourceHealth] = {r.name: SourceHealth(name=r.name) for r in self._routes}
        # 가장 최근 요청이 **어떤 소스로도** 서빙되지 못했는가. health().degraded의
        # 근거다 — 소스 하나의 실패가 아니라 최종 데이터 손실만 알람 대상이다.
        # 기동 직후엔 아직 요청이 없으므로 False(정상)에서 시작한다.
        self._last_unserved = False
        # 사이클 내 quote 캐시. 0이면 비활성(기존 동작 그대로).
        self._quote_cache_seconds = float(quote_cache_seconds)
        self._quote_cache: dict[str, tuple[float, Quote | None]] = {}
        # 봉 캐시. 키 = (symbol, interval, bar_boundary), 값 = _BarCacheEntry.
        self._bar_cache_enabled = bool(bar_cache_enabled)
        self._bar_cache_max_entries = int(bar_cache_max_entries)
        self._bar_cache: dict[tuple[str, str, datetime], _BarCacheEntry] = {}
        self._bar_cache_hits = 0
        self._bar_cache_misses = 0
        self._bar_source_calls = 0
        # 사이클당 콜드 페치 예산(None=무제한, 기존 동작 그대로). 유니버스 롤
        # 직후처럼 신규 심볼 수십 개의 캐시 미스가 한 사이클에 몰리면 그 사이클
        # 자체가 수십 회 소스 호출로 18~23초까지 늘어나고, 그동안 보유 포지션의
        # 손절 판정(quote() 기반, 이 예산과 무관)도 사이클이 끝날 때까지 대기한다.
        # 예산을 넘는 캐시 미스는 소스를 때리지 않고 즉시 거부해 다음 사이클(들)로
        # 미룬다 — 콜드 페치 총량은 그대로지만 한 사이클에 몰리지 않게 편다.
        self._cold_fetch_budget_per_cycle = cold_fetch_budget_per_cycle
        self._cold_fetch_count = 0
        # 실패 로그 스로틀 상태(D5/D6) — (route_name, symbol)별 마지막 WARNING
        # 시각(monotonic)과 "진짜 데이터 손실"로 한 번이라도 WARNING을 찍은 적
        # 있는지. time.monotonic()을 쓰는 이유는 quote 캐시 TTL과 같다 —
        # self._clock은 백테스트에서 점프하므로 스로틀 판정에 쓸 수 없다.
        self._route_last_warned: dict[tuple[str, str], float] = {}
        self._route_was_failing: set[tuple[str, str]] = set()

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
        pending: list[tuple[tuple[str, str], str]] = []
        for route in self._candidates(Capability.QUOTE, symbol):
            try:
                result = route.source.quote(symbol)
            except Exception as e:
                self._record_failure(route.name, e)
                pending.append((
                    (route.name, symbol),
                    "MarketDataService: %s.quote(%s) 실패, 폴백 시도: %s: %s"
                    % (route.name, symbol, type(e).__name__, e),
                ))
                continue
            self._record_success(route.name)
            self._last_unserved = False  # 폴백이었더라도 끝내 받았으면 정상이다
            self._flush_route_failures(pending, fallback_succeeded=True)
            self._log_route_recovery(route.name, symbol)
            return None if result is None else _normalize_quote(result)
        # 후보 소스를 전부 소진 — 이때만 데이터 손실이다.
        self._last_unserved = True
        self._flush_route_failures(pending, fallback_succeeded=False)
        return None

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        """같은 봉 경계 안에서는 (symbol, interval)당 소스를 정확히 한 번만 친다.

        전략 8개가 같은 20종목의 1분봉을 보는 구성에서 캐시가 없으면 사이클마다
        160회 브로커 API를 때린다 — Toss MARKET_DATA는 10 TPS라 그 자체로 rate
        limit이고, 순수 껍질(PureStrategyShell)이 정적 DataNeeds로 매 사이클 전량을
        다시 요청하기 때문에 전략을 늘릴수록 선형으로 악화된다. 경계 캐시를 끼우면
        같은 구성이 20회로 떨어진다(전략 수와 무관해진다).

        `bar_cache_enabled=False`면 이 경로는 통째로 비활성이고 동작은 캐시 도입
        전과 100% 동일하다.
        """
        if not self._bar_cache_enabled:
            return self._finalize_bars(self._history_raw(symbol, interval, n), interval, n)

        key = (symbol, interval, _bar_boundary(self._clock.now(), interval))
        entry = self._bar_cache.get(key)
        # 판정 기준은 캐시된 **행 수**가 아니라 그때 요청한 n이다. 소스가 가진
        # 봉이 요청보다 적을 수 있는데(신규 상장, 얕은 히스토리) 행 수로 판정하면
        # n=200 요청에 30개를 받아 캐시한 뒤 n=50 요청이 매번 miss가 나 캐시가
        # 영영 안 먹는다. "이미 그만큼 이상 요청해봤다"가 옳은 기준이다.
        if entry is not None and n <= entry.n:
            self._bar_cache_hits += 1
            return self._finalize_bars(entry.frame, interval, n)

        self._bar_cache_misses += 1
        if (
            self._cold_fetch_budget_per_cycle is not None
            and self._cold_fetch_count >= self._cold_fetch_budget_per_cycle
        ):
            # 소스는 때리지 않는다 — 예산 소진은 "시도해서 실패"가 아니라 "이번
            # 사이클엔 아예 시도하지 않음"이다. 호출부(PureStrategyShell._snapshot)는
            # 이 예외를 감싸지 않으므로 loop.run_cycle의 전략별 try/except까지 올라가
            # 그 전략의 이번 사이클만 스킵된다 — 다음 사이클에 예산이 리셋되면 재시도된다.
            raise ColdFetchBudgetExceeded(
                f"콜드 페치 예산 초과 ({self._cold_fetch_budget_per_cycle}/사이클, "
                f"{symbol} {interval}) — 다음 사이클"
            )
        self._cold_fetch_count += 1
        frame = self._history_raw(symbol, interval, n)
        # **실패는 캐시하지 않는다.** 빈 프레임을 캐시하면 그 봉 내내(1분봉이면 1분,
        # 일봉이면 하루) 모든 전략이 데이터 없이 돌아간다 — 손절 판정이 조용히 멈춘다.
        if not frame.empty:
            self._store_bars(key, frame, n)
        return self._finalize_bars(frame, interval, n)

    def reset_cycle_budget(self) -> None:
        """콜드 페치 예산 카운터를 리셋한다. loop가 사이클 경계마다 호출한다
        (봉 캐시 통계 로그 배선 옆, `run_cycle` 밖 while 루프 최상단).

        `bar_cache_stats()`의 hits/misses/source_calls는 런타임 전체 누적값으로
        일부러 리셋하지 않는다(관측 목적이 다르다: 저건 "런타임 동안 캐시가 얼마나
        일했나", 이건 "이번 사이클에 남은 예산이 얼마나 되나"). 예산이 없으면
        (None) 카운터는 그냥 안 쓰인다 — 호출해도 무해하다."""
        self._cold_fetch_count = 0

    def _finalize_bars(self, frame: pd.DataFrame, interval: str, n: int) -> pd.DataFrame:
        """완성봉 필터와 tail(n)은 **캐시 뒤가 아니라 앞**에서 매번 새로 적용한다.
        캐시에는 정규화만 된 원본이 들어가므로, 같은 경계 안이라도 look-ahead 방어선은
        항상 현재 클록 기준으로 다시 계산된다(캐시가 있든 없든 결과가 같아야 한다)."""
        return _filter_completed_bars(frame, interval, self._clock.now()).tail(n)

    def _store_bars(self, key: tuple[str, str, datetime], frame: pd.DataFrame, n: int) -> None:
        symbol, interval, boundary = key
        # 키에 경계가 들어 있으므로 정리하지 않으면 분마다 새 항목이 쌓인다.
        # 같은 (symbol, interval)의 지난 경계는 다시 쓰이지 않으니 즉시 버린다 —
        # 이걸로 캐시 크기가 상한이 아니라 유니버스 크기에 묶인다.
        for stale in [
            k for k in self._bar_cache
            if k[0] == symbol and k[1] == interval and k[2] != boundary
        ]:
            del self._bar_cache[stale]
        self._bar_cache[key] = _BarCacheEntry(frame=frame, n=n)
        # 백스톱: 그래도 상한을 넘으면 가장 오래 전에 들어온 항목부터 버린다
        # (dict는 삽입 순서를 유지한다).
        while len(self._bar_cache) > self._bar_cache_max_entries:
            del self._bar_cache[next(iter(self._bar_cache))]

    def bar_cache_stats(self) -> dict[str, int]:
        """봉 캐시 계측 스냅샷. `source_calls`는 소스 `history()` 호출 **시도** 횟수다
        (폴백 시도 포함) — 실제로 브로커 API에 나간 요청 수와 일치해야 관측값으로서
        의미가 있다."""
        return {
            "hits": self._bar_cache_hits,
            "misses": self._bar_cache_misses,
            "source_calls": self._bar_source_calls,
        }

    def _history_raw(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        # 소스에는 n+1을 요청한다(2026-08-24). 어댑터들은 요청 개수로 잘라
        # 반환하는데, _finalize_bars의 완성봉 필터가 형성 중인 마지막 봉을 버리면 소비자는
        # n-1개를 받는다 — cross_momentum(월요일 **장중** 리밸런스)이 일봉
        # 21개를 요구하며 항상 20개를 받아 '랭킹봉부족: 21'로 태어나서 한 번도
        # 랭킹하지 못했다(전 종목, 전 회차). 형성 중일 수 있는 봉은 어느
        # interval이든 마지막 1개뿐이므로 여유분은 +1이면 충분하고, 마지막
        # tail(n)이 초과분을 잘라 장 마감 후에도 결과는 정확히 n개다.
        pending: list[tuple[tuple[str, str], str]] = []
        for route in self._candidates(Capability.BARS, symbol, interval):
            self._bar_source_calls += 1
            try:
                result = route.source.history(symbol, interval, n + 1)
            except Exception as e:
                self._record_failure(route.name, e)
                pending.append((
                    (route.name, symbol),
                    "MarketDataService: %s.history(%s,%s) 실패, 폴백 시도: %s: %s"
                    % (route.name, symbol, interval, type(e).__name__, e),
                ))
                continue
            self._record_success(route.name)
            self._last_unserved = False
            self._flush_route_failures(pending, fallback_succeeded=True)
            self._log_route_recovery(route.name, symbol)
            return _normalize_frame(result)
        self._last_unserved = True
        self._flush_route_failures(pending, fallback_succeeded=False)
        return pd.DataFrame(columns=_OHLCV_COLUMNS)

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

    def _flush_route_failures(
        self, pending: list[tuple[tuple[str, str], str]], *, fallback_succeeded: bool,
    ) -> None:
        """이번 호출에서 모은 라우트 실패들을 로그로 낸다.

        `fallback_succeeded=True`(다른 라우트가 결국 응답했다)면 항상 DEBUG —
        설계대로 동작한 폴백은 소음이지 경고가 아니다. `False`(끝내 아무 라우트도
        못 살렸다 — 진짜 데이터 손실)면 (route,symbol)별 스로틀을 적용한다:
        첫 발생 또는 `_FAILURE_WARN_INTERVAL_SECONDS` 경과 후 재발만 WARNING,
        그 사이 반복은 DEBUG.
        """
        if not pending:
            return
        if fallback_succeeded:
            for _ident, message in pending:
                logger.debug(message)
            return
        now = time.monotonic()
        for ident, message in pending:
            self._route_was_failing.add(ident)
            last = self._route_last_warned.get(ident)
            if last is None or (now - last) >= _FAILURE_WARN_INTERVAL_SECONDS:
                self._route_last_warned[ident] = now
                logger.warning(message)
            else:
                logger.debug(message)

    def _log_route_recovery(self, route_name: str, symbol: str) -> None:
        """그 (route, symbol)이 "진짜 데이터 손실" 스로틀 상태에서 회복됐으면
        WARNING 한 줄로 알린다. 폴백이 계속 대신 응답해준 라우트(한 번도
        `_route_was_failing`에 들지 않은 경우)는 애초에 경고를 낸 적이 없으므로
        여기서도 조용하다."""
        ident = (route_name, symbol)
        if ident in self._route_was_failing:
            self._route_was_failing.discard(ident)
            self._route_last_warned.pop(ident, None)
            logger.warning("MarketDataService: %s(%s) 정상 회복", route_name, symbol)
