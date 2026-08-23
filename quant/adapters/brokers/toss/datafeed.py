"""TossDataFeed — domain.interfaces.DataFeed 구현체.

quote()는 /prices 스냅샷, history()는 1분봉을 CSV 캐시에 저장해 두고 5m/15m는
그 1분봉을 quant.adapters.data.resample.resample_1m으로 리샘플링해서 만든다.
백테스트와 같은 함수를 써야 봉 경계가 어긋나지 않는다.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from quant.adapters.env import REPO_ROOT

import pandas as pd

from quant.adapters.data.resample import resample_1m
from quant.core.ports import DataSourceError
from quant.core.models import Quote

from .client import TossClient

logger = logging.getLogger(__name__)

# 저장소 루트는 adapters.env.REPO_ROOT 한 곳에서만 센다 — parents[3] 은
# `quant/` 였고, 토큰 캐시가 quant/data/cache 에 따로 쌓여 갈렸다
# (토스는 client_credentials 당 토큰이 1개라 캐시가 둘이면 서로를 무효화한다).
DEFAULT_CANDLE_CACHE_DIR = REPO_ROOT / "data" / "cache" / "candles"

# 1분봉은 1분에 하나만 생긴다 — 그보다 자주 재조회할 이유가 없다.
_CACHE_FRESH_SECONDS = 55
_INCREMENTAL_COUNT = 200  # 증분 갱신 시 1회 호출로 끝나는 크기

# 일봉(ATR 등)은 장중에 갱신되지 않는다. orb_scan은 심볼별 ATR을 매 사이클(5s)마다
# 다시 계산하는데, 캐시 없이 history("1d", ...)를 호출하면 워치리스트가 커질수록
# KR 09:05~09:10 진입 구간에서 사이클이 밀린다(차트 조회 5 TPS 제한) — 그 지연이
# 손절 평가까지 늦추므로 신선도가 낮아도 되는 일봉은 넉넉히 캐싱한다.
_DAILY_CACHE_FRESH_SECONDS = 600
_DAILY_CACHE_FAIL_SECONDS = 60  # 실패도 캐싱 — 안 그러면 실패하는 심볼이 매 사이클 재시도로 같은 지연을 반복한다
_DAILY_FETCH_COUNT = 90  # 요청 n과 무관하게 넉넉히 받아 캐시를 여러 n에 재사용한다

# quote()는 심볼당 개별 /prices 호출이었다 — 관심종목 상한이 시장별 20으로 오르며
# 한 사이클에 심볼 수만큼 호출이 나가 MARKET_DATA(10 TPS)를 소모했다(2026-08-12
# 실측 3.3% 429). /prices는 최대 200심볼 배치를 지원하므로, 이 TTL 안에 요청된
# 심볼 전체를 한 번에 묶어 조회하고 캐시에서 서빙한다 — 사이클당 실질 호출 1회.
_QUOTE_CACHE_FRESH_SECONDS = 2.0


def _interval_minutes(interval: str) -> int:
    """"15m" -> 15. resample_1m은 문자열이 아니라 정수 분을 받는다."""
    return int(interval.rstrip("m"))


class TossDataFeed:
    def __init__(
        self, client: TossClient, cache_dir: Path = DEFAULT_CANDLE_CACHE_DIR,
        symbols: list[str] | None = None,
    ) -> None:
        self._client = client
        self._cache_dir = Path(cache_dir)
        self._bars_1m: dict[str, pd.DataFrame] = {}
        self._bars_1d: dict[str, tuple[float, pd.DataFrame]] = {}  # symbol -> (cached_at, bars)
        self._daily_failures: dict[str, tuple[float, DataSourceError]] = {}  # symbol -> (failed_at, error)
        # quote 배치 조회 — symbols(알려진 유니버스)를 미리 알면 첫 호출부터 전부
        # 묶어서 받는다. 모르면(symbols=None) quote()가 불릴 때마다 그 심볼을
        # 누적해 다음 배치부터 포함시킨다 — 둘 다 아래 _refresh_quotes가 처리한다.
        self._quote_symbols: set[str] = set(symbols or [])
        self._quote_cache: dict[str, Quote | None] = {}
        self._quote_cache_at: float = 0.0

    def quote(self, symbol: str) -> Quote | None:
        self._quote_symbols.add(symbol)
        now = time.monotonic()
        stale = (now - self._quote_cache_at) >= _QUOTE_CACHE_FRESH_SECONDS
        if stale or symbol not in self._quote_cache:
            self._refresh_quotes()
        return self._quote_cache.get(symbol)

    def _refresh_quotes(self) -> None:
        """이 인스턴스가 지금까지 요청받은 심볼 전부를 **한 번의** /prices 배치
        호출로 갱신한다. 사이클마다 여러 심볼이 quote()를 부르더라도(전략의
        포지션 관리, risk.approve 사이징, PaperBroker 체결가 등) 실제 HTTP 호출은
        캐시 TTL(_QUOTE_CACHE_FRESH_SECONDS) 안에서 최대 1회다."""
        symbols = sorted(self._quote_symbols)
        if not symbols:
            return
        try:
            result = self._client.prices(symbols)
        except Exception as e:
            # 벤더 예외는 여기서 끊고 도메인 예외로 바꾼다. None으로 위장하면
            # MarketDataService가 정상 응답으로 오인해 폴백하지 않는다. 캐시는
            # 갱신하지 않는다 — 실패를 성공처럼 캐싱하지 않는다.
            raise DataSourceError(
                f"toss quote(배치 {len(symbols)}종목) 실패: {type(e).__name__}: {e}"
            ) from e
        fresh: dict[str, Quote] = {}
        for row in result:
            sym = row.get("symbol")
            if not sym:
                continue
            try:
                fresh[sym] = Quote(
                    symbol=sym,
                    ts=pd.Timestamp(row["timestamp"]).to_pydatetime(),
                    price=float(row["lastPrice"]),
                )
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("toss quote 배치 응답 파싱 실패 (%s): %s: %s", sym, type(e).__name__, e)
        for sym in symbols:
            # 조회는 성공했지만 응답에 없는 심볼 — 실패가 아니라 "시세 없음"(None).
            self._quote_cache[sym] = fresh.get(sym)
        self._quote_cache_at = time.monotonic()

    def _cache_path(self, symbol: str) -> Path:
        return self._cache_dir / f"{symbol}_1m.csv"

    def _load_1m(self, symbol: str, n: int) -> pd.DataFrame:
        """1분봉을 증분으로 유지한다.

        엔진은 poll_seconds(기본 10초)마다 도는데 1분봉은 1분에 하나만 생긴다.
        매 사이클 전체 구간을 다시 받으면 심볼당 5회씩 API를 때리게 되고
        (MARKET_DATA_CHART TPS 5), 토스 토큰이 유휴 시 빨리 만료되는 특성과
        겹쳐 매 사이클 재인증까지 유발한다. 그래서 캐시가 신선하면 호출하지
        않고, 오래됐으면 최신 구간만 얇게 받아 병합한다.
        """
        cached = self._bars_1m.get(symbol)
        if cached is None:
            cached = self._read_disk_cache(symbol)

        if cached is not None and not cached.empty:
            age = (pd.Timestamp.now(tz=cached.index[-1].tz) - cached.index[-1]).total_seconds()
            if age < _CACHE_FRESH_SECONDS:
                self._bars_1m[symbol] = cached
                return cached
            fetch_count = _INCREMENTAL_COUNT   # 최신 구간만
        else:
            fetch_count = max(n * 20, 200)     # 콜드 스타트 — 워밍업 구간까지 받는다

        try:
            fresh = self._client.candles(symbol, interval="1m", count=fetch_count)
        except Exception as e:
            if cached is not None and not cached.empty:
                # 오래됐지만 실제 데이터가 있다 — 빈 값보다 낫고, 실패도 아니다.
                logger.warning("TossDataFeed 1분봉 갱신 실패, 캐시 사용 (%s): %s: %s",
                               symbol, type(e).__name__, e)
                return cached
            raise DataSourceError(
                f"toss 1분봉({symbol}) 조회 실패이고 캐시도 없음: {type(e).__name__}: {e}") from e

        merged = fresh if cached is None or cached.empty else pd.concat([cached, fresh])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self._bars_1m[symbol] = merged
        self._write_disk_cache(symbol, merged)
        return merged

    def _read_disk_cache(self, symbol: str) -> pd.DataFrame | None:
        path = self._cache_path(symbol)
        if not path.exists():
            return None
        try:
            return pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception as e:
            logger.warning("1분봉 캐시 읽기 실패 (%s): %s: %s", symbol, type(e).__name__, e)
            return None

    def _write_disk_cache(self, symbol: str, bars: pd.DataFrame) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            bars.to_csv(self._cache_path(symbol))
        except Exception as e:  # 캐시 쓰기 실패가 거래를 막아선 안 된다
            logger.warning("1분봉 캐시 쓰기 실패 (%s): %s: %s", symbol, type(e).__name__, e)

    def _load_1d(self, symbol: str, n: int) -> pd.DataFrame:
        """일봉을 사이클 단위로 캐싱한다. 파일 상단 `_DAILY_CACHE_*` 상수 설명 참고."""
        now = time.monotonic()

        failure = self._daily_failures.get(symbol)
        if failure is not None:
            failed_at, error = failure
            if now - failed_at < _DAILY_CACHE_FAIL_SECONDS:
                raise error
            del self._daily_failures[symbol]

        cached = self._bars_1d.get(symbol)
        if cached is not None:
            cached_at, bars = cached
            if now - cached_at < _DAILY_CACHE_FRESH_SECONDS:
                return bars

        try:
            bars = self._client.candles(symbol, interval="1d", count=max(n, _DAILY_FETCH_COUNT))
        except Exception as e:
            error = DataSourceError(f"toss 일봉({symbol}) 조회 실패: {type(e).__name__}: {e}")
            self._daily_failures[symbol] = (now, error)
            raise error from e

        self._bars_1d[symbol] = (now, bars)
        return bars

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        if interval == "1d":
            return self._load_1d(symbol, n).tail(n)

        bars_1m = self._load_1m(symbol, n)
        if interval == "1m":
            return bars_1m.tail(n)

        return resample_1m(bars_1m, _interval_minutes(interval)).tail(n)
