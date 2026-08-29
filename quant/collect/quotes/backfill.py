"""과거 데이터 백필: CandleSource를 월별로 페이징해 파티션 Parquet로 쓴다.

- 저장 경로: 1분봉(기본, interval="1m")은 data/history/{symbol}/{YYYY}/{MM}.parquet —
  기존 레이아웃 그대로, 하위 호환. 1분봉이 아닌 native interval(예: 15m)은
  data/history/{symbol}/{interval}/{YYYY}/{MM}.parquet로 별도 하위 디렉터리에
  쓴다 — 경로만 보고도 실제 봉 간격을 오인할 수 없게 하기 위함(HONESTY CONSTRAINT,
  docs/data-availability.md). 1분봉 파티션과 절대 같은 경로를 공유하지 않는다.
- 멱등/재개 가능: 이미 완성된(과거로 끝난) 월 파티션은 스킵, 그 외엔 기존 데이터
  마지막 시각 이후의 gap만 다시 받는다.
- 순수성: write 시점엔 리샘플/FX 변환을 하지 않는다 — 원본 통화의, 요청한 interval
  그대로. 1분봉이 아닌 interval을 요청하면 source.native_interval이 정확히 일치할
  때만 받는다 — 리샘플/보간으로 지어내지 않는다.
- 데이터 품질: timestamp 기준 dedup, 오름차순 정렬, 비정상봉(가격<=0, high<low) 제거,
  거래일 기준 gap(빠진 세션) 로그.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from quant.collect.quotes.source import CandleSource, MultiIntervalCandleSource

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_DIR = Path("data/history")
_COLUMNS = ["open", "high", "low", "close", "volume"]
# 파티션 인덱스 컬럼의 정식 이름. 벤더마다 다르게 부른다(alpaca/toss=`ts`,
# yfinance=`Date`) — 그대로 쓰면 파일마다 스키마가 갈리고, `count(ts)` 로 세는
# DuckDB 커버리지가 그 파일을 NULL 로 흘려보내 **데이터가 있는데 "없다"고 답한다**
# (2026-08-13 실측: 봉 8개를 새로 받았는데 커버리지는 변하지 않았다).
# 벤더별로 받아주는 게 아니라 쓰는 지점 하나에서 정규화한다.
_INDEX_NAME = "ts"
_COMPLETE_TOLERANCE = pd.Timedelta(days=5)  # 월말이 주말/휴장일이면 이 정도 여유를 둔다


@dataclass
class BackfillReport:
    symbol: str
    partitions_written: list[str] = field(default_factory=list)
    partitions_skipped: list[str] = field(default_factory=list)
    total_bars: int = 0
    gaps: list[str] = field(default_factory=list)  # 빠진 평일(추정 거래일) 목록


def _as_utc_ts(ts: datetime) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t


def _month_bounds(year: int, month: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(year=year, month=month, day=1, tz="UTC")
    end = start + pd.DateOffset(months=1) - pd.Timedelta(microseconds=1)
    return start, end


def _month_range(start: pd.Timestamp, end: pd.Timestamp):
    year, month = start.year, start.month
    end_year, end_month = end.year, end.month
    while (year, month) <= (end_year, end_month):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


def _partition_path(root: Path, symbol: str, year: int, month: int, interval: str = "1m") -> Path:
    if interval == "1m":
        return root / symbol / f"{year:04d}" / f"{month:02d}.parquet"
    return root / symbol / interval / f"{year:04d}" / f"{month:02d}.parquet"


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = _INDEX_NAME  # 벤더 이름(예: yfinance 의 `Date`)을 여기서 통일한다
    good = (
        (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)
        & (df["high"] >= df["low"])
    )
    dropped = int((~good).sum())
    if dropped:
        logger.warning("dropping %d bad bar(s) (non-positive price or high<low)", dropped)
    return df[good]


def _find_gaps(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    """[start, end] 구간 내 데이터가 전혀 없는 평일(주말 제외, 실제 미국 공휴일
    캘린더는 반영하지 않은 근사치) 목록 — 데이터셋의 구멍을 가시화하기 위함."""
    have_days = set(df.index.tz_convert("UTC").normalize().unique()) if not df.empty else set()
    gaps = []
    day = start.normalize()
    last_day = end.normalize()
    while day <= last_day:
        if day.weekday() < 5 and day not in have_days:
            gaps.append(day.date().isoformat())
        day += pd.Timedelta(days=1)
    return gaps


def backfill(
    symbol: str,
    source: CandleSource | MultiIntervalCandleSource,
    start: datetime,
    end: datetime,
    history_dir: str | Path = DEFAULT_HISTORY_DIR,
    now: datetime | None = None,
    interval: str = "1m",
) -> BackfillReport:
    if interval == "1m":
        fetch: Callable[[str, datetime, datetime], pd.DataFrame] = source.fetch_1m
    else:
        native = getattr(source, "native_interval", None)
        if native != interval:
            raise ValueError(
                f"source의 native_interval({native!r})이 요청한 interval({interval!r})과 "
                "다릅니다 — 리샘플/보간으로 위장해 백필하지 않는다."
            )
        fetch = source.fetch

    root = Path(history_dir)
    start_ts = _as_utc_ts(start)
    end_ts = _as_utc_ts(end)
    now_ts = _as_utc_ts(now) if now is not None else pd.Timestamp.now(tz="UTC")

    report = BackfillReport(symbol=symbol)
    all_bars: list[pd.DataFrame] = []

    for year, month in _month_range(start_ts, end_ts):
        m_start, m_end = _month_bounds(year, month)
        window_start = max(start_ts, m_start)
        window_end = min(end_ts, m_end)
        if window_start > window_end:
            continue

        path = _partition_path(root, symbol, year, month, interval)
        existing = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=_COLUMNS)
        if not existing.empty:
            if existing.index.tz is None:
                existing.index = existing.index.tz_localize("UTC")
            elif str(existing.index.tz) != "UTC":
                # 같은 심볼·interval 파티션을 서로 다른 벤더가 채울 수 있다(예:
                # 069500 1d는 backfill_kr_daily.sh가 yfinance로, KR 개별종목 백필은
                # Toss로 채운다). 둘 다 tz-aware지만 표현이 다르면(고정 오프셋 vs
                # UTC) 아래 concat이 tz 불일치로 DatetimeIndex를 잃고 object dtype
                # Index가 되어 한참 뒤(`_find_gaps`의 tz_convert)에서 죽는다 — 실측.
                # 벤더 경계마다 정규화를 반복하는 대신 여기(쓰는 지점)에서 한 번에
                # 통일한다.
                existing.index = existing.index.tz_convert("UTC")

        month_closed = m_end < now_ts
        label = f"{year:04d}-{month:02d}"

        if (
            not existing.empty
            and month_closed
            and existing.index.max() >= window_end - _COMPLETE_TOLERANCE
        ):
            report.partitions_skipped.append(label)
            all_bars.append(existing)
            continue

        # +1분 offset은 "이미 받은 마지막 봉 이후"를 보장하기 위한 보수적 커서다 —
        # native interval이 1분보다 넓어도(예: 15분) 다음 실제 봉의 시작보다 항상
        # 앞서므로 데이터 누락이 없다(겹치는 소량 재조회는 아래 dedup으로 정리됨).
        gap_start = existing.index.max() + pd.Timedelta(minutes=1) if not existing.empty else window_start
        if gap_start > window_end:
            report.partitions_skipped.append(label)
            all_bars.append(existing)
            continue

        fresh = fetch(symbol, max(gap_start, window_start), window_end)
        combined = _clean(pd.concat([existing, fresh]) if not existing.empty else fresh)

        if combined.empty:
            # **빈 파티션을 만들지 않는다.** 빈 DataFrame 은 parquet 왕복에서
            # DatetimeIndex 를 잃고 RangeIndex 가 되는데, 그 파일이 섞이면 concat
            # 인덱스가 혼합 타입이 되고 한참 뒤 tz_convert 가 죽는다 — 봉을 읽는
            # 시점이 아니라 리플레이 타임라인을 만들 때다. ce6a755(history.py)와
            # 884f1eb(regime provider)가 그 파일을 걸러내는 방어였고, 생산지가
            # 여기다(실측: 로컬에 13개, 전부 0행).
            report.partitions_skipped.append(label)
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        combined.to_parquet(path)
        report.partitions_written.append(label)
        all_bars.append(combined)
        logger.info("%s %s: %d bar(s) written to %s", symbol, label, len(combined), path)

    merged = pd.concat(all_bars) if all_bars else pd.DataFrame(columns=_COLUMNS)
    merged = merged[~merged.index.duplicated(keep="last")].sort_index() if not merged.empty else merged
    report.total_bars = len(merged)
    report.gaps = _find_gaps(merged, start_ts, min(end_ts, now_ts))
    if report.gaps:
        logger.info("%s: %d missing weekday session(s) in range", symbol, len(report.gaps))
    return report
