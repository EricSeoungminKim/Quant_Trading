"""quant.adapters.macro.fred(FRED CSV 파싱 + 멱등 원장 append) +
quant.adapters.regime_indicators.FileMacroIndicatorClient(US_BOND_10Y, 파일 경유,
KR_BOND_*는 여전히 None) 계약 검증. fetch_series 1건만 httpx 모킹 — 그 외는
네트워크 호출 없음(순수 함수/파일 I/O)."""
from __future__ import annotations

import json

import pytest

from quant.adapters.macro.fred import append_macro_rows, fetch_series, parse_fred_csv
from quant.adapters.regime_indicators import FileMacroIndicatorClient

# --------------------------------------------------------------------- parse_fred_csv


def test_parse_fred_csv_normal_rows():
    text = "DATE,DGS10\n2026-08-24,4.66\n2026-08-25,4.70\n"
    assert parse_fred_csv(text) == [("2026-08-24", 4.66), ("2026-08-25", 4.70)]


def test_parse_fred_csv_skips_missing_dot():
    text = "DATE,DGS10\n2026-08-22,.\n2026-08-24,4.66\n"
    assert parse_fred_csv(text) == [("2026-08-24", 4.66)]


def test_parse_fred_csv_skips_malformed_lines():
    text = "DATE,DGS10\n2026-08-23,broken,extra\n2026-08-24,4.66\nnotanumber\n2026-08-25,not_a_float\n"
    assert parse_fred_csv(text) == [("2026-08-24", 4.66)]


def test_parse_fred_csv_empty_input():
    assert parse_fred_csv("") == []
    assert parse_fred_csv("DATE,DGS10\n") == []


# --------------------------------------------------------------------- fetch_series


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeHttpClient:
    def __init__(self, text=None, raise_on_get=False):
        self._text = text
        self._raise = raise_on_get

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        if self._raise:
            raise RuntimeError("network down")
        return _FakeResp(self._text)


def test_fetch_series_parses_response(monkeypatch):
    fake = _FakeHttpClient(text="DATE,DGS10\n2026-08-24,4.66\n")
    monkeypatch.setattr("quant.adapters.macro.fred.http_client", lambda timeout=30.0, user_agent=None: fake)

    assert fetch_series("DGS10") == [("2026-08-24", 4.66)]


def test_fetch_series_network_failure_returns_none(monkeypatch):
    fake = _FakeHttpClient(raise_on_get=True)
    monkeypatch.setattr("quant.adapters.macro.fred.http_client", lambda timeout=30.0, user_agent=None: fake)

    assert fetch_series("DGS10") is None


# --------------------------------------------------------------------- append_macro_rows (멱등)


def test_append_macro_rows_idempotent_same_date_updates(tmp_path):
    path = tmp_path / "macro_rates.jsonl"

    added1 = append_macro_rows([{"date": "2026-08-24", "series": "us_10y", "value": 4.66}], path=path)
    assert added1 == 1

    # 같은 (date, series)를 다른 값으로 재기록 — 새 값으로 갱신, 중복 행 없음.
    added2 = append_macro_rows([{"date": "2026-08-24", "series": "us_10y", "value": 4.70}], path=path)
    assert added2 == 0  # 새 키가 아니므로 추가 건수는 0

    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["value"] == 4.70


def test_append_macro_rows_different_series_or_date_both_kept(tmp_path):
    path = tmp_path / "macro_rates.jsonl"
    append_macro_rows([{"date": "2026-08-24", "series": "us_10y", "value": 4.66}], path=path)
    append_macro_rows([{"date": "2026-08-25", "series": "us_10y", "value": 4.70}], path=path)
    append_macro_rows([{"date": "2026-08-24", "series": "vix", "value": 15.21}], path=path)

    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3


def test_append_macro_rows_empty_rows_is_noop(tmp_path):
    path = tmp_path / "macro_rates.jsonl"
    assert append_macro_rows([], path=path) == 0
    assert not path.exists()


# --------------------------------------------------------------------- FileMacroIndicatorClient


def _write_ledger(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_file_macro_client_returns_latest_and_prev_close(tmp_path):
    path = tmp_path / "macro_rates.jsonl"
    _write_ledger(path, [
        {"date": "2026-08-20", "series": "us_10y", "value": 4.60},
        {"date": "2026-08-21", "series": "us_10y", "value": 4.63},
        {"date": "2026-08-24", "series": "us_10y", "value": 4.66},
    ])
    c = FileMacroIndicatorClient(path=path)

    assert c.indicator_price("US_BOND_10Y") == 4.66
    assert c.indicator_prev_close("US_BOND_10Y") == 4.63


def test_file_macro_client_sorts_out_of_order_rows(tmp_path):
    """원장이 항상 날짜순으로 쌓인다고 가정하지 않는다 — 정렬 후 최신/전일을 뽑는다."""
    path = tmp_path / "macro_rates.jsonl"
    _write_ledger(path, [
        {"date": "2026-08-24", "series": "us_10y", "value": 4.66},
        {"date": "2026-08-20", "series": "us_10y", "value": 4.60},
        {"date": "2026-08-21", "series": "us_10y", "value": 4.63},
    ])
    c = FileMacroIndicatorClient(path=path)

    assert c.indicator_price("US_BOND_10Y") == 4.66
    assert c.indicator_prev_close("US_BOND_10Y") == 4.63


def test_file_macro_client_insufficient_values_returns_none(tmp_path):
    path = tmp_path / "macro_rates.jsonl"
    _write_ledger(path, [{"date": "2026-08-24", "series": "us_10y", "value": 4.66}])
    c = FileMacroIndicatorClient(path=path)

    assert c.indicator_price("US_BOND_10Y") is None
    assert c.indicator_prev_close("US_BOND_10Y") is None


def test_file_macro_client_missing_file_returns_none(tmp_path):
    c = FileMacroIndicatorClient(path=tmp_path / "nope.jsonl")

    assert c.indicator_price("US_BOND_10Y") is None
    assert c.indicator_prev_close("US_BOND_10Y") is None


def test_file_macro_client_kr_bond_still_none_no_fake_data(tmp_path):
    """국내 국채는 여전히 미구현이다 — 미국 10년물을 국내 국채인 것처럼 위장하지 않는다."""
    path = tmp_path / "macro_rates.jsonl"
    _write_ledger(path, [
        {"date": "2026-08-20", "series": "us_10y", "value": 4.60},
        {"date": "2026-08-24", "series": "us_10y", "value": 4.66},
    ])
    c = FileMacroIndicatorClient(path=path)

    for symbol in ("KR_BOND_10Y", "KR_BOND_2Y", "KR_BOND_30Y", "KOSPI"):
        assert c.indicator_price(symbol) is None
        assert c.indicator_prev_close(symbol) is None
