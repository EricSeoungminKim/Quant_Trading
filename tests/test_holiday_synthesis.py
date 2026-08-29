"""`quant/analyze/holiday_synthesis.py` 단위 테스트 — 휴장 기간 종합 결정론 집계
(소유자 요청 2026-08-29).

`detect_gap`은 앵커 일봉(`opendays.py`와 같은 parquet 픽스처)으로, `aggregate`/
`is_empty`는 이미 로드된 engine.json/calendar 딕셔너리로 테스트한다 — 파일 I/O가
필요한 것과 순수 집계를 분리한다(모듈 자체의 설계와 동일한 경계).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from quant.analyze.holiday_synthesis import aggregate, detect_gap, is_empty


def _write_daily(anchor_dir: Path, dates: list[str]) -> None:
    """`tests/test_opendays.py::_write_daily`와 동일 관례 — 04:00 UTC 인덱스 일봉."""
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


def _anchor(tmp_path: Path, market: str) -> Path:
    from quant.analyze.opendays import anchor_dir_for

    return anchor_dir_for(market, tmp_path)


# ── (1) detect_gap — 평일 연속(어제 개장)이면 None ──────────────────────

def test_detect_gap_none_when_yesterday_was_open(tmp_path):
    """화요일 아침, 월요일이 개장일이었으면 휴장이 없다."""
    _write_daily(_anchor(tmp_path, "KR"), ["2026-08-17", "2026-08-18"])  # 월(17)~화(18)

    assert detect_gap("KR", tmp_path, date(2026, 8, 18)) is None


def test_detect_gap_none_when_anchor_missing():
    assert detect_gap("KR", Path("/does/not/exist"), date(2026, 8, 18)) is None


def test_detect_gap_none_on_weekend_itself(tmp_path):
    """토요일 아침(휴장 첫날) 자체는 "재개장 아침"이 아니다."""
    _write_daily(_anchor(tmp_path, "KR"), ["2026-08-14"])  # 금요일 개장

    assert detect_gap("KR", tmp_path, date(2026, 8, 15)) is None  # 토요일


# ── (2) 월요일 시나리오 — 금 개장, 토·일 휴장 ────────────────────────────

def test_detect_gap_monday_after_friday_open(tmp_path):
    _write_daily(_anchor(tmp_path, "KR"), ["2026-08-14"])  # 2026-08-14 = 금요일

    result = detect_gap("KR", tmp_path, date(2026, 8, 17))  # 2026-08-17 = 월요일

    assert result is not None
    last_open, gap_days, window = result
    assert last_open == date(2026, 8, 14)
    assert gap_days == 2  # 토·일
    assert window == [date(2026, 8, 15), date(2026, 8, 16)]


# ── (3) aggregate — 테마 빈도·신규 종목·점수 추이 (손검증) ───────────────

def _engine(symbols=None, score100=None, label=None):
    payload: dict = {"symbols": symbols or []}
    if score100 is not None:
        payload["stance"] = {"score100": score100, "label": label}
    return payload


def test_aggregate_theme_freq_new_symbols_and_stance_trend():
    days = [
        {
            "date": date(2026, 8, 15),
            "engine": _engine(
                symbols=[
                    {"symbol": "005930", "name": "삼성전자", "sector": "반도체", "is_new": False},
                    {"symbol": "000660", "name": "SK하이닉스", "sector": "반도체", "is_new": True},
                ],
                score100=55, label="중립",
            ),
            "calendar_events": [
                {"name": "Consumer Price Index", "high_impact": True, "is_today": True},
                {"name": "그냥 이벤트", "high_impact": False, "is_today": True},
            ],
        },
        {
            "date": date(2026, 8, 16),
            "engine": _engine(
                symbols=[
                    {"symbol": "373220", "name": "LG에너지솔루션", "sector": "이차전지",
                     "is_new": True},
                    {"symbol": "000660", "name": "SK하이닉스", "sector": "반도체",
                     "is_new": True},  # 이미 8/15 에 신규 — first_seen 은 8/15 유지
                ],
                score100=62, label="약간 우호",
            ),
            "calendar_events": [],
        },
    ]

    agg = aggregate(days)

    assert agg["missing_engine_days"] == []
    assert agg["missing_calendar_days"] == []
    assert agg["theme_freq"] == [
        {"theme": "반도체", "count": 3}, {"theme": "이차전지", "count": 1},
    ]
    assert agg["new_symbols"] == [
        {"symbol": "000660", "name": "SK하이닉스", "first_seen": "2026-08-15"},
        {"symbol": "373220", "name": "LG에너지솔루션", "first_seen": "2026-08-16"},
    ]
    assert agg["stance_trend"] == [
        {"date": "2026-08-15", "score100": 55, "label": "중립"},
        {"date": "2026-08-16", "score100": 62, "label": "약간 우호"},
    ]
    assert agg["high_impact_events"] == [
        {"date": "2026-08-15", "name": "Consumer Price Index"},
    ]


# ── (4) 스냅샷 결손 시 부분 집계 + 결손 표기 ─────────────────────────────

def test_aggregate_partial_with_missing_days_marked():
    days = [
        {
            "date": date(2026, 8, 15),
            "engine": _engine(
                symbols=[{"symbol": "005930", "name": "삼성전자", "sector": "반도체",
                          "is_new": True}],
                score100=50, label="중립",
            ),
            "calendar_events": None,  # 이 날 스냅샷 결손
        },
        {
            "date": date(2026, 8, 16),
            "engine": None,  # 이 날 engine.json 결손
            "calendar_events": [],
        },
    ]

    agg = aggregate(days)

    assert agg["missing_engine_days"] == ["2026-08-16"]
    assert agg["missing_calendar_days"] == ["2026-08-15"]
    # 결손이 아닌 데이터로는 정상 집계된다.
    assert agg["theme_freq"] == [{"theme": "반도체", "count": 1}]
    assert agg["new_symbols"] == [
        {"symbol": "005930", "name": "삼성전자", "first_seen": "2026-08-15"},
    ]
    assert agg["stance_trend"] == [{"date": "2026-08-15", "score100": 50, "label": "중립"}]


def test_aggregate_all_missing_returns_empty_not_error():
    days = [{"date": date(2026, 8, 15), "engine": None, "calendar_events": None}]

    agg = aggregate(days)

    assert agg["missing_engine_days"] == ["2026-08-15"]
    assert agg["missing_calendar_days"] == ["2026-08-15"]
    assert is_empty(agg)


# ── is_empty ──────────────────────────────────────────────────────────────

def test_is_empty_true_for_blank_aggregate():
    agg = aggregate([])
    assert is_empty(agg)


def test_is_empty_false_when_any_signal_present():
    days = [{
        "date": date(2026, 8, 15),
        "engine": _engine(symbols=[], score100=50, label="중립"),
        "calendar_events": [],
    }]
    assert not is_empty(aggregate(days))
