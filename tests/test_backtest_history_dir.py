"""`run_backtest(history_dir=...)` — 파티션 루트를 호출자가 지정할 수 있어야 한다.

**왜 필요한가.** 별도 연구 저장소(quant-backtest)가 자기 데이터 레이크 위에서
이 엔진을 그대로 돌린다. 레이아웃을 새로 만들어 변환해 넘기면 그 변환이 조용히
틀릴 수 있으므로(봉 하나 밀림, tz 어긋남 — 둘 다 에러 없이 "결과"를 낸다),
레이아웃은 하나로 두고 **루트만** 바꾼다.

여기서 지키는 계약은 둘이다:
1. `history_dir` 를 주면 그 디렉터리를 읽는다 (기본 `data/history` 가 아니라).
2. `history_dir` 를 주지 않으면 기존 동작 그대로다.

그리고 진단 계약 하나: **요청한 간격의 봉이 없는 심볼이 있으면 명확한 에러**로
멈춰야 한다. 예전에는 이 검사가 `end` 필터 뒤에 있어서, 빈 인덱스가 먼저
`AttributeError: 'Index' object has no attribute 'tz'` 로 죽었다 — 원인은
"데이터가 없다"인데 메시지는 타임존을 가리켜 진단이 엉뚱한 데로 샜다.
"""
from __future__ import annotations

import pandas as pd
import pytest

from quant.adapters.data.history import clear_partition_cache
from quant.backtest.engine import run_backtest

_SESSION_START = "13:30:00"  # 09:30 ET = 13:30 UTC (EDT)


def _write_1m(root, symbol: str, days: int = 30, start: str = "2026-06-01") -> None:
    """정규장 1분봉 파티션을 월별로 쓴다 — 백필이 쓰는 레이아웃 그대로."""
    idx = []
    day = pd.Timestamp(start, tz="UTC")
    made = 0
    while made < days:
        if day.weekday() < 5:
            open_ts = day.normalize() + pd.Timedelta(_SESSION_START)
            idx.extend(pd.date_range(open_ts, periods=390, freq="1min"))
            made += 1
        day += pd.Timedelta(days=1)
    index = pd.DatetimeIndex(idx, name="ts")
    # 결정론적이고 단조롭지 않은 가격 — 값 자체는 이 테스트의 관심사가 아니다.
    price = pd.Series(range(len(index)), index=index).mod(97) + 100.0
    df = pd.DataFrame({
        "open": price, "high": price + 0.5, "low": price - 0.5,
        "close": price, "volume": 1000.0,
    })
    for (year, month), chunk in df.groupby([df.index.year, df.index.month]):
        path = root / symbol / f"{year:04d}" / f"{month:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        chunk.to_parquet(path)


@pytest.fixture(autouse=True)
def _fresh_cache():
    # 파티션 캐시는 (절대경로, 심볼) 키라 tmp_path 끼리 섞이지 않지만,
    # 다른 테스트가 남긴 실데이터 캐시와의 상호작용을 없애 둔다.
    clear_partition_cache()
    yield
    clear_partition_cache()


def test_history_dir_reads_the_given_root(tmp_path):
    lake = tmp_path / "lake"
    _write_1m(lake, "TQQQ", days=25)

    result = run_backtest(
        strategy_id="donchian", days=10, interval="15m", source="history",
        symbols=["TQQQ"], history_dir=lake,
    )
    assert len(result.equity_curve) > 1
    # 리플레이가 우리가 쓴 구간 안에 있어야 한다 — 다른 디렉터리를 읽었다면
    # 시각이 완전히 다른 곳에 찍힌다.
    assert result.equity_curve.index[0] >= pd.Timestamp("2026-06-01", tz="UTC")
    assert result.equity_curve.index[-1] <= pd.Timestamp("2026-08-01", tz="UTC")


def test_history_dir_accepts_str_and_path_alike(tmp_path):
    lake = tmp_path / "lake"
    _write_1m(lake, "TQQQ", days=25)

    as_path = run_backtest(
        strategy_id="donchian", days=10, interval="15m", source="history",
        symbols=["TQQQ"], history_dir=lake,
    )
    clear_partition_cache()
    as_str = run_backtest(
        strategy_id="donchian", days=10, interval="15m", source="history",
        symbols=["TQQQ"], history_dir=str(lake),
    )
    assert as_path.metrics == as_str.metrics


def test_missing_interval_fails_with_a_message_about_data_not_timezones(tmp_path):
    """봉이 없는 심볼은 '데이터가 없다'고 말해야 한다 — `.tz` AttributeError 가 아니라.

    회귀 테스트다: `end` 를 함께 넘기면 빈 인덱스가 먼저 `bar_closes.tz` 에
    닿아 `AttributeError: 'Index' object has no attribute 'tz'` 로 죽었다.
    """
    lake = tmp_path / "lake"
    _write_1m(lake, "TQQQ", days=25)  # SQQQ 는 일부러 만들지 않는다

    with pytest.raises(ValueError) as exc:
        run_backtest(
            strategy_id="donchian", days=10, interval="15m", source="history",
            symbols=["TQQQ", "SQQQ"], history_dir=lake,
            end=pd.Timestamp("2026-07-01", tz="UTC"),
        )
    message = str(exc.value)
    assert "SQQQ" in message
    assert "봉이 없는 심볼" in message


def test_default_history_dir_is_unchanged(tmp_path, monkeypatch):
    """`history_dir` 를 안 주면 기존 기본값(data/history)을 읽는다.

    실데이터에 의존하지 않기 위해 cwd 를 옮겨 그 기본 경로를 만들어 둔다 —
    "기본값이 여전히 상대경로 data/history 다"라는 계약만 본다.
    """
    monkeypatch.chdir(tmp_path)
    _write_1m(tmp_path / "data" / "history", "TQQQ", days=25)

    result = run_backtest(
        strategy_id="donchian", days=10, interval="15m", source="history",
        symbols=["TQQQ"],
        settings_path=str(_repo_settings()),
    )
    assert len(result.equity_curve) > 1


def _repo_settings():
    from quant.adapters.env import REPO_ROOT

    return REPO_ROOT / "config" / "settings.yaml"
