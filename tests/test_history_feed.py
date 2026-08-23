"""HistoryDataFeed 파티션 로딩 견고성 — tz 혼재(2026-08-24 EC2 실측 결함).

기존 `tests/test_history.py`는 fetch/적재를 다룬다 — 여기는 **읽기 경로**의
파티션 통합만 본다.
"""
from __future__ import annotations

import pandas as pd

from quant.adapters.data.history import HistoryDataFeed, clear_partition_cache

# ── tz 혼재 파티션 (2026-08-24, EC2 실측 결함) ──────────────────────────────
# 069500·122630 의 1분봉이 5~7월은 UTC+09:00, 8월은 UTC 로 저장돼 있었다
# (수집기 세대 교체의 흔적). tz-aware 끼리라도 tz 가 다르면 concat 인덱스가
# object 로 떨어지고, resample 이 "Only valid with DatetimeIndex" TypeError 로
# 죽는다 — 그리고 build_market_data 의 try 가 **심볼 하나 때문에 폴백 라우트
# 전체를** 꺼버렸다(8-19부터 재시작마다 "과거 데이터 폴백 라우트 비활성").
# 같은 순간의 다른 표기이므로 UTC 통일은 데이터 조작이 아니다.

def _part(dirpath, name, ts_list, tz):
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz=tz) for t in ts_list])
    df = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        index=idx,
    )
    dirpath.mkdir(parents=True, exist_ok=True)
    df.to_parquet(dirpath / name)


def test_mixed_tz_partitions_unify_to_utc(tmp_path):
    root = tmp_path / "history"
    _part(root / "069500" / "2026", "07.parquet",
          ["2026-07-01 09:00", "2026-07-01 09:01"], tz="Asia/Seoul")
    _part(root / "069500" / "2026", "08.parquet",
          ["2026-08-01 00:00", "2026-08-01 00:01"], tz="UTC")

    clear_partition_cache()
    feed = HistoryDataFeed(["069500"], history_dir=root)
    bars = feed.bars_1m["069500"]
    assert isinstance(bars.index, pd.DatetimeIndex), "혼재 concat 이 object Index 로 떨어지면 안 된다"
    assert str(bars.index.tz) == "UTC"
    assert len(bars) == 4
    # 리샘플 타임라인이 실제로 나온다(예전엔 여기서 TypeError)
    closes = feed.bar_closes("069500", "15m")
    assert len(closes) > 0


def test_naive_partition_is_dropped_loudly_not_guessed(tmp_path, caplog):
    """tz 없는 파티션은 벽시계가 어느 존인지 알 수 없다 — 9시간을 추측해 붙이면
    조작이다. 버리되 반드시 로그를 남긴다(조용한 소실 금지)."""
    import logging

    root = tmp_path / "history"
    idx = pd.DatetimeIndex([pd.Timestamp("2026-07-01 09:00")])  # naive
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                       "volume": 1.0}, index=idx)
    (root / "069500" / "2026").mkdir(parents=True)
    df.to_parquet(root / "069500" / "2026" / "07.parquet")
    _part(root / "069500" / "2026", "08.parquet", ["2026-08-01 00:00"], tz="UTC")

    clear_partition_cache()
    with caplog.at_level(logging.WARNING):
        feed = HistoryDataFeed(["069500"], history_dir=root)
    assert len(feed.bars_1m["069500"]) == 1  # UTC 파티션만
    assert any("tz 없는 파티션" in r.message for r in caplog.records)
