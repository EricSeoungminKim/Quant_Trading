"""HistoryDataFeed(resample correctness, no-look-ahead)와 backfill(idempotent/
resumable, gap logging)을 합성 Parquet 픽스처로 검증한다. 네트워크 호출 없음."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant.adapters.data.history import HistoryDataFeed
from quant.collect.quotes.backfill import backfill


# --------------------------------------------------------------------- helpers

def _write_partition(root: Path, symbol: str, year: int, month: int, df: pd.DataFrame) -> None:
    path = root / symbol / f"{year:04d}" / f"{month:02d}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)


def _make_1m_bars(start: str, n_minutes: int, start_price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n_minutes, freq="1min", tz="UTC")
    prices = [start_price + i for i in range(n_minutes)]
    return pd.DataFrame({
        "open": prices,
        "high": [p + 0.5 for p in prices],
        "low": [p - 0.5 for p in prices],
        "close": prices,
        "volume": [10.0] * n_minutes,
    }, index=idx)


class FakeCandleSource:
    """CandleSource 스텁 — 미리 준비된 1분봉에서 [start,end] 슬라이스만 돌려주고
    호출 인자를 기록한다(idempotent/resumable 검증용)."""

    def __init__(self, bars: pd.DataFrame):
        self._bars = bars
        self.calls: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []

    def fetch_1m(self, symbol, start, end) -> pd.DataFrame:
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        self.calls.append((symbol, start_ts, end_ts))
        return self._bars.loc[(self._bars.index >= start_ts) & (self._bars.index <= end_ts)]


# --------------------------------------------------------- HistoryDataFeed: resample

def test_history_data_feed_resamples_1m_to_15m_correctly(tmp_path):
    bars = _make_1m_bars("2024-01-02T09:30:00Z", 60)  # 4 exact 15m bins
    _write_partition(tmp_path, "TQQQ", 2024, 1, bars)

    feed = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)
    feed.set_now(bars.index[-1])  # 마지막 1분봉 시점까지 전부 보임

    out = feed.history("TQQQ", "15m", 10)

    # resample_1m은 항상 마지막 bin을 버리므로 4개 bin 중 3개만 반환된다.
    assert len(out) == 3
    first = out.iloc[0]
    assert out.index[0] == bars.index[0]
    assert first["open"] == bars.iloc[0]["open"]
    assert first["high"] == bars.iloc[:15]["high"].max()
    assert first["low"] == bars.iloc[:15]["low"].min()
    assert first["close"] == bars.iloc[14]["close"]
    assert first["volume"] == bars.iloc[:15]["volume"].sum()


# --------------------------------------------------------- HistoryDataFeed: look-ahead

def test_history_data_feed_never_returns_bars_after_now(tmp_path):
    bars = _make_1m_bars("2024-01-02T09:30:00Z", 60)
    _write_partition(tmp_path, "TQQQ", 2024, 1, bars)

    feed = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)
    now = bars.index[29]  # 세션 중간 시점
    feed.set_now(now)

    m1 = feed.history("TQQQ", "1m", 100)
    assert (m1.index < now).all()  # now 시점의 봉(형성 중)은 제외

    m15 = feed.history("TQQQ", "15m", 100)
    assert (m15.index + pd.Timedelta(minutes=15) <= now).all()

    q = feed.quote("TQQQ")
    assert q is not None
    # now에 막 열린 봉(index == now)은 아직 형성 중이다 — 그 종가는 now+1분의
    # 미래 가격이므로 현재가로 쓰면 안 된다. history("1m")와 같은 기준.
    assert q.ts < now
    assert q.price == bars.iloc[28]["close"]


def test_history_data_feed_quote_never_uses_the_forming_native_bar(tmp_path):
    """native interval(1분봉 없음) 경로의 look-ahead 회귀 방지.

    실제로 터졌던 결함: 봉 인덱스는 봉 **시가** 시각인데 quote()가 `index <= now`로
    걸러 now에 막 열린 봉을 골랐고, 그 종가는 now+15분의 가격이었다. 15분봉
    백테스트의 모든 체결가와 손절/목표 판정에 15분치 미래 정보가 들어갔다.
    history()의 native 경로는 처음부터 봉 **마감** 기준(index+interval <= now)을
    썼기 때문에 두 메서드가 서로 다른 시각의 가격을 주고 있었다."""
    bars = _make_1m_bars("2024-01-02T09:30:00Z", 4)  # 값만 재사용, 15m 간격으로 재색인
    bars.index = pd.date_range("2024-01-02T09:30:00Z", periods=4, freq="15min", tz="UTC")
    path = tmp_path / "TQQQ" / "15m" / "2024" / "01.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    bars.to_parquet(path)

    feed = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)
    assert feed.bars_1m["TQQQ"].empty  # native 경로를 타는지 확인

    now = bars.index[1] + pd.Timedelta(minutes=15)  # 2번째 봉이 막 마감된 시점
    feed.set_now(now)

    q = feed.quote("TQQQ")
    assert q is not None
    assert q.ts == bars.index[1]
    assert q.price == bars.iloc[1]["close"]
    assert q.price != bars.iloc[2]["close"], "형성 중인 다음 봉의 종가(미래)를 쓰고 있다"

    # quote()와 history()가 같은 시각의 같은 가격을 말해야 한다.
    last_closed = feed.history("TQQQ", "15m", 1)
    assert last_closed.index[-1] == q.ts
    assert last_closed.iloc[-1]["close"] == q.price


def test_quote_uses_the_finest_native_interval_when_several_exist(tmp_path):
    """한 심볼에 여러 native interval이 있으면 quote()는 **가장 짧은** 것을 써야 한다.

    실제로 터졌던 결함: dict 순서(=파일 글롭 정렬 순서)대로 아무 interval이나
    집었다. 15m/1d/5m 파티션이 함께 있으면 "15m"이 먼저 잡히는데, 5분봉 리플레이의
    09:35 사이클에서 09:30 15분봉은 아직 마감 전(09:45)이라 보이지 않아 **전날 마지막
    봉**이 현재가가 됐다. 모든 체결이 전날 종가로 나면서 10년 백테스트에서 진입
    2,558건 중 당일 청산이 0건이 됐다 — 에러 없이 '그럴듯한' 수익률이 나왔다.
    """
    for interval, freq, periods in [("5m", "5min", 12), ("15m", "15min", 4)]:
        idx = pd.date_range("2024-01-02T09:30:00Z", periods=periods, freq=freq, tz="UTC")
        df = pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0,
             "close": [100.0 + i for i in range(periods)], "volume": 1.0},
            index=idx,
        )
        path = tmp_path / "TQQQ" / interval / "2024" / "01.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)

    feed = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)
    # 09:35 = 첫 5분봉이 막 마감된 시점. 15분봉은 아직 하나도 마감되지 않았다.
    feed.set_now(pd.Timestamp("2024-01-02T09:35:00Z"))

    q = feed.quote("TQQQ")
    assert q is not None
    assert q.ts == pd.Timestamp("2024-01-02T09:30:00Z")
    assert q.price == 100.0, "굵은 봉을 골라 과거 가격을 현재가로 쓰고 있다"


def test_partition_cache_shares_data_but_never_shares_replay_time(tmp_path):
    """파티션 캐시는 **읽기 전용 데이터만** 공유해야 한다.

    같은 경로로 두 번째 피드를 만들면 디스크를 다시 읽지 않는다(연구 루프가
    run_backtest을 수백 번 부르므로 로딩 비용이 그대로 실행시간이 된다). 하지만
    리플레이 시각(`_now`)까지 공유되면 한 백테스트가 다른 백테스트의 시계를
    움직이는 셈이라 결과가 조용히 오염된다.
    """
    from quant.adapters.data.history import clear_partition_cache

    clear_partition_cache()
    idx = pd.date_range("2024-01-02T09:30:00Z", periods=4, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": [10.0, 11.0, 12.0, 13.0], "volume": 1.0},
        index=idx,
    )
    path = tmp_path / "TQQQ" / "15m" / "2024" / "01.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)

    first = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)
    second = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)
    # 같은 객체를 공유해 재로딩을 피한다.
    assert first._native["TQQQ"]["15m"] is second._native["TQQQ"]["15m"]

    first.set_now(idx[1] + pd.Timedelta(minutes=15))
    assert second._now is None, "리플레이 시각이 새어 나가면 백테스트끼리 서로를 오염시킨다"
    assert second.quote("TQQQ") is None
    clear_partition_cache()


def test_partition_cache_key_is_absolute_not_cwd_relative(tmp_path, monkeypatch):
    """상대 경로를 캐시 키로 쓰면 cwd가 다른 두 디렉토리가 서로를 덮어쓴다.

    실제로 터졌던 결함: 기본 history_dir이 상대 경로 `"data/history"`인데 키를
    그대로 문자열로 썼다. e2e 테스트가 `chdir(tmp_path)` 후 tmp에 픽스처를 쓰자
    그것이 `("data/history", "TQQQ")` 키를 점유했고, 이후 실데이터로 도는
    백테스트가 전부 그 픽스처를 읽어 깨졌다.
    """
    from quant.adapters.data.history import clear_partition_cache

    clear_partition_cache()
    for workspace, close in [("a", 111.0), ("b", 222.0)]:
        idx = pd.date_range("2024-01-02T09:30:00Z", periods=2, freq="15min", tz="UTC")
        df = pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": close, "volume": 1.0}, index=idx
        )
        path = tmp_path / workspace / "data" / "history" / "TQQQ" / "15m" / "2024" / "01.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)

    seen = []
    for workspace in ("a", "b"):
        monkeypatch.chdir(tmp_path / workspace)
        feed = HistoryDataFeed(["TQQQ"])  # 기본 상대 경로 "data/history"
        seen.append(float(feed._native["TQQQ"]["15m"]["close"].iloc[0]))

    assert seen == [111.0, 222.0], f"cwd가 다른 워크스페이스가 캐시를 공유했다: {seen}"
    clear_partition_cache()


# ------------------------------------------------------------- backfill: write + clean

def test_backfill_writes_partition_and_drops_bad_bars(tmp_path):
    bars = _make_1m_bars("2024-01-29T09:30:00Z", 5)
    # 나쁜 봉(high<low) 하나를 섞는다 — 제거되어야 한다.
    bad_ts = bars.index[2] + pd.Timedelta(seconds=1)
    bars.loc[bad_ts] = {"open": 100, "high": 90, "low": 95, "close": 100, "volume": 1.0}
    bars = bars.sort_index()

    source = FakeCandleSource(bars)
    report = backfill(
        "TQQQ", source,
        start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-01-29T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-01-29T12:00:00Z"),
    )

    assert report.partitions_written == ["2024-01"]
    path = tmp_path / "TQQQ" / "2024" / "01.parquet"
    saved = pd.read_parquet(path)
    assert len(saved) == 5  # 나쁜 봉 제거됨
    assert (saved["high"] >= saved["low"]).all()


# ------------------------------------------------------ backfill: idempotent/resumable

def test_backfill_skips_closed_partition_and_refetches_only_the_gap(tmp_path):
    all_bars = pd.concat([
        _make_1m_bars("2024-01-29T09:30:00Z", 3),
        _make_1m_bars("2024-01-30T09:30:00Z", 3),
        _make_1m_bars("2024-01-31T09:30:00Z", 3),
        _make_1m_bars("2024-02-01T09:30:00Z", 3),
        _make_1m_bars("2024-02-02T09:30:00Z", 3),
        _make_1m_bars("2024-02-05T09:30:00Z", 3),
        _make_1m_bars("2024-02-06T09:30:00Z", 3),
        _make_1m_bars("2024-02-07T09:30:00Z", 3),
        _make_1m_bars("2024-02-08T09:30:00Z", 3),
    ])

    source1 = FakeCandleSource(all_bars)
    report1 = backfill(
        "TQQQ", source1,
        start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-02-02T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-02-02T12:00:00Z"),
    )
    assert set(report1.partitions_written) == {"2024-01", "2024-02"}
    assert report1.gaps == []  # Jan29-Feb2는 전부 평일이고 데이터가 있다

    source2 = FakeCandleSource(all_bars)
    report2 = backfill(
        "TQQQ", source2,
        start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-02-08T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-02-08T12:00:00Z"),
    )

    # 1월은 이미 완결된(과거로 끝난) 파티션 — 재조회 없이 스킵되어야 한다.
    assert report2.partitions_skipped == ["2024-01"]
    assert not any(call[0] == "TQQQ" and pd.Timestamp("2024-01-01", tz="UTC") <= call[1] < pd.Timestamp("2024-02-01", tz="UTC")
                   for call in source2.calls)

    # 2월은 기존 데이터(1~2일)를 넘어선 gap만 재조회해야 한다.
    assert report2.partitions_written == ["2024-02"]
    assert len(source2.calls) == 1
    _, gap_start, gap_end = source2.calls[0]
    feb2_last = all_bars.loc["2024-02-02"].index.max()
    assert gap_start > feb2_last  # 이미 받은 2/1~2/2 구간은 다시 요청하지 않는다

    feb_path = tmp_path / "TQQQ" / "2024" / "02.parquet"
    saved_feb = pd.read_parquet(feb_path)
    assert len(saved_feb) == 6 * 3  # Feb 1,2,5,6,7,8 각 3분


# --------------------------------------------------------------- backfill: gap logging

def test_backfill_reports_missing_weekday_sessions(tmp_path):
    # 1/30(화)이 통째로 빠진 데이터 — gap으로 잡혀야 한다.
    bars = pd.concat([
        _make_1m_bars("2024-01-29T09:30:00Z", 3),
        _make_1m_bars("2024-01-31T09:30:00Z", 3),
    ])
    source = FakeCandleSource(bars)
    report = backfill(
        "TQQQ", source,
        start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-01-31T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-01-31T12:00:00Z"),
    )
    assert report.gaps == ["2024-01-30"]
