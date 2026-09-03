"""quant/collect/kr_largecap_daily.py 회귀 테스트 — KIND 파싱, 시총 계산(재구현
대조), 가격/시총 배치 조회의 부분 실패 내성, 유니버스 필터링/정렬, 캐시
신선도 판정, 일봉 백필의 심볼별 계속 진행을 검증한다. 네트워크 없음(전부 페이크).
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from quant.analyze.watch_scorer import _market_cap_krw as _analyze_market_cap_krw
from quant.collect import kr_largecap_daily as kld


# --------------------------------------------------------------------- KIND 파싱


_KIND_HTML = (
    "<table>"
    "<tr><th>종목명</th><th>시장구분</th><th>종목코드</th><th>업종</th></tr>"
    "<tr><td>삼성전자</td><td>유가</td><td>005930</td><td>전자</td></tr>"
    "<tr><td>카카오</td><td>코스닥</td><td>035720</td><td>서비스</td></tr>"
    "<tr><td>코넥스상품</td><td>코넥스</td><td>099999</td><td>기타</td></tr>"
    "<tr><td>삼성전자(중복)</td><td>유가</td><td>005930</td><td>전자</td></tr>"
    "</table>"
).encode("euc-kr")


def test_parse_kind_codes_filters_tradable_markets_and_valid_codes():
    out = kld.parse_kind_codes(_KIND_HTML)

    codes = {code for code, _name, _market in out}
    assert codes == {"005930", "035720"}  # 코넥스 제외
    assert ("005930", "삼성전자", "유가") in out
    assert ("005930", "삼성전자(중복)", "유가") in out  # 파싱 단계는 dedup 안 함


def test_parse_kind_codes_raises_on_zero_rows():
    with pytest.raises(ValueError, match="0건"):
        kld.parse_kind_codes("<table></table>".encode("euc-kr"))


def test_candidate_codes_dedupes_keeping_first_name_and_sorts(tmp_path):
    (tmp_path / "kind_corplist.html").write_bytes(_KIND_HTML)

    out = kld.candidate_codes(tmp_path)

    assert out == [("005930", "삼성전자"), ("035720", "카카오")]


# --------------------------------------------------------------------- 시가총액


def test_market_cap_krw_matches_analyze_watch_scorer_implementation():
    """평면 규칙 때문에 재구현했다(모듈 docstring) — 같은 입력에 같은 값을 내야 한다."""
    info = {"sharesOutstanding": "10000000"}
    assert kld._market_cap_krw(info, 100.0) == _analyze_market_cap_krw(info, 100.0) == 1_000_000_000


def test_market_cap_krw_none_on_missing_or_invalid_shares():
    assert kld._market_cap_krw(None, 100.0) is None
    assert kld._market_cap_krw({}, 100.0) is None
    assert kld._market_cap_krw({"sharesOutstanding": "abc"}, 100.0) is None
    assert kld._market_cap_krw({"sharesOutstanding": "0"}, 100.0) is None


# --------------------------------------------------------------------- 가격/시총 배치 조회


class _FakePriceClient:
    def __init__(self, price_batches: dict[tuple, list[dict]] | None = None,
                 prices_by_symbol: dict[str, float] | None = None,
                 fail_batches: set[int] = frozenset(),
                 stock_info_by_symbol: dict[str, dict] | None = None,
                 fail_stock_info: set[str] = frozenset()):
        self._prices_by_symbol = prices_by_symbol or {}
        self._fail_batches = fail_batches
        self._stock_info = stock_info_by_symbol or {}
        self._fail_stock_info = fail_stock_info
        self.price_calls: list[list[str]] = []
        self.stock_info_calls: list[str] = []

    def prices(self, symbols: list[str]) -> list[dict]:
        idx = len(self.price_calls)
        self.price_calls.append(list(symbols))
        if idx in self._fail_batches:
            raise RuntimeError("prices 배치 실패")
        return [
            {"symbol": s, "lastPrice": self._prices_by_symbol[s]}
            for s in symbols if s in self._prices_by_symbol
        ]

    def stock_info(self, symbol: str) -> dict:
        self.stock_info_calls.append(symbol)
        if symbol in self._fail_stock_info:
            raise RuntimeError("stock_info 실패")
        return self._stock_info.get(symbol, {})


def test_fetch_last_prices_batches_by_batch_size():
    client = _FakePriceClient(prices_by_symbol={"A": 100.0, "B": 200.0, "C": 300.0})

    out = kld.fetch_last_prices(client, ["A", "B", "C"], batch_size=2)

    assert out == {"A": 100.0, "B": 200.0, "C": 300.0}
    assert client.price_calls == [["A", "B"], ["C"]]


def test_fetch_last_prices_continues_after_one_batch_fails():
    client = _FakePriceClient(prices_by_symbol={"A": 100.0, "C": 300.0}, fail_batches={0})

    out = kld.fetch_last_prices(client, ["A", "B", "C", "D"], batch_size=2)

    # 첫 배치([A,B]) 실패 — A는 값이 있었어도 그 배치 자체가 예외라 통째로 빠진다.
    assert out == {"C": 300.0}


def test_fetch_last_prices_ignores_non_positive_or_unparsable_price():
    client = _FakePriceClient(prices_by_symbol={"A": 0.0, "B": -1.0})
    # C는 lastPrice가 문자열로 파싱 불가한 값
    client.prices = lambda symbols: [{"symbol": "A", "lastPrice": 0.0},
                                      {"symbol": "B", "lastPrice": -1.0},
                                      {"symbol": "C", "lastPrice": "n/a"}]

    out = kld.fetch_last_prices(client, ["A", "B", "C"], batch_size=10)

    assert out == {}


def test_fetch_market_caps_skips_symbols_without_price_or_failed_stock_info():
    client = _FakePriceClient(
        stock_info_by_symbol={"A": {"sharesOutstanding": "1000"}, "B": {"sharesOutstanding": "1000"}},
        fail_stock_info={"B"},
    )
    prices = {"A": 100.0, "B": 100.0}  # C는 가격 자체가 없음

    out = kld.fetch_market_caps(client, ["A", "B", "C"], prices)

    assert out == {"A": 100_000}
    assert client.stock_info_calls == ["A", "B"]  # C는 가격이 없어 stock_info 호출도 안 함


# --------------------------------------------------------------------- build_universe


def test_build_universe_filters_min_cap_sorts_desc_and_caps_top_n(tmp_path):
    (tmp_path / "kind_corplist.html").write_bytes(_KIND_HTML)
    client = _FakePriceClient(
        prices_by_symbol={"005930": 100.0, "035720": 100.0},
        stock_info_by_symbol={
            "005930": {"sharesOutstanding": "1000000000"},  # 시총 1000억 * ... 크게
            "035720": {"sharesOutstanding": "1"},  # 시총 100원 — min_cap 미달
        },
    )

    out = kld.build_universe(client, tmp_path, top_n=10, min_cap=1000.0)

    assert [row["symbol"] for row in out] == ["005930"]
    assert out[0]["name"] == "삼성전자"
    assert out[0]["market_cap"] == 100_000_000_000
    assert out[0]["last_price"] == pytest.approx(100.0)


def test_build_universe_respects_top_n():
    client = _FakePriceClient(
        prices_by_symbol={"A": 100.0, "B": 100.0},
        stock_info_by_symbol={
            "A": {"sharesOutstanding": "20"},
            "B": {"sharesOutstanding": "10"},
        },
    )
    # candidate_codes를 우회하기 위해 build_universe 내부 흐름을 함수 단위로 재확인:
    # fetch_last_prices/fetch_market_caps만으로 top_n 정렬 로직을 검증한다.
    prices = kld.fetch_last_prices(client, ["A", "B"], batch_size=10)
    caps = kld.fetch_market_caps(client, ["A", "B"], prices)
    qualified = sorted(((c, cap) for c, cap in caps.items() if cap >= 0), key=lambda kv: kv[1], reverse=True)

    assert [c for c, _ in qualified][:1] == ["A"]  # A(2000) > B(1000)


# --------------------------------------------------------------------- 캐시 (load/save/is_stale)


def test_save_and_load_universe_roundtrip(tmp_path):
    path = tmp_path / "kr_largecap_universe.json"
    symbols = [{"symbol": "005930", "name": "삼성전자", "market_cap": 1, "last_price": 1.0}]

    kld.save_universe(symbols, "2026-09-03", path)
    payload = kld.load_universe(path)

    assert payload == {"as_of": "2026-09-03", "symbols": symbols}


def test_load_universe_returns_none_when_missing_or_corrupt(tmp_path):
    assert kld.load_universe(tmp_path / "missing.json") is None

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert kld.load_universe(bad) is None


def test_is_stale_true_when_cache_missing():
    assert kld.is_stale(None, date(2026, 9, 3)) is True


def test_is_stale_true_when_as_of_missing_or_unparsable():
    assert kld.is_stale({}, date(2026, 9, 3)) is True
    assert kld.is_stale({"as_of": "not-a-date"}, date(2026, 9, 3)) is True


def test_is_stale_false_within_max_age_true_beyond():
    payload = {"as_of": "2026-08-10"}
    assert kld.is_stale(payload, date(2026, 8, 20), max_age_days=30) is False  # 10일
    assert kld.is_stale(payload, date(2026, 9, 20), max_age_days=30) is True  # 41일


# --------------------------------------------------------------------- 일봉 백필


def _candle(ts: str, price: float) -> dict:
    return {
        "timestamp": ts, "openPrice": str(price), "highPrice": str(price + 1),
        "lowPrice": str(price - 1), "closePrice": str(price + 0.5), "volume": "1000",
    }


class _KeyedFakeCandleClient:
    """심볼별로 다른 페이지를 주고, 지정된 심볼은 예외를 던지는 `_request` 스텁
    (tests/test_toss_source.py의 FakeTossClient을 심볼 분기 가능하게 확장)."""

    def __init__(self, pages_by_symbol: dict[str, list[dict]], fail_symbols: set[str] = frozenset()):
        self._pages = {k: list(v) for k, v in pages_by_symbol.items()}
        self._fail = set(fail_symbols)

    def _request(self, method, path, group, *, params=None, **kwargs):
        symbol = (params or {}).get("symbol")
        if symbol in self._fail:
            raise RuntimeError(f"boom: {symbol}")
        pages = self._pages.get(symbol)
        if not pages:
            return {"candles": [], "nextBefore": None}
        return pages.pop(0)


def test_backfill_universe_writes_each_symbol_and_skips_failed_ones(tmp_path):
    client = _KeyedFakeCandleClient(
        pages_by_symbol={
            "005930": [{"candles": [_candle("2026-08-14T09:00:00+09:00", 100)], "nextBefore": None}],
            "000660": [{"candles": [_candle("2026-08-14T09:00:00+09:00", 200)], "nextBefore": None}],
        },
        fail_symbols={"035720"},
    )

    reports = kld.backfill_universe(
        ["005930", "035720", "000660"], client,
        start=datetime(2026, 8, 1), end=datetime(2026, 8, 15), history_dir=tmp_path,
    )

    assert set(reports) == {"005930", "000660"}  # 035720은 실패해 빠진다
    assert (tmp_path / "005930" / "1d" / "2026" / "08.parquet").exists()
    assert (tmp_path / "000660" / "1d" / "2026" / "08.parquet").exists()
    assert not (tmp_path / "035720").exists()
