"""신규 데이터 소스(YFinanceCandleSource, AlpacaCandleSource)와 backfill()/
HistoryDataFeed의 native(1분봉이 아닌) interval 확장을 검증한다. 네트워크 호출
없음 — yfinance/httpx는 전부 모킹.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant.adapters.data.history import HistoryDataFeed
from quant.collect.quotes.backfill import backfill


# --------------------------------------------------------------------- helpers

def _make_bars(start: str, n_bars: int, freq: str, start_price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n_bars, freq=freq, tz="UTC")
    prices = [start_price + i for i in range(n_bars)]
    return pd.DataFrame({
        "open": prices,
        "high": [p + 0.5 for p in prices],
        "low": [p - 0.5 for p in prices],
        "close": prices,
        "volume": [10.0] * n_bars,
    }, index=idx)


class FakeMultiIntervalSource:
    """MultiIntervalCandleSource 스텁 — native_interval을 선언하고 [start,end]
    슬라이스만 돌려주며 호출 인자를 기록한다."""

    def __init__(self, native_interval: str, bars: pd.DataFrame):
        self.native_interval = native_interval
        self._bars = bars
        self.calls: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []

    def fetch(self, symbol, start, end) -> pd.DataFrame:
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        self.calls.append((symbol, start_ts, end_ts))
        return self._bars.loc[(self._bars.index >= start_ts) & (self._bars.index <= end_ts)]


# ------------------------------------------------------ backfill: native interval

def test_backfill_writes_native_interval_to_dedicated_partition_path(tmp_path):
    bars = _make_bars("2024-01-29T09:30:00Z", 26, "15min")
    source = FakeMultiIntervalSource("15m", bars)

    report = backfill(
        "TQQQ", source,
        start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-01-29T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-01-29T12:00:00Z"), interval="15m",
    )

    assert report.partitions_written == ["2024-01"]
    path = tmp_path / "TQQQ" / "15m" / "2024" / "01.parquet"
    assert path.exists()
    saved = pd.read_parquet(path)
    assert len(saved) == 26

    # 1분봉 경로와 절대 겹치지 않는다.
    assert not (tmp_path / "TQQQ" / "2024" / "01.parquet").exists()


def test_backfill_names_the_index_ts_regardless_of_vendor(tmp_path):
    """벤더가 뭐라 부르든 파티션의 인덱스 컬럼은 `ts` 다.

    **2026-08-13 실측 사고.** yfinance 는 일봉 인덱스를 `Date` 로 준다. 그대로
    쓰니 `data/history/QQQ/1d/2026/08.parquet` 만 `Date` 컬럼을 갖게 됐고,
    `quant.adapters.olap.coverage()` 는 `count(ts)` 를 세므로 `union_by_name` 아래서
    그 파일이 **NULL 로 흘러가 안 보였다** — 봉 8개를 새로 받았는데 커버리지는
    "마지막 봉 07-31, 2659개"로 변하지 않았다.

    데이터는 있는데 측정이 "없다"고 말하는 것 — Phase 4 의 적합도 게이트와 백필
    신선도 감시가 **둘 다** 이 컬럼을 읽는다. 그래서 벤더별로 받아주는 게 아니라
    쓰는 지점 하나에서 정규화한다.
    """
    bars = _make_bars("2024-01-29T00:00:00Z", 3, "1D")
    bars.index.name = "Date"  # yfinance 가 실제로 주는 이름
    source = FakeMultiIntervalSource("1d", bars)

    backfill(
        "QQQ", source,
        start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-01-31T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-02-15T00:00:00Z"), interval="1d",
    )

    import pyarrow.parquet as pq
    schema = pq.read_schema(tmp_path / "QQQ" / "1d" / "2024" / "01.parquet")
    assert "ts" in schema.names
    assert "Date" not in schema.names


def test_backfill_does_not_write_an_empty_partition(tmp_path):
    """받은 봉이 0개면 파일을 만들지 않는다.

    빈 DataFrame 은 parquet 왕복에서 DatetimeIndex 를 잃고 RangeIndex 가 된다.
    그 파일이 섞이면 `concat` 인덱스가 혼합 타입이 되고, 한참 뒤 `tz_convert` 가
    "Cannot convert tz-naive timestamps" 로 죽는다 — 봉을 읽는 시점이 아니라
    리플레이 타임라인을 만들 때 터져서 원인을 찾기 어렵다.

    ce6a755(history.py)와 884f1eb(regime provider)는 그 파일을 **걸러내는** 방어였다.
    여기가 생산지다 — 실측으로 로컬에 13개가 그렇게 쌓여 있었고, 전부 0행이었다.
    """
    empty = _make_bars("2024-01-29T00:00:00Z", 0, "1D")
    source = FakeMultiIntervalSource("1d", empty)

    report = backfill(
        "QQQ", source,
        start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-01-31T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-02-15T00:00:00Z"), interval="1d",
    )

    assert not (tmp_path / "QQQ" / "1d" / "2024" / "01.parquet").exists()
    assert report.partitions_written == []
    assert report.total_bars == 0


def test_backfill_still_writes_when_some_bars_arrive(tmp_path):
    """빈 파티션을 안 쓴다는 규칙이 정상 경로를 막지 않는지 — 위 테스트의 짝."""
    source = FakeMultiIntervalSource("1d", _make_bars("2024-01-29T00:00:00Z", 3, "1D"))

    report = backfill(
        "QQQ", source,
        start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-01-31T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-02-15T00:00:00Z"), interval="1d",
    )

    assert report.partitions_written == ["2024-01"]
    assert len(pd.read_parquet(tmp_path / "QQQ" / "1d" / "2024" / "01.parquet")) == 3


def test_backfill_normalizes_existing_partition_tz_before_concat_with_fresh_bars(tmp_path):
    """회귀 재현(2026-08-19, KR 개별종목 일봉 백필): 같은 심볼·interval 파티션을
    서로 다른 벤더가 채울 수 있다 — 069500 1d는 backfill_kr_daily.sh가 yfinance로
    (UTC 정규화), KR 개별종목 백필은 Toss로(원래 고정 오프셋 +09:00) 채운다.

    수정 전에는 파일에 이미 있는 tz-aware-지만-비UTC 인덱스를 그대로 두고 UTC
    fresh 봉과 concat했다 — pandas가 tz 불일치를 object dtype Index로 흘려버려
    DatetimeIndex를 잃고, 한참 뒤(`_find_gaps`의 `.tz_convert` 호출)에서
    `AttributeError: 'Index' object has no attribute 'tz_convert'`로 죽었다(실측).
    이제는 partition을 읽는 시점에 UTC로 정규화해 이 충돌을 원천 차단한다."""
    existing_idx = pd.DatetimeIndex(["2024-01-02T09:00:00+09:00"])  # 고정 오프셋, UTC 아님
    existing = pd.DataFrame({
        "open": [100.0], "high": [100.5], "low": [99.5], "close": [100.2], "volume": [10.0],
    }, index=existing_idx)
    existing.index.name = "ts"
    path = tmp_path / "069500" / "1d" / "2024" / "01.parquet"
    path.parent.mkdir(parents=True)
    existing.to_parquet(path)

    # existing 마지막 봉(2024-01-02)이 윈도우 끝(01-31)에서 5일 완결 허용치보다 훨씬
    # 이전이므로 "이미 완결"로 스킵되지 않고 실제로 gap fetch가 일어난다.
    fresh_bars = _make_bars("2024-01-30T00:00:00Z", 1, "1D")  # UTC — Toss 소스 수정 후와 동일
    source = FakeMultiIntervalSource("1d", fresh_bars)

    report = backfill(
        "069500", source,
        start=pd.Timestamp("2024-01-01T00:00:00Z"), end=pd.Timestamp("2024-01-31T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-02-15T00:00:00Z"), interval="1d",
    )

    assert report.partitions_written == ["2024-01"]
    saved = pd.read_parquet(path)
    assert len(saved) == 2  # 기존 1봉 + 신규 1봉, dedup 없이 합쳐짐
    assert str(saved.index.tz) == "UTC"


def test_backfill_rejects_native_interval_mismatch(tmp_path):
    bars = _make_bars("2024-01-29T09:30:00Z", 4, "1h")
    source = FakeMultiIntervalSource("1h", bars)  # native는 1h인데 15m을 요청

    with pytest.raises(ValueError, match="native_interval"):
        backfill(
            "TQQQ", source,
            start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-01-29T23:59:59Z"),
            history_dir=tmp_path, now=pd.Timestamp("2024-01-29T12:00:00Z"), interval="15m",
        )


# --------------------------------------------------- HistoryDataFeed: native-only

def test_history_data_feed_serves_native_interval_without_resampling(tmp_path):
    bars = _make_bars("2024-01-29T09:30:00Z", 26, "15min")  # 1분봉 없이 15m native만 존재
    source = FakeMultiIntervalSource("15m", bars)
    backfill(
        "TQQQ", source,
        start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-01-29T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-01-29T12:00:00Z"), interval="15m",
    )

    feed = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)
    assert feed.bars_1m["TQQQ"].empty  # 1분봉은 아예 없다

    now = bars.index[-1] + pd.Timedelta(minutes=15)  # 마지막 봉까지 완성된 시점
    feed.set_now(now)
    out = feed.history("TQQQ", "15m", 100)

    # 리샘플 없이 native 그대로 반환돼야 한다 — 26개 전부, 값도 원본과 동일.
    assert len(out) == 26
    assert out.index[0] == bars.index[0]
    assert out.iloc[0]["close"] == bars.iloc[0]["close"]


def test_history_data_feed_refuses_to_upsample_native_interval(tmp_path):
    bars = _make_bars("2024-01-29T09:30:00Z", 26, "15min")
    source = FakeMultiIntervalSource("15m", bars)
    backfill(
        "TQQQ", source,
        start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-01-29T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-01-29T12:00:00Z"), interval="15m",
    )

    feed = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)
    feed.set_now(bars.index[-1] + pd.Timedelta(minutes=15))

    # 5분봉은 갖고 있지 않다 — 15분봉을 쪼개 지어내지 않고 빈 결과를 반환해야 한다.
    out = feed.history("TQQQ", "5m", 100)
    assert out.empty


def test_history_data_feed_bar_closes_native_interval(tmp_path):
    bars = _make_bars("2024-01-29T09:30:00Z", 26, "15min")
    source = FakeMultiIntervalSource("15m", bars)
    backfill(
        "TQQQ", source,
        start=pd.Timestamp("2024-01-29T00:00:00Z"), end=pd.Timestamp("2024-01-29T23:59:59Z"),
        history_dir=tmp_path, now=pd.Timestamp("2024-01-29T12:00:00Z"), interval="15m",
    )

    feed = HistoryDataFeed(["TQQQ"], history_dir=tmp_path)
    closes = feed.bar_closes("TQQQ", "15m")
    assert len(closes) == 26
    assert closes[0] == bars.index[0] + pd.Timedelta(minutes=15)


# --------------------------------------------------------------- YFinanceCandleSource

class _FakeYFTicker:
    """yfinance.Ticker 스텁 — 고정된 DataFrame을 반환하고 호출 인자를 기록한다."""

    calls: list[dict] = []
    tickers: list[str] = []  # yf.Ticker(...)에 실제로 넘어온 심볼 기록(KR 매핑 검증용)
    index_override: pd.DatetimeIndex | None = None  # 테스트가 커스텀 인덱스를 주입할 때 사용

    def __init__(self, symbol):
        self.symbol = symbol
        _FakeYFTicker.tickers.append(symbol)

    def history(self, **kwargs):
        _FakeYFTicker.calls.append(kwargs)
        idx = _FakeYFTicker.index_override
        if idx is None:
            idx = pd.date_range("2026-06-01 09:30", periods=4, freq="15min", tz="America/New_York")
        import pandas as _pd
        n = len(idx)
        return _pd.DataFrame({
            "Open": [70.0 + 0.5 * i for i in range(n)],
            "High": [70.6 + 0.5 * i for i in range(n)],
            "Low": [69.9 + 0.5 * i for i in range(n)],
            "Close": [70.5 + 0.5 * i for i in range(n)],
            "Volume": [1000.0 + 100.0 * i for i in range(n)],
            "Dividends": [0.0] * n,
            "Stock Splits": [0.0] * n,
        }, index=idx)


def test_yfinance_source_rejects_unsupported_interval():
    from quant.collect.quotes.yf_source import YFinanceCandleSource

    with pytest.raises(ValueError, match="interval"):
        YFinanceCandleSource("1m")


def test_yfinance_source_normalizes_to_utc_and_matches_columns(monkeypatch):
    import quant.collect.quotes.yf_source as yf_source_mod

    _FakeYFTicker.calls = []
    monkeypatch.setattr(yf_source_mod.yf, "Ticker", _FakeYFTicker)

    source = yf_source_mod.YFinanceCandleSource("15m")
    assert source.native_interval == "15m"

    start = pd.Timestamp("2026-06-01", tz="UTC")
    end = pd.Timestamp("2026-06-02", tz="UTC")
    out = source.fetch("TQQQ", start, end)

    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert str(out.index.tz) == "UTC"
    assert out.index.is_monotonic_increasing
    assert not out.index.duplicated().any()


def test_yfinance_source_clamps_start_beyond_hard_limit(monkeypatch):
    import quant.collect.quotes.yf_source as yf_source_mod

    _FakeYFTicker.calls = []
    monkeypatch.setattr(yf_source_mod.yf, "Ticker", _FakeYFTicker)

    source = yf_source_mod.YFinanceCandleSource("15m")
    now = pd.Timestamp.now(tz="UTC")
    far_past_start = now - pd.Timedelta(days=200)  # 60일 한도를 훌쩍 초과
    source.fetch("TQQQ", far_past_start, now)

    passed_start = pd.Timestamp(_FakeYFTicker.calls[-1]["start"])
    # 실제로 보낸 start는 60일 한도 근방으로 clamp돼 있어야 한다(요청한 200일 전이 아님).
    assert passed_start > far_past_start
    assert (now - passed_start) <= pd.Timedelta(days=60)


def test_yfinance_source_maps_kr_6digit_symbol_to_ks_suffix(monkeypatch):
    """KR 앵커 일봉 백필(G): 6자리 숫자 심볼은 Yahoo에 "{symbol}.KS"로 질의하되,
    fetch()가 반환/저장에 쓰는 심볼(호출부 인자)은 원래 6자리 그대로여야 한다 —
    개장일 판정 앵커 경로(data/history/069500/1d)와 어긋나면 안 된다."""
    import quant.collect.quotes.yf_source as yf_source_mod

    _FakeYFTicker.calls = []
    _FakeYFTicker.tickers = []
    monkeypatch.setattr(yf_source_mod.yf, "Ticker", _FakeYFTicker)

    source = yf_source_mod.YFinanceCandleSource("1d")
    start = pd.Timestamp("2026-08-01", tz="UTC")
    end = pd.Timestamp("2026-08-14", tz="UTC")

    source.fetch("069500", start, end)
    assert _FakeYFTicker.tickers[-1] == "069500.KS"


def test_yfinance_source_leaves_non_numeric_symbol_unmapped(monkeypatch):
    """QQQ 같은 비숫자 심볼은 매핑 없이 그대로 Yahoo에 질의한다."""
    import quant.collect.quotes.yf_source as yf_source_mod

    _FakeYFTicker.calls = []
    _FakeYFTicker.tickers = []
    monkeypatch.setattr(yf_source_mod.yf, "Ticker", _FakeYFTicker)

    source = yf_source_mod.YFinanceCandleSource("1d")
    start = pd.Timestamp("2026-08-01", tz="UTC")
    end = pd.Timestamp("2026-08-14", tz="UTC")

    source.fetch("QQQ", start, end)
    assert _FakeYFTicker.tickers[-1] == "QQQ"


def test_yfinance_source_kr_daily_kst_midnight_preserves_trade_date(monkeypatch):
    """회귀 재현: KR(069500.KS) 일봉은 Yahoo가 tz-aware Asia/Seoul 00:00으로 돌려준다.
    수정 전에는 이걸 그대로 tz_convert("UTC")해서 08-14 KST 자정 봉이 08-13 15:00
    UTC로 저장됐다 — opendays.py의 개장일 판정이 하루 어긋나는 실제 결함이었다.
    수정 후에는 로컬 캘린더 날짜를 그대로 UTC 자정으로 보존해야 한다."""
    import quant.collect.quotes.yf_source as yf_source_mod

    _FakeYFTicker.calls = []
    _FakeYFTicker.tickers = []
    _FakeYFTicker.index_override = pd.DatetimeIndex(
        ["2026-08-14 00:00:00"], tz="Asia/Seoul"
    )
    monkeypatch.setattr(yf_source_mod.yf, "Ticker", _FakeYFTicker)

    try:
        source = yf_source_mod.YFinanceCandleSource("1d")
        start = pd.Timestamp("2026-08-01", tz="UTC")
        end = pd.Timestamp("2026-08-15", tz="UTC")
        out = source.fetch("069500", start, end)
    finally:
        _FakeYFTicker.index_override = None

    assert len(out) == 1
    assert str(out.index.tz) == "UTC"
    assert out.index[0] == pd.Timestamp("2026-08-14", tz="UTC")


def test_yfinance_source_us_naive_daily_index_unchanged(monkeypatch):
    """US 일봉의 기존 tz-naive 경로(America/New_York으로 간주 후 UTC 변환)는
    이번 KR 수정으로 바뀌면 안 된다 — 기존 QQQ 파티션이 계속 이어붙어야 한다."""
    import quant.collect.quotes.yf_source as yf_source_mod

    _FakeYFTicker.calls = []
    _FakeYFTicker.tickers = []
    _FakeYFTicker.index_override = pd.DatetimeIndex(["2026-08-14 00:00:00"])  # tz-naive
    monkeypatch.setattr(yf_source_mod.yf, "Ticker", _FakeYFTicker)

    try:
        source = yf_source_mod.YFinanceCandleSource("1d")
        start = pd.Timestamp("2026-08-01", tz="UTC")
        end = pd.Timestamp("2026-08-15", tz="UTC")
        out = source.fetch("QQQ", start, end)
    finally:
        _FakeYFTicker.index_override = None

    assert len(out) == 1
    assert str(out.index.tz) == "UTC"
    expected = pd.Timestamp("2026-08-14", tz="America/New_York").tz_convert("UTC")
    assert out.index[0] == expected


# --------------------------------------------------------------- AlpacaCandleSource

def test_alpaca_source_fails_clearly_without_env_keys(monkeypatch):
    from quant.collect.quotes.alpaca_source import AlpacaCandleSource

    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ALPACA_API_KEY_ID"):
        AlpacaCandleSource()


def test_alpaca_source_paginates_with_next_page_token(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")

    from quant.collect.quotes.alpaca_source import AlpacaCandleSource

    pages = [
        {
            "bars": [
                {"t": "2024-01-02T09:30:00Z", "o": 10.0, "h": 10.5, "l": 9.5, "c": 10.2, "v": 100.0},
            ],
            "next_page_token": "tok1",
        },
        {
            "bars": [
                {"t": "2024-01-02T09:31:00Z", "o": 10.2, "h": 10.6, "l": 9.9, "c": 10.4, "v": 120.0},
            ],
            "next_page_token": None,
        },
    ]

    class FakeResponse:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class FakeHttpClient:
        def __init__(self, *args, **kwargs):
            self.requests = []

        def get(self, url, params=None):
            self.requests.append(params)
            return FakeResponse(pages[len(self.requests) - 1])

    # 이 테스트의 관심사는 페이지네이션이지 세션 필터가 아니므로 필터를 끈다
    source = AlpacaCandleSource(regular_session_only=False)
    source._http = FakeHttpClient()

    out = source.fetch("TQQQ", pd.Timestamp("2024-01-02T00:00:00Z"), pd.Timestamp("2024-01-02T23:59:59Z"))

    assert len(out) == 2
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert source._http.requests[1]["page_token"] == "tok1"


def test_alpaca_source_1m_instance_implements_fetch_1m(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")

    from quant.collect.quotes.alpaca_source import AlpacaCandleSource

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"bars": [], "next_page_token": None}

    class FakeHttpClient:
        def get(self, url, params=None):
            return FakeResponse()

    source = AlpacaCandleSource(interval="1m", regular_session_only=False)
    source._http = FakeHttpClient()
    out = source.fetch_1m("TQQQ", pd.Timestamp("2024-01-02", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC"))
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]


def test_alpaca_source_non_1m_instance_rejects_fetch_1m(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")

    from quant.collect.quotes.alpaca_source import AlpacaCandleSource

    source = AlpacaCandleSource(interval="15m")
    with pytest.raises(NotImplementedError):
        source.fetch_1m("TQQQ", pd.Timestamp("2024-01-02", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC"))


def test_alpaca_regular_session_filter_drops_extended_hours():
    """Alpaca는 04:00~20:00 ET 시간외까지 준다. 정규장만 남기지 않으면 Donchian의
    거래량 필터가 망가진다 — 시간외 거래량이 정규장의 6.6%라 평균이 끌려 내려가
    조건이 사실상 항상 참이 된다 (2026-08-06 실측)."""
    import pandas as pd

    from quant.collect.quotes.alpaca_source import _regular_session_only

    # ET 기준 프리마켓(08:00), 정규장(10:00, 15:45, 15:55), 애프터(17:00)
    idx = pd.to_datetime([
        "2024-06-03T12:00:00Z",  # 08:00 ET 프리마켓
        "2024-06-03T14:00:00Z",  # 10:00 ET 정규장
        "2024-06-03T19:45:00Z",  # 15:45 ET 정규장
        "2024-06-03T19:55:00Z",  # 15:55 ET 정규장 마지막 5분봉
        "2024-06-03T21:00:00Z",  # 17:00 ET 애프터
    ], utc=True)
    df = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx
    )

    # 15분봉: 마지막으로 남길 봉의 시가는 15:45(16:00에 마감).
    et15 = _regular_session_only(df, 15).tz_convert("America/New_York")
    assert [t.strftime("%H:%M") for t in et15.index] == ["10:00", "15:45"]

    # 5분봉: 경계가 15:55로 넓어져야 한다. 15:45로 하드코딩돼 있으면 마감 직전
    # 10분(=EoD 청산이 일어나는 구간)이 조용히 사라진다.
    et5 = _regular_session_only(df, 5).tz_convert("America/New_York")
    assert [t.strftime("%H:%M") for t in et5.index] == ["10:00", "15:45", "15:55"]
