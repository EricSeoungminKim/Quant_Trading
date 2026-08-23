"""개장일 판정은 공휴일표가 아니라 앵커(KR=069500, US=QQQ) 일봉 데이터로 한다.

**왜.** 공휴일표는 대체휴일·임시공휴일을 놓치면 조용히 틀린다. 앵커의 마지막 봉
날짜는 그 시장이 실제로 열렸을 때만 생기므로 유지보수 없이 정답이 나온다.

**안전한 방향은 창이 넓어지는 쪽이다** — 앵커 부재/파손은 `None`으로, `None`은
빈 창으로 이어진다(집계 과다는 허용해도 과소는 허용하지 않는다).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from quant.analyze.opendays import anchor_dir_for, last_open_day, window_dates


def _write_daily(anchor_dir: Path, dates: list[str]) -> None:
    """실측과 같은 04:00 UTC 인덱스로 소형 일봉 parquet 를 월별 파티션에 쓴다.

    QQQ 실측 봉은 `2026-08-14 04:00+00` = 08-14 거래일이므로, 04:00 UTC 를
    그대로 재현해야 UTC-date 환산 로직이 실제 데이터로도 맞는지 확인된다.
    """
    idx = pd.DatetimeIndex(
        [pd.Timestamp(d, tz="UTC") + pd.Timedelta(hours=4) for d in dates]
    )
    n = len(dates)
    df = pd.DataFrame({
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
        "close": [100.5] * n, "volume": [1000.0] * n,
    }, index=idx)
    for (year, month), part in df.groupby([df.index.year, df.index.month]):
        path = anchor_dir / f"{year:04d}" / f"{month:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(path)


def _write_empty(anchor_dir: Path, year: int, month: int) -> None:
    """백필이 무봉 월에 남기는 0행 파일 — concat 시 인덱스 타입이 섞이는 결함의 원인."""
    df = pd.DataFrame(
        {"open": [], "high": [], "low": [], "close": [], "volume": []},
        index=pd.DatetimeIndex([], tz="UTC"),
    )
    path = anchor_dir / f"{year:04d}" / f"{month:02d}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)


# ── anchor_dir_for ───────────────────────────────────────────────────────

def test_anchor_dir_for_kr(tmp_path):
    assert anchor_dir_for("KR", tmp_path) == tmp_path / "data" / "history" / "069500" / "1d"


def test_anchor_dir_for_us(tmp_path):
    assert anchor_dir_for("US", tmp_path) == tmp_path / "data" / "history" / "QQQ" / "1d"


def test_anchor_dir_for_unknown_market_raises(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        anchor_dir_for("JP", tmp_path)


# ── last_open_day ────────────────────────────────────────────────────────

def test_last_open_day_reads_across_month_partitions(tmp_path):
    """(a) 7월 말~8월 두 파티션에서 today=08-18 미만 마지막 봉은 08-14."""
    anchor = tmp_path / "1d"
    _write_daily(anchor, ["2026-07-30", "2026-07-31", "2026-08-03", "2026-08-14"])

    assert last_open_day(anchor, date(2026, 8, 18)) == date(2026, 8, 14)


def test_last_open_day_excludes_today_itself(tmp_path):
    """(b) today 당일 봉이 있어도 그건 포함하지 않고 그 전 봉을 쓴다."""
    anchor = tmp_path / "1d"
    _write_daily(anchor, ["2026-08-13", "2026-08-14"])

    assert last_open_day(anchor, date(2026, 8, 14)) == date(2026, 8, 13)


def test_last_open_day_missing_dir_returns_none(tmp_path):
    """(c) 앵커 디렉토리 자체가 없으면 None — 예외를 던지지 않는다."""
    anchor = tmp_path / "does_not_exist" / "1d"

    assert last_open_day(anchor, date(2026, 8, 18)) is None


def test_last_open_day_ignores_empty_partition(tmp_path):
    """(d) 백필이 남긴 0행 파티션은 무시하고 실데이터 파티션에서 답을 찾는다."""
    anchor = tmp_path / "1d"
    _write_daily(anchor, ["2026-08-14"])
    _write_empty(anchor, 2026, 9)

    assert last_open_day(anchor, date(2026, 9, 18)) == date(2026, 8, 14)


def test_last_open_day_all_partitions_empty_returns_none(tmp_path):
    """모든 파티션이 0행이면 실데이터가 하나도 없다 — None."""
    anchor = tmp_path / "1d"
    _write_empty(anchor, 2026, 8)

    assert last_open_day(anchor, date(2026, 8, 18)) is None


# ── window_dates ─────────────────────────────────────────────────────────

def test_window_dates_between_last_open_and_today(tmp_path):
    """(e) last_open 다음날부터 today 전날까지 오름차순."""
    assert window_dates(date(2026, 8, 14), date(2026, 8, 18)) == [
        date(2026, 8, 15), date(2026, 8, 16), date(2026, 8, 17),
    ]


def test_window_dates_none_last_open_is_empty(tmp_path):
    """(f) 판정 불가(None)면 집계 자체를 건너뛴다 — 빈 리스트."""
    assert window_dates(None, date(2026, 8, 18)) == []


def test_window_dates_caps_to_most_recent(tmp_path):
    """(g) 창이 cap 을 넘으면 가장 최근 cap 일만 남긴다."""
    result = window_dates(date(2026, 8, 1), date(2026, 8, 15), cap=7)

    assert result == [
        date(2026, 8, 8), date(2026, 8, 9), date(2026, 8, 10), date(2026, 8, 11),
        date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14),
    ]
