from datetime import date

import pandas as pd
import pytest

from quant.collect.sources import market as market_module
from quant.collect.sources.market import ANCHORS, TICKERS, crosscheck, fetch_quotes, fetch_symbol_quotes


def test_tickers_has_both_markets_and_is_nonempty():
    assert TICKERS["KR"]
    assert TICKERS["US"]


def test_anchors_kr_has_samsung_and_skhynix():
    assert ANCHORS["KR"]["005930.KS"] == "삼성전자"
    assert ANCHORS["KR"]["000660.KS"] == "SK하이닉스"


def test_crosscheck_within_tolerance_has_no_warnings():
    primary = {"^GSPC": 5000.0}
    secondary = {"^GSPC": 5100.0}  # 2% 차이 — 3% 허용오차 이내 (FRED 는 1~2일 지연될 수 있음)
    result = crosscheck(primary, secondary)
    assert result["warnings"] == []
    assert result["checked"] == ["^GSPC"]


def test_crosscheck_large_divergence_produces_warning():
    primary = {"^GSPC": 7728.20}
    secondary = {"^GSPC": 6500.00}
    result = crosscheck(primary, secondary)
    assert len(result["warnings"]) == 1
    assert "^GSPC" in result["warnings"][0]


def test_crosscheck_ignores_symbols_missing_from_secondary():
    primary = {"^GSPC": 5000.0, "^KS11": 3000.0}
    secondary = {"^GSPC": 5010.0}
    result = crosscheck(primary, secondary)
    assert result["checked"] == ["^GSPC"]
    assert result["warnings"] == []


@pytest.mark.live
def test_fetch_quotes_kr_live():
    data = fetch_quotes("KR")
    assert data["quotes"]
    for sym, q in data["quotes"].items():
        assert "close" in q and q["close"] is not None
        assert len(q["history"]) >= 2
    assert "005930.KS" in data["anchors"]


@pytest.mark.live
def test_fetch_quotes_us_crosscheck_actually_runs():
    """FRED 교차검증이 실제로 동작하는지 확인 — checked 가 비어있으면 여전히
    무력화된 상태다(yfinance 단일 벤더 의존에 대한 유일한 방어책이 없는 것)."""
    data = fetch_quotes("US")
    assert data["crosscheck"]["checked"], "FRED 교차검증이 아무 심볼도 커버하지 못했다"


# ------------------------------------------------------- fetch_symbol_quotes

def test_fetch_symbol_quotes_empty_list_returns_empty_dict_without_download(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("빈 리스트인데 yf.download 를 호출했다")

    monkeypatch.setattr(market_module.yf, "download", boom)
    assert fetch_symbol_quotes([]) == {}


def test_fetch_symbol_quotes_single_symbol_handles_series_response(monkeypatch):
    """일부 yfinance 버전은 심볼 1개일 때 컬럼 레벨이 빠진 Series 를 준다 —
    DataFrame 뿐 아니라 이 경우도 처리해야 한다."""
    idx = pd.date_range("2026-08-01", periods=3, freq="D")
    series = pd.Series([100.0, 105.0, 110.0], index=idx)

    monkeypatch.setattr(
        market_module.yf, "download", lambda *a, **k: pd.DataFrame({"Close": series})
    )
    # DataFrame({"Close": series})["Close"] 는 Series 다 — 실제 결측 컬럼 케이스와 동일한 모양.
    out = fetch_symbol_quotes(["005930.KS"])
    assert out["005930.KS"]["close"] == 110.0
    assert out["005930.KS"]["prev"] == 105.0
    assert out["005930.KS"]["history"] == [100.0, 105.0, 110.0]


def test_fetch_symbol_quotes_multiple_symbols_dataframe(monkeypatch):
    idx = pd.date_range("2026-08-01", periods=2, freq="D")
    close = pd.DataFrame(
        {"005930.KS": [230000.0, 256250.0], "192820.KQ": [15000.0, 15300.0]}, index=idx
    )
    frame = pd.concat({"Close": close}, axis=1)

    monkeypatch.setattr(market_module.yf, "download", lambda *a, **k: frame)
    out = fetch_symbol_quotes(["005930.KS", "192820.KQ"])
    assert out["005930.KS"]["close"] == 256250.0
    assert out["192820.KQ"]["close"] == 15300.0


def test_fetch_symbol_quotes_skips_symbol_missing_from_response(monkeypatch):
    idx = pd.date_range("2026-08-01", periods=2, freq="D")
    close = pd.DataFrame({"005930.KS": [230000.0, 256250.0]}, index=idx)
    frame = pd.concat({"Close": close}, axis=1)

    monkeypatch.setattr(market_module.yf, "download", lambda *a, **k: frame)
    out = fetch_symbol_quotes(["005930.KS", "999999.KQ"])
    assert "005930.KS" in out
    assert "999999.KQ" not in out


def test_fetch_symbol_quotes_download_failure_returns_empty_dict(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("네트워크 끊김")

    monkeypatch.setattr(market_module.yf, "download", boom)
    assert fetch_symbol_quotes(["005930.KS"]) == {}


@pytest.mark.live
def test_fetch_symbol_quotes_live_single_and_multiple():
    single = fetch_symbol_quotes(["005930.KS"])
    assert single["005930.KS"]["close"] is not None
    assert len(single["005930.KS"]["history"]) >= 2

    multi = fetch_symbol_quotes(["005930.KS", "000660.KS"])
    assert "005930.KS" in multi and "000660.KS" in multi


def test_fetch_symbol_quotes_carries_ohlcv(monkeypatch):
    """단일 심볼(컬럼 레벨 없는 실제 yfinance 응답 모양)에서도 ohlcv 가 붙는다."""
    idx = pd.date_range("2026-01-01", periods=35, freq="B")
    frame = pd.DataFrame(
        {
            "Open": [100.0 + i for i in range(35)],
            "High": [105.0 + i for i in range(35)],
            "Low": [95.0 + i for i in range(35)],
            "Close": [102.0 + i for i in range(35)],
            "Volume": [1000.0 + i for i in range(35)],
        },
        index=idx,
    )
    monkeypatch.setattr(market_module.yf, "download", lambda *a, **k: frame)

    q = fetch_symbol_quotes(["TQQQ"])
    df = q["TQQQ"]["ohlcv"]
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.is_monotonic_increasing
    # 기존 키 불변
    assert {"close", "prev", "change_pct", "history"} <= set(q["TQQQ"].keys())
    assert q["TQQQ"]["close"] == 136.0  # Close 컬럼 마지막 값 — 기존 계산 경로와 동일


# ------------------------------------------- 2026-08-19 KOSPI 스테일 데이터 사고

def test_fetch_symbol_quotes_excludes_unconfirmed_today_bar(monkeypatch):
    """회귀 테스트 — 2026-08-19 KOSPI 사고: 마지막 봉이 '오늘' 날짜면 아직 장중
    진행 중이거나 마감 전이라 계속 값이 바뀌는 미확정 스냅샷일 수 있다. 이걸
    그대로 '오늘 종가'로 써서 실제 마지막 확정 종가(-1.55%, 6,869.83)를 두고
    +2.42%를 냈다. 오늘(08-16) 봉의 9999.0 은 아직 마감 안 된 값이므로 버리고,
    그 이전 확정 거래일 두 개(08-14→08-15)로 계산해야 한다."""
    idx = pd.date_range("2026-08-14", periods=3, freq="D")  # 08-14, 08-15(확정), 08-16(오늘=미확정)
    series = pd.Series([6977.94, 6869.83, 9999.0], index=idx)

    monkeypatch.setattr(
        market_module.yf, "download", lambda *a, **k: pd.DataFrame({"Close": series})
    )
    out = fetch_symbol_quotes(["^KS11"], today=date(2026, 8, 16))
    assert out["^KS11"]["close"] == 6869.83
    assert out["^KS11"]["prev"] == 6977.94
    assert out["^KS11"]["date"] == "2026-08-15"
    assert round(out["^KS11"]["change_pct"], 2) == -1.55  # 실제 KOSPI 08-18 등락률과 동일 계산식


def test_fetch_symbol_quotes_single_confirmed_bar_omits_change_pct(monkeypatch):
    """확정 거래일이 1개뿐이면 '전일 대비'를 계산할 기준이 없다 — 0%(변동없음)로
    위장하면 결측을 거짓으로 감추는 것과 같으므로 prev/change_pct 키 자체가 없어야 한다."""
    idx = pd.date_range("2026-08-15", periods=2, freq="D")  # 08-15(확정 1개), 08-16(오늘=미확정)
    series = pd.Series([6869.83, 1234.0], index=idx)

    monkeypatch.setattr(
        market_module.yf, "download", lambda *a, **k: pd.DataFrame({"Close": series})
    )
    out = fetch_symbol_quotes(["^KS11"], today=date(2026, 8, 16))
    assert out["^KS11"]["close"] == 6869.83
    assert "prev" not in out["^KS11"]
    assert "change_pct" not in out["^KS11"]


def test_fetch_symbol_quotes_normal_calc_when_today_bar_confirmed(monkeypatch):
    """`today` 이전으로 확정된 거래일이 2개 이상이면 평소대로 정상 계산된다."""
    idx = pd.date_range("2026-08-13", periods=2, freq="D")
    series = pd.Series([6813.34, 6977.94], index=idx)

    monkeypatch.setattr(
        market_module.yf, "download", lambda *a, **k: pd.DataFrame({"Close": series})
    )
    out = fetch_symbol_quotes(["^KS11"], today=date(2026, 8, 20))
    assert out["^KS11"]["close"] == 6977.94
    assert out["^KS11"]["prev"] == 6813.34
    assert out["^KS11"]["date"] == "2026-08-14"


def test_fetch_symbol_quotes_holiday_gap_compares_actual_previous_trading_day(monkeypatch):
    """날짜가 연속하지 않을 때(휴장일이 낀 경우)도 실제로 존재하는 마지막 두
    거래일을 정확히 비교한다 — 위치(-1,-2)가 아니라 날짜 기준이라 자동으로
    맞다. 2026-08-17 이 광복절 대체공휴일이라 휴장이었던 실제 상황과 동일한
    간격(08-14 → 08-18, 08-15/16 주말 + 08-17 휴장)."""
    idx = pd.to_datetime(["2026-08-13", "2026-08-14", "2026-08-18"])
    series = pd.Series([6813.34, 6977.94, 6869.83], index=idx)

    monkeypatch.setattr(
        market_module.yf, "download", lambda *a, **k: pd.DataFrame({"Close": series})
    )
    out = fetch_symbol_quotes(["^KS11"], today=date(2026, 8, 19))
    assert out["^KS11"]["close"] == 6869.83
    assert out["^KS11"]["prev"] == 6977.94
    assert out["^KS11"]["date"] == "2026-08-18"


def test_fetch_symbols_excludes_unconfirmed_today_bar(monkeypatch):
    """`_fetch_symbols`(TICKERS/ANCHORS 경로)도 `fetch_symbol_quotes`와 동일한
    미확정 '오늘' 봉 제외 로직을 쓴다 — 같은 파일 안에서 로직이 갈리면 한쪽만
    고치고 다른 쪽은 그대로 버그로 남는다."""
    idx = pd.date_range("2026-08-14", periods=3, freq="D")
    close = pd.DataFrame({"^KS11": [6977.94, 6869.83, 9999.0]}, index=idx)
    frame = pd.concat({"Close": close}, axis=1)

    monkeypatch.setattr(market_module.yf, "download", lambda *a, **k: frame)
    out = market_module._fetch_symbols({"^KS11": "KOSPI"}, today=date(2026, 8, 16))
    assert out["^KS11"]["close"] == 6869.83
    assert out["^KS11"]["prev"] == 6977.94


def test_symbol_ohlcv_excludes_unconfirmed_today_row(monkeypatch):
    """OHLCV(이동평균·변동성 계산용)도 마감 안 된 '오늘' 행이 섞이면 안 된다 —
    close/change_pct 와 같은 원인, 같은 파일이라 별도로 회귀시킨다."""
    idx = pd.date_range("2026-01-01", periods=35, freq="B")
    today_idx = idx[-1] + pd.Timedelta(days=1)
    full_idx = idx.append(pd.DatetimeIndex([today_idx]))
    frame = pd.DataFrame(
        {
            "Open": [100.0 + i for i in range(35)] + [1.0],
            "High": [105.0 + i for i in range(35)] + [1.0],
            "Low": [95.0 + i for i in range(35)] + [1.0],
            "Close": [102.0 + i for i in range(35)] + [1.0],
            "Volume": [1000.0 + i for i in range(35)] + [1.0],
        },
        index=full_idx,
    )
    monkeypatch.setattr(market_module.yf, "download", lambda *a, **k: frame)

    q = fetch_symbol_quotes(["TQQQ"], today=today_idx.date())
    assert q["TQQQ"]["close"] == 136.0  # 마지막 확정 봉(오늘 제외)
    df = q["TQQQ"]["ohlcv"]
    assert today_idx.date() not in set(df.index.date)
    assert len(df) == 35


def test_missing_ohlcv_means_no_key(monkeypatch):
    """종가는 있는데 volume 이 전부 NaN 인 심볼은 ohlcv 키 자체가 없다(0 프레임 위장 금지)."""
    idx = pd.date_range("2026-01-01", periods=35, freq="B")
    fields = {}
    for field, base in [("Open", 100.0), ("High", 105.0), ("Low", 95.0), ("Close", 102.0)]:
        fields[field] = pd.DataFrame(
            {"TQQQ": [base + i for i in range(35)], "BAD": [base + i for i in range(35)]},
            index=idx,
        )
    fields["Volume"] = pd.DataFrame(
        {"TQQQ": [1000.0 + i for i in range(35)], "BAD": [float("nan")] * 35}, index=idx
    )
    frame = pd.concat(fields, axis=1)
    monkeypatch.setattr(market_module.yf, "download", lambda *a, **k: frame)

    q = fetch_symbol_quotes(["TQQQ", "BAD"])
    assert "ohlcv" in q["TQQQ"]
    assert "close" in q["BAD"]  # 기존 close 경로는 살아있다
    assert "ohlcv" not in q["BAD"]
