"""Parquet 파티션(quant.collect.quotes.backfill이 쓴 것)에서 읽는 DataFeed 구현체.

look-ahead 금지(ADR-4, domain.interfaces.DataFeed): history()/quote()는 set_now()로
지정된 리플레이 시각 이전 완성봉만 반환한다. 1분봉을 원본 그대로 읽어 interval별로
즉석 리샘플한다(resample_1m 재사용) — write 시점엔 리샘플하지 않는다는 ingest의
순수성 원칙과 대칭을 이룬다.

1분봉이 없는 심볼(예: yfinance처럼 native interval이 15m인 소스로만 백필된 경우)은
`quant/collect/quotes/backfill.py`가 `data/history/{symbol}/{interval}/...`에 쓴
native 파티션을 읽는다 — 이 경우 요청한 interval과 native 저장 간격이 정확히 일치할
때만 서빙한다. 없는 간격을 리샘플/업샘플로 지어내지 않는다(HONESTY CONSTRAINT,
docs/data-availability.md) — 예컨대 15분봉만 있는데 5분봉을 요청하면 빈 결과를
반환한다.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from quant.adapters.data.resample import resample_1m
from quant.core.models import Quote

logger = logging.getLogger(__name__)

_COLUMNS = ["open", "high", "low", "close", "volume"]

# (root, symbol) -> (1분봉, {interval: native봉}). 파티션 파일은 백테스트/연구가
# 도는 동안 바뀌지 않으므로 프로세스 수명 동안 재사용해도 안전하다.
#
# 왜 필요한가: `run_backtest`은 호출될 때마다 HistoryDataFeed를 새로 만들고, 그때마다
# 10년치 parquet(심볼당 20만 행)을 전부 다시 읽는다. walk-forward는 이 함수를 윈도우 x
# 시행 횟수만큼 호출하므로(수백 회) 로딩만으로 수 시간이 간다. 캐시하면 그 비용이 1회로
# 줄어든다. 담는 것은 **읽기 전용 DataFrame뿐**이며, 리플레이 시각(`_now`) 같은
# 인스턴스 상태는 절대 공유하지 않는다.
_PARTITION_CACHE: dict[tuple[str, str], tuple[pd.DataFrame, dict[str, pd.DataFrame]]] = {}


def clear_partition_cache() -> None:
    """디스크의 파티션이 바뀐 뒤(백필 직후 등) 캐시를 버린다."""
    _PARTITION_CACHE.clear()


def _interval_minutes(interval: str) -> int:
    return 24 * 60 if interval == "1d" else int(interval.rstrip("m"))


class HistoryDataFeed:
    """DataFeed 구현. bars_1m/set_now()는 StubDataFeed와 동일한 공개 표면을 가진다 —
    backtest.engine.run_backtest이 source 종류를 몰라도 되게 하기 위함."""

    def __init__(self, symbols: list[str], history_dir: str | Path = "data/history"):
        self.symbols = list(symbols)
        self._root = Path(history_dir)
        self.bars_1m: dict[str, pd.DataFrame] = {}
        self._native: dict[str, dict[str, pd.DataFrame]] = {}
        # 캐시 키는 **반드시 절대 경로**여야 한다. 기본값 "data/history"는 상대
        # 경로라, cwd를 바꿔 가며 도는 프로세스(테스트, 여러 워크스페이스)에서
        # 서로 다른 디렉토리가 같은 키를 공유하게 된다 — 실제로 e2e 테스트의
        # tmp 픽스처가 실데이터 캐시를 덮어써서 그 뒤 백테스트가 전부 깨졌다.
        root_key = str(self._root.resolve())
        for symbol in self.symbols:
            key = (root_key, symbol)
            if key not in _PARTITION_CACHE:
                _PARTITION_CACHE[key] = (self._load_1m(symbol), self._load_native(symbol))
            self.bars_1m[symbol], self._native[symbol] = _PARTITION_CACHE[key]
        self._now: datetime | None = None

    @property
    def root(self) -> Path:
        """파티션 루트. 호출자가 '이 심볼에 어떤 간격이 있나'를 물을 때 쓴다."""
        return self._root

    @staticmethod
    def _read_parts(parts: list[Path]) -> pd.DataFrame:
        """파티션들을 하나로. **빈 파일은 버린다.**

        백필은 데이터가 없는 달에도 0행 파일을 남기는데, 빈 DataFrame 은
        DatetimeIndex 를 잃고 RangeIndex 가 된다. 그대로 `concat` 하면 인덱스가
        혼합 타입이 되고, 그때부터 `tz_convert` 가
        `TypeError: Cannot convert tz-naive timestamps` 로 죽는다 — 봉을 읽는
        시점이 아니라 **한참 뒤 리플레이 타임라인을 만들 때** 터져서 원인을
        찾기 어렵다.

        2026-08-13 실측: QQQ 는 빈 파일 4개 때문에 `--source history` 백테스트가
        통째로 불가능했고, TQQQ/SQQQ 는 빈 파일이 안 쓰는 간격에 있어 우연히
        피하고 있었다.
        """
        frames = []
        for p in parts:
            d = pd.read_parquet(p)
            if d.empty:
                continue
            # tz 통일(2026-08-24 실측 결함): 069500·122630 의 1분봉이 5~7월은
            # UTC+09:00, 8월은 UTC 로 저장돼 있었다(수집기 세대 교체의 흔적).
            # tz-aware 끼리라도 tz 가 다르면 concat 인덱스가 object 로 떨어지고
            # resample 이 TypeError 로 죽는다 — 그 예외가 조립부에서 **심볼
            # 하나 때문에 폴백 라우트 전체를** 꺼버렸다(8-19부터 재시작마다
            # "과거 데이터 폴백 라우트 비활성"). 같은 순간의 다른 표기이므로
            # UTC 변환은 데이터 조작이 아니다.
            #
            # tz 가 아예 없는 파티션은 **버린다** — 그 벽시계가 어느 존인지 알
            # 수 없고, 9시간을 추측해 붙이면 그게 조작이다. 조용히 버리지 않고
            # 로그를 남긴다(조용한 소실 금지).
            if isinstance(d.index, pd.DatetimeIndex) and d.index.tz is not None:
                d.index = d.index.tz_convert("UTC")
            else:
                logger.warning("tz 없는 파티션 버림(존 추측 금지): %s (%d행)", p, len(d))
                continue
            frames.append(d)
        if not frames:
            return pd.DataFrame(columns=_COLUMNS)
        df = pd.concat(frames)
        return df[~df.index.duplicated(keep="last")].sort_index()

    def _load_1m(self, symbol: str) -> pd.DataFrame:
        sym_dir = self._root / symbol
        parts = sorted(sym_dir.glob("*/*.parquet")) if sym_dir.exists() else []
        if not parts:
            return pd.DataFrame(columns=_COLUMNS)
        return self._read_parts(parts)

    def _load_native(self, symbol: str) -> dict[str, pd.DataFrame]:
        """1분봉이 아닌 native interval 파티션을 interval별로 로드한다.
        data/history/{symbol}/{interval}/{YYYY}/{MM}.parquet(3단계 경로)만 글롭해
        1분봉 레이아웃(data/history/{symbol}/{YYYY}/{MM}.parquet, 2단계)과 절대
        섞이지 않는다."""
        sym_dir = self._root / symbol
        if not sym_dir.exists():
            return {}
        by_interval: dict[str, list[Path]] = {}
        for p in sorted(sym_dir.glob("*/*/*.parquet")):
            interval = p.parent.parent.name
            by_interval.setdefault(interval, []).append(p)
        return {
            interval: self._read_parts(parts)
            for interval, parts in by_interval.items()
        }

    def interval_bars(self, symbol: str, interval: str) -> pd.DataFrame:
        """`interval` 완성봉 OHLCV 프레임(인덱스 = 봉 **시가** 시각). 1분봉이 있으면
        리샘플해서 만들고, 없으면 native interval 저장소에서 정확히 일치하는 것만
        쓴다(업샘플 금지) — `bar_closes`와 **같은 선택 규칙**이며, 실제로
        `bar_closes`가 이 프레임의 인덱스에서 유도된다(규칙이 두 곳으로 갈라지면
        타임라인과 봉 내용이 서로 다른 소스를 가리키게 된다).

        왜 공개하는가: 봉내(intrabar) 체결 모델(`backtest.engine`의
        `fill_model="intrabar"`)은 봉의 고가/저가/시가를 봐야 한다 — 마감 시각만
        주는 `bar_closes`로는 "봉 안에서 손절선이 닿았는가"를 답할 수 없다.
        `history()`는 리플레이 시각 기준 완성봉만 주므로(look-ahead 방지) 세션
        마지막 봉이 빠지는 등 경계가 다르다 — 그 경계는 전략용이지 체결 모델용이
        아니다. 읽기 전용이며 리플레이 시각(`_now`)과 무관하다.
        """
        bars = self.bars_1m.get(symbol)
        if bars is not None and not bars.empty:
            return resample_1m(bars, _interval_minutes(interval))
        native_bars = self._native.get(symbol, {}).get(interval)
        if native_bars is None or native_bars.empty:
            return pd.DataFrame(columns=_COLUMNS)
        return native_bars

    def bar_closes(self, symbol: str, interval: str) -> pd.DatetimeIndex:
        """`interval` 완성봉들의 마감 시각(백테스트 리플레이 타임라인용). 1분봉이
        있으면 리샘플해서 뽑고, 없으면 native interval 저장소에서 정확히 일치하는
        것만 쓴다(업샘플 금지)."""
        bars = self.interval_bars(symbol, interval)
        if bars.empty:
            return pd.DatetimeIndex([])
        return bars.index + pd.Timedelta(minutes=_interval_minutes(interval))

    def set_now(self, now: datetime) -> None:
        self._now = now

    def quote(self, symbol: str) -> Quote | None:
        """리플레이 시각 기준 "현재가" = 그 시각까지 **마감된** 봉의 종가.

        look-ahead 금지는 history()뿐 아니라 quote()에도 적용된다. 봉 인덱스는 봉
        **시가** 시각이므로 `index <= now`로 거르면 now에 막 열린(아직 형성 중인)
        봉이 포함되고, 그 봉의 종가는 now + interval 시점의 미래 가격이다. 15분봉
        백테스트에서 이는 체결가와 손절/목표 판정에 15분치 미래 정보를 주입한다.
        올바른 기준은 봉 **마감**(index + interval)이 now를 넘지 않는 것 —
        history()의 native 경로가 이미 쓰는 것과 동일한 기준이다.
        """
        if self._now is None:
            return None
        bars = self.bars_1m.get(symbol)
        if bars is not None and not bars.empty:
            return self._last_closed_quote(symbol, bars, 1)
        # 1분봉이 없으면 native interval로 대체하되, 반드시 **가장 짧은** 간격을
        # 쓴다. 굵은 봉일수록 마감이 늦어 "현재가"가 그만큼 과거가 되기 때문이다.
        #
        # dict 순서(=파일 글롭 순서)대로 아무거나 집으면 조용히 망가진다. 실측:
        # 한 심볼에 15m/1d/5m 파티션이 함께 있으면 정렬상 "15m"이 먼저 잡혀,
        # 5분봉 리플레이의 09:35 사이클에서 09:30 15분봉은 아직 마감 전(09:45)이라
        # 보이지 않고 **전날 15:45 봉**이 현재가로 잡혔다. 그 결과 모든 체결이
        # 전날 종가로 났고 백테스트 전체가 무의미해졌다(진입 2,558건 중 당일 청산 0건).
        native = self._native.get(symbol, {})
        for interval in sorted(native, key=_interval_minutes):
            native_bars = native[interval]
            if not native_bars.empty:
                return self._last_closed_quote(symbol, native_bars, _interval_minutes(interval))
        return None

    def _visible_count(self, index: pd.DatetimeIndex, minutes: int) -> int:
        """봉 **마감**(index + interval)이 self._now를 넘지 않는 봉의 개수.

        `index + delta <= now`는 `index <= now - delta`와 동치이고, 인덱스가 정렬돼
        있으므로 이진탐색으로 구한다. 불리언 마스크로 짜면 조회 1회가 O(전체 봉)이
        되는데, 10년 5분봉 리플레이는 사이클 20만 회 x 봉 20만 개라 그 자체로
        실행이 불가능해진다(15분봉에서 이미 백테스트가 타임아웃 났다).
        """
        return int(index.searchsorted(self._now - pd.Timedelta(minutes=minutes), side="right"))

    def _last_closed_quote(self, symbol: str, bars: pd.DataFrame, minutes: int) -> Quote | None:
        count = self._visible_count(bars.index, minutes)
        if count == 0:
            return None
        return Quote(
            symbol=symbol, ts=bars.index[count - 1], price=float(bars["close"].iloc[count - 1])
        )

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        bars = self.bars_1m.get(symbol)
        if bars is not None and not bars.empty and self._now is not None:
            visible = bars.iloc[: int(bars.index.searchsorted(self._now, side="right"))]
            if interval == "1m":
                # ts == self._now인 마지막 행은 그 순간 막 열린, 아직 형성 중인 봉 — 제외.
                if not visible.empty and visible.index[-1] == self._now:
                    visible = visible.iloc[:-1]
                return visible.tail(n)
            minutes = _interval_minutes(interval)
            # **보이는 구간 전체를 리샘플하지 않는다.** 필요한 것은 마지막 n봉뿐이고,
            # 그 n봉을 만드는 데 필요한 1분봉은 많아야 (n+2)x minutes 개다(+2 는
            # 앞쪽 부분 버킷과 resample_1m 이 항상 버리는 마지막 미완성 버킷의 여유).
            # 버킷 경계는 벽시계로 정해지므로 앞을 잘라도 남는 버킷의 내용은
            # 동일하다 — 즉 이 슬라이스는 **결과를 바꾸지 않는다**.
            #
            # 왜 필요한가(2026-09-03 실측): 다년치 1분봉 레이크(심볼당 100만 행)에서
            # 이 함수는 사이클마다 O(보이는 전체 행) 을 리샘플한다. 리플레이가
            # 진행될수록 `visible` 이 100만 행까지 자라 사이클 하나가 수백 ms 가
            # 되고, 40거래일 x 4심볼 창 하나가 몇 분씩 걸려 walk-forward 자체가
            # 불가능해졌다. 잘라내면 상수 시간이 된다.
            #
            # 갭(야간·점심 휴장)이 있으면 같은 행 수가 **더 많은** 버킷에 걸리므로
            # 항상 n 개 이상이 나온다 — 모자랄 위험은 없다.
            need = (n + 2) * minutes
            window = visible.iloc[-need:] if len(visible) > need else visible
            return resample_1m(window, minutes).tail(n)

        # 1분봉이 없는 심볼 — native 저장소에서 요청한 interval과 정확히 일치하는
        # 것만 서빙한다. 다른 native interval을 리샘플/업샘플하지 않는다.
        native_bars = self._native.get(symbol, {}).get(interval)
        if native_bars is None or native_bars.empty or self._now is None:
            return pd.DataFrame(columns=_COLUMNS)
        count = self._visible_count(native_bars.index, _interval_minutes(interval))
        return native_bars.iloc[max(count - n, 0) : count]
