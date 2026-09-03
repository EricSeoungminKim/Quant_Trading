"""`HistoryDataFeed.history()` 의 tail 슬라이스 — **결과를 바꾸지 않아야 한다**.

다년치 1분봉 레이크에서 이 함수는 사이클마다 "보이는 전체"를 리샘플했다. 리플레이가
진행될수록 그 구간이 100만 행까지 자라 walk-forward 가 사실상 불가능해졌다
(2026-09-03 실측: 40거래일 x 4심볼 창 하나가 몇 분). 그래서 필요한 만큼만 잘라
리샘플한다.

**최적화는 답을 바꾸면 최적화가 아니라 버그다.** 여기서 지키는 것은 하나:
잘라낸 결과가 안 잘랐을 때와 정확히 같다 — 갭이 있든 없든, 요청 봉 수가
데이터보다 많든.
"""
from __future__ import annotations

import pandas as pd
import pytest

from quant.adapters.data.history import HistoryDataFeed, clear_partition_cache
from quant.adapters.data.resample import resample_1m


def _write(root, symbol, index):
    price = pd.Series(range(len(index)), index=index).mod(41) + 100.0
    df = pd.DataFrame({
        "open": price, "high": price + 1.0, "low": price - 1.0,
        "close": price + 0.25, "volume": 10.0,
    })
    df.index.name = "ts"
    for (year, month), chunk in df.groupby([df.index.year, df.index.month]):
        p = root / symbol / f"{year:04d}" / f"{month:02d}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        chunk.to_parquet(p)


def _dense(days: int, start="2026-01-05"):
    """정규장 1분봉(하루 390개, 주말 제외) — 세션 사이에 야간 갭이 있다."""
    idx = []
    day = pd.Timestamp(start, tz="UTC")
    made = 0
    while made < days:
        if day.weekday() < 5:
            idx.extend(pd.date_range(day.normalize() + pd.Timedelta("13:30:00"),
                                     periods=390, freq="1min"))
            made += 1
        day += pd.Timedelta(days=1)
    return pd.DatetimeIndex(idx, name="ts")


def _reference(bars, now, interval_minutes, n):
    """최적화 이전의 계산 — 보이는 구간 전체를 리샘플한 뒤 tail(n)."""
    visible = bars.iloc[: int(bars.index.searchsorted(now, side="right"))]
    return resample_1m(visible, interval_minutes).tail(n)


def _same(got, want):
    """값과 타임스탬프가 같은지 본다.

    `check_freq=False` 인 이유: 잘라낸 창이 한 세션 안에 들어가면 리샘플 결과가
    빈 버킷 없이 균일해져 인덱스에 `freq` 가 붙고, 갭을 포함한 전체 구간에서는
    `dropna` 가 빈 버킷을 지워 `freq=None` 이 된다. **인덱스 메타데이터일 뿐
    데이터가 아니다** — 값도 타임스탬프도 동일하다(아래 index 비교가 그걸 못박는다).
    """
    pd.testing.assert_frame_equal(got, want, check_freq=False)
    assert list(got.index) == list(want.index)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_partition_cache()
    yield
    clear_partition_cache()


@pytest.mark.parametrize("interval,minutes", [("5m", 5), ("15m", 15), ("1d", 24 * 60)])
@pytest.mark.parametrize("n", [1, 5, 40, 200])
def test_sliced_history_equals_unsliced(tmp_path, interval, minutes, n):
    index = _dense(20)
    _write(tmp_path, "TQQQ", index)
    feed = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)

    # 리플레이 후반부(보이는 구간이 충분히 길어진 시점)에서 비교한다 — 슬라이스가
    # 실제로 발동하는 지점이어야 검증이 의미가 있다.
    for now in (index[len(index) // 2], index[-1], index[-137]):
        feed.set_now(now)
        got = feed.history("TQQQ", interval, n)
        want = _reference(feed.bars_1m["TQQQ"], now, minutes, n)
        _same(got, want)


def test_asking_for_more_bars_than_exist_returns_everything(tmp_path):
    index = _dense(2)
    _write(tmp_path, "TQQQ", index)
    feed = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)
    feed.set_now(index[-1])

    got = feed.history("TQQQ", "15m", 10_000)
    want = _reference(feed.bars_1m["TQQQ"], index[-1], 15, 10_000)
    _same(got, want)
    assert len(got) < 10_000  # 데이터가 그만큼 없다 — 지어내지 않는다


def test_gaps_do_not_starve_the_window(tmp_path):
    """갭(야간 휴장)이 있으면 같은 행 수가 더 많은 버킷에 걸린다 — 그래도 n봉이
    나와야 한다. 세션 경계를 걸치는 시점에서 확인한다."""
    index = _dense(10)
    _write(tmp_path, "TQQQ", index)
    feed = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)

    # 어떤 세션의 개장 직후 — 직전 봉들은 전날 세션에 있다.
    now = index[390 * 3 + 5]
    feed.set_now(now)
    got = feed.history("TQQQ", "15m", 26)
    assert len(got) == 26
    _same(got, _reference(feed.bars_1m["TQQQ"], now, 15, 26))
