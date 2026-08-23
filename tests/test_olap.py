"""Parquet 위 커버리지 조회 — "이 구간을 점수 매길 자격이 있나".

봉이 빠진 구간에서 돌린 백테스트는 숫자가 멀쩡해 보이지만 근거가 없다. 하네스는
그 숫자를 그대로 믿고 "이 변형이 낫다"고 말한다. 그래서 적합도와 함께 낸다.

**핵심 회귀**: 백필은 데이터가 없는 달에도 0행 파일을 남기는데, 빈 DataFrame 은
DatetimeIndex 를 잃고 RangeIndex 가 된다 — 그 파일에는 `ts` 컬럼이 아예 없다.
pandas 는 무해하게 흘려보내지만 DuckDB 는 스키마 불일치로 **글롭 전체를 실패**
시킨다. 2026-08-13 실측에서 1,030개 중 14개가 그런 파일이었고, TQQQ/15m 커버리지
조회가 통째로 None 을 냈다.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant.adapters.olap import Coverage, coverage, glob_for, query

duckdb = pytest.importorskip("duckdb")


@pytest.fixture
def history(tmp_path: Path) -> Path:
    """정상 파일 2개 + **0행 파일 1개**(백필이 실제로 남기는 모양)."""
    root = tmp_path / "history"
    d = root / "TQQQ" / "15m"
    (d / "2026").mkdir(parents=True)
    for month, start in [("06", "2026-06-01"), ("07", "2026-07-01")]:
        idx = pd.date_range(start, periods=3, freq="15min", tz="UTC", name="ts")
        pd.DataFrame(
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0},
            index=idx,
        ).to_parquet(d / "2026" / f"{month}.parquet")
    # 이번 달 — 아직 백필 안 됨. 빈 프레임은 RangeIndex 라 ts 컬럼이 없다.
    pd.DataFrame(columns=["open", "high", "low", "close", "volume"]).to_parquet(
        d / "2026" / "08.parquet"
    )
    return root


def test_empty_month_file_does_not_break_the_whole_glob(history: Path):
    """이게 이 모듈의 존재 이유다 — 빈 파일 하나가 커버리지를 통째로 못 막게."""
    c = coverage("TQQQ", "15m", root=history)
    assert c is not None, "0행 파일 때문에 None 이 나오면 union_by_name 이 빠진 것"
    assert c.n_bars == 6


def test_coverage_reports_the_real_window(history: Path):
    c = coverage("TQQQ", "15m", root=history)
    assert c.first_ts.startswith("2026-06-01")
    assert c.last_ts.startswith("2026-07-01")
    assert c.n_days == 2


def test_missing_symbol_is_unknown_not_zero(history: Path):
    """'봉이 0개'와 '조회할 수 없었다'는 다른 사건이다 — 섞으면 게이트가 무력해진다."""
    assert coverage("없는종목", "15m", root=history) is None


def test_empty_only_directory_is_unknown_not_zero(tmp_path: Path):
    """전부 0행이면 ts 가 하나도 없다 — 0봉이라고 답하면 '데이터 있음'으로 읽힌다."""
    d = tmp_path / "h" / "X" / "1d" / "2026"
    d.mkdir(parents=True)
    pd.DataFrame(columns=["open", "close"]).to_parquet(d / "01.parquet")
    assert coverage("X", "1d", root=tmp_path / "h") is None


def test_glob_pattern_is_recursive():
    assert glob_for("TQQQ", "15m", Path("data/history")).endswith(
        "data/history/TQQQ/15m/**/*.parquet"
    )


def test_coverage_is_json_serialisable(history: Path):
    import json
    d = coverage("TQQQ", "15m", root=history).to_dict()
    assert json.loads(json.dumps(d)) == d
    assert set(d) == set(Coverage.__dataclass_fields__)


def test_bad_sql_returns_none_not_an_exception():
    """탐색 질의 실패가 호출자를 죽이면, 에이전트가 SQL 하나 틀릴 때마다 멈춘다."""
    assert query("SELECT * FROM 없는테이블") is None
